"""Whale-feed source for MRKT (tgmrkt.io) Telegram-gift sales.

MRKT exposes a public POST endpoint used by their own web Mini-App at
``cdn.tgmrkt.io`` to list the latest marketplace events ("Latest" feed).
The endpoint returns recent sales / listings / etc. across all
collections. We poll it every ``POLL_INTERVAL_SEC`` seconds and forward
every confirmed sale at or above ``threshold_ton`` to ``on_sold``.

Endpoint: ``POST https://api.tgmrkt.io/api/v1/feed``

Body::

    {"count": 50,
     "cursor": "",
     "collectionNames": [], "modelNames": [], "backdropNames": [],
     "number": null, "type": [],
     "minPrice": null, "maxPrice": null,
     "ordering": "Latest", "lowToHigh": false, "query": null}

Response (abbreviated)::

    {"items": [
       {"type": "sale",
        "id": "<uuid>",
        "amount": 3960000000,           # nano-TON
        "date": "2026-04-27T09:37:28.834486Z",
        "gift": {
           "name": "BDayCandle-209935", # already-CamelCase slug
           "title": "B-Day Candle",
           "collectionName": "B-Day Candle",
           "number": 209935,
           "modelName": "Broadway",
           "backdropName": "Mint Green",
           "symbolName": "Plume",
           "salePrice": 3960000000,
           ...
        }
       },
       …
    ]}

Authorization IS required — pass an ``Authorization`` header with the
JWT obtained via :func:`sniper.auth.get_mrkt_token` (Chrome's HAR export
silently strips this header for security; the OPTIONS preflight in the
HAR confirms the real request includes it).

On a 401/403 the caller's ``refresh_token`` coroutine is invoked to
mint a fresh token, then the request is retried once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx

from sniper.whale_types import WhaleSale

logger = logging.getLogger(__name__)


MRKT_API_BASE = "https://api.tgmrkt.io"
FEED_PATH = "/api/v1/feed"
GIFT_THRESHOLD_TON = 100.0
POLL_INTERVAL_SEC = 60.0
HTTP_TIMEOUT_SEC = 20.0
NANO_PER_TON = 1_000_000_000
SEEN_BUFFER = 1000
# Skip events older than this — guards against the feed reshuffling
# stale rows for any reason. Genuine sales appear within a minute or
# two; six hours is generous.
MAX_SALE_AGE = timedelta(hours=6)

# Body shape mirrors what the MRKT Mini-App sends (captured via HAR):
#   type=["Sale"]   server-side filter — return ONLY purchase events,
#                  not listings/de-listings/etc. Without it the server
#                  may reject the request or return a mixed feed.
#   count=20       Mini-App default; larger values may be capped or
#                  trigger anti-abuse 401s.
#   cursor=""      empty cursor = newest page; subsequent pages would
#                  pass the previous response's nextCursor.
_REQUEST_BODY = {
    "count": 20,
    "cursor": "",
    "collectionNames": [],
    "modelNames": [],
    "backdropNames": [],
    "number": None,
    "type": ["Sale"],
    "minPrice": None,
    "maxPrice": None,
    "ordering": "Latest",
    "lowToHigh": False,
    "query": None,
}

# Headers mirror exactly what the Mini-App sends. Cloudflare in front
# of api.tgmrkt.io appears to fingerprint the sec-fetch-* combination,
# so dropping any of these can flip the response to 401.
_BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://cdn.tgmrkt.io",
    "Referer": "https://cdn.tgmrkt.io/",
    "Sec-Ch-Ua": (
        '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}


_stats: dict[str, int] = {
    "polls": 0,
    "items_seen": 0,
    "sales_seen": 0,
    "whales_emitted": 0,
    "stale_filtered": 0,
    "errors": 0,
    "auth_refreshes": 0,
}


def get_stats() -> dict[str, int]:
    return dict(_stats)


def _parse_iso_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    # MRKT timestamps end in 'Z' which fromisoformat accepts on 3.11+
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_sale(item: dict) -> WhaleSale | None:
    """Convert one MRKT feed item to a WhaleSale, or ``None`` to skip."""
    if item.get("type") != "sale":
        return None

    amount_nano = item.get("amount")
    if amount_nano is None:
        return None
    try:
        price_ton = float(amount_nano) / NANO_PER_TON
    except (TypeError, ValueError):
        return None
    if price_ton <= 0:
        return None

    gift = item.get("gift") or {}
    slug = gift.get("name")  # already CamelCase like "BDayCandle-209935"
    title_full = gift.get("title")
    number = gift.get("number")
    if not slug or title_full is None or number is None:
        return None

    full_title = f"{title_full} #{number}"
    return WhaleSale(
        source="MRKT",
        title=full_title,
        price_ton=price_ton,
        collection_title=gift.get("collectionName") or title_full,
        num=number,
        slug=slug,
        model=gift.get("modelName"),
        backdrop=gift.get("backdropName"),
        symbol=gift.get("symbolName"),
        nft_address=None,
        seller_address=None,
        buyer_address=None,
    )


class _AuthError(Exception):
    pass


async def _fetch_feed(client: httpx.AsyncClient, token: str) -> list[dict]:
    url = MRKT_API_BASE + FEED_PATH
    headers = dict(_BASE_HEADERS)
    # The Mini-App does NOT send an Authorization header to /feed — the
    # endpoint is publicly readable from the right Origin/Referer.
    # Sending our /api/v1/auth UUID actually flips the response to 401
    # (likely treated as an invalid bearer). The ``token`` argument is
    # kept in the signature for API-symmetry with run_mrkt_feed but is
    # intentionally unused here.
    _ = token
    resp = await client.post(url, json=_REQUEST_BODY, headers=headers)
    if resp.status_code in (401, 403):
        body = resp.text[:200] if resp.text else "<empty>"
        logger.warning(
            "MRKT /feed %d: body=%r",
            resp.status_code,
            body,
        )
        raise _AuthError(f"MRKT auth error {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    return list(data.get("items", []))


async def run_mrkt_feed(
    on_sold: Callable[[WhaleSale], Awaitable[None]],
    initial_token: str,
    refresh_token: Callable[[], Awaitable[str]] | None = None,
    threshold_ton: float = GIFT_THRESHOLD_TON,
    poll_interval_sec: float = POLL_INTERVAL_SEC,
) -> None:
    """Main loop: poll MRKT's Latest feed and forward whales to ``on_sold``.

    ``initial_token`` should be a freshly minted JWT from
    :func:`sniper.auth.get_mrkt_token`. ``refresh_token`` is called if
    the server replies 401/403 (typical when the token expires after
    a few hours); the returned new token replaces the cached one.
    """
    seen_ids: set[str] = set()
    bootstrap_done = False
    token = initial_token
    # MRKT's /feed sits behind a Cloudflare WAF rule that 401s every
    # request from non-residential IPs, regardless of headers / TLS
    # fingerprint / token. After this many consecutive 401s we stop
    # polling entirely (silent suspend) — the source then sleeps in a
    # long loop so the rest of whale-feed isn't slowed by retries.
    # MRKT sales are still picked up by the on-chain Getgems source.
    AUTH_FAIL_LIMIT = 5
    SUSPEND_SLEEP_SEC = 3600.0
    consecutive_auth_fails = 0
    suspended = False

    logger.info(
        "MRKT feed starting: threshold=%.1f TON, poll=%.1fs, token_present=%s",
        threshold_ton,
        poll_interval_sec,
        bool(token),
    )

    backoff = poll_interval_sec
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
        while True:
            if suspended:
                # Long sleep, no API calls. Wake every hour just in case
                # the WAF rule changes; one /feed attempt — if it works,
                # un-suspend and resume normal polling.
                await asyncio.sleep(SUSPEND_SLEEP_SEC)
                try:
                    items = await _fetch_feed(client, token)
                except _AuthError:
                    continue
                except Exception:
                    continue
                logger.info(
                    "MRKT feed back online after suspend — resuming.",
                )
                suspended = False
                consecutive_auth_fails = 0
                # fall through into normal processing path with `items`
            else:
                try:
                    items = await _fetch_feed(client, token)
                except _AuthError:
                    consecutive_auth_fails += 1
                    if consecutive_auth_fails >= AUTH_FAIL_LIMIT:
                        logger.warning(
                            "MRKT /feed has 401'd %d times in a row — "
                            "Cloudflare/WAF appears to block this IP. "
                            "Suspending MRKT polling. Sales will still "
                            "be captured via the Getgems on-chain "
                            "source.",
                            consecutive_auth_fails,
                        )
                        suspended = True
                        continue
                    if refresh_token is None:
                        _stats["errors"] += 1
                        await asyncio.sleep(poll_interval_sec)
                        continue
                    try:
                        new_tok = await refresh_token()
                    except Exception:
                        logger.exception("MRKT token refresh failed")
                        new_tok = ""
                    if new_tok:
                        token = new_tok
                        _stats["auth_refreshes"] += 1
                    else:
                        _stats["errors"] += 1
                    await asyncio.sleep(poll_interval_sec)
                    continue
                except Exception:
                    _stats["errors"] += 1
                    logger.exception("MRKT feed HTTP error")
                    await asyncio.sleep(min(backoff * 2, 300.0))
                    backoff = min(backoff * 2, 300.0)
                    continue
            # Successful response — reset backoff & auth-fail counter.
            consecutive_auth_fails = 0
            backoff = poll_interval_sec
            _stats["polls"] += 1
            _stats["items_seen"] += len(items)

            if not bootstrap_done:
                # Mark every visible item id as seen on first poll so we
                # don't replay history.
                for it in items:
                    iid = it.get("id")
                    if iid:
                        seen_ids.add(iid)
                bootstrap_done = True
                logger.info(
                    "MRKT bootstrap: marked %d existing feed items as seen.",
                    len(items),
                )
                await asyncio.sleep(poll_interval_sec)
                continue

            now = datetime.now(timezone.utc)
            new_whales: list[WhaleSale] = []
            for it in items:
                iid = it.get("id")
                if not iid or iid in seen_ids:
                    continue
                seen_ids.add(iid)

                if it.get("type") != "sale":
                    continue
                _stats["sales_seen"] += 1

                # Freshness filter
                dt = _parse_iso_ts(it.get("date", ""))
                if dt is not None and (now - dt) > MAX_SALE_AGE:
                    _stats["stale_filtered"] += 1
                    continue

                sale = _to_sale(it)
                if sale is None:
                    continue
                if sale.price_ton < threshold_ton:
                    continue
                new_whales.append(sale)

            # Bound dedup memory
            if len(seen_ids) > SEEN_BUFFER:
                fresh = {it.get("id") for it in items if it.get("id")}
                seen_ids = fresh | set(list(seen_ids)[-SEEN_BUFFER:])

            # Feed is newest-first; emit oldest-first so posts arrive in
            # chronological order.
            for sale in reversed(new_whales):
                _stats["whales_emitted"] += 1
                logger.info(
                    "MRKT whale sale: %s @ %.2f TON",
                    sale.title,
                    sale.price_ton,
                )
                try:
                    await on_sold(sale)
                except Exception:
                    logger.exception(
                        "on_sold callback failed for MRKT sale %s",
                        sale.title,
                    )

            await asyncio.sleep(poll_interval_sec)

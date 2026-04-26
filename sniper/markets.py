"""Third-party marketplace monitoring: Tonnel, MRKT, Portals."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


def _now_msk() -> str:
    """Current time in Moscow as HH:MM:SS."""
    return datetime.now(_MSK).strftime("%H:%M:%S")


class _MarketAuthError(Exception):
    """Raised when a marketplace returns 401/403 — caller should re-auth."""

    def __init__(self, marketplace: str, status: int):
        super().__init__(f"{marketplace} auth error {status}")
        self.marketplace = marketplace
        self.status = status


_TONNEL_URL = "https://gifts2.tonnel.network/api/pageGifts"
_MRKT_URL = "https://api.tgmrkt.io/api/v1/gifts/saling"
_MRKT_BUY_URL = "https://api.tgmrkt.io/api/v1/gifts/buy"
_PORTALS_API = "https://portal-market.com/api"
_PORTALS_SEARCH_URL = f"{_PORTALS_API}/nfts/search"
_PORTALS_BUY_URL = f"{_PORTALS_API}/nfts"
_PORTALS_COLLECTIONS_URL = f"{_PORTALS_API}/collections"

_seen_market_ids: set[str] = set()
_portals_collection_map: dict[str, str] = {}


@dataclass
class MarketTarget:
    gift_name: str
    max_price: float
    model: str | None = None
    pattern: str | None = None
    backdrop: str | None = None
    markets: list[str] = field(default_factory=lambda: ["tonnel", "mrkt", "portals"])


@dataclass
class MarketListing:
    marketplace: str
    gift_name: str
    price: float
    currency: str
    model: str | None = None
    pattern: str | None = None
    backdrop: str | None = None
    gift_number: int | None = None
    listing_id: str = ""
    url: str = ""
    raw_id: str = ""  # marketplace-native ID for buy API


def _match_listing_to_target(
    listing: MarketListing, targets: list[MarketTarget]
) -> MarketTarget | None:
    """Return the first target whose filters (collection/model/backdrop/pattern/price)
    accept this listing, or None."""
    listing_name = (listing.gift_name or "").lower()
    listing_model = (listing.model or "").lower()
    listing_backdrop = (listing.backdrop or "").lower()
    listing_pattern = (listing.pattern or "").lower()

    for t in targets:
        if listing.marketplace.lower() not in [m.lower() for m in t.markets]:
            continue
        if t.gift_name and t.gift_name.lower() != listing_name:
            continue
        if t.model and t.model.lower() != listing_model:
            continue
        if t.backdrop and t.backdrop.lower() != listing_backdrop:
            continue
        if t.pattern and t.pattern.lower() != listing_pattern:
            continue
        if listing.price > t.max_price:
            continue
        return t
    return None


async def _poll_tonnel(
    client: httpx.AsyncClient,
    target: MarketTarget,
) -> list[MarketListing]:
    """Poll Tonnel marketplace for listings matching target."""
    filter_parts = [
        '"price":{"$exists":true}',
        '"refunded":{"$ne":true}',
        '"buyer":{"$exists":false}',
        '"export_at":{"$exists":true}',
        f'"gift_name":"{target.gift_name}"',
    ]
    if target.model:
        filter_parts.append(f'"model":{{"$regex":"^{target.model}"}}')
    if target.backdrop:
        filter_parts.append(f'"backdrop":{{"$regex":"^{target.backdrop}"}}')
    if target.pattern:
        filter_parts.append(f'"pattern":{{"$regex":"^{target.pattern}"}}')
    filter_parts.append('"asset":"TON"')

    body = {
        "page": 1,
        "limit": 10,
        "sort": '{"price":1}',
        "filter": "{" + ",".join(filter_parts) + "}",
        "price_range": [0, target.max_price],
        "user_auth": "",
    }

    try:
        resp = await client.post(_TONNEL_URL, json=body, timeout=10)
        if resp.status_code == 403:
            logger.warning("Tonnel 403 Forbidden for %s (IP blocked?)", target.gift_name)
            return []
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError:
        logger.warning("Tonnel HTTP error for %s", target.gift_name, exc_info=True)
        return []
    except Exception:
        logger.warning("Tonnel request failed for %s", target.gift_name)
        return []

    listings = []
    gifts = data.get("gifts", [])
    if isinstance(data, list):
        gifts = data

    for g in gifts:
        price = g.get("price")
        if price is None:
            continue
        price_f = float(price)
        if price_f > target.max_price:
            continue

        gift_id = g.get("gift_id", "")
        lid = f"tonnel_{gift_id}"
        gift_num = g.get("gift_num")
        gname = g.get("gift_name", target.gift_name)
        slug = gname.replace(" ", "") + f"-{gift_num}" if gift_num else ""
        url = f"https://t.me/nft/{slug}" if slug else ""
        listings.append(
            MarketListing(
                marketplace="Tonnel",
                gift_name=gname,
                price=price_f,
                currency="TON",
                model=g.get("model"),
                pattern=g.get("pattern"),
                backdrop=g.get("backdrop"),
                gift_number=gift_num,
                listing_id=lid,
                url=url,
            )
        )
    return listings


async def _poll_mrkt_batch(
    client: httpx.AsyncClient,
    targets: list[MarketTarget],
    auth_token: str,
) -> list[MarketListing]:
    """Poll MRKT for all targets in a single request.

    MRKT's /gifts/saling endpoint accepts an array of collection names, so one
    batched call replaces N per-target calls and avoids HTTP 429 rate limits.
    Filters (model/backdrop/pattern) and per-target max_price are applied
    client-side because the API applies a single set of filters to the whole
    request.
    """
    if not targets:
        return []

    collections = sorted({t.gift_name for t in targets})
    max_price_ton = max(t.max_price for t in targets)
    body: dict[str, Any] = {
        "collectionNames": collections,
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": int(max_price_ton * 1_000_000_000),
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": 100,
        "cursor": "",
        "query": None,
        "promotedFirst": False,
    }

    headers = {"Authorization": auth_token, "Referer": "https://cdn.tgmrkt.io/"}
    try:
        resp = await client.post(_MRKT_URL, json=body, headers=headers, timeout=10)
        if resp.status_code == 429:
            logger.warning("MRKT 429 rate-limited (batch of %d collections)", len(collections))
            return []
        if resp.status_code in (401, 403):
            raise _MarketAuthError("MRKT", resp.status_code)
        resp.raise_for_status()
        data = resp.json()
    except _MarketAuthError:
        raise
    except httpx.HTTPStatusError:
        logger.warning("MRKT HTTP error (batch)", exc_info=True)
        return []
    except Exception:
        logger.warning("MRKT request failed (batch)")
        return []

    listings = []
    items = data.get("gifts", [])

    for g in items:
        if not g.get("isOnSale"):
            continue
        sale_price = g.get("salePrice")
        if sale_price is None:
            continue
        price_ton = float(sale_price) / 1_000_000_000

        gift_id = g.get("id", g.get("giftId", ""))
        lid = f"mrkt_{gift_id}"
        gift_num = g.get("number")
        coll = g.get("collectionName", "")
        slug = coll.replace(" ", "") + f"-{gift_num}" if gift_num else ""
        url = f"https://t.me/nft/{slug}" if slug else ""
        listings.append(
            MarketListing(
                marketplace="MRKT",
                gift_name=coll,
                price=price_ton,
                currency="TON",
                model=g.get("modelTitle"),
                pattern=g.get("symbolName"),
                backdrop=g.get("backdropName"),
                gift_number=gift_num,
                listing_id=lid,
                url=url,
                raw_id=str(gift_id),
            )
        )
    return listings


async def _ensure_portals_collections(client: httpx.AsyncClient, auth_token: str) -> None:
    """Fetch Portals collection name→id map if not cached."""
    global _portals_collection_map
    if _portals_collection_map:
        return
    try:
        headers = {"Authorization": auth_token}
        resp = await client.get(
            _PORTALS_COLLECTIONS_URL,
            params={"offset": 0, "limit": 200},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        for c in data.get("collections", []):
            _portals_collection_map[c["name"].lower()] = c["id"]
        logger.info("Portals: cached %d collections", len(_portals_collection_map))
    except Exception:
        logger.warning("Failed to fetch Portals collections", exc_info=True)


async def _poll_portals_batch(
    client: httpx.AsyncClient,
    targets: list[MarketTarget],
    auth_token: str,
) -> list[MarketListing]:
    """Poll Portals for all targets in a single request.

    Portals' /nfts/search accepts multiple `collection_ids[]` params, so one
    batched call replaces N per-target calls. Per-target max_price and filters
    are applied client-side in run_market_monitor.
    """
    if not targets:
        return []

    await _ensure_portals_collections(client, auth_token)

    col_ids: list[str] = []
    for t in targets:
        cid = _portals_collection_map.get(t.gift_name.lower())
        if cid and cid not in col_ids:
            col_ids.append(cid)

    if not col_ids:
        logger.debug("Portals: no known collections for %d targets", len(targets))
        return []

    params: list[tuple[str, str]] = [
        ("offset", "0"),
        ("limit", "100"),
        ("sort_by", "price asc"),
        ("exclude_bundled", "true"),
    ]
    for cid in col_ids:
        params.append(("collection_ids[]", cid))

    headers = {"Authorization": auth_token}
    try:
        resp = await client.get(_PORTALS_SEARCH_URL, params=params, headers=headers, timeout=10)
        if resp.status_code in (401, 403):
            raise _MarketAuthError("Portals", resp.status_code)
        if resp.status_code == 429:
            logger.warning("Portals 429 rate-limited (batch of %d collections)", len(col_ids))
            return []
        resp.raise_for_status()
        data = resp.json()
    except _MarketAuthError:
        raise
    except httpx.HTTPStatusError:
        logger.warning("Portals HTTP error (batch)", exc_info=True)
        return []
    except Exception:
        logger.warning("Portals request failed (batch)")
        return []

    listings = []
    for g in data.get("results", []):
        price = g.get("price")
        if price is None:
            continue
        price_f = float(price)

        nft_id = g.get("id", "")
        lid = f"portals_{nft_id}"
        attrs = g.get("attributes", [])
        model = None
        pattern = None
        backdrop = None
        for a in attrs:
            if a.get("type") == "model":
                model = a.get("value")
            elif a.get("type") == "symbol":
                pattern = a.get("value")
            elif a.get("type") == "backdrop":
                backdrop = a.get("value")

        tg_id = g.get("tg_id", "")
        url = f"https://t.me/nft/{tg_id}" if tg_id else ""
        listings.append(
            MarketListing(
                marketplace="Portals",
                gift_name=g.get("name", ""),
                price=price_f,
                currency="TON",
                model=model,
                pattern=pattern,
                backdrop=backdrop,
                gift_number=g.get("external_collection_number"),
                listing_id=lid,
                url=url,
                raw_id=str(nft_id),
            )
        )
    return listings


async def _buy_mrkt(
    client: httpx.AsyncClient,
    listing: MarketListing,
    auth_token: str,
) -> bool:
    """Attempt to buy a gift on MRKT."""
    if not listing.raw_id:
        logger.warning("MRKT buy: no raw_id for %s", listing.gift_name)
        return False
    headers = {"Authorization": auth_token, "Referer": "https://cdn.tgmrkt.io/"}
    body = {"giftId": listing.raw_id}
    try:
        resp = await client.post(_MRKT_BUY_URL, json=body, headers=headers, timeout=15)
        if resp.status_code in (200, 204):
            logger.info(
                "MRKT BUY OK: %s #%s — %.4f TON",
                listing.gift_name,
                listing.gift_number or "?",
                listing.price,
            )
            return True
        logger.warning("MRKT buy failed %d: %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("MRKT buy error for %s", listing.gift_name)
    return False


async def _buy_portals(
    client: httpx.AsyncClient,
    listing: MarketListing,
    auth_token: str,
) -> bool:
    """Attempt to buy a gift on Portals."""
    if not listing.raw_id:
        logger.warning("Portals buy: no raw_id for %s", listing.gift_name)
        return False
    headers = {"Authorization": auth_token}
    body = {"nft_details": [{"id": listing.raw_id, "price": str(listing.price)}]}
    try:
        resp = await client.post(_PORTALS_BUY_URL, json=body, headers=headers, timeout=15)
        if resp.status_code in (200, 204):
            logger.info(
                "Portals BUY OK: %s #%s — %.4f TON",
                listing.gift_name,
                listing.gift_number or "?",
                listing.price,
            )
            return True
        logger.warning("Portals buy failed %d: %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("Portals buy error for %s", listing.gift_name)
    return False


_auto_buy_enabled = False


def set_auto_buy(enabled: bool) -> None:
    global _auto_buy_enabled
    _auto_buy_enabled = enabled


def is_auto_buy() -> bool:
    return _auto_buy_enabled


async def run_market_monitor(
    targets: list[MarketTarget] | None = None,
    notify_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    poll_interval: float = 5.0,
    mrkt_token: str = "",
    portals_token: str = "",
    target_fn: Callable[[], list[MarketTarget]] | None = None,
    mrkt_reauth_fn: Callable[[], Coroutine[Any, Any, str]] | None = None,
    portals_reauth_fn: Callable[[], Coroutine[Any, Any, str]] | None = None,
) -> None:
    """Continuously poll third-party marketplaces and send notifications.

    Either pass static `targets` or a dynamic `target_fn` that returns
    the current list on each cycle.
    """
    active_markets = set()
    active_markets.add("tonnel")
    if mrkt_token:
        active_markets.add("mrkt")
    if portals_token:
        active_markets.add("portals")

    logger.info(
        "Market monitor started: markets=%s, interval=%.1fs",
        active_markets,
        poll_interval,
    )

    _error_count = 0
    _last_error_alert = 0.0
    _warmed_up = False

    async with httpx.AsyncClient() as client:
        while True:
            t0 = time.monotonic()

            current_targets = target_fn() if target_fn else (targets or [])
            if not current_targets:
                logger.debug("Market monitor: no targets, sleeping")
                await asyncio.sleep(poll_interval)
                continue

            logger.debug(
                "Market cycle: %d targets: %s",
                len(current_targets),
                [t.gift_name for t in current_targets],
            )

            tonnel_targets = [
                t for t in current_targets
                if "tonnel" in active_markets and "tonnel" in t.markets
            ]
            mrkt_targets = [
                t for t in current_targets
                if "mrkt" in active_markets and "mrkt" in t.markets
            ]
            portals_targets = [
                t for t in current_targets
                if "portals" in active_markets and "portals" in t.markets
            ]

            tasks: list[Coroutine[Any, Any, list[MarketListing]]] = []
            for t in tonnel_targets:
                tasks.append(_poll_tonnel(client, t))
            if mrkt_targets:
                tasks.append(_poll_mrkt_batch(client, mrkt_targets, mrkt_token))
            if portals_targets:
                tasks.append(_poll_portals_batch(client, portals_targets, portals_token))

            all_listings: list[MarketListing] = []
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list):
                        all_listings.extend(r)
                    elif isinstance(r, _MarketAuthError):
                        if r.marketplace == "MRKT" and mrkt_reauth_fn:
                            logger.warning(
                                "MRKT auth expired (status %d), re-fetching token…",
                                r.status,
                            )
                            try:
                                new_token = await mrkt_reauth_fn()
                            except Exception:
                                logger.exception("MRKT re-auth failed")
                                new_token = ""
                            if new_token:
                                mrkt_token = new_token
                                logger.info("MRKT token refreshed")
                        elif r.marketplace == "Portals" and portals_reauth_fn:
                            logger.warning(
                                "Portals auth expired (status %d), re-fetching token…",
                                r.status,
                            )
                            try:
                                new_token = await portals_reauth_fn()
                            except Exception:
                                logger.exception("Portals re-auth failed")
                                new_token = ""
                            if new_token:
                                portals_token = new_token
                                logger.info("Portals token refreshed")
                        else:
                            logger.warning(
                                "%s auth error %d (no re-auth fn configured)",
                                r.marketplace,
                                r.status,
                            )
                    elif isinstance(r, Exception):
                        _error_count += 1
                        logger.warning("Market poll error: %s", r)
                        if (
                            notify_fn
                            and _error_count >= 3
                            and (time.monotonic() - _last_error_alert) > 300
                        ):
                            _last_error_alert = time.monotonic()
                            try:
                                await notify_fn(f"⚠️ Ошибки маркетов ({_error_count}x)\n{r}")
                            except Exception:
                                pass

            for listing in all_listings:
                if listing.listing_id in _seen_market_ids:
                    continue
                _seen_market_ids.add(listing.listing_id)

                if not _warmed_up:
                    continue

                matched_target = _match_listing_to_target(listing, current_targets)
                if not matched_target:
                    continue

                logger.info(
                    "MARKET HIT [%s]: %s #%s — %.4f %s",
                    listing.marketplace,
                    listing.gift_name,
                    listing.gift_number or "?",
                    listing.price,
                    listing.currency,
                )

                buy_result = ""
                if _auto_buy_enabled and listing.raw_id:
                    ok = False
                    if listing.marketplace == "MRKT" and mrkt_token:
                        ok = await _buy_mrkt(client, listing, mrkt_token)
                    elif listing.marketplace == "Portals" and portals_token:
                        ok = await _buy_portals(client, listing, portals_token)
                    buy_result = "\n✅ Куплено!" if ok else "\n❌ Покупка не удалась"

                if notify_fn:
                    model_info = f"\nМодель: {listing.model}" if listing.model else ""
                    num = f" #{listing.gift_number}" if listing.gift_number else ""
                    link = f"\n🔗 {listing.url}" if listing.url else ""
                    seen = f"\n🕐 Обнаружено: {_now_msk()} МСК"
                    msg = (
                        f"📍 {listing.marketplace}\n"
                        f"🎯 {listing.gift_name}{num}\n"
                        f"Цена: {listing.price:.4f} {listing.currency} "
                        f"(макс {matched_target.max_price})"
                        f"{model_info}"
                        f"{seen}"
                        f"{link}"
                        f"{buy_result}"
                    )
                    try:
                        await notify_fn(msg)
                    except Exception:
                        logger.exception("Failed to send market notification")

            if not _warmed_up:
                _warmed_up = True
                logger.info(
                    "Market monitor warm-up done, %d listings cached",
                    len(_seen_market_ids),
                )

            elapsed = time.monotonic() - t0
            sleep_for = max(0.5, poll_interval - elapsed)
            await asyncio.sleep(sleep_for)

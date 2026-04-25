"""Third-party marketplace monitoring: Tonnel, MRKT, Portals."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TONNEL_URL = "https://gifts2.tonnel.network/api/pageGifts"
_MRKT_URL = "https://api.tgmrkt.io/api/v1/gifts/saling"
_PORTALS_API = "https://portal-market.com/api"
_PORTALS_SEARCH_URL = f"{_PORTALS_API}/nfts/search"
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

        lid = f"tonnel_{g.get('gift_id', '')}"
        listings.append(
            MarketListing(
                marketplace="Tonnel",
                gift_name=g.get("gift_name", target.gift_name),
                price=price_f,
                currency="TON",
                model=g.get("model"),
                pattern=g.get("pattern"),
                backdrop=g.get("backdrop"),
                gift_number=g.get("gift_num"),
                listing_id=lid,
            )
        )
    return listings


async def _poll_mrkt(
    client: httpx.AsyncClient,
    target: MarketTarget,
    auth_token: str,
) -> list[MarketListing]:
    """Poll MRKT marketplace for listings matching target."""
    body: dict[str, Any] = {
        "collectionNames": [target.gift_name],
        "modelNames": [target.model] if target.model else [],
        "backdropNames": [target.backdrop] if target.backdrop else [],
        "symbolNames": [target.pattern] if target.pattern else [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": target.max_price,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": 10,
        "cursor": "",
        "query": None,
        "promotedFirst": False,
    }

    headers = {"Authorization": auth_token}
    try:
        resp = await client.post(_MRKT_URL, json=body, headers=headers, timeout=10)
        if resp.status_code == 429:
            logger.warning("MRKT 429 rate-limited for %s", target.gift_name)
            return []
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError:
        logger.warning("MRKT HTTP error for %s", target.gift_name, exc_info=True)
        return []
    except Exception:
        logger.warning("MRKT request failed for %s", target.gift_name)
        return []

    listings = []
    items = data.get("gifts", data.get("items", []))
    if isinstance(data, list):
        items = data

    for g in items:
        price = g.get("price") or g.get("priceTon")
        if price is None:
            continue
        price_f = float(price)
        if price_f > target.max_price:
            continue

        lid = f"mrkt_{g.get('id', g.get('giftId', ''))}"
        listings.append(
            MarketListing(
                marketplace="MRKT",
                gift_name=g.get("collectionName", target.gift_name),
                price=price_f,
                currency="TON",
                model=g.get("modelName"),
                pattern=g.get("symbolName"),
                backdrop=g.get("backdropName"),
                gift_number=g.get("number"),
                listing_id=lid,
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


async def _poll_portals(
    client: httpx.AsyncClient,
    target: MarketTarget,
    auth_token: str,
) -> list[MarketListing]:
    """Poll Portals marketplace for listings matching target."""
    await _ensure_portals_collections(client, auth_token)

    col_id = _portals_collection_map.get(target.gift_name.lower())
    if not col_id:
        logger.debug("Portals: collection not found for %s", target.gift_name)
        return []

    params: dict[str, Any] = {
        "offset": 0,
        "limit": 10,
        "sort_by": "price asc",
        "collection_ids[]": col_id,
        "exclude_bundled": "true",
    }

    headers = {"Authorization": auth_token}
    try:
        resp = await client.get(_PORTALS_SEARCH_URL, params=params, headers=headers, timeout=10)
        if resp.status_code in (401, 403):
            logger.warning("Portals auth error %d for %s", resp.status_code, target.gift_name)
            return []
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError:
        logger.warning("Portals HTTP error for %s", target.gift_name, exc_info=True)
        return []
    except Exception:
        logger.warning("Portals request failed for %s", target.gift_name)
        return []

    listings = []
    for g in data.get("results", []):
        price = g.get("price")
        if price is None:
            continue
        price_f = float(price)
        if price_f > target.max_price:
            continue

        lid = f"portals_{g.get('id', '')}"
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

        listings.append(
            MarketListing(
                marketplace="Portals",
                gift_name=g.get("name", target.gift_name),
                price=price_f,
                currency="TON",
                model=model,
                pattern=pattern,
                backdrop=backdrop,
                gift_number=g.get("external_collection_number"),
                listing_id=lid,
            )
        )
    return listings


async def run_market_monitor(
    targets: list[MarketTarget] | None = None,
    notify_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    poll_interval: float = 5.0,
    mrkt_token: str = "",
    portals_token: str = "",
    target_fn: Callable[[], list[MarketTarget]] | None = None,
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
            for ti, target in enumerate(current_targets):
                if ti > 0:
                    await asyncio.sleep(2.0)
                all_listings: list[MarketListing] = []

                tasks = []
                if "tonnel" in active_markets and "tonnel" in target.markets:
                    tasks.append(_poll_tonnel(client, target))
                if "mrkt" in active_markets and "mrkt" in target.markets:
                    tasks.append(_poll_mrkt(client, target, mrkt_token))
                if "portals" in active_markets and "portals" in target.markets:
                    tasks.append(_poll_portals(client, target, portals_token))

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, list):
                            all_listings.extend(r)
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

                    logger.info(
                        "MARKET HIT [%s]: %s #%s — %.4f %s",
                        listing.marketplace,
                        listing.gift_name,
                        listing.gift_number or "?",
                        listing.price,
                        listing.currency,
                    )

                    if notify_fn:
                        model_info = f"\nМодель: {listing.model}" if listing.model else ""
                        num = f" #{listing.gift_number}" if listing.gift_number else ""
                        msg = (
                            f"📍 {listing.marketplace}\n"
                            f"🎯 {listing.gift_name}{num}\n"
                            f"Цена: {listing.price:.4f} {listing.currency} "
                            f"(макс {target.max_price})"
                            f"{model_info}"
                        )
                        try:
                            await notify_fn(msg)
                        except Exception:
                            logger.exception("Failed to send market notification")

            elapsed = time.monotonic() - t0
            sleep_for = max(0.5, poll_interval - elapsed)
            await asyncio.sleep(sleep_for)

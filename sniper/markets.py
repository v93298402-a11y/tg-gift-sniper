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
_PORTALS_URL = "https://portal-market.com/api/v1/gifts"

_seen_market_ids: set[str] = set()


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
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.debug("Tonnel request failed for %s", target.gift_name)
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
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.debug("MRKT request failed for %s", target.gift_name)
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


async def _poll_portals(
    client: httpx.AsyncClient,
    target: MarketTarget,
    auth_token: str,
) -> list[MarketListing]:
    """Poll Portals marketplace for listings matching target."""
    params: dict[str, Any] = {
        "status": "listed",
        "sort_by": "price_asc",
        "limit": 10,
        "offset": 0,
        "max_price": target.max_price,
    }
    if target.model:
        params["filter_by_models"] = target.model
    if target.backdrop:
        params["filter_by_backdrops"] = target.backdrop

    headers = {"Authorization": auth_token}
    try:
        resp = await client.get(_PORTALS_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.debug("Portals request failed for %s", target.gift_name)
        return []

    listings = []
    items = data.get("items", data.get("gifts", []))
    if isinstance(data, list):
        items = data

    for g in items:
        name = g.get("name", "")
        if name.lower() != target.gift_name.lower():
            continue

        price = g.get("price")
        if price is None:
            continue
        price_f = float(price)
        if price_f > target.max_price:
            continue

        lid = f"portals_{g.get('id', g.get('tg_id', ''))}"
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
                gift_name=name,
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

    async with httpx.AsyncClient() as client:
        while True:
            t0 = time.monotonic()

            current_targets = target_fn() if target_fn else (targets or [])
            for target in current_targets:
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

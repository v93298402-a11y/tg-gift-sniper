"""Track floor prices and sales across all marketplaces."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SaleRecord:
    """A listing that was observed and then disappeared (likely sold)."""

    collection: str
    marketplace: str
    price: float
    currency: str
    floor_at_time: float
    discount_pct: float  # how much below floor (0..100)
    gift_number: int | None = None
    model: str | None = None
    url: str = ""
    timestamp: float = field(default_factory=time.time)


# Current floor price per collection (lowest active listing)
_floor_prices: dict[str, float] = {}

# All active listing IDs → their data (for detecting disappearances).
# Listing IDs are prefixed with "<marketplace>:<collection>:<id>" so we can
# recompute the cross-market floor by scanning only the relevant entries.
_active_listings: dict[str, dict] = {}

# Sales log (items that disappeared from listings)
_sales_log: list[SaleRecord] = []

# Max sales to keep in memory
_MAX_SALES = 5000


def update_floor(collection: str, price: float) -> None:
    """Update the floor price for a collection."""
    current = _floor_prices.get(collection)
    if current is None or price < current:
        _floor_prices[collection] = price


def get_floor(collection: str) -> float | None:
    """Get current floor price for a collection."""
    return _floor_prices.get(collection)


def record_listings(
    collection: str,
    marketplace: str,
    listings: list[dict],
) -> None:
    """Record active listings. Detect disappeared ones as sales.

    Each listing dict: {id, price, currency, gift_number, model, url}
    """
    prefix = f"{marketplace}:{collection}:"
    current_ids = set()

    for li in listings:
        lid = prefix + str(li["id"])
        current_ids.add(lid)
        price = li["price"]
        _active_listings[lid] = {
            "collection": collection,
            "marketplace": marketplace,
            "price": price,
            "currency": li.get("currency", "TON"),
            "gift_number": li.get("gift_number"),
            "model": li.get("model"),
            "url": li.get("url", ""),
            "seen_at": time.time(),
        }

    # Find disappeared listings (sold) for this marketplace+collection only
    gone = []
    for lid, data in list(_active_listings.items()):
        if lid.startswith(prefix) and lid not in current_ids:
            gone.append((lid, data))
    # Remove gone listings before recomputing floor so the new floor
    # reflects the *remaining* listings, not the sold-at price itself.
    for lid, _ in gone:
        del _active_listings[lid]

    # Recompute floor as the minimum price across ALL active listings of
    # this collection (across every marketplace), not just this batch.
    coll_floor: float | None = None
    for lid, data in _active_listings.items():
        if data["collection"] != collection:
            continue
        if coll_floor is None or data["price"] < coll_floor:
            coll_floor = data["price"]
    if coll_floor is not None:
        _floor_prices[collection] = coll_floor
    elif collection in _floor_prices:
        # No listings remain — drop stale floor.
        del _floor_prices[collection]

    for lid, data in gone:
        if coll_floor and coll_floor > 0:
            discount = (1 - data["price"] / coll_floor) * 100
        else:
            discount = 0
        _sales_log.append(
            SaleRecord(
                collection=data["collection"],
                marketplace=data["marketplace"],
                price=data["price"],
                currency=data["currency"],
                floor_at_time=coll_floor or 0,
                discount_pct=max(0, discount),
                gift_number=data.get("gift_number"),
                model=data.get("model"),
                url=data.get("url", ""),
            )
        )

    # Trim old entries
    if len(_sales_log) > _MAX_SALES:
        _sales_log[:] = _sales_log[-_MAX_SALES:]


def get_below_floor_sales(
    minutes: int = 30,
    min_discount: float = 30.0,
) -> list[SaleRecord]:
    """Get sales below floor by at least min_discount% in last N minutes."""
    cutoff = time.time() - minutes * 60
    return [s for s in _sales_log if s.timestamp >= cutoff and s.discount_pct >= min_discount]


def get_stats_text(minutes: int = 30) -> str:
    """Format stats as text for the bot."""
    sales = get_below_floor_sales(minutes=minutes, min_discount=30.0)

    if not sales:
        return f"За последние {minutes} мин. нет продаж ниже флора на 30%+."

    lines = [f"📊 Продажи ниже флора на 30%+ ({minutes} мин):\n"]
    for s in sorted(sales, key=lambda x: -x.discount_pct):
        num = f" #{s.gift_number}" if s.gift_number else ""
        model_info = f" [{s.model}]" if s.model else ""
        link = f"\n  🔗 {s.url}" if s.url else ""
        lines.append(
            f"• {s.collection}{num}{model_info}\n"
            f"  {s.marketplace}: {s.price:.4f} TON "
            f"(флор {s.floor_at_time:.4f}, -{s.discount_pct:.0f}%)"
            f"{link}"
        )

    lines.append(f"\nВсего: {len(sales)} продаж")
    return "\n".join(lines)


def get_floor_summary() -> str:
    """Get current floor prices for all tracked collections."""
    if not _floor_prices:
        return "Нет данных о флор-ценах."
    lines = ["📊 Текущие флор-цены:\n"]
    for coll, price in sorted(_floor_prices.items()):
        lines.append(f"• {coll}: {price:.4f} TON")
    return "\n".join(lines)

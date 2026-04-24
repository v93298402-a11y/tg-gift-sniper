"""Polls payments.getResaleStarGifts for each target and triggers buy."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from telethon import functions, types
from telethon.errors import BadRequestError, FloodWaitError

from sniper.buyer import buy_gift
from sniper.config import Config, TargetGift

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Keep track of slugs we already attempted to buy (avoid double-buying)
_seen_slugs: set[str] = set()

# Stats
_stats = {
    "polls": 0,
    "gifts_seen": 0,
    "buys_attempted": 0,
    "buys_ok": 0,
    "buys_fail": 0,
    "flood_waits": 0,
}


def get_stats() -> dict[str, int]:
    return dict(_stats)


def _extract_price(gift: types.StarGiftUnique) -> int | None:
    """Extract the Stars resale price from a StarGiftUnique."""
    if not gift.resell_amount:
        return None
    for amt in gift.resell_amount:
        if isinstance(amt, types.StarsAmount):
            return int(amt.amount)
    return None


def _gift_matches_filter(gift: types.StarGiftUnique, target: TargetGift) -> bool:
    """Check if a gift's attributes match the target's model/pattern/backdrop filter."""
    if not (target.model or target.pattern or target.backdrop):
        return True

    for attr in gift.attributes:
        if target.model and isinstance(attr, types.StarGiftAttributeModel):
            if attr.name.lower() == target.model.lower():
                return True
        if target.pattern and isinstance(attr, types.StarGiftAttributePattern):
            if attr.name.lower() == target.pattern.lower():
                return True
        if target.backdrop and isinstance(attr, types.StarGiftAttributeBackdrop):
            if attr.name.lower() == target.backdrop.lower():
                return True

    return False


async def _poll_gift_id(
    client: TelegramClient,
    gift_id: int,
    targets: list[TargetGift],
    cfg: Config,
) -> None:
    """Single poll cycle for one gift_id, checked against multiple targets."""
    try:
        result = await client(
            functions.payments.GetResaleStarGiftsRequest(
                gift_id=gift_id,
                sort_by_price=True,
                offset="",
                limit=20,
            )
        )
    except FloodWaitError as e:
        _stats["flood_waits"] += 1
        logger.warning(
            "FLOOD_WAIT %ds on gift_id=%d, sleeping…",
            e.seconds,
            gift_id,
        )
        await asyncio.sleep(e.seconds + 1)
        return
    except BadRequestError as e:
        if "STARGIFT_INVALID" in str(e):
            names = ", ".join(t.name for t in targets)
            logger.error(
                "Invalid gift_id=%d (%s) — run 'python -m sniper --list-gifts' "
                "to see valid IDs. Skipping.",
                gift_id,
                names,
            )
        else:
            logger.exception("Bad request polling gift_id=%d", gift_id)
        return
    except Exception:
        logger.exception("Error polling gift_id=%d", gift_id)
        return

    _stats["polls"] += 1

    if not hasattr(result, "gifts") or not result.gifts:
        logger.debug("No resale listings for gift_id=%d", gift_id)
        return

    for gift in result.gifts:
        if not isinstance(gift, types.StarGiftUnique):
            continue

        _stats["gifts_seen"] += 1
        slug = gift.slug
        price = _extract_price(gift)

        if price is None:
            logger.debug("Skipping gift slug=%s — no Stars price", slug)
            continue

        if slug in _seen_slugs:
            continue

        if price > cfg.max_spend_per_buy:
            continue

        for target in targets:
            if price > target.max_price:
                continue

            if not _gift_matches_filter(gift, target):
                continue

            logger.info(
                "HIT: %s #%d — %d Stars (max %d) slug=%s",
                target.name,
                gift.num,
                price,
                target.max_price,
                slug,
            )

            _seen_slugs.add(slug)
            _stats["buys_attempted"] += 1

            ok = await buy_gift(
                client,
                slug=slug,
                price=price,
                gift_title=f"{target.name} #{gift.num}",
                dry_run=cfg.dry_run,
                pay_with_ton=target.pay_with_ton,
            )
            if ok:
                _stats["buys_ok"] += 1
                if cfg.notify_chat_id:
                    await _send_notification(client, cfg.notify_chat_id, target, gift, price)
            else:
                _stats["buys_fail"] += 1
            break  # gift matched a target, move to next gift


async def _send_notification(
    client: TelegramClient,
    chat_id: int,
    target: TargetGift,
    gift: types.StarGiftUnique,
    price: int,
) -> None:
    currency = "TON" if target.pay_with_ton else "Stars"
    try:
        await client.send_message(
            chat_id,
            f"Bought **{target.name} #{gift.num}** for {price} {currency}\nSlug: `{gift.slug}`",
            parse_mode="md",
        )
    except Exception:
        logger.exception("Failed to send notification")


async def run_loop(client: TelegramClient, cfg: Config) -> None:
    """Main polling loop — runs until cancelled."""
    reload_counter = 0
    logger.info(
        "Starting sniper loop: %d targets, interval=%.1fs, dry_run=%s",
        len(cfg.targets),
        cfg.poll_interval,
        cfg.dry_run,
    )

    while True:
        t0 = time.monotonic()

        # Group targets by gift_id → one API call per collection
        by_gift: dict[int, list[TargetGift]] = defaultdict(list)
        for target in cfg.targets:
            by_gift[target.gift_id].append(target)

        for gift_id, targets in by_gift.items():
            await _poll_gift_id(client, gift_id, targets, cfg)

        elapsed = time.monotonic() - t0
        sleep_for = max(0.1, cfg.poll_interval - elapsed)
        logger.debug(
            "Cycle done in %.2fs, sleeping %.2fs | stats=%s",
            elapsed,
            sleep_for,
            _stats,
        )
        await asyncio.sleep(sleep_for)

        reload_counter += 1
        if reload_counter >= 20:
            cfg.reload_targets()
            reload_counter = 0

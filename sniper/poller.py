"""Polls payments.getResaleStarGifts for each target and triggers buy."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from telethon import functions, types
from telethon.errors import BadRequestError, FloodWaitError

from sniper.buyer import buy_gift
from sniper.config import Config, TargetGift

CONNECTION_ERRORS = (ConnectionError, OSError)

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


def _now_msk() -> str:
    """Current time in Moscow as HH:MM:SS."""
    return datetime.now(_MSK).strftime("%H:%M:%S")

# Keep track of slugs we already attempted to buy (avoid double-buying)
_seen_slugs: set[str] = set()

# Optional bot notification callback (set from __main__)
_notify_fn: Callable[..., Coroutine[Any, Any, None]] | None = None


def set_notify_fn(fn: Callable[..., Coroutine[Any, Any, None]]) -> None:
    global _notify_fn
    _notify_fn = fn


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


_NANOTON = 1_000_000_000


def _extract_prices(gift: types.StarGiftUnique) -> dict[str, float]:
    """Extract resale prices from a StarGiftUnique.

    Returns dict with 'stars' (int) and/or 'ton' (float, in TON) keys.
    StarsTonAmount comes in nanotons, so we convert to TON.
    """
    prices: dict[str, float] = {}
    if not gift.resell_amount:
        return prices
    for amt in gift.resell_amount:
        if isinstance(amt, types.StarsAmount):
            prices["stars"] = amt.amount
        elif isinstance(amt, types.StarsTonAmount):
            prices["ton"] = amt.amount / _NANOTON
    return prices


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


# Fixed gap between per-collection requests inside one polling cycle.
# Telegram's rate limiter on GetResaleStarGiftsRequest throttled the bot at
# 1.0s; 2.0s keeps the flood-wait rate at ~0 for typical target counts.
_PER_COLLECTION_GAP = 2.0


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
    except CONNECTION_ERRORS:
        logger.warning("Connection lost, reconnecting…")
        try:
            await client.connect()
        except Exception:
            logger.exception("Reconnect failed")
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
        prices = _extract_prices(gift)

        if not prices:
            logger.debug("Skipping gift slug=%s — no price", slug)
            continue

        if slug in _seen_slugs:
            continue
        _seen_slugs.add(slug)

        if not _warmed_up:
            continue

        for target in targets:
            price_key = "ton" if target.pay_with_ton else "stars"
            price = prices.get(price_key)
            if price is None:
                continue

            if price > cfg.max_spend_per_buy:
                continue

            if price > target.max_price:
                continue

            if not _gift_matches_filter(gift, target):
                continue

            currency = "TON" if target.pay_with_ton else "Stars"
            price_fmt = f"{price:.4f}" if target.pay_with_ton else str(int(price))
            logger.info(
                "HIT: %s #%d — %s %s (max %s) slug=%s",
                target.name,
                gift.num,
                price_fmt,
                currency,
                target.max_price,
                slug,
            )

            _stats["buys_attempted"] += 1

            from sniper.markets import is_auto_buy as _is_auto_buy

            auto_buy_on = _is_auto_buy()

            ok = await buy_gift(
                client,
                slug=slug,
                price=price,
                gift_title=f"{target.name} #{gift.num}",
                dry_run=not auto_buy_on,
                pay_with_ton=target.pay_with_ton,
            )
            if ok:
                _stats["buys_ok"] += 1
                if cfg.notify_chat_id:
                    await _send_notification(client, cfg.notify_chat_id, target, gift, price)
            else:
                _stats["buys_fail"] += 1

            if _notify_fn:
                try:
                    tg_link = f"https://t.me/nft/{slug}" if slug else ""
                    link_line = f"\n🔗 {tg_link}" if tg_link else ""
                    seen_line = f"\n🕐 Обнаружено: {_now_msk()} МСК"
                    if auto_buy_on:
                        buy_result = "\n✅ Куплено!" if ok else "\n❌ Покупка не удалась"
                    else:
                        buy_result = "\nАвтопокупка: OFF"
                    msg = (
                        f"📍 Telegram Resale\n"
                        f"🎯 {target.name} #{gift.num}\n"
                        f"Цена: {price_fmt} {currency} (макс {target.max_price})"
                        f"{seen_line}"
                        f"{buy_result}"
                        f"{link_line}"
                    )
                    await _notify_fn(msg)
                except Exception:
                    logger.exception("Failed to send bot notification")

            break  # gift matched a target, move to next gift


async def _send_notification(
    client: TelegramClient,
    chat_id: int,
    target: TargetGift,
    gift: types.StarGiftUnique,
    price: float,
) -> None:
    currency = "TON" if target.pay_with_ton else "Stars"
    price_fmt = f"{price:.4f}" if target.pay_with_ton else str(int(price))
    try:
        await client.send_message(
            chat_id,
            f"Bought **{target.name} #{gift.num}** for {price_fmt} {currency}\n"
            f"Slug: `{gift.slug}`",
            parse_mode="md",
        )
    except Exception:
        logger.exception("Failed to send notification")


def _bot_targets_to_config(bot_targets: list[dict]) -> list[TargetGift]:
    """Convert bot-managed target dicts into TargetGift objects."""
    return [
        TargetGift(
            gift_id=int(t["gift_id"]),
            max_price=int(t["max_price"]),
            name=str(t["name"]),
            pay_with_ton=bool(t.get("pay_with_ton", False)),
            model=t.get("model"),
            pattern=t.get("pattern"),
            backdrop=t.get("backdrop"),
        )
        for t in bot_targets
    ]


_warmed_up = False


async def run_loop(
    client: TelegramClient,
    cfg: Config,
    use_bot_targets: bool = False,
    self_mode: bool = False,
) -> None:
    """Main polling loop — runs until cancelled."""
    global _warmed_up
    reload_counter = 0
    logger.info(
        "Starting sniper loop: %d targets, interval=%.1fs",
        len(cfg.targets),
        cfg.poll_interval,
    )

    while True:
        t0 = time.monotonic()

        # Merge config targets with dynamic targets
        if self_mode:
            from sniper.selfbot import get_active_targets as self_targets

            all_targets = _bot_targets_to_config(self_targets())
        elif use_bot_targets:
            from sniper.bot import get_active_targets

            all_targets = _bot_targets_to_config(get_active_targets())
        else:
            all_targets = list(cfg.targets)

        # Group targets by gift_id → one API call per collection
        by_gift: dict[int, list[TargetGift]] = defaultdict(list)
        for target in all_targets:
            by_gift[target.gift_id].append(target)

        # Space requests out so Telegram's per-method rate limiter doesn't
        # emit FloodWait on GetResaleStarGiftsRequest.
        gift_items = list(by_gift.items())
        for i, (gift_id, targets) in enumerate(gift_items):
            if i > 0:
                await asyncio.sleep(_PER_COLLECTION_GAP)
            await _poll_gift_id(client, gift_id, targets, cfg)

        if not _warmed_up:
            _warmed_up = True
            logger.info("Sniper warm-up done, %d slugs cached", len(_seen_slugs))

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

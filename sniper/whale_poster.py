"""Post whale-feed sale events to a Telegram channel via Bot API.

Format mimics @giftwhalefeed:

    🎉 GIFT SOLD!

    🏷 <Collection> #<num>
    ├ Model: <model>
    ├ Backdrop: <backdrop>
    ├ Symbol: <symbol>
    ├ Price: 156.00 TON (~$200.00)
    └ Sold on Telegram

    https://t.me/nft/<slug>
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from telethon import types

    from sniper.whale_feed import TrackedListing

logger = logging.getLogger(__name__)


# Cache TON/USD rate so we don't hit CoinGecko on every post.
_USD_CACHE: dict[str, float | int] = {"price": 0.0, "ts": 0}
_USD_TTL_SEC = 300  # refresh every 5 minutes
_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=the-open-network&vs_currencies=usd"
)


async def _ton_usd_rate() -> float:
    """Return cached TON/USD rate; refresh every _USD_TTL_SEC seconds."""
    now = time.time()
    if (
        _USD_CACHE["price"]
        and now - float(_USD_CACHE["ts"]) < _USD_TTL_SEC
    ):
        return float(_USD_CACHE["price"])
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(_COINGECKO_URL)
            resp.raise_for_status()
            data = resp.json()
            price = float(data["the-open-network"]["usd"])
    except Exception:
        logger.warning("Failed to fetch TON/USD rate; using last known")
        return float(_USD_CACHE["price"])  # may be 0.0 on first failure
    _USD_CACHE["price"] = price
    _USD_CACHE["ts"] = now
    return price


def _format_post(
    listing: TrackedListing,
    final_price_ton: float | None,
    usd_rate: float,
) -> str:
    """Return the channel-message body."""
    price_ton = final_price_ton if final_price_ton is not None else listing.price_ton
    title = f"{listing.collection_title} #{listing.num}"
    link = f"https://t.me/nft/{listing.slug}"

    lines: list[str] = []
    lines.append("🎉 GIFT SOLD!")
    lines.append("")
    lines.append(f"🏷 {title}")
    if listing.model:
        lines.append(f"├ Model: {listing.model}")
    if listing.backdrop:
        lines.append(f"├ Backdrop: {listing.backdrop}")
    if listing.symbol:
        lines.append(f"├ Symbol: {listing.symbol}")

    if usd_rate > 0:
        usd = price_ton * usd_rate
        price_line = f"├ Price: {price_ton:.2f} TON (~${usd:.2f})"
    else:
        price_line = f"├ Price: {price_ton:.2f} TON"
    lines.append(price_line)
    lines.append("└ Sold on Telegram")
    lines.append("")
    lines.append(link)
    return "\n".join(lines)


def create_whale_poster(
    bot_token: str,
    channel: str,
    min_interval_sec: float = 1.1,
):
    """Return an async `post(listing, gift)` callback that publishes to a channel.

    Posts are throttled to at most 1 per `min_interval_sec` to stay within
    Telegram's bot anti-spam limits for channels.
    """
    from telegram import Bot

    bot = Bot(token=bot_token)
    lock = asyncio.Lock()
    state = {"last_sent": 0.0}

    async def post(listing: TrackedListing, gift: types.StarGiftUnique | None) -> None:
        usd = await _ton_usd_rate()
        text = _format_post(listing, listing.price_ton, usd)

        async with lock:
            now = asyncio.get_event_loop().time()
            wait = min_interval_sec - (now - state["last_sent"])
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await bot.send_message(
                    chat_id=channel,
                    text=text,
                    disable_web_page_preview=False,
                )
                logger.info(
                    "Posted whale sale: %s #%d @ %.2f TON",
                    listing.collection_title,
                    listing.num,
                    listing.price_ton,
                )
            except Exception:
                logger.exception(
                    "Failed to post whale sale slug=%s",
                    listing.slug,
                )
            finally:
                state["last_sent"] = asyncio.get_event_loop().time()

    return post

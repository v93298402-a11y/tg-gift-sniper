"""Post whale-feed sale events to a Telegram channel via Bot API.

Supports multiple sources (Telegram Resale, Getgems, Fragment, …). All
sources hand a :class:`~sniper.whale_types.WhaleSale` to the poster. The
poster:

  * deduplicates across sources (same sale reported by 2+ sources gets one
    post — first one wins) using a TTL cache;
  * caches TON/USD rate from CoinGecko (refreshed every 5 min);
  * throttles outbound channel messages to ≤ 1 per ``min_interval_sec``.

Format mimics @giftwhalefeed:

    🎉 GIFT SOLD!

    🏷 <Collection> #<num>
    ├ Model: <model>
    ├ Backdrop: <backdrop>
    ├ Symbol: <symbol>
    ├ Price: 156.00 TON (~$200.00)
    └ Sold on <Source>

    https://t.me/nft/<slug>
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from sniper.whale_types import DedupCache, WhaleSale

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


def _format_post(sale: WhaleSale, usd_rate: float) -> str:
    """Return the channel-message body for a sale."""
    if sale.collection_title and sale.num is not None:
        title = f"{sale.collection_title} #{sale.num}"
    else:
        title = sale.title

    lines: list[str] = []
    lines.append("🎉 GIFT SOLD!")
    lines.append("")
    lines.append(f"🏷 {title}")
    if sale.model:
        lines.append(f"├ Model: {sale.model}")
    if sale.backdrop:
        lines.append(f"├ Backdrop: {sale.backdrop}")
    if sale.symbol:
        lines.append(f"├ Symbol: {sale.symbol}")

    if usd_rate > 0:
        usd = sale.price_ton * usd_rate
        price_line = f"├ Price: {sale.price_ton:.2f} TON (~${usd:.2f})"
    else:
        price_line = f"├ Price: {sale.price_ton:.2f} TON"
    lines.append(price_line)
    lines.append(f"└ Sold on {sale.source}")

    link = sale.link
    if link:
        lines.append("")
        lines.append(link)
    return "\n".join(lines)


def create_whale_poster(
    bot_token: str,
    channel: str,
    min_interval_sec: float = 1.1,
    dedup_ttl_sec: float = 1800.0,
):
    """Return an async ``post(sale)`` callback that publishes to a channel.

    Posts are throttled to at most 1 per ``min_interval_sec`` to stay within
    Telegram's bot anti-spam limits for channels.

    Cross-source dedup: if the same sale (same dedup key) was already posted
    in the last ``dedup_ttl_sec`` seconds, the duplicate is silently dropped.
    """
    from telegram import Bot

    bot = Bot(token=bot_token)
    lock = asyncio.Lock()
    state = {"last_sent": 0.0}
    dedup = DedupCache(ttl_sec=dedup_ttl_sec)

    async def post(sale: WhaleSale) -> None:
        key = sale.dedup_key
        if dedup.seen(key):
            logger.info(
                "Skipping duplicate %s sale: %s @ %.2f TON (key=%s)",
                sale.source,
                sale.title,
                sale.price_ton,
                key,
            )
            return

        usd = await _ton_usd_rate()
        text = _format_post(sale, usd)

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
                dedup.mark(key)
                logger.info(
                    "Posted %s sale: %s @ %.2f TON",
                    sale.source,
                    sale.title,
                    sale.price_ton,
                )
            except Exception:
                logger.exception(
                    "Failed to post %s sale: %s @ %.2f TON",
                    sale.source,
                    sale.title,
                    sale.price_ton,
                )
            finally:
                state["last_sent"] = asyncio.get_event_loop().time()

    return post

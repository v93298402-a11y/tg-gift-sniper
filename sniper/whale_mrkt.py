"""Whale-feed source for MRKT (tgmrkt.io) Telegram-gift sales.

This source is a *Telethon channel scraper*, not an HTTP API client.

MRKT operates an official "Real-Time Sales" notification channel at
``@mrktnotification`` (linked from the main ``@official_mrkt`` channel
description). Every gift sale on the marketplace produces a single
post in this channel within seconds. The post format is::

    ⚡ Gift Sold

    Jack-in-the-Box #11960

    - Model: Super Block (3%)
    - Symbol: Valkyrie (%)
    - Backdrop: Moonstone (%)

    😋Price: 3.21 TON

    [forwarded NFT preview]
    [inline button: "Check it on MRKT"]

…or sometimes a single-line variant for pinned / promoted whales::

    ⚡ Gift Sold  Scared Cat #11554  - Model: Puss in Boots (4%) -
    Symbol: Blood Drop (%) - Backdrop: Azure Blue (%) 😋 Price: 142.97 TON

We previously polled MRKT's ``/api/v1/feed`` HTTP endpoint, but it sits
behind a Cloudflare WAF rule that returns 401 to every request from
non-residential IPs (verified across direct httpx, curl_cffi with
Chrome TLS fingerprint, HTTP/2, with/without our Telethon-issued auth
token, with/without browser-like Origin/Referer/sec-* headers). The
WAF rule cannot be bypassed without a residential proxy, so we instead
read the same data from the marketplace's own Telegram channel using
the existing Telethon session.

Advantages of the channel-scraper approach:

* No HTTP auth, no proxy, no Cloudflare hassle.
* Real-time: posts arrive ≤1s after each sale.
* Uses the Telethon client we already have.

Disadvantages:

* Tied to MRKT's chosen post format; if they reword posts the parser
  must follow. The format has been stable for months, but a fallback
  log path is included so format drift surfaces immediately.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from telethon import events
from telethon.errors import FloodWaitError

from sniper.whale_enrich import enrich_attributes
from sniper.whale_types import WhaleSale, derive_slug

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)


GIFT_THRESHOLD_TON = 100.0
MRKT_CHANNEL = "mrktnotification"

_GIFT_SOLD_ANCHOR = "Gift Sold"

# "Title #12345" — accepts letters, digits, spaces, hyphens, apostrophes,
# dots in the title. Matches first occurrence after "Gift Sold".
# IMPORTANT: marketplaces (and Telegram itself) frequently render names
# with TYPOGRAPHIC apostrophes — right single quote U+2019 (’),
# left single quote U+2018 (‘), modifier letter apostrophe U+02BC (ʼ).
# If the regex only allows ASCII '\'' the engine treats the curly
# apostrophe as a non-class char, skips past it, and starts matching
# at the next letter, producing e.g. "s Cap" instead of "Santa’s Cap".
_TITLE_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 .'’‘ʼ\-]*?)\s*#\s*(\d+)",
)
# "Price: 142.97 TON" or "😋Price:142.97 TON". Tolerates missing/extra
# whitespace and stray emoji directly preceding "Price".
_PRICE_RE = re.compile(
    r"Price\s*:?\s*([\d]+(?:[.,]\d+)?)\s*TON",
    re.IGNORECASE,
)
# "Model: Super Block (3%)" — capture everything between the keyword
# and the next attribute boundary, then strip ``(rarity)`` and trailing
# punctuation in :func:`_clean_attr`. Single-line whale posts bunch
# attributes on one line separated by " - ", so we stop at the next
# attribute keyword or the price-emoji rather than relying on \n.
_ATTR_BOUNDARY = (
    r"(?=\s*(?:-\s*(?:Model|Symbol|Backdrop)|Price|😋|\n|$))"
)
_MODEL_RE = re.compile(
    rf"Model\s*:\s*(.+?){_ATTR_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_SYMBOL_RE = re.compile(
    rf"Symbol\s*:\s*(.+?){_ATTR_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_BACKDROP_RE = re.compile(
    rf"Backdrop\s*:\s*(.+?){_ATTR_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_RARITY_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


_stats: dict[str, int] = {
    "messages_seen": 0,
    "sales_parsed": 0,
    "whales_emitted": 0,
    "parse_errors": 0,
    "below_threshold": 0,
}


def get_stats() -> dict[str, int]:
    return dict(_stats)


def _clean_attr(s: str | None) -> str | None:
    """Tidy an attribute capture: drop trailing rarity, dashes, emoji."""
    if s is None:
        return None
    s = s.strip()
    s = _RARITY_PAREN_RE.sub("", s)
    s = s.strip(" -·.,;:")
    return s or None


def parse_sale_message(text: str) -> WhaleSale | None:
    """Parse one MRKT notification post into a :class:`WhaleSale`.

    Returns ``None`` if the message is not a recognised "Gift Sold"
    post. Only the price field is mandatory; if other fields are
    missing they're left as ``None`` and the poster will degrade
    gracefully.
    """
    if not text:
        return None
    if _GIFT_SOLD_ANCHOR not in text:
        return None

    # Trim everything before the anchor so a stray "#12345" earlier in
    # the message (e.g. quoted source post) can't fool _TITLE_RE.
    body = text[text.index(_GIFT_SOLD_ANCHOR) + len(_GIFT_SOLD_ANCHOR):]

    title_match = _TITLE_RE.search(body)
    if not title_match:
        return None
    title = title_match.group(1).strip()
    try:
        num = int(title_match.group(2))
    except ValueError:
        return None
    full_title = f"{title} #{num}"
    slug, collection_title, _ = derive_slug(full_title)

    price_match = _PRICE_RE.search(body)
    if not price_match:
        return None
    raw_price = price_match.group(1).replace(",", ".")
    try:
        price_ton = float(raw_price)
    except ValueError:
        return None
    if price_ton <= 0:
        return None

    model_match = _MODEL_RE.search(body)
    symbol_match = _SYMBOL_RE.search(body)
    backdrop_match = _BACKDROP_RE.search(body)

    return WhaleSale(
        source="MRKT",
        title=full_title,
        price_ton=price_ton,
        collection_title=collection_title or title,
        num=num,
        slug=slug,
        model=_clean_attr(model_match.group(1)) if model_match else None,
        symbol=_clean_attr(symbol_match.group(1)) if symbol_match else None,
        backdrop=_clean_attr(backdrop_match.group(1)) if backdrop_match else None,
        nft_address=None,
        seller_address=None,
        buyer_address=None,
    )


async def run_mrkt_feed(
    client: TelegramClient,
    on_sold: Callable[[WhaleSale], Awaitable[None]],
    threshold_ton: float = GIFT_THRESHOLD_TON,
    channel: str = MRKT_CHANNEL,
) -> None:
    """Subscribe to ``@mrktnotification`` via Telethon and forward whales.

    Runs forever. Resolves the channel entity once on startup, then
    registers a NewMessage handler scoped to that channel. The handler
    parses each post; sales at or above ``threshold_ton`` are forwarded
    to ``on_sold``.

    The Telethon client must already be connected and authorised
    (i.e. the same client used by the rest of whale-feed). If the
    bound account isn't a member of the channel, this function logs
    a warning and returns — the channel is public, so a one-time
    join from any Telegram client is sufficient.
    """
    try:
        entity = await client.get_entity(channel)
    except FloodWaitError as e:
        logger.warning(
            "MRKT: FloodWait %ds resolving @%s; aborting source.",
            e.seconds,
            channel,
        )
        return
    except Exception:
        logger.exception(
            "MRKT: failed to resolve @%s — is the account subscribed?",
            channel,
        )
        return

    logger.info(
        "MRKT feed starting: channel=@%s threshold=%.1f TON",
        channel,
        threshold_ton,
    )

    @client.on(events.NewMessage(chats=entity))
    async def _on_new_post(event):  # noqa: ANN001
        _stats["messages_seen"] += 1
        text = event.message.message or ""
        try:
            sale = parse_sale_message(text)
        except Exception:
            _stats["parse_errors"] += 1
            logger.exception("MRKT: parser crashed on message %s", event.id)
            return
        if sale is None:
            return
        _stats["sales_parsed"] += 1
        if sale.price_ton < threshold_ton:
            _stats["below_threshold"] += 1
            return

        # Backfill any missing attributes (rare for MRKT — their posts
        # usually carry Model/Symbol/Backdrop — but cheap insurance
        # against format drift).
        if not (sale.model and sale.symbol and sale.backdrop):
            try:
                await enrich_attributes(client, sale)
            except Exception:
                logger.exception("MRKT: enrich raised, posting partial sale")

        _stats["whales_emitted"] += 1
        logger.info(
            "MRKT whale: %s @ %.2f TON (model=%s sym=%s bd=%s)",
            sale.title,
            sale.price_ton,
            sale.model,
            sale.symbol,
            sale.backdrop,
        )
        try:
            await on_sold(sale)
        except Exception:
            logger.exception("MRKT: on_sold callback raised")

    # Keep the wrapping task alive forever — the registered handler
    # runs on the client's main loop regardless of this coroutine's
    # state. Using an Event that's never set is cleaner than calling
    # run_until_disconnected from each source independently.
    await asyncio.Event().wait()

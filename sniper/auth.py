"""Auto-fetch auth tokens for third-party marketplaces via Telegram WebView."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlparse

from telethon import functions

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

_PORTALS_BOT = "portals"
_PORTALS_URL = "https://portal-market.com/"

_MRKT_BOT = "mrkt"
_MRKT_URL = "https://tgmrkt.io/"


def _extract_init_data(url: str) -> str:
    """Extract tgWebAppData from the WebView result URL fragment."""
    parsed = urlparse(url)
    fragment = parsed.fragment
    qs = parse_qs(fragment)
    raw = qs.get("tgWebAppData", [""])[0]
    return unquote(raw) if raw else ""


async def get_portals_token(client: TelegramClient) -> str:
    """Get Portals auth token via RequestWebView."""
    try:
        bot = await client.get_input_entity(_PORTALS_BOT)
        result = await client(
            functions.messages.RequestWebViewRequest(
                peer=bot,
                bot=bot,
                platform="android",
                url=_PORTALS_URL,
            )
        )
        init_data = _extract_init_data(result.url)
        if init_data:
            token = f"tma {init_data}"
            logger.info("Portals auth token obtained (%d chars)", len(token))
            return token
        logger.warning("Failed to extract Portals init data from URL")
    except Exception:
        logger.exception("Failed to get Portals auth token")
    return ""


async def get_mrkt_token(client: TelegramClient) -> str:
    """Get MRKT auth token via RequestWebView."""
    try:
        bot = await client.get_input_entity(_MRKT_BOT)
        result = await client(
            functions.messages.RequestWebViewRequest(
                peer=bot,
                bot=bot,
                platform="android",
                url=_MRKT_URL,
            )
        )
        init_data = _extract_init_data(result.url)
        if init_data:
            logger.info("MRKT auth token obtained (%d chars)", len(init_data))
            return init_data
        logger.warning("Failed to extract MRKT init data from URL")
    except Exception:
        logger.exception("Failed to get MRKT auth token")
    return ""

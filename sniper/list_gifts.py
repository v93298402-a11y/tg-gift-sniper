"""Utility to list all available gift collections with their IDs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import functions, types

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)


async def list_gifts(client: TelegramClient) -> None:
    """Fetch and print all available gift types."""
    result = await client(functions.payments.GetStarGiftsRequest(hash=0))

    if isinstance(result, types.payments.StarGiftsNotModified):
        print("No gifts data available.")
        return

    print()
    print("=" * 70)
    print(f"{'ID':<25} {'Title':<20} {'Stars':<10} {'Resale':<10} {'Status'}")
    print("=" * 70)

    for gift in result.gifts:
        if not isinstance(gift, types.StarGift):
            continue

        title = gift.title or "(no title)"
        stars = gift.stars
        status = ""
        resale = ""

        if gift.sold_out:
            status = "SOLD OUT"
            if gift.availability_resale:
                resale = f"{gift.availability_resale} on resale"
        elif gift.limited:
            remaining = gift.availability_remains or 0
            total = gift.availability_total or 0
            status = f"{remaining}/{total} left"

        print(f"{gift.id:<25} {title:<20} {stars:<10} {resale:<10} {status}")

    print("=" * 70)
    print()
    print("Use the ID values above as gift_id in config.yaml.")
    print("Only gifts with 'on resale' status can be sniped.")
    print()

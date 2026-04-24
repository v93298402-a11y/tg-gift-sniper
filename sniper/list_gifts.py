"""Utility to list available gift collections and their models/attributes."""

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


async def list_models(client: TelegramClient, gift_id: int) -> None:
    """Fetch and print available models/attributes for a specific gift collection."""
    result = await client(
        functions.payments.GetResaleStarGiftsRequest(
            gift_id=gift_id,
            sort_by_price=True,
            offset="",
            limit=1,
            attributes_hash=0,
        )
    )

    if not hasattr(result, "attributes") or not result.attributes:
        print(f"\nNo attributes found for gift_id={gift_id}")
        return

    models = []
    patterns = []
    backdrops = []

    for attr in result.attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            models.append(attr)
        elif isinstance(attr, types.StarGiftAttributePattern):
            patterns.append(attr)
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            backdrops.append(attr)

    if models:
        print(f"\n{'=' * 50}")
        print(f"MODELS for gift_id={gift_id}")
        print(f"{'=' * 50}")
        for m in models:
            rarity = ""
            if m.rarity:
                rarity_name = type(m.rarity).__name__.replace("StarGiftAttributeRarity", "")
                rarity = f" [{rarity_name}]"
            print(f"  {m.name}{rarity}")

    if patterns:
        print(f"\n{'=' * 50}")
        print(f"PATTERNS for gift_id={gift_id}")
        print(f"{'=' * 50}")
        for p in patterns:
            rarity = ""
            if p.rarity:
                rarity_name = type(p.rarity).__name__.replace("StarGiftAttributeRarity", "")
                rarity = f" [{rarity_name}]"
            print(f"  {p.name}{rarity}")

    if backdrops:
        print(f"\n{'=' * 50}")
        print(f"BACKDROPS for gift_id={gift_id}")
        print(f"{'=' * 50}")
        for b in backdrops:
            rarity = ""
            if b.rarity:
                rarity_name = type(b.rarity).__name__.replace("StarGiftAttributeRarity", "")
                rarity = f" [{rarity_name}]"
            print(f"  {b.name}{rarity}")

    print()
    print("Use these names in config.yaml, e.g.:")
    print('  model: "Goldizzle"')
    print('  pattern: "Stars"')
    print('  backdrop: "Crimson"')
    print()

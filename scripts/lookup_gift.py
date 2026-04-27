"""Look up the current owner of a Telegram-Resale gift by slug.

Usage:

    .venv/bin/python scripts/lookup_gift.py IonicDryer-11992

Prints:
  * owner_id (Telegram Peer — typically PeerUser with user_id, the buyer)
  * owner_address (TON wallet address — populated only if the gift was
    transferred onto a wallet)
  * owner_name (display name; sometimes empty)
  * resolved User profile (first/last name + @username) if the session
    has previously seen the owner in some chat — Telegram enforces this
    "input access hash" rule, so unknown random IDs cannot be resolved.

If the owner cannot be resolved, you can still construct a clickable
profile link in Telegram by typing it as ``tg://user?id=<id>`` in any
chat — Telegram itself will resolve the ID server-side (provided the
target user hasn't restricted lookup-by-ID in privacy settings).
"""

from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
from telethon import TelegramClient, functions, types

from sniper.config import DEFAULT_API_HASH, DEFAULT_API_ID

load_dotenv(os.path.join(_ROOT, ".env"))


async def main(slug: str) -> None:
    api_id = int(os.getenv("API_ID", str(DEFAULT_API_ID)))
    api_hash = os.getenv("API_HASH", DEFAULT_API_HASH)
    session = os.path.join(_ROOT, os.getenv("SESSION_NAME", "sniper"))
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: session not authorised.")
        return

    try:
        result = await client(
            functions.payments.GetUniqueStarGiftRequest(slug=slug)
        )
    except Exception as e:
        print(f"GetUniqueStarGift failed: {e}")
        await client.disconnect()
        return

    gift = getattr(result, "gift", None)
    if not isinstance(gift, types.StarGiftUnique):
        print(f"No StarGiftUnique returned for slug={slug}")
        await client.disconnect()
        return

    print(f"=== {slug} ===")
    print(f"  title:         {gift.title} #{gift.num}")
    print(f"  owner_id:      {gift.owner_id}")
    print(f"  owner_address: {gift.owner_address}")
    print(f"  owner_name:    {gift.owner_name}")
    listed = bool(gift.resell_amount)
    print(f"  currently listed: {listed}")

    owner = gift.owner_id
    if isinstance(owner, types.PeerUser):
        print(f"  ==> buyer/owner Telegram user_id: {owner.user_id}")
        try:
            user = await client.get_entity(owner)
        except Exception as e:
            print(f"  cannot resolve user profile (Telegram input-access-hash rule): {e}")
            print(
                f"  workaround: paste 'tg://user?id={owner.user_id}' into any "
                "chat — Telegram will render a clickable profile link "
                "(unless the user has restricted lookup-by-ID in privacy)."
            )
        else:
            uname = f"@{user.username}" if user.username else "(no username)"
            print(
                f"  resolved user: {user.first_name or ''} "
                f"{user.last_name or ''}  {uname}  (id={user.id})"
            )

    await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: lookup_gift.py <slug>   e.g. IonicDryer-11992")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))

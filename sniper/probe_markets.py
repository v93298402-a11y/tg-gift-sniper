"""One-shot diagnostic: probe MRKT and Portals sales-history endpoints.

Run on the VPS once to discover which endpoint each marketplace exposes
for "recently sold gifts" (the sales feed used by GiftWhaleFeed and
similar channels). Auto-fetches an auth token from the running
Telethon session, then tries a list of candidate endpoints with
both GET and POST and prints which ones return JSON with gift-sale
data.

Usage::

    cd /root/tg-gift-sniper
    sudo systemctl stop whale-feed   # free up the Telethon session
    .venv/bin/python -m sniper.probe_markets
    sudo systemctl start whale-feed
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient

from sniper.auth import get_mrkt_token, get_portals_token

logger = logging.getLogger(__name__)


_MRKT_BASE = "https://api.tgmrkt.io"
_MRKT_CANDIDATES_GET: list[str] = [
    "/api/v1/gifts/sold",
    "/api/v1/gifts/history",
    "/api/v1/sales",
    "/api/v1/gifts/sales",
    "/api/v1/transactions",
    "/api/v1/activity",
    "/api/v1/feed",
    "/api/v1/gifts/recent",
]
_MRKT_CANDIDATES_POST: list[tuple[str, dict]] = [
    ("/api/v1/transactions", {}),
    ("/api/v1/transactions", {"count": 20, "ordering": "Date", "lowToHigh": False}),
    ("/api/v1/transactions", {"count": 20, "type": "Sold"}),
    ("/api/v1/feed", {}),
    ("/api/v1/feed", {"count": 20}),
    ("/api/v1/gifts/feed", {"count": 20}),
    ("/api/v1/gifts/sold", {"count": 20}),
    ("/api/v1/gifts/transactions", {"count": 20}),
    ("/api/v1/gifts/history", {"count": 20}),
]

_PORTALS_BASE = "https://portal-market.com"
_PORTALS_CANDIDATES_GET: list[tuple[str, dict]] = [
    ("/api/nfts/sold", {"limit": 20, "offset": 0}),
    ("/api/nfts/history", {"limit": 20, "offset": 0}),
    ("/api/nfts/sales", {"limit": 20, "offset": 0}),
    ("/api/nfts/recent-sales", {"limit": 20}),
    ("/api/sales", {"limit": 20, "offset": 0}),
    ("/api/transactions", {"limit": 20, "offset": 0}),
    ("/api/transactions/list", {"limit": 20, "offset": 0}),
    ("/api/activity", {"limit": 20, "offset": 0}),
    ("/api/market/activity", {"limit": 20, "offset": 0}),
    ("/api/market/sales", {"limit": 20, "offset": 0}),
    ("/api/marketplace/activity", {"limit": 20}),
    ("/api/feed", {"limit": 20}),
    ("/api/history", {"limit": 20}),
    ("/api/nfts/activity", {"limit": 20, "offset": 0}),
    ("/api/collections/activity", {"limit": 20}),
]


def _summarise_json(body: object, limit: int = 800) -> str:
    try:
        s = json.dumps(body, ensure_ascii=False)
    except Exception:
        s = repr(body)
    return s[:limit] + ("…" if len(s) > limit else "")


async def _probe_mrkt(token: str) -> None:
    print("\n========== MRKT ==========")
    if not token:
        print("(no token — skipping MRKT)")
        return
    headers = {"Authorization": token, "Referer": "https://cdn.tgmrkt.io/"}
    async with httpx.AsyncClient(timeout=10.0) as http:
        for path in _MRKT_CANDIDATES_GET:
            url = f"{_MRKT_BASE}{path}"
            try:
                r = await http.get(url, headers=headers)
            except Exception as e:
                print(f"GET  {path:40s} -> EXC {e}")
                continue
            print(f"GET  {path:40s} -> {r.status_code}", end="")
            if r.status_code == 200:
                print(f"  body={_summarise_json(r.json())}")
            else:
                print(f"  body={r.text[:200]!r}")

        for path, body in _MRKT_CANDIDATES_POST:
            url = f"{_MRKT_BASE}{path}"
            try:
                r = await http.post(url, json=body, headers=headers)
            except Exception as e:
                print(f"POST {path:40s} body={body!s:60s} -> EXC {e}")
                continue
            print(f"POST {path:40s} body={body!s:60s} -> {r.status_code}", end="")
            if r.status_code == 200:
                print(f"  body={_summarise_json(r.json())}")
            else:
                print(f"  body={r.text[:200]!r}")


async def _probe_portals(token: str) -> None:
    print("\n========== PORTALS ==========")
    if not token:
        print("(no token — skipping Portals)")
        return
    headers = {"Authorization": token}
    async with httpx.AsyncClient(timeout=10.0) as http:
        for path, params in _PORTALS_CANDIDATES_GET:
            url = f"{_PORTALS_BASE}{path}"
            try:
                r = await http.get(url, params=params, headers=headers)
            except Exception as e:
                print(f"GET {path:40s} params={params!s:50s} -> EXC {e}")
                continue
            print(f"GET {path:40s} params={params!s:50s} -> {r.status_code}", end="")
            if r.status_code == 200:
                print(f"  body={_summarise_json(r.json())}")
            else:
                print(f"  body={r.text[:200]!r}")


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    api_id = int(os.getenv("API_ID", "0"))
    api_hash = os.getenv("API_HASH", "")
    session = os.getenv("SESSION_NAME", "sniper")
    if not api_id or not api_hash:
        print("ERROR: API_ID / API_HASH missing from .env")
        return
    client = TelegramClient(session, api_id, api_hash)
    await client.start()

    print("Auto-fetching MRKT token…")
    mrkt = await get_mrkt_token(client)
    print(f"  got MRKT token: {len(mrkt)} chars" if mrkt else "  FAILED")

    print("Auto-fetching Portals token…")
    portals = await get_portals_token(client)
    print(f"  got Portals token: {len(portals)} chars" if portals else "  FAILED")

    await _probe_mrkt(mrkt)
    await _probe_portals(portals)

    await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()) or 0)

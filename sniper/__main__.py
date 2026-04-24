"""Entry point — python -m sniper  or  tg-sniper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from telethon import TelegramClient

from sniper.config import DEFAULT_API_HASH, DEFAULT_API_ID, Config
from sniper.list_gifts import list_gifts, list_models
from sniper.poller import get_stats, run_loop

logger = logging.getLogger("sniper")


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt)


def _make_client() -> TelegramClient:
    import os

    from dotenv import load_dotenv

    load_dotenv()
    api_id = int(os.getenv("API_ID", str(DEFAULT_API_ID)))
    api_hash = os.getenv("API_HASH", DEFAULT_API_HASH)
    session = os.getenv("SESSION_NAME", "sniper")
    return TelegramClient(session, api_id, api_hash)


async def _list_gifts() -> None:
    """Connect and list all available gifts, then exit."""
    client = _make_client()
    await client.start()
    await list_gifts(client)
    await client.disconnect()


async def _list_models(gift_id: int) -> None:
    """Connect and list available models for a gift, then exit."""
    client = _make_client()
    await client.start()
    await list_models(client, gift_id)
    await client.disconnect()


async def _run(cfg: Config) -> None:
    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)

    logger.info("Connecting to Telegram…")
    await client.start()

    me = await client.get_me()
    logger.info("Logged in as %s (id=%d)", me.first_name, me.id)

    if cfg.dry_run:
        logger.warning("DRY-RUN mode is ON — no real purchases will be made")

    loop = asyncio.get_event_loop()

    def _shutdown() -> None:
        logger.info("Shutting down… stats=%s", get_stats())
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        await run_loop(client, cfg)
    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()
        logger.info("Disconnected. Final stats: %s", get_stats())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tg-sniper",
        description="Telegram collectible gift sniper",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--list-gifts",
        action="store_true",
        help="List all available gift collections with IDs, then exit",
    )
    parser.add_argument(
        "--list-models",
        type=int,
        metavar="GIFT_ID",
        help="List available models/patterns/backdrops for a gift collection, then exit",
    )
    args = parser.parse_args()

    if args.list_gifts:
        _setup_logging("INFO")
        asyncio.run(_list_gifts())
        return

    if args.list_models:
        _setup_logging("INFO")
        asyncio.run(_list_models(args.list_models))
        return

    cfg = Config.load(args.config)
    _setup_logging(cfg.log_level)

    logger.info("Config loaded: %s", cfg)

    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        logger.info("Interrupted. Final stats: %s", get_stats())
        sys.exit(0)


if __name__ == "__main__":
    main()

"""Entry point — python -m sniper  or  tg-sniper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from telethon import TelegramClient

from sniper.config import Config
from sniper.poller import get_stats, run_loop

logger = logging.getLogger("sniper")


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt)


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
    args = parser.parse_args()

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

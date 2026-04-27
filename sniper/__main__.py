"""Entry point — python -m sniper  or  tg-sniper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from telethon import TelegramClient

from sniper.auth import get_mrkt_token, get_portals_token
from sniper.config import DEFAULT_API_HASH, DEFAULT_API_ID, Config
from sniper.list_gifts import list_gifts, list_models
from sniper.markets import MarketTarget as MktTarget
from sniper.markets import run_market_monitor
from sniper.poller import get_stats, run_loop
from sniper.whale_feed import get_stats as get_whale_stats
from sniper.whale_feed import run_whale_feed
from sniper.whale_fragment import get_stats as get_fragment_stats
from sniper.whale_fragment import run_fragment_feed
from sniper.whale_getgems import get_stats as get_getgems_stats
from sniper.whale_getgems import run_getgems_feed
from sniper.whale_poster import create_whale_poster

logger = logging.getLogger("sniper")


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt)


def _make_client() -> TelegramClient:
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


async def _run_whale(threshold_ton: float) -> None:
    """Run the multi-source whale feed (Telegram Resale + Getgems + Fragment).

    Telegram Resale is always on (uses Telethon session). Getgems is
    enabled if ``GETGEMS_API_KEY`` is set in the environment. Fragment is
    enabled by default and disabled by setting ``WHALE_FRAGMENT_ENABLED=0``.
    """
    from dotenv import load_dotenv

    load_dotenv()
    bot_token = os.getenv("WHALE_BOT_TOKEN", "").strip()
    channel = os.getenv("WHALE_CHANNEL", "").strip()
    getgems_key = os.getenv("GETGEMS_API_KEY", "").strip()
    fragment_enabled = os.getenv("WHALE_FRAGMENT_ENABLED", "1").strip() != "0"
    if not bot_token:
        logger.error("WHALE_BOT_TOKEN not set in .env — cannot start whale feed")
        sys.exit(1)
    if not channel:
        logger.error("WHALE_CHANNEL not set in .env — cannot start whale feed")
        sys.exit(1)

    client = _make_client()
    logger.info("Connecting to Telegram for whale feed…")
    await client.start()
    me = await client.get_me()
    logger.info("Logged in as %s (id=%d)", me.first_name, me.id)

    poster = create_whale_poster(bot_token=bot_token, channel=channel)
    logger.info(
        "Whale poster ready: channel=%s, threshold=%.1f TON",
        channel,
        threshold_ton,
    )

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info(
            "Whale feed shutting down… telegram=%s getgems=%s fragment=%s",
            get_whale_stats(),
            get_getgems_stats(),
            get_fragment_stats(),
        )
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    tasks: list[asyncio.Task] = []
    tasks.append(
        asyncio.create_task(
            run_whale_feed(client, on_sold=poster, threshold_ton=threshold_ton),
            name="whale-telegram",
        )
    )
    if getgems_key:
        logger.info("Getgems source enabled (API key present).")
        tasks.append(
            asyncio.create_task(
                run_getgems_feed(
                    api_key=getgems_key,
                    on_sold=poster,
                    threshold_ton=threshold_ton,
                ),
                name="whale-getgems",
            )
        )
    else:
        logger.info(
            "Getgems source disabled — set GETGEMS_API_KEY in .env to enable.",
        )

    if fragment_enabled:
        logger.info("Fragment source enabled (HTML polling).")
        tasks.append(
            asyncio.create_task(
                run_fragment_feed(
                    on_sold=poster,
                    threshold_ton=threshold_ton,
                ),
                name="whale-fragment",
            )
        )
    else:
        logger.info(
            "Fragment source disabled (WHALE_FRAGMENT_ENABLED=0).",
        )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()
        logger.info(
            "Whale feed disconnected. Final stats: telegram=%s getgems=%s fragment=%s",
            get_whale_stats(),
            get_getgems_stats(),
            get_fragment_stats(),
        )


async def _run(cfg: Config, bot_mode: bool = False, self_mode: bool = False) -> None:
    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)

    logger.info("Connecting to Telegram…")
    await client.start()

    me = await client.get_me()
    logger.info("Logged in as %s (id=%d)", me.first_name, me.id)

    use_dynamic_targets = False

    if self_mode:
        from sniper.selfbot import register_handlers

        register_handlers(client, cfg)
        use_dynamic_targets = True
        logger.info("Saved Messages interface enabled. Send /menu to Saved Messages.")

    if bot_mode:
        from sniper.bot import build_application, create_notifier, set_telethon_client

        bot_token = os.getenv("BOT_TOKEN", "")
        if not bot_token:
            logger.error("BOT_TOKEN not set in .env — cannot start bot interface")
            await client.disconnect()
            sys.exit(1)

        set_telethon_client(client, cfg)
        app = build_application(bot_token, owner_id=me.id)

        from sniper.poller import set_notify_fn

        set_notify_fn(create_notifier(bot_token, me.id))
        logger.info("Bot notifications enabled for user %d", me.id)

        use_dynamic_targets = True
        logger.info("Starting bot interface…")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Bot interface started. Send /start to your bot.")

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Shutting down… stats=%s", get_stats())
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # Auto-fetch marketplace tokens
    mrkt_token = os.getenv("MRKT_TOKEN", "")
    portals_token = os.getenv("PORTALS_TOKEN", "")

    if not mrkt_token:
        logger.info("Auto-fetching MRKT auth token…")
        mrkt_token = await get_mrkt_token(client)
    if not portals_token:
        logger.info("Auto-fetching Portals auth token…")
        portals_token = await get_portals_token(client)

    async def _refresh_mrkt_token() -> str:
        return await get_mrkt_token(client)

    async def _refresh_portals_token() -> str:
        return await get_portals_token(client)

    # Build dynamic target getter that merges config + bot targets
    cfg_market_targets = [
        MktTarget(
            gift_name=t.gift_name,
            max_price=t.max_price,
            model=t.model,
            pattern=t.pattern,
            backdrop=t.backdrop,
            markets=t.markets,
        )
        for t in cfg.market_targets
    ]

    def _get_market_targets() -> list[MktTarget]:
        targets = list(cfg_market_targets)
        if bot_mode or self_mode:
            if bot_mode:
                from sniper.bot import get_market_targets as bot_mkt
            else:
                from sniper.selfbot import get_market_targets as bot_mkt
            for t in bot_mkt():
                # Strip " (Stars)" / " (TON)" suffix from name
                name = t.get("name", "")
                for suffix in (" (Stars)", " (TON)"):
                    name = name.replace(suffix, "")
                targets.append(
                    MktTarget(
                        gift_name=name,
                        max_price=float(t["market_max_price"]),
                        model=t.get("model"),
                        pattern=t.get("pattern"),
                        backdrop=t.get("backdrop"),
                    )
                )
        return targets

    market_notify_fn = None
    if bot_mode:
        from sniper.bot import create_notifier

        market_notify_fn = create_notifier(bot_token, me.id)

    asyncio.create_task(
        run_market_monitor(
            notify_fn=market_notify_fn,
            poll_interval=cfg.poll_interval + 2,
            mrkt_token=mrkt_token,
            portals_token=portals_token,
            target_fn=_get_market_targets,
            mrkt_reauth_fn=_refresh_mrkt_token,
            portals_reauth_fn=_refresh_portals_token,
        )
    )
    logger.info("Market monitor started")

    try:
        await run_loop(client, cfg, use_bot_targets=use_dynamic_targets, self_mode=self_mode)
    except asyncio.CancelledError:
        pass
    finally:
        if bot_mode:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass
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
    parser.add_argument(
        "--bot",
        action="store_true",
        help="Start with Telegram bot interface for managing targets",
    )
    parser.add_argument(
        "--self",
        action="store_true",
        dest="self_mode",
        help="Manage targets via Saved Messages (no separate bot needed)",
    )
    parser.add_argument(
        "--whale",
        action="store_true",
        help="Run only the whale-feed monitor (sales \u2265 threshold posted to channel)",
    )
    parser.add_argument(
        "--whale-threshold",
        type=float,
        default=100.0,
        help="Minimum TON price to consider a sale 'whale' (default: 100.0)",
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

    if args.whale:
        _setup_logging("INFO")
        try:
            asyncio.run(_run_whale(threshold_ton=args.whale_threshold))
        except KeyboardInterrupt:
            logger.info("Whale feed interrupted.")
            sys.exit(0)
        return

    cfg = Config.load(args.config)
    _setup_logging(cfg.log_level)

    logger.info("Config loaded: %s", cfg)

    try:
        asyncio.run(_run(cfg, bot_mode=args.bot, self_mode=args.self_mode))
    except KeyboardInterrupt:
        logger.info("Interrupted. Final stats: %s", get_stats())
        sys.exit(0)


if __name__ == "__main__":
    main()

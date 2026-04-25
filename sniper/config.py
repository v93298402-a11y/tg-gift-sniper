"""Configuration loader — reads config.yaml and .env."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Telegram Desktop (open-source) credentials — safe to use, no registration needed.
# Can be overridden via .env if you have your own api_id/api_hash.
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


@dataclass
class TargetGift:
    gift_id: int
    max_price: int
    name: str
    pay_with_ton: bool = False
    model: str | None = None
    pattern: str | None = None
    backdrop: str | None = None


@dataclass
class MarketTarget:
    gift_name: str
    max_price: float
    model: str | None = None
    pattern: str | None = None
    backdrop: str | None = None
    markets: list[str] = field(default_factory=lambda: ["tonnel", "mrkt", "portals"])


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_name: str
    poll_interval: float
    dry_run: bool
    max_spend_per_buy: int
    targets: list[TargetGift]
    market_targets: list[MarketTarget]
    notify_chat_id: int | None
    log_level: str
    config_path: Path = field(repr=False)

    @classmethod
    def load(cls, config_path: str | Path = "config.yaml") -> Config:
        load_dotenv()

        api_id_raw = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        if not api_id_raw or not api_hash:
            logger.info("API_ID/API_HASH not set in .env, using Telegram Desktop defaults")
            api_id_raw = str(DEFAULT_API_ID)
            api_hash = DEFAULT_API_HASH

        session_name = os.getenv("SESSION_NAME", "sniper")

        config_path = Path(config_path)
        if not config_path.exists():
            logger.error("Config file not found: %s", config_path)
            sys.exit(1)

        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        targets: list[TargetGift] = []
        for entry in raw.get("targets", []):
            targets.append(
                TargetGift(
                    gift_id=int(entry["gift_id"]),
                    max_price=int(entry["max_price"]),
                    name=str(entry.get("name", f"gift-{entry['gift_id']}")),
                    pay_with_ton=bool(entry.get("pay_with_ton", False)),
                    model=entry.get("model"),
                    pattern=entry.get("pattern"),
                    backdrop=entry.get("backdrop"),
                )
            )

        if not targets:
            logger.warning("No targets defined in config — add via bot or config.yaml")

        market_targets: list[MarketTarget] = []
        for entry in raw.get("market_targets", []):
            mkts = entry.get("markets", ["tonnel", "mrkt", "portals"])
            market_targets.append(
                MarketTarget(
                    gift_name=str(entry["gift_name"]),
                    max_price=float(entry["max_price"]),
                    model=entry.get("model"),
                    pattern=entry.get("pattern"),
                    backdrop=entry.get("backdrop"),
                    markets=mkts,
                )
            )

        notify_raw = raw.get("notify_chat_id")
        notify_chat_id = int(notify_raw) if notify_raw else None

        return cls(
            api_id=int(api_id_raw),
            api_hash=api_hash,
            session_name=session_name,
            poll_interval=float(raw.get("poll_interval", 3)),
            dry_run=bool(raw.get("dry_run", True)),
            max_spend_per_buy=int(raw.get("max_spend_per_buy", 5000)),
            targets=targets,
            market_targets=market_targets,
            notify_chat_id=notify_chat_id,
            log_level=str(raw.get("log_level", "INFO")).upper(),
            config_path=config_path,
        )

    def reload_targets(self) -> None:
        """Hot-reload targets from the config file."""
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            new_targets: list[TargetGift] = []
            for entry in raw.get("targets", []):
                new_targets.append(
                    TargetGift(
                        gift_id=int(entry["gift_id"]),
                        max_price=int(entry["max_price"]),
                        name=str(entry.get("name", f"gift-{entry['gift_id']}")),
                        pay_with_ton=bool(entry.get("pay_with_ton", False)),
                        model=entry.get("model"),
                        pattern=entry.get("pattern"),
                        backdrop=entry.get("backdrop"),
                    )
                )
            if new_targets:
                self.targets = new_targets
                self.dry_run = bool(raw.get("dry_run", True))
                self.poll_interval = float(raw.get("poll_interval", 3))
                self.max_spend_per_buy = int(raw.get("max_spend_per_buy", 5000))
                logger.info(
                    "Config reloaded: %d targets, dry_run=%s, interval=%.1fs",
                    len(self.targets),
                    self.dry_run,
                    self.poll_interval,
                )
        except Exception:
            logger.exception("Failed to reload config, keeping previous values")

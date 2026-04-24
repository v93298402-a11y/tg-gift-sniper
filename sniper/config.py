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


@dataclass
class TargetGift:
    gift_id: int
    max_price: int
    name: str


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_name: str
    poll_interval: float
    dry_run: bool
    max_spend_per_buy: int
    targets: list[TargetGift]
    notify_chat_id: int | None
    log_level: str
    config_path: Path = field(repr=False)

    @classmethod
    def load(cls, config_path: str | Path = "config.yaml") -> Config:
        load_dotenv()

        api_id_raw = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        if not api_id_raw or not api_hash:
            logger.error("API_ID and API_HASH must be set in .env")
            sys.exit(1)

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
                )
            )

        if not targets:
            logger.error("No targets defined in config")
            sys.exit(1)

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

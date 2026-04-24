"""Saved Messages interface for managing sniper targets via Telethon userbot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import events, functions, types

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Runtime state shared with sniper engine
_active_targets: list[dict] = []
_dry_run: bool = True
_client: TelegramClient | None = None

# Conversation state per user (simple state machine)
_conv_state: dict | None = None


def get_active_targets() -> list[dict]:
    return list(_active_targets)


def is_dry_run() -> bool:
    return _dry_run


async def _send(text: str) -> None:
    """Send a message to Saved Messages."""
    if _client:
        await _client.send_message("me", text)


async def _fetch_collections() -> list[dict]:
    if not _client:
        return []
    result = await _client(functions.payments.GetStarGiftsRequest(hash=0))
    if isinstance(result, types.payments.StarGiftsNotModified):
        return []
    collections = []
    for gift in result.gifts:
        if not isinstance(gift, types.StarGift):
            continue
        if not gift.sold_out or not gift.availability_resale:
            continue
        collections.append(
            {
                "id": gift.id,
                "title": gift.title or "(no title)",
                "stars": gift.stars,
                "resale_count": gift.availability_resale,
            }
        )
    return collections


async def _fetch_attributes(gift_id: int) -> dict[str, list[dict]]:
    if not _client:
        return {}
    result = await _client(
        functions.payments.GetResaleStarGiftsRequest(
            gift_id=gift_id,
            sort_by_price=True,
            offset="",
            limit=1,
            attributes_hash=0,
        )
    )
    attrs: dict[str, list[dict]] = {"models": [], "patterns": [], "backdrops": []}
    if not hasattr(result, "attributes") or not result.attributes:
        return attrs
    for attr in result.attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            attrs["models"].append({"name": attr.name})
        elif isinstance(attr, types.StarGiftAttributePattern):
            attrs["patterns"].append({"name": attr.name})
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            attrs["backdrops"].append({"name": attr.name})
    return attrs


def _format_menu() -> str:
    dry_status = "ON (покупки НЕ делаются)" if _dry_run else "OFF (покупки АКТИВНЫ)"
    return (
        "Снайпер — Saved Messages\n\n"
        "Команды:\n"
        "/add — добавить таргет\n"
        "/list — мои таргеты\n"
        "/del <номер> — удалить таргет\n"
        "/delall — удалить все таргеты\n"
        "/dryrun — переключить dry-run\n"
        "/menu — это меню\n"
        f"\nDry-run: {dry_status}\n"
        f"Активных таргетов: {len(_active_targets)}"
    )


async def _handle_add(text: str) -> None:
    global _conv_state
    collections = await _fetch_collections()
    if not collections:
        await _send("Нет коллекций на ресейле.")
        return
    lines = ["Выбери коллекцию (отправь номер):\n"]
    for i, c in enumerate(collections[:30]):
        lines.append(f"{i + 1}. {c['title']} ({c['resale_count']} шт)")
    _conv_state = {"step": "pick_collection", "collections": collections[:30]}
    await _send("\n".join(lines))


async def _handle_conv(text: str) -> None:
    """Handle multi-step conversation for adding targets."""
    global _conv_state
    if _conv_state is None:
        return

    step = _conv_state["step"]

    if step == "pick_collection":
        try:
            idx = int(text.strip()) - 1
            col = _conv_state["collections"][idx]
        except (ValueError, IndexError):
            await _send("Неверный номер. Попробуй ещё:")
            return
        _conv_state = {
            "step": "loading_attrs",
            "gift_id": col["id"],
            "gift_title": col["title"],
            "model": None,
            "pattern": None,
            "backdrop": None,
        }
        await _send(f"Загружаю атрибуты {col['title']}...")
        attrs = await _fetch_attributes(col["id"])
        _conv_state["attrs"] = attrs

        models = attrs.get("models", [])
        if models:
            lines = [f"Коллекция: {col['title']}\nВыбери модель (номер или 0 — пропустить):\n"]
            lines.append("0. Пропустить (любая модель)")
            for i, m in enumerate(models):
                lines.append(f"{i + 1}. {m['name']}")
            _conv_state["step"] = "pick_model"
            _conv_state["models"] = models
            await _send("\n".join(lines))
        else:
            await _ask_backdrop_step()

    elif step == "pick_model":
        try:
            idx = int(text.strip())
        except ValueError:
            await _send("Введи номер:")
            return
        if idx == 0:
            _conv_state["model"] = None
        elif 1 <= idx <= len(_conv_state["models"]):
            _conv_state["model"] = _conv_state["models"][idx - 1]["name"]
        else:
            await _send("Неверный номер:")
            return
        await _ask_backdrop_step()

    elif step == "pick_backdrop":
        try:
            idx = int(text.strip())
        except ValueError:
            await _send("Введи номер:")
            return
        if idx == 0:
            _conv_state["backdrop"] = None
        elif 1 <= idx <= len(_conv_state["backdrops"]):
            _conv_state["backdrop"] = _conv_state["backdrops"][idx - 1]["name"]
        else:
            await _send("Неверный номер:")
            return
        await _ask_pattern_step()

    elif step == "pick_pattern":
        try:
            idx = int(text.strip())
        except ValueError:
            await _send("Введи номер:")
            return
        if idx == 0:
            _conv_state["pattern"] = None
        elif 1 <= idx <= len(_conv_state["patterns"]):
            _conv_state["pattern"] = _conv_state["patterns"][idx - 1]["name"]
        else:
            await _send("Неверный номер:")
            return
        await _ask_price_step()

    elif step == "set_price":
        try:
            price = int(text.strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            await _send("Введи положительное число:")
            return
        _conv_state["max_price"] = price
        _conv_state["step"] = "set_payment"
        await _send(
            f"Макс. цена: {price}\n\n"
            "Чем платить? (отправь номер):\n"
            "1. Stars\n"
            "2. TON\n"
            "3. Оба (Stars + TON)"
        )

    elif step == "set_payment":
        try:
            choice = int(text.strip())
            if choice not in (1, 2, 3):
                raise ValueError
        except ValueError:
            await _send("Отправь 1 (Stars), 2 (TON) или 3 (оба):")
            return

        gift_id = _conv_state["gift_id"]
        gift_title = _conv_state["gift_title"]
        model = _conv_state.get("model")
        backdrop = _conv_state.get("backdrop")
        pattern = _conv_state.get("pattern")
        max_price = _conv_state["max_price"]

        if choice in (1, 3):
            name = gift_title
            if model:
                name += f" {model}"
            name += " (Stars)"
            _active_targets.append(
                {
                    "gift_id": gift_id,
                    "max_price": max_price,
                    "name": name,
                    "pay_with_ton": False,
                    "model": model,
                    "pattern": pattern,
                    "backdrop": backdrop,
                }
            )
        if choice in (2, 3):
            name = gift_title
            if model:
                name += f" {model}"
            name += " (TON)"
            _active_targets.append(
                {
                    "gift_id": gift_id,
                    "max_price": max_price,
                    "name": name,
                    "pay_with_ton": True,
                    "model": model,
                    "pattern": pattern,
                    "backdrop": backdrop,
                }
            )

        payment_label = {1: "Stars", 2: "TON", 3: "Stars + TON"}
        summary = f"Таргет добавлен!\n\nКоллекция: {gift_title}"
        if model:
            summary += f"\nМодель: {model}"
        if backdrop:
            summary += f"\nФон: {backdrop}"
        if pattern:
            summary += f"\nПаттерн: {pattern}"
        summary += f"\nМакс. цена: {max_price}"
        summary += f"\nОплата: {payment_label[choice]}"
        summary += f"\nDry-run: {'ON' if _dry_run else 'OFF'}"
        summary += f"\n\nВсего таргетов: {len(_active_targets)}"

        _conv_state = None
        await _send(summary)


async def _ask_backdrop_step() -> None:
    attrs = _conv_state.get("attrs", {})
    backdrops = attrs.get("backdrops", [])
    if backdrops:
        title = _conv_state["gift_title"]
        model_info = f"\nМодель: {_conv_state['model']}" if _conv_state["model"] else ""
        lines = [f"Коллекция: {title}{model_info}\nВыбери фон (номер или 0 — пропустить):\n"]
        lines.append("0. Пропустить (любой фон)")
        for i, b in enumerate(backdrops):
            lines.append(f"{i + 1}. {b['name']}")
        _conv_state["step"] = "pick_backdrop"
        _conv_state["backdrops"] = backdrops
        await _send("\n".join(lines))
    else:
        await _ask_pattern_step()


async def _ask_pattern_step() -> None:
    attrs = _conv_state.get("attrs", {})
    patterns = attrs.get("patterns", [])
    if patterns:
        title = _conv_state["gift_title"]
        model_info = f"\nМодель: {_conv_state['model']}" if _conv_state["model"] else ""
        bd_info = f"\nФон: {_conv_state['backdrop']}" if _conv_state["backdrop"] else ""
        lines = [
            f"Коллекция: {title}{model_info}{bd_info}\nВыбери паттерн (номер или 0 — пропустить):\n"
        ]
        lines.append("0. Пропустить (любой паттерн)")
        for i, p in enumerate(patterns):
            lines.append(f"{i + 1}. {p['name']}")
        _conv_state["step"] = "pick_pattern"
        _conv_state["patterns"] = patterns
        await _send("\n".join(lines))
    else:
        await _ask_price_step()


async def _ask_price_step() -> None:
    title = _conv_state["gift_title"]
    model = _conv_state.get("model")
    backdrop = _conv_state.get("backdrop")
    pattern = _conv_state.get("pattern")

    summary = f"Коллекция: {title}"
    if model:
        summary += f"\nМодель: {model}"
    if backdrop:
        summary += f"\nФон: {backdrop}"
    if pattern:
        summary += f"\nПаттерн: {pattern}"
    summary += "\n\nВведи максимальную цену (число):"
    _conv_state["step"] = "set_price"
    await _send(summary)


def register_handlers(client: TelegramClient) -> None:
    """Register event handlers on the Telethon client for Saved Messages commands."""
    global _client
    _client = client

    @client.on(events.NewMessage(outgoing=True, chats="me"))
    async def on_saved_message(event: events.NewMessage.Event) -> None:
        global _conv_state, _dry_run
        text = event.raw_text.strip()

        if not text:
            return

        # Commands
        if text.lower() in ("/start", "/menu"):
            _conv_state = None
            await _send(_format_menu())
            return

        if text.lower() == "/add":
            _conv_state = None
            await _handle_add(text)
            return

        if text.lower() == "/list":
            _conv_state = None
            if not _active_targets:
                await _send("Нет активных таргетов. Отправь /add чтобы добавить.")
                return
            lines = ["Активные таргеты:\n"]
            for i, t in enumerate(_active_targets):
                pay = "TON" if t["pay_with_ton"] else "Stars"
                line = f"{i + 1}. {t['name']} — max {t['max_price']} {pay}"
                if t.get("model"):
                    line += f" [{t['model']}]"
                lines.append(line)
            lines.append(f"\nВсего: {len(_active_targets)}")
            lines.append("Отправь /del <номер> чтобы удалить.")
            await _send("\n".join(lines))
            return

        if text.lower().startswith("/del "):
            _conv_state = None
            try:
                idx = int(text.split()[1]) - 1
                if 0 <= idx < len(_active_targets):
                    removed = _active_targets.pop(idx)
                    await _send(f"Удалён: {removed['name']}\nОсталось: {len(_active_targets)}")
                else:
                    await _send(f"Неверный номер. Доступно: 1-{len(_active_targets)}")
            except (ValueError, IndexError):
                await _send("Используй: /del <номер>")
            return

        if text.lower() == "/delall":
            _conv_state = None
            count = len(_active_targets)
            _active_targets.clear()
            await _send(f"Удалено {count} таргетов.")
            return

        if text.lower() == "/dryrun":
            _conv_state = None
            _dry_run = not _dry_run
            status = "ON (покупки НЕ делаются)" if _dry_run else "OFF (покупки АКТИВНЫ)"
            await _send(f"Dry-run: {status}")
            return

        if text.lower() == "/cancel":
            _conv_state = None
            await _send("Отменено. Отправь /menu для меню.")
            return

        # If in conversation, handle input
        if _conv_state is not None:
            await _handle_conv(text)
            return

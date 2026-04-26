"""Telegram bot interface for managing sniper targets via inline keyboards."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telethon import functions, types

from sniper.markets import is_auto_buy, set_auto_buy

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Conversation states
(
    PICK_COLLECTION,
    PICK_MODEL,
    PICK_BACKDROP,
    PICK_PATTERN,
    SET_PRICE,
    SET_PAYMENT,
    SEARCH_PICKER,
) = range(7)

# Runtime state shared with sniper engine
_active_targets: list[dict] = []
_notifications_enabled: bool = True
_owner_id: int | None = None
_telethon_client: TelegramClient | None = None
_targets_file: Path = Path("targets.json")
_menu_state_file: Path = Path("menu_state.json")
_bot_state_file: Path = Path("bot_state.json")


def _save_bot_state() -> None:
    """Persist toggles (auto-buy, notifications) so they survive restarts."""
    try:
        _bot_state_file.write_text(
            json.dumps(
                {
                    "auto_buy": is_auto_buy(),
                    "notifications": _notifications_enabled,
                }
            )
        )
    except Exception:
        logger.exception("Failed to save bot state")


def _load_bot_state() -> None:
    """Restore toggles from disk; defaults stay if file is missing/corrupt."""
    global _notifications_enabled
    if not _bot_state_file.exists():
        return
    try:
        data = json.loads(_bot_state_file.read_text())
    except Exception:
        logger.exception("Failed to load bot state")
        return
    if "auto_buy" in data:
        set_auto_buy(bool(data["auto_buy"]))
    if "notifications" in data:
        _notifications_enabled = bool(data["notifications"])


def _load_last_menu_msg() -> int | None:
    """Load the last menu message ID from disk."""
    if not _menu_state_file.exists():
        return None
    try:
        data = json.loads(_menu_state_file.read_text())
        return data.get("last_menu_msg")
    except Exception:
        return None


def _save_last_menu_msg(msg_id: int) -> None:
    """Persist the last menu message ID."""
    try:
        _menu_state_file.write_text(json.dumps({"last_menu_msg": msg_id}))
    except Exception:
        pass


def _save_targets() -> None:
    """Persist active targets to disk."""
    try:
        _targets_file.write_text(json.dumps(_active_targets, ensure_ascii=False, indent=2))
    except Exception:
        logger.exception("Failed to save targets")


def _load_targets() -> list[dict]:
    """Load persisted targets from disk."""
    if not _targets_file.exists():
        return []
    try:
        return json.loads(_targets_file.read_text())
    except Exception:
        logger.exception("Failed to load targets")
        return []


def get_active_targets() -> list[dict]:
    return [t for t in _active_targets if not t.get("paused")]


def get_market_targets() -> list[dict]:
    """Return targets that have marketplace monitoring enabled."""
    return [
        t
        for t in _active_targets
        if not t.get("paused") and t.get("market_max_price") and t.get("market_max_price") > 0
    ]


def notifications_enabled() -> bool:
    return _notifications_enabled


def _check_owner(user_id: int) -> bool:
    """Only allow the owner to use the bot."""
    if _owner_id is None:
        return True
    return user_id == _owner_id


async def _fetch_collections() -> list[dict]:
    """Fetch gift collections from Telegram API via Telethon."""
    if not _telethon_client:
        return []
    result = await _telethon_client(functions.payments.GetStarGiftsRequest(hash=0))
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


_attrs_cache: dict[int, dict[str, list[dict]]] = {}


async def _fetch_attributes(gift_id: int) -> dict[str, list[dict]]:
    """Fetch available models/patterns/backdrops for a gift collection."""
    if gift_id in _attrs_cache:
        return _attrs_cache[gift_id]

    if not _telethon_client:
        return {}
    result = await _telethon_client(
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

    _attrs_cache[gift_id] = attrs

    return attrs


# ─── Handlers ───────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _check_owner(update.effective_user.id):
        return
    for key in list(context.user_data.keys()):
        if key.startswith("_"):
            context.user_data.pop(key, None)

    # Delete previous menu message (from file, survives restarts)
    prev_id = _load_last_menu_msg()
    if prev_id:
        try:
            await update.effective_chat.delete_message(prev_id)
        except Exception:
            pass

    # Delete the /start command message itself
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.effective_chat.send_message(
        "Снайпер-бот. Выбери действие:",
        reply_markup=InlineKeyboardMarkup(_main_menu_kb()),
    )
    _save_last_menu_msg(msg.message_id)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for /start - shows main menu."""
    await cmd_start(update, context)


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    query = update.callback_query
    await query.answer()
    if not _check_owner(query.from_user.id):
        return None

    if query.data == "add_target":
        return await _show_collections(query, context)
    elif query.data == "my_targets":
        context.user_data.pop("_search_targets", None)
        return await _show_my_targets(query, context)
    elif query.data == "search_targets":
        context.user_data["_search_targets"] = True
        await query.edit_message_text(
            "🔍 Введи запрос для поиска (название, модель, фон, паттерн):"
        )
        return None
    elif query.data == "toggle_autobuy":
        return await _toggle_auto_buy(query, context)
    elif query.data == "toggle_notif":
        return await _toggle_notifications(query, context)
    elif query.data == "main_menu":
        await query.edit_message_text(
            "Снайпер-бот. Выбери действие:",
            reply_markup=InlineKeyboardMarkup(_main_menu_kb()),
        )
    return None


def _main_menu_kb() -> list[list[InlineKeyboardButton]]:
    """Build main menu keyboard with current toggle states."""
    auto_buy_label = "🛒 Автопокупка: ON" if is_auto_buy() else "🛒 Автопокупка: OFF"
    notif_label = "🔔 Уведомления: ON" if _notifications_enabled else "🔕 Уведомления: OFF"
    return [
        [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [
            InlineKeyboardButton(auto_buy_label, callback_data="toggle_autobuy"),
            InlineKeyboardButton(notif_label, callback_data="toggle_notif"),
        ],
    ]


async def _toggle_auto_buy(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_auto_buy(not is_auto_buy())
    _save_bot_state()
    status = (
        "ON — бот покупает автоматически (Telegram + MRKT + Portals)"
        if is_auto_buy()
        else "OFF — только уведомления, без покупок"
    )
    await query.edit_message_text(
        f"Автопокупка: {status}",
        reply_markup=InlineKeyboardMarkup(_main_menu_kb()),
    )


async def _toggle_notifications(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _notifications_enabled
    _notifications_enabled = not _notifications_enabled
    _save_bot_state()
    status = (
        "ON — уведомления приходят" if _notifications_enabled else "OFF — уведомления отключены"
    )
    await query.edit_message_text(
        f"Уведомления: {status}",
        reply_markup=InlineKeyboardMarkup(_main_menu_kb()),
    )


_COLLECTIONS_PAGE_SIZE = 20


async def _show_collections(query, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> int:
    if page == 0:
        await query.edit_message_text("Загружаю коллекции...")
    collections = await _fetch_collections()

    if not collections:
        await query.edit_message_text("Нет коллекций на ресейле.")
        return ConversationHandler.END

    context.user_data["_collections"] = collections
    context.user_data["_all_collections"] = collections
    return await _show_collections_page(query, context, page)


async def _show_collections_page(query, context, page: int) -> int:
    collections = context.user_data.get("_collections", [])
    total = len(collections)
    start = page * _COLLECTIONS_PAGE_SIZE
    end = start + _COLLECTIONS_PAGE_SIZE
    page_items = collections[start:end]

    buttons = []
    for c in page_items:
        label = f"{c['title']} ({c['resale_count']} шт)"
        data = json.dumps({"a": "col", "id": c["id"], "t": c["title"]})
        if len(data) <= 64:
            buttons.append([InlineKeyboardButton(label, callback_data=data)])
        else:
            short = json.dumps({"a": "col", "id": c["id"]})
            context.user_data[f"title_{c['id']}"] = c["title"]
            buttons.append([InlineKeyboardButton(label, callback_data=short)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f'{{"a":"pg","p":{page - 1}}}'))
    if end < total:
        nav.append(InlineKeyboardButton("Далее »", callback_data=f'{{"a":"pg","p":{page + 1}}}'))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"col"}')])
    buttons.append([InlineKeyboardButton("« Меню", callback_data="main_menu")])

    total_pages = (total + _COLLECTIONS_PAGE_SIZE - 1) // _COLLECTIONS_PAGE_SIZE
    await query.edit_message_text(
        f"Выбери коллекцию ({page + 1}/{total_pages}):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PICK_COLLECTION


async def cb_pick_collection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "main_menu":
        await cb_main_menu(update, context)
        return ConversationHandler.END

    data = json.loads(query.data)

    if data.get("a") == "pg":
        return await _show_collections_page(query, context, data["p"])

    if data.get("a") == "search":
        context.user_data["_search_picker"] = data["t"]
        await query.edit_message_text("🔍 Введи название коллекции для поиска:")
        return SEARCH_PICKER

    gift_id = data["id"]
    title = data.get("t") or context.user_data.get(f"title_{gift_id}", f"gift-{gift_id}")

    context.user_data["gift_id"] = gift_id
    context.user_data["gift_title"] = title
    context.user_data["model"] = None
    context.user_data["pattern"] = None
    context.user_data["backdrop"] = None

    await query.edit_message_text(f"Загружаю атрибуты {title}...")
    attrs = await _fetch_attributes(gift_id)

    models = attrs.get("models", [])
    if models:
        context.user_data["_models"] = models
        search_q = context.user_data.pop("_search_model_q", "")
        buttons = [
            [InlineKeyboardButton("Пропустить (любая модель)", callback_data='{"a":"mod","n":""}')],
        ]
        for m in models:
            if search_q and search_q.lower() not in m["name"].lower():
                continue
            cb_data = json.dumps({"a": "mod", "n": m["name"]})
            buttons.append([InlineKeyboardButton(m["name"], callback_data=cb_data)])
        buttons.append([InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"mod"}')])
        buttons.append([InlineKeyboardButton("« Назад", callback_data="back_col")])
        header = f"Коллекция: {title}\nВыбери модель:"
        if search_q:
            header = f'Коллекция: {title}\n🔍 "{search_q}":'
        await query.edit_message_text(
            header,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_MODEL
    else:
        return await _ask_backdrop(query, context, attrs)


async def cb_pick_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back_col":
        return await _show_collections(query, context)

    data = json.loads(query.data)

    if data.get("a") == "search":
        context.user_data["_search_picker"] = data["t"]
        await query.edit_message_text("🔍 Введи название модели для поиска:")
        return SEARCH_PICKER

    model_name = data["n"] or None
    context.user_data["model"] = model_name

    gift_id = context.user_data["gift_id"]
    attrs = await _fetch_attributes(gift_id)
    return await _ask_backdrop(query, context, attrs)


async def _ask_backdrop(query, context, attrs: dict) -> int:
    backdrops = attrs.get("backdrops", [])
    title = context.user_data["gift_title"]

    if backdrops:
        context.user_data["_backdrops"] = backdrops
        search_q = context.user_data.pop("_search_bd_q", "")
        buttons = [
            [InlineKeyboardButton("Пропустить (любой фон)", callback_data='{"a":"bd","n":""}')],
        ]
        for b in backdrops:
            if search_q and search_q.lower() not in b["name"].lower():
                continue
            cb_data = json.dumps({"a": "bd", "n": b["name"]})
            buttons.append([InlineKeyboardButton(b["name"], callback_data=cb_data)])
        buttons.append([InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"bd"}')])
        model_info = f"\nМодель: {context.user_data['model']}" if context.user_data["model"] else ""
        header = f"Коллекция: {title}{model_info}\nВыбери фон:"
        if search_q:
            header = f'Коллекция: {title}{model_info}\n🔍 "{search_q}":'
        await query.edit_message_text(
            header,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_BACKDROP
    else:
        return await _ask_pattern(query, context, attrs)


async def cb_pick_backdrop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)

    if data.get("a") == "search":
        context.user_data["_search_picker"] = data["t"]
        await query.edit_message_text("🔍 Введи название фона для поиска:")
        return SEARCH_PICKER

    backdrop_name = data["n"] or None
    context.user_data["backdrop"] = backdrop_name

    gift_id = context.user_data["gift_id"]
    attrs = await _fetch_attributes(gift_id)
    return await _ask_pattern(query, context, attrs)


async def _ask_pattern(query, context, attrs: dict) -> int:
    patterns = attrs.get("patterns", [])
    title = context.user_data["gift_title"]

    if patterns:
        context.user_data["_patterns"] = patterns
        search_q = context.user_data.pop("_search_pt_q", "")
        buttons = [
            [InlineKeyboardButton("Пропустить (любой паттерн)", callback_data='{"a":"pt","n":""}')],
        ]
        for p in patterns:
            if search_q and search_q.lower() not in p["name"].lower():
                continue
            cb_data = json.dumps({"a": "pt", "n": p["name"]})
            buttons.append([InlineKeyboardButton(p["name"], callback_data=cb_data)])
        buttons.append([InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"pt"}')])
        model_info = f"\nМодель: {context.user_data['model']}" if context.user_data["model"] else ""
        bd_info = f"\nФон: {context.user_data['backdrop']}" if context.user_data["backdrop"] else ""
        header = f"Коллекция: {title}{model_info}{bd_info}\nВыбери паттерн:"
        if search_q:
            header = f'Коллекция: {title}{model_info}{bd_info}\n🔍 "{search_q}":'
        await query.edit_message_text(
            header,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_PATTERN
    else:
        return await _ask_price(query, context)


async def cb_pick_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)

    if data.get("a") == "search":
        context.user_data["_search_picker"] = data["t"]
        await query.edit_message_text("🔍 Введи название паттерна для поиска:")
        return SEARCH_PICKER

    pattern_name = data["n"] or None
    context.user_data["pattern"] = pattern_name

    return await _ask_price(query, context)


async def _ask_price(query, context) -> int:
    title = context.user_data["gift_title"]
    model = context.user_data.get("model")
    backdrop = context.user_data.get("backdrop")
    pattern = context.user_data.get("pattern")

    summary = f"Коллекция: {title}"
    if model:
        summary += f"\nМодель: {model}"
    if backdrop:
        summary += f"\nФон: {backdrop}"
    if pattern:
        summary += f"\nПаттерн: {pattern}"

    summary += "\n\nВведи максимальную цену (число):"
    await query.edit_message_text(summary)
    return SET_PRICE


async def msg_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        price = int(text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи положительное число:")
        return SET_PRICE

    context.user_data["max_price"] = price

    kb = [
        [
            InlineKeyboardButton("Stars", callback_data='{"a":"pay","m":"stars"}'),
            InlineKeyboardButton("TON", callback_data='{"a":"pay","m":"ton"}'),
        ],
        [InlineKeyboardButton("Оба (Stars + TON)", callback_data='{"a":"pay","m":"both"}')],
    ]
    await update.message.reply_text(
        f"Макс. цена: {price}\nЧем платить?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SET_PAYMENT


async def cb_set_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)
    method = data["m"]

    gift_id = context.user_data["gift_id"]
    gift_title = context.user_data["gift_title"]
    model = context.user_data.get("model")
    backdrop = context.user_data.get("backdrop")
    pattern = context.user_data.get("pattern")
    max_price = context.user_data["max_price"]

    targets_to_add = []
    if method in ("stars", "both"):
        name = gift_title
        if model:
            name += f" {model}"
        name += " (Stars)"
        targets_to_add.append(
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
    if method in ("ton", "both"):
        name = gift_title
        if model:
            name += f" {model}"
        name += " (TON)"
        targets_to_add.append(
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

    has_ton = any(t["pay_with_ton"] for t in targets_to_add)
    for t in targets_to_add:
        t["market_max_price"] = max_price if t["pay_with_ton"] else None
        _active_targets.append(t)
    _save_targets()

    if has_ton:
        markets_label = "Telegram + Tonnel + MRKT + Portals"
    else:
        markets_label = "Только Telegram"

    summary = f"Таргет добавлен!\n\nКоллекция: {gift_title}"
    if model:
        summary += f"\nМодель: {model}"
    if backdrop:
        summary += f"\nФон: {backdrop}"
    if pattern:
        summary += f"\nПаттерн: {pattern}"
    summary += f"\nМакс. цена: {max_price}"
    payment_label = {"stars": "Stars", "ton": "TON", "both": "Stars + TON"}
    summary += f"\nОплата: {payment_label[method]}"
    summary += f"\nМаркеты: {markets_label}"
    summary += f"\nАвтопокупка: {'ON' if is_auto_buy() else 'OFF'}"
    summary += f"\n\nВсего активных таргетов: {len(_active_targets)}"

    kb = [
        [InlineKeyboardButton("Добавить ещё", callback_data="add_target")],
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(summary, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def _show_my_targets(query, context: ContextTypes.DEFAULT_TYPE, search: str = "") -> None:
    if not _active_targets:
        kb = [
            [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
            [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
        ]
        await query.edit_message_text(
            "Нет активных таргетов.", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    filtered: list[tuple[int, dict]] = []
    q = search.lower().strip()
    for i, t in enumerate(_active_targets):
        if q:
            fields = [
                t.get("name", ""),
                t.get("model", "") or "",
                t.get("pattern", "") or "",
                t.get("backdrop", "") or "",
            ]
            if not any(q in f.lower() for f in fields):
                continue
        filtered.append((i, t))

    if not filtered and q:
        kb = [
            [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
            [InlineKeyboardButton("🔍 Поиск", callback_data="search_targets")],
            [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
        ]
        await query.edit_message_text(
            f'Ничего не найдено по запросу "{search}".',
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    buttons = []
    for i, t in filtered:
        pay = "TON" if t["pay_with_ton"] else "Stars"
        paused = " ⏸" if t.get("paused") else ""
        label = f"{i + 1}. {t['name']} — {t['max_price']} {pay}{paused}"
        if t.get("model"):
            label += f" [{t['model']}]"
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f'{{"a":"select","i":{i}}}',
                )
            ]
        )

    title = f"Результаты поиска ({len(filtered)}):" if q else "Активные таргеты:"
    buttons.append([InlineKeyboardButton("🔍 Поиск", callback_data="search_targets")])
    if not q:
        buttons.append([InlineKeyboardButton("Удалить все", callback_data='{"a":"del_all"}')])
    buttons.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    await query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(buttons))


async def _show_my_targets_from_msg(
    update: Update, context: ContextTypes.DEFAULT_TYPE, search: str = ""
) -> None:
    """Show filtered targets via reply_text (for text-based search)."""
    if not _active_targets:
        kb = [
            [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
            [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
        ]
        await update.message.reply_text(
            "Нет активных таргетов.", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    filtered: list[tuple[int, dict]] = []
    q = search.lower().strip()
    for i, t in enumerate(_active_targets):
        if q:
            fields = [
                t.get("name", ""),
                t.get("model", "") or "",
                t.get("pattern", "") or "",
                t.get("backdrop", "") or "",
            ]
            if not any(q in f.lower() for f in fields):
                continue
        filtered.append((i, t))

    if not filtered:
        kb = [
            [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
            [InlineKeyboardButton("🔍 Поиск", callback_data="search_targets")],
            [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
        ]
        await update.message.reply_text(
            f'Ничего не найдено по запросу "{search}".',
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    buttons = []
    for i, t in filtered:
        pay = "TON" if t["pay_with_ton"] else "Stars"
        paused = " ⏸" if t.get("paused") else ""
        label = f"{i + 1}. {t['name']} — {t['max_price']} {pay}{paused}"
        if t.get("model"):
            label += f" [{t['model']}]"
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f'{{"a":"select","i":{i}}}',
                )
            ]
        )

    title = f'🔍 Результаты "{search}" ({len(filtered)}):'
    buttons.append([InlineKeyboardButton("🔍 Новый поиск", callback_data="search_targets")])
    buttons.append([InlineKeyboardButton("Все таргеты", callback_data="my_targets")])
    buttons.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(buttons))


async def cb_target_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)
    action = data["a"]
    menu_kb = _main_menu_kb()

    if action == "del_all":
        _active_targets.clear()
        _save_targets()
        await query.edit_message_text(
            "Все таргеты удалены.", reply_markup=InlineKeyboardMarkup(menu_kb)
        )
        return

    idx = data.get("i", -1)
    if idx < 0 or idx >= len(_active_targets):
        return

    if action == "del":
        removed = _active_targets.pop(idx)
        _save_targets()
        await query.edit_message_text(
            f"Удалён: {removed['name']}", reply_markup=InlineKeyboardMarkup(menu_kb)
        )
        return

    if action == "pause":
        t = _active_targets[idx]
        t["paused"] = not t.get("paused", False)
        _save_targets()
        state = "⏸ Пауза" if t["paused"] else "▶️ Активен"
        await query.edit_message_text(
            f"{t['name']}: {state}", reply_markup=InlineKeyboardMarkup(menu_kb)
        )
        return

    if action == "select":
        t = _active_targets[idx]
        pay = "TON" if t["pay_with_ton"] else "Stars"
        paused_label = " (на паузе)" if t.get("paused") else ""
        text = f"{t['name']}{paused_label}\nМакс. цена: {t['max_price']} {pay}\n"
        if t.get("model"):
            text += f"Модель: {t['model']}\n"
        if t.get("backdrop"):
            text += f"Фон: {t['backdrop']}\n"
        if t.get("pattern"):
            text += f"Паттерн: {t['pattern']}\n"
        text += "\nВыбери действие:"
        kb = [
            [
                InlineKeyboardButton("✏️ Изменить цену", callback_data=f'{{"a":"edit","i":{idx}}}'),
            ],
            [
                InlineKeyboardButton(
                    "▶️ Возобновить" if t.get("paused") else "⏸ Пауза",
                    callback_data=f'{{"a":"pause","i":{idx}}}',
                ),
            ],
            [
                InlineKeyboardButton("🗑 Удалить", callback_data=f'{{"a":"del","i":{idx}}}'),
            ],
            [InlineKeyboardButton("« Назад", callback_data="my_targets")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return

    if action == "edit":
        t = _active_targets[idx]
        context.user_data["_edit_idx"] = idx
        pay = "TON" if t["pay_with_ton"] else "Stars"
        text = (
            f"Редактирование: {t['name']}\n"
            f"Текущая макс. цена: {t['max_price']} {pay}\n\n"
            f"Введи новую макс. цену:"
        )
        await query.edit_message_text(text)
        return


async def msg_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input: search query or price editing."""
    if context.user_data.get("_search_targets"):
        context.user_data.pop("_search_targets", None)
        query_text = update.message.text.strip()
        await _show_my_targets_from_msg(update, context, query_text)
        return

    idx = context.user_data.get("_edit_idx")
    if idx is None or idx < 0 or idx >= len(_active_targets):
        await update.message.reply_text("Ошибка. Попробуй снова через /start")
        return

    text = update.message.text.strip()
    try:
        new_price = int(text) if text.isdigit() else float(text)
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи положительное число:")
        return

    t = _active_targets[idx]
    old_price = t["max_price"]
    t["max_price"] = new_price
    if t.get("pay_with_ton"):
        t["market_max_price"] = new_price
    _save_targets()
    context.user_data.pop("_edit_idx", None)

    pay = "TON" if t["pay_with_ton"] else "Stars"
    kb = [
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
    ]
    await update.message.reply_text(
        f"Цена обновлена: {t['name']}\n{old_price} → {new_price} {pay}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cb_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle navigation callbacks within conversation."""
    query = update.callback_query
    await query.answer()

    if query.data == "main_menu":
        await cb_main_menu(update, context)
        return ConversationHandler.END
    if query.data == "add_target":
        return await _show_collections(query, context)
    if query.data == "my_targets":
        await _show_my_targets(query, context)
        return ConversationHandler.END

    return ConversationHandler.END


async def msg_search_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle search text input during target creation pickers."""
    picker = context.user_data.pop("_search_picker", None)
    q = update.message.text.strip()
    gift_id = context.user_data.get("gift_id")

    if picker == "col":
        collections = context.user_data.get("_all_collections") or context.user_data.get(
            "_collections", []
        )
        filtered = [c for c in collections if q.lower() in c["title"].lower()]
        if not filtered:
            kb = [
                [InlineKeyboardButton("Все коллекции", callback_data="add_target")],
                [InlineKeyboardButton("« Меню", callback_data="main_menu")],
            ]
            await update.message.reply_text(
                f'Ничего не найдено по "{q}".',
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return PICK_COLLECTION
        context.user_data["_collections"] = filtered
        buttons = []
        for c in filtered:
            label = f"{c['title']} ({c['resale_count']} шт)"
            data = json.dumps({"a": "col", "id": c["id"], "t": c["title"]})
            if len(data) <= 64:
                buttons.append([InlineKeyboardButton(label, callback_data=data)])
            else:
                short = json.dumps({"a": "col", "id": c["id"]})
                context.user_data[f"title_{c['id']}"] = c["title"]
                buttons.append([InlineKeyboardButton(label, callback_data=short)])
        buttons.append([InlineKeyboardButton("Все коллекции", callback_data="add_target")])
        buttons.append([InlineKeyboardButton("« Меню", callback_data="main_menu")])
        await update.message.reply_text(
            f'🔍 "{q}" ({len(filtered)}):',
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_COLLECTION

    if picker == "mod" and gift_id:
        attrs = await _fetch_attributes(gift_id)
        title = context.user_data.get("gift_title", "")
        models = attrs.get("models", [])
        filtered = [m for m in models if q.lower() in m["name"].lower()]
        if not filtered:
            kb = [
                [InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"mod"}')],
                [InlineKeyboardButton("« Назад", callback_data="back_col")],
            ]
            await update.message.reply_text(
                f'Модель не найдена по "{q}".',
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return PICK_MODEL
        buttons = [
            [
                InlineKeyboardButton(
                    "Пропустить (любая модель)",
                    callback_data='{"a":"mod","n":""}',
                )
            ],
        ]
        for m in filtered:
            cb_data = json.dumps({"a": "mod", "n": m["name"]})
            buttons.append([InlineKeyboardButton(m["name"], callback_data=cb_data)])
        buttons.append(
            [
                InlineKeyboardButton(
                    "🔍 Поиск",
                    callback_data='{"a":"search","t":"mod"}',
                )
            ]
        )
        buttons.append([InlineKeyboardButton("« Назад", callback_data="back_col")])
        await update.message.reply_text(
            f'Коллекция: {title}\n🔍 "{q}":',
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_MODEL

    if picker == "bd" and gift_id:
        attrs = await _fetch_attributes(gift_id)
        title = context.user_data.get("gift_title", "")
        backdrops = attrs.get("backdrops", [])
        filtered = [b for b in backdrops if q.lower() in b["name"].lower()]
        if not filtered:
            kb = [
                [InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"bd"}')],
                [InlineKeyboardButton("« Меню", callback_data="main_menu")],
            ]
            await update.message.reply_text(
                f'Фон не найден по "{q}".',
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return PICK_BACKDROP
        buttons = [
            [
                InlineKeyboardButton(
                    "Пропустить (любой фон)",
                    callback_data='{"a":"bd","n":""}',
                )
            ],
        ]
        for b in filtered:
            cb_data = json.dumps({"a": "bd", "n": b["name"]})
            buttons.append([InlineKeyboardButton(b["name"], callback_data=cb_data)])
        buttons.append(
            [
                InlineKeyboardButton(
                    "🔍 Поиск",
                    callback_data='{"a":"search","t":"bd"}',
                )
            ]
        )
        await update.message.reply_text(
            f'Коллекция: {title}\n🔍 "{q}":',
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_BACKDROP

    if picker == "pt" and gift_id:
        attrs = await _fetch_attributes(gift_id)
        title = context.user_data.get("gift_title", "")
        patterns = attrs.get("patterns", [])
        filtered = [p for p in patterns if q.lower() in p["name"].lower()]
        if not filtered:
            kb = [
                [InlineKeyboardButton("🔍 Поиск", callback_data='{"a":"search","t":"pt"}')],
                [InlineKeyboardButton("« Меню", callback_data="main_menu")],
            ]
            await update.message.reply_text(
                f'Паттерн не найден по "{q}".',
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return PICK_PATTERN
        buttons = [
            [
                InlineKeyboardButton(
                    "Пропустить (любой паттерн)",
                    callback_data='{"a":"pt","n":""}',
                )
            ],
        ]
        for p in filtered:
            cb_data = json.dumps({"a": "pt", "n": p["name"]})
            buttons.append([InlineKeyboardButton(p["name"], callback_data=cb_data)])
        buttons.append(
            [
                InlineKeyboardButton(
                    "🔍 Поиск",
                    callback_data='{"a":"search","t":"pt"}',
                )
            ]
        )
        await update.message.reply_text(
            f'Коллекция: {title}\n🔍 "{q}":',
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_PATTERN

    return ConversationHandler.END


def build_application(bot_token: str, owner_id: int | None = None) -> Application:
    """Build the telegram bot Application."""
    global _owner_id
    _owner_id = owner_id

    app = Application.builder().token(bot_token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_main_menu,
                pattern=r"^(add_target|my_targets|search_targets|toggle_autobuy|toggle_notif|main_menu)$",
            ),
        ],
        states={
            PICK_COLLECTION: [
                CallbackQueryHandler(cb_pick_collection, pattern=r"^\{"),
                CallbackQueryHandler(cb_nav, pattern=r"^(main_menu|add_target|my_targets)$"),
            ],
            PICK_MODEL: [
                CallbackQueryHandler(cb_pick_model, pattern=r"^\{"),
                CallbackQueryHandler(cb_nav, pattern=r"^(main_menu|back_col)$"),
            ],
            PICK_BACKDROP: [
                CallbackQueryHandler(cb_pick_backdrop, pattern=r"^\{"),
                CallbackQueryHandler(cb_nav, pattern=r"^(main_menu)$"),
            ],
            PICK_PATTERN: [
                CallbackQueryHandler(cb_pick_pattern, pattern=r"^\{"),
                CallbackQueryHandler(cb_nav, pattern=r"^(main_menu)$"),
            ],
            SET_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_set_price),
            ],
            SET_PAYMENT: [
                CallbackQueryHandler(cb_set_payment, pattern=r'^\{.*"a":"pay"'),
                CallbackQueryHandler(cb_nav, pattern=r"^(main_menu)$"),
            ],
            SEARCH_PICKER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_search_picker),
                CallbackQueryHandler(cb_nav, pattern=r"^(main_menu|add_target|back_col)$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("menu", cmd_menu),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv_handler)
    app.add_handler(
        CallbackQueryHandler(cb_target_action, pattern=r'^\{.*"a":"(del|pause|edit|select)')
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text_input))
    app.add_handler(
        CallbackQueryHandler(
            cb_main_menu,
            pattern=r"^(my_targets|search_targets|toggle_autobuy|toggle_notif|main_menu)$",
        )
    )

    return app


def create_notifier(bot_token: str, chat_id: int):
    """Create an async notification function that sends via Bot API.

    Notifications are throttled to at most 1 message per second per chat to
    stay within Telegram's bot anti-spam limits. Without this, a burst of
    new-listing messages can trip Telegram's automatic bot deletion.
    """
    from telegram import Bot

    bot = Bot(token=bot_token)
    lock = asyncio.Lock()
    state = {"last_sent": 0.0}
    _MIN_INTERVAL = 1.1  # seconds between messages to the same chat

    async def notify(text: str) -> None:
        if not _notifications_enabled:
            return
        async with lock:
            now = asyncio.get_event_loop().time()
            wait = _MIN_INTERVAL - (now - state["last_sent"])
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            finally:
                state["last_sent"] = asyncio.get_event_loop().time()

    return notify


def set_telethon_client(client: TelegramClient, cfg: object | None = None) -> None:
    global _telethon_client
    _telethon_client = client

    _load_bot_state()

    saved = _load_targets()
    if saved:
        for t in saved:
            if t.get("pay_with_ton") and not t.get("market_max_price"):
                t["market_max_price"] = t["max_price"]
        _active_targets.extend(saved)
        logger.info("Loaded %d targets from %s", len(saved), _targets_file)

    if cfg is not None:
        cfg_ids = {
            (t["gift_id"], t.get("pay_with_ton", False), t.get("model")) for t in _active_targets
        }
        for t in cfg.targets:
            key = (t.gift_id, t.pay_with_ton, t.model)
            if key not in cfg_ids:
                _active_targets.append(
                    {
                        "gift_id": t.gift_id,
                        "max_price": t.max_price,
                        "name": t.name,
                        "pay_with_ton": t.pay_with_ton,
                        "model": t.model,
                        "pattern": t.pattern,
                        "backdrop": t.backdrop,
                        "market_max_price": t.max_price if t.pay_with_ton else None,
                    }
                )

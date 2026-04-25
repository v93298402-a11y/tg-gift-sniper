"""Telegram bot interface for managing sniper targets via inline keyboards."""

from __future__ import annotations

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

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Conversation states
PICK_COLLECTION, PICK_MODEL, PICK_BACKDROP, PICK_PATTERN, SET_PRICE, SET_PAYMENT = range(6)

# Runtime state shared with sniper engine
_active_targets: list[dict] = []
_dry_run: bool = True
_owner_id: int | None = None
_telethon_client: TelegramClient | None = None
_targets_file: Path = Path("targets.json")


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


def is_dry_run() -> bool:
    return _dry_run


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


async def _fetch_attributes(gift_id: int) -> dict[str, list[dict]]:
    """Fetch available models/patterns/backdrops for a gift collection."""
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

    return attrs


# ─── Handlers ───────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _check_owner(update.effective_user.id):
        return
    kb = [
        [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [
            InlineKeyboardButton(
                f"Dry-run: {'ON' if _dry_run else 'OFF'}", callback_data="toggle_dry"
            )
        ],
    ]
    await update.message.reply_text(
        "Снайпер-бот. Выбери действие:", reply_markup=InlineKeyboardMarkup(kb)
    )


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
        return await _show_my_targets(query, context)
    elif query.data == "toggle_dry":
        return await _toggle_dry_run(query, context)
    elif query.data == "main_menu":
        kb = [
            [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
            [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
            [
                InlineKeyboardButton(
                    f"Dry-run: {'ON' if _dry_run else 'OFF'}", callback_data="toggle_dry"
                )
            ],
        ]
        await query.edit_message_text(
            "Снайпер-бот. Выбери действие:", reply_markup=InlineKeyboardMarkup(kb)
        )
    return None


async def _toggle_dry_run(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _dry_run
    _dry_run = not _dry_run
    kb = [
        [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [
            InlineKeyboardButton(
                f"Dry-run: {'ON' if _dry_run else 'OFF'}", callback_data="toggle_dry"
            )
        ],
    ]
    status = "включен (покупки НЕ делаются)" if _dry_run else "ВЫКЛЮЧЕН (покупки АКТИВНЫ)"
    await query.edit_message_text(f"Dry-run {status}", reply_markup=InlineKeyboardMarkup(kb))


_COLLECTIONS_PAGE_SIZE = 20


async def _show_collections(query, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> int:
    if page == 0:
        await query.edit_message_text("Загружаю коллекции...")
    collections = await _fetch_collections()

    if not collections:
        await query.edit_message_text("Нет коллекций на ресейле.")
        return ConversationHandler.END

    context.user_data["_collections"] = collections
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
        buttons = [
            [InlineKeyboardButton("Пропустить (любая модель)", callback_data='{"a":"mod","n":""}')],
        ]
        for m in models:
            cb_data = json.dumps({"a": "mod", "n": m["name"]})
            buttons.append([InlineKeyboardButton(m["name"], callback_data=cb_data)])
        buttons.append([InlineKeyboardButton("« Назад", callback_data="back_col")])
        await query.edit_message_text(
            f"Коллекция: {title}\nВыбери модель:",
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
    model_name = data["n"] or None
    context.user_data["model"] = model_name

    gift_id = context.user_data["gift_id"]
    attrs = await _fetch_attributes(gift_id)
    return await _ask_backdrop(query, context, attrs)


async def _ask_backdrop(query, context, attrs: dict) -> int:
    backdrops = attrs.get("backdrops", [])
    title = context.user_data["gift_title"]

    if backdrops:
        buttons = [
            [InlineKeyboardButton("Пропустить (любой фон)", callback_data='{"a":"bd","n":""}')],
        ]
        for b in backdrops:
            cb_data = json.dumps({"a": "bd", "n": b["name"]})
            buttons.append([InlineKeyboardButton(b["name"], callback_data=cb_data)])
        model_info = f"\nМодель: {context.user_data['model']}" if context.user_data["model"] else ""
        await query.edit_message_text(
            f"Коллекция: {title}{model_info}\nВыбери фон:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_BACKDROP
    else:
        return await _ask_pattern(query, context, attrs)


async def cb_pick_backdrop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)
    backdrop_name = data["n"] or None
    context.user_data["backdrop"] = backdrop_name

    gift_id = context.user_data["gift_id"]
    attrs = await _fetch_attributes(gift_id)
    return await _ask_pattern(query, context, attrs)


async def _ask_pattern(query, context, attrs: dict) -> int:
    patterns = attrs.get("patterns", [])
    title = context.user_data["gift_title"]

    if patterns:
        buttons = [
            [InlineKeyboardButton("Пропустить (любой паттерн)", callback_data='{"a":"pt","n":""}')],
        ]
        for p in patterns:
            cb_data = json.dumps({"a": "pt", "n": p["name"]})
            buttons.append([InlineKeyboardButton(p["name"], callback_data=cb_data)])
        model_info = f"\nМодель: {context.user_data['model']}" if context.user_data["model"] else ""
        bd_info = f"\nФон: {context.user_data['backdrop']}" if context.user_data["backdrop"] else ""
        await query.edit_message_text(
            f"Коллекция: {title}{model_info}{bd_info}\nВыбери паттерн:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return PICK_PATTERN
    else:
        return await _ask_price(query, context)


async def cb_pick_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)
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
    summary += f"\nDry-run: {'ON' if _dry_run else 'OFF'}"
    summary += f"\n\nВсего активных таргетов: {len(_active_targets)}"

    kb = [
        [InlineKeyboardButton("Добавить ещё", callback_data="add_target")],
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(summary, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def _show_my_targets(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _active_targets:
        kb = [
            [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
            [InlineKeyboardButton("« Главное меню", callback_data="main_menu")],
        ]
        await query.edit_message_text(
            "Нет активных таргетов.", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    text = "Активные таргеты:\n\n"
    num_buttons = []
    for i, t in enumerate(_active_targets):
        pay = "TON" if t["pay_with_ton"] else "Stars"
        paused = " ⏸" if t.get("paused") else ""
        n = i + 1
        line = f"{n}. {t['name']} — {t['max_price']} {pay}{paused}"
        if t.get("model"):
            line += f" [{t['model']}]"
        text += line + "\n"
        num_buttons.append(InlineKeyboardButton(str(n), callback_data=f'{{"a":"select","i":{i}}}'))

    text += "\nНажми номер для действий:"
    buttons = []
    for row_start in range(0, len(num_buttons), 5):
        buttons.append(num_buttons[row_start : row_start + 5])
    buttons.append([InlineKeyboardButton("Удалить все", callback_data='{"a":"del_all"}')])
    buttons.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def cb_target_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = json.loads(query.data)
    action = data["a"]
    menu_kb = [
        [InlineKeyboardButton("Добавить таргет", callback_data="add_target")],
        [InlineKeyboardButton("Мои таргеты", callback_data="my_targets")],
        [
            InlineKeyboardButton(
                f"Dry-run: {'ON' if _dry_run else 'OFF'}", callback_data="toggle_dry"
            )
        ],
    ]

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


async def msg_edit_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new price input for target editing."""
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


def build_application(bot_token: str, owner_id: int | None = None) -> Application:
    """Build the telegram bot Application."""
    global _owner_id
    _owner_id = owner_id

    app = Application.builder().token(bot_token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_main_menu,
                pattern=r"^(add_target|my_targets|toggle_dry|main_menu)$",
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_edit_price))
    app.add_handler(
        CallbackQueryHandler(cb_main_menu, pattern=r"^(my_targets|toggle_dry|main_menu)$")
    )

    return app


def create_notifier(bot_token: str, chat_id: int):
    """Create an async notification function that sends via Bot API."""
    from telegram import Bot

    bot = Bot(token=bot_token)

    async def notify(text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text)

    return notify


def set_telethon_client(client: TelegramClient, cfg: object | None = None) -> None:
    global _telethon_client, _dry_run
    _telethon_client = client

    saved = _load_targets()
    if saved:
        _active_targets.extend(saved)
        logger.info("Loaded %d targets from %s", len(saved), _targets_file)

    if cfg is not None:
        _dry_run = cfg.dry_run
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
                    }
                )

import logging
import re
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
from database import get_state, set_state, clear_state, get_user_settings
from handlers.start import main_menu_keyboard


def _parse_amount(text: str) -> float:
    """Parse amount from user input, stripping currency symbols and spaces."""
    cleaned = re.sub(r"[^\d.,]", "", text.replace(" ", ""))
    if not cleaned:
        raise ValueError(f"No numeric content in: {text!r}")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        parts = cleaned.rsplit(".", 1)
        cleaned = parts[0].replace(".", "") + "." + parts[1]
    return float(cleaned)

logger = logging.getLogger(__name__)

EXPENSE_CATEGORIES = [
    ("🔧 Ремонт/обслуживание", "repair"),
    ("💡 Коммунальные платежи", "utilities"),
    ("🏛 Налоги", "tax"),
    ("🛡 Страховка", "insurance"),
    ("📋 Управление", "management"),
    ("📢 Реклама", "advertising"),
    ("📦 Прочее", "other"),
]


def _category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for label, val in EXPENSE_CATEGORIES:
        rows.append([InlineKeyboardButton(label, callback_data=f"exp_cat_{val}")])
    return InlineKeyboardMarkup(rows)


def _objects_keyboard() -> InlineKeyboardMarkup:
    objects = sheets.get_objects()
    rows = []
    for obj in objects:
        rows.append([InlineKeyboardButton(
            f"🏠 {obj.get('name')}",
            callback_data=f"exp_obj_{obj.get('id')}",
        )])
    rows.append([InlineKeyboardButton("🏢 Общие расходы (без объекта)", callback_data="exp_obj_general")])
    return InlineKeyboardMarkup(rows)


async def record_expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    clear_state(user_id)
    set_state(user_id, "record_expense_select_obj", {})
    await msg.reply_text(
        "🔧 *Запись расхода*\n\nВыберите объект:",
        parse_mode="Markdown",
        reply_markup=_objects_keyboard(),
    )


async def expense_obj_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    obj_id = query.data.replace("exp_obj_", "")

    if obj_id == "general":
        obj_name = "Общие"
    else:
        objects = sheets.get_objects()
        obj = next((o for o in objects if str(o.get("id")) == str(obj_id)), None)
        obj_name = obj.get("name", "?") if obj else "?"

    set_state(user_id, "record_expense_category", {
        "object_id": obj_id,
        "object_name": obj_name,
    })
    await query.edit_message_text(
        f"🔧 Расход по *{obj_name}*\n\nВыберите категорию:",
        parse_mode="Markdown",
        reply_markup=_category_keyboard(),
    )


async def expense_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    category = query.data.replace("exp_cat_", "")
    _, data = get_state(user_id)
    data["category"] = category
    set_state(user_id, "record_expense_amount", data)

    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    data["symbol"] = sym

    await query.edit_message_text(
        f"Категория: *{category}*\n\nВведите *сумму* ({sym}):",
        parse_mode="Markdown",
    )


async def expense_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "record_expense_amount":
        return

    try:
        amount = _parse_amount(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите сумму числом, например: 150 или 150.50")
        return

    data["amount"] = amount
    set_state(user_id, "record_expense_description", data)
    await update.message.reply_text(
        "📝 Добавьте краткое описание (или нажмите Пропустить):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Пропустить", callback_data="exp_desc_skip")]
        ]),
    )


async def expense_desc_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["description"] = ""
    await _save_expense(query.message, user_id, data)


async def expense_description_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "record_expense_description":
        return
    data["description"] = update.message.text.strip()
    await _save_expense(update.message, user_id, data)


async def _save_expense(msg, user_id: int, data: dict) -> None:
    data["date"] = date.today().strftime("%d.%m.%Y")
    ok = sheets.record_expense(data)
    clear_state(user_id)
    sym = data.get("symbol", "$")
    status_text = "✅ Расход записан!" if ok else "⚠️ Сохранено локально (синхронизируется)."
    await msg.reply_text(
        f"{status_text}\n\n"
        f"🏠 {data.get('object_name')}\n"
        f"📂 {data.get('category')}\n"
        f"💰 {sym}{data.get('amount'):.2f}\n"
        f"📝 {data.get('description') or '—'}\n"
        f"📅 {data.get('date')}\n\n"
        f"🔗 [Открыть в таблице]({sheets.spreadsheet_url()})",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

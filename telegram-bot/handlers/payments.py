import logging
import re
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
from database import get_state, set_state, clear_state, get_user_settings
from handlers.start import main_menu_keyboard


def _parse_amount(text: str) -> float:
    """Parse amount from user input, stripping currency symbols and spaces.
    Accepts: '300', '300.50', '300,50', '$300', '300$', '300 USD', '1 000' etc.
    """
    cleaned = re.sub(r"[^\d.,]", "", text.replace(" ", ""))
    if not cleaned:
        raise ValueError(f"No numeric content in: {text!r}")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        parts = cleaned.rsplit(".", 1)
        cleaned = parts[0].replace(".", "") + "." + parts[1]
    return float(cleaned)

logger = logging.getLogger(__name__)


def _objects_keyboard(prefix: str) -> InlineKeyboardMarkup:
    objects = sheets.get_active_objects()
    if not objects:
        objects = sheets.get_objects()
    rows = []
    for obj in objects:
        rows.append([InlineKeyboardButton(
            f"🏠 {obj.get('name')} ({obj.get('tenant_name', '?')})",
            callback_data=f"{prefix}{obj.get('id')}",
        )])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


async def record_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    objects = sheets.get_active_objects() or sheets.get_objects()
    if not objects:
        await msg.reply_text(
            "Объекты не найдены. Сначала добавьте объект!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить объект", callback_data="menu_add_object")]
            ]),
        )
        return

    clear_state(user_id)
    set_state(user_id, "record_payment_select_obj", {})
    await msg.reply_text(
        "💰 *Запись платежа*\n\nВыберите объект:",
        parse_mode="Markdown",
        reply_markup=_objects_keyboard("pay_obj_"),
    )


async def payment_obj_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    obj_id = query.data.replace("pay_obj_", "")

    objects = sheets.get_objects()
    obj = next((o for o in objects if str(o.get("id")) == str(obj_id)), None)
    if not obj:
        await query.edit_message_text("Объект не найден.")
        return

    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")

    set_state(user_id, "record_payment_amount", {
        "object_id": obj_id,
        "object_name": obj.get("name"),
        "expected_amount": obj.get("rent_amount"),
        "symbol": sym,
    })

    await query.edit_message_text(
        f"💰 *Платёж за {obj.get('name')}*\n\n"
        f"Ожидается: {sym}{obj.get('rent_amount')}\n"
        f"Арендатор: {obj.get('tenant_name')}\n\n"
        "Введите *полученную сумму*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ Полная сумма ({sym}{obj.get('rent_amount')})",
                callback_data=f"pay_full_{obj_id}",
            )]
        ]),
    )


async def payment_full_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    obj_id = query.data.replace("pay_full_", "")
    _, data = get_state(user_id)
    data["received_amount"] = data.get("expected_amount")
    set_state(user_id, "record_payment_note", data)

    await query.edit_message_text(
        "✅ Полная сумма записана.\n\nДобавьте примечание (или нажмите Пропустить):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Пропустить", callback_data="pay_note_skip")]
        ]),
    )


async def payment_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "record_payment_amount":
        return

    try:
        amount = _parse_amount(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите сумму числом, например: 300 или 300.50")
        return

    data["received_amount"] = amount
    expected = float(data.get("expected_amount", 0))
    diff = amount - expected
    sym = data.get("symbol", "$")

    diff_text = ""
    if diff < 0:
        diff_text = f"\n⚠️ Недоплата: {sym}{abs(diff):.2f}"
    elif diff > 0:
        diff_text = f"\n✅ Переплата: {sym}{diff:.2f}"

    set_state(user_id, "record_payment_note", data)
    await update.message.reply_text(
        f"Получено: {sym}{amount:.2f} (ожидалось {sym}{expected:.2f}){diff_text}\n\n"
        "Добавьте примечание (или нажмите Пропустить):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Пропустить", callback_data="pay_note_skip")]
        ]),
    )


async def payment_note_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["note"] = ""
    await _save_payment(query.message, user_id, data, edit=False)


async def payment_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "record_payment_note":
        return
    data["note"] = update.message.text.strip()
    await _save_payment(update.message, user_id, data, edit=False)


async def _save_payment(msg, user_id: int, data: dict, edit: bool = False) -> None:
    data["date"] = date.today().strftime("%d.%m.%Y")
    ok = sheets.record_payment(data)
    clear_state(user_id)
    sym = data.get("symbol", "$")
    status_text = "Амирхон ака, платёж записан! ✅" if ok else "Амирхон ака, сохранено локально ⚠️"
    text = (
        f"{status_text}\n\n"
        f"🏠 {data.get('object_name')}\n"
        f"💰 Получено: {sym}{data.get('received_amount')}\n"
        f"📅 {data.get('date')}\n"
        f"📝 {data.get('note') or '—'}\n\n"
        f"🔗 [Открыть в таблице]({sheets.spreadsheet_url()})"
    )
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())


async def confirm_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.message.text.split("_", 1)
    if len(parts) < 2:
        return
    obj_id = parts[1]
    objects = sheets.get_objects()
    obj = next((o for o in objects if str(o.get("id")) == str(obj_id)), None)
    if not obj:
        await update.message.reply_text("Объект не найден.")
        return

    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    data = {
        "object_id": obj_id,
        "object_name": obj.get("name"),
        "expected_amount": obj.get("rent_amount"),
        "received_amount": obj.get("rent_amount"),
        "note": "Подтверждено через бот",
        "symbol": sym,
    }
    await _save_payment(update.message, user_id, data)


async def missed_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.message.text.split("_", 1)
    if len(parts) < 2:
        return
    obj_id = parts[1]
    objects = sheets.get_objects()
    obj = next((o for o in objects if str(o.get("id")) == str(obj_id)), None)
    if not obj:
        await update.message.reply_text("Объект не найден.")
        return

    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    data = {
        "object_id": obj_id,
        "object_name": obj.get("name"),
        "expected_amount": obj.get("rent_amount"),
        "received_amount": 0,
        "note": "Пропущено — отмечено через бот",
        "symbol": sym,
    }
    await _save_payment(update.message, user_id, data)

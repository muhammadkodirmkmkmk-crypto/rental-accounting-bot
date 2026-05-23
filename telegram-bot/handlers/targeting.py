import asyncio
import logging
import re
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
from database import get_state, set_state, clear_state, get_user_settings
from handlers.start import targeting_menu_keyboard

logger = logging.getLogger(__name__)

TARGET_EXPENSE_CATEGORIES = [
    ("📢 Реклама", "advertising"),
    ("🛠 Сервисы/инструменты", "tools"),
    ("👤 Зарплата фрилансера", "freelancer"),
    ("📋 Управление", "management"),
    ("📦 Прочее", "other"),
]


def _parse_amount(text: str) -> float:
    cleaned = re.sub(r"[^\d.,]", "", text.replace(" ", ""))
    if not cleaned:
        raise ValueError(f"No numeric content in: {text!r}")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        parts = cleaned.rsplit(".", 1)
        cleaned = parts[0].replace(".", "") + "." + parts[1]
    return float(cleaned)


async def targeting_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    await msg.reply_text("🎯 *Таргет проекты*\n\nВыберите действие:", parse_mode="Markdown",
                         reply_markup=targeting_menu_keyboard())


# ── Clients ──────────────────────────────────────────────────

async def add_client_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    clear_state(user_id)
    set_state(user_id, "tgt_add_client_name", {})
    await msg.reply_text("🎯 Добавляем клиента!\n\n👤 *Имя клиента* (например: ООО Ромашка):",
                         parse_mode="Markdown")


async def clients_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    clients = await asyncio.to_thread(sheets.get_target_clients)
    if not clients:
        await msg.reply_text("Клиенты не найдены. Добавьте первого!",
                             reply_markup=InlineKeyboardMarkup([[
                                 InlineKeyboardButton("➕ Добавить клиента", callback_data="tgt_add_client")
                             ]]))
        return

    keyboard = []
    for c in clients:
        keyboard.append([InlineKeyboardButton(
            f"🎯 {c.get('name')} — {c.get('monthly_fee')}",
            callback_data=f"tgt_client_{c.get('id')}",
        )])
    keyboard.append([InlineKeyboardButton("➕ Добавить клиента", callback_data="tgt_add_client")])
    await msg.reply_text("🎯 *Мои клиенты*", parse_mode="Markdown",
                         reply_markup=InlineKeyboardMarkup(keyboard))


async def tgt_client_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    client_id = query.data.replace("tgt_client_", "")
    clients = await asyncio.to_thread(sheets.get_target_clients)
    client = next((c for c in clients if str(c.get("id")) == client_id), None)
    if not client:
        await query.edit_message_text("Клиент не найден.")
        return

    settings = get_user_settings(query.from_user.id)
    sym = settings.get("symbol", "$")
    text = (
        f"🎯 *{client.get('name')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Ежемесячная оплата: {sym}{client.get('monthly_fee')}\n"
        f"📅 День оплаты: {client.get('payment_day')} числа\n"
        f"📝 Начало работы: {client.get('start_date')}\n"
        f"🔘 Статус: {client.get('status', 'active')}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Записать платёж", callback_data=f"tgt_pay_{client_id}")],
        [InlineKeyboardButton("◀ К списку", callback_data="tgt_clients")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Add client text flow ──────────────────────────────────────

async def add_client_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    value = update.message.text.strip()

    if state == "tgt_add_client_name":
        data["name"] = value
        set_state(user_id, "tgt_add_client_fee", data)
        settings = get_user_settings(user_id)
        sym = settings.get("symbol", "$")
        await update.message.reply_text(f"💰 *Ежемесячная оплата* ({sym}):", parse_mode="Markdown")

    elif state == "tgt_add_client_fee":
        try:
            fee = _parse_amount(value)
        except ValueError:
            await update.message.reply_text("Введите сумму цифрами.")
            return
        data["monthly_fee"] = str(fee)
        set_state(user_id, "tgt_add_client_day", data)
        await update.message.reply_text("📅 *День оплаты* (1-28):", parse_mode="Markdown")

    elif state == "tgt_add_client_day":
        try:
            day = int(value)
            if not (1 <= day <= 28):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число от 1 до 28.")
            return
        data["payment_day"] = str(day)
        data["start_date"] = date.today().strftime("%d.%m.%Y")
        data["status"] = "active"
        settings = get_user_settings(user_id)
        data["currency"] = settings.get("currency", "USD")
        ok = await asyncio.to_thread(sheets.add_target_client, data)
        clear_state(user_id)
        sym = settings.get("symbol", "$")
        await update.message.reply_text(
            f"{'Амирхон ака, клиент добавлен! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🎯 *{data['name']}*\n"
            f"💰 {sym}{data['monthly_fee']}/мес., день оплаты: {day}",
            parse_mode="Markdown",
            reply_markup=targeting_menu_keyboard(),
        )


# ── Record targeting payment ──────────────────────────────────

async def record_tpayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    clients = await asyncio.to_thread(sheets.get_target_clients)
    if not clients:
        await msg.reply_text("Нет клиентов.", reply_markup=targeting_menu_keyboard())
        return

    keyboard = [[InlineKeyboardButton(f"🎯 {c.get('name')}", callback_data=f"tgt_pay_{c.get('id')}")]
                for c in clients]
    clear_state(user_id)
    set_state(user_id, "tgt_pay_select", {})
    await msg.reply_text("💰 Выберите клиента:", reply_markup=InlineKeyboardMarkup(keyboard))


async def tgt_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    client_id = query.data.replace("tgt_pay_", "")

    clients = await asyncio.to_thread(sheets.get_target_clients)
    client = next((c for c in clients if str(c.get("id")) == client_id), None)
    if not client:
        await query.edit_message_text("Клиент не найден.")
        return

    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    set_state(user_id, "tgt_pay_amount", {
        "client_id": client_id,
        "client_name": client.get("name"),
        "expected": client.get("monthly_fee"),
        "symbol": sym,
    })
    await query.edit_message_text(
        f"💰 Платёж от *{client.get('name')}*\n"
        f"Ожидается: {sym}{client.get('monthly_fee')}\n\n"
        "Введите полученную сумму:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Полная сумма ({sym}{client.get('monthly_fee')})",
                                 callback_data=f"tgt_pay_full_{client_id}")
        ]]),
    )


async def tgt_pay_full_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["received"] = data.get("expected")
    await _save_tpayment(query.message, user_id, data)


async def tgt_pay_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "tgt_pay_amount":
        return
    try:
        amount = _parse_amount(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите сумму цифрами.")
        return
    data["received"] = str(amount)
    await _save_tpayment(update.message, user_id, data)


async def _save_tpayment(msg, user_id: int, data: dict) -> None:
    expected = float(data.get("expected", 0))
    received = float(data.get("received", 0))
    diff = received - expected
    status = "paid" if diff >= 0 else ("partial" if received > 0 else "missed")
    row_data = {
        "client_id": data.get("client_id"),
        "client_name": data.get("client_name"),
        "expected_amount": expected,
        "received_amount": received,
        "difference": diff,
        "status": status,
        "note": "",
        "date": date.today().strftime("%d.%m.%Y"),
    }
    ok = await asyncio.to_thread(sheets.record_target_payment, row_data)
    clear_state(user_id)
    sym = data.get("symbol", "$")
    await msg.reply_text(
        f"{'Амирхон ака, платёж записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
        f"🎯 {data.get('client_name')}: {sym}{received:.2f}\n"
        f"📅 {row_data['date']}",
        reply_markup=targeting_menu_keyboard(),
    )


# ── Record targeting expense ──────────────────────────────────

async def record_texpense_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    keyboard = [[InlineKeyboardButton(label, callback_data=f"tgt_exp_cat_{val}")]
                for label, val in TARGET_EXPENSE_CATEGORIES]
    clear_state(user_id)
    set_state(user_id, "tgt_exp_category", {})
    await msg.reply_text("🔧 *Расход по таргету*\n\nВыберите категорию:",
                         parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def tgt_exp_cat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    category = query.data.replace("tgt_exp_cat_", "")
    _, data = get_state(user_id)
    data["category"] = category
    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    data["symbol"] = sym
    set_state(user_id, "tgt_exp_amount", data)
    await query.edit_message_text(f"Категория: *{category}*\n\nВведите сумму ({sym}):",
                                  parse_mode="Markdown")


async def tgt_exp_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "tgt_exp_amount":
        return
    try:
        amount = _parse_amount(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите сумму цифрами.")
        return
    data["amount"] = amount
    row_data = {
        "client_id": "general",
        "category": data.get("category"),
        "amount": amount,
        "description": "Через бот",
        "date": date.today().strftime("%d.%m.%Y"),
    }
    ok = await asyncio.to_thread(sheets.record_target_expense, row_data)
    clear_state(user_id)
    sym = data.get("symbol", "$")
    await update.message.reply_text(
        f"{'Амирхон ака, расход записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
        f"📂 {data.get('category')}: {sym}{amount:.2f}\n"
        f"📅 {row_data['date']}",
        reply_markup=targeting_menu_keyboard(),
    )


# ── Report ────────────────────────────────────────────────────

async def tgt_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    settings = get_user_settings(
        (update.effective_user or update.callback_query.from_user).id
    )
    sym = settings.get("symbol", "$")
    today = date.today()
    payments = await asyncio.to_thread(sheets.get_target_payments_for_month, today.year, today.month)
    expenses = await asyncio.to_thread(sheets.get_target_expenses_for_month, today.year, today.month)

    income = sum(float(p.get("received_amount", 0)) for p in payments)
    total_exp = sum(float(e.get("amount", 0)) for e in expenses)
    net = income - total_exp

    clients = await asyncio.to_thread(sheets.get_target_clients)
    expected = sum(float(c.get("monthly_fee", 0)) for c in clients if c.get("status") == "active")

    month_name = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"][today.month - 1]

    text = (
        f"Амирхон ака, отчёт по таргету готов 📊\n\n"
        f"🎯 *{month_name} {today.year}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Поступило: {sym}{income:.2f} (ожид. {sym}{expected:.2f})\n"
        f"🔧 Расходы: {sym}{total_exp:.2f}\n"
        f"📈 Чистая прибыль: {sym}{net:.2f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Платежей получено: {len(payments)}\n"
        f"Активных клиентов: {len(clients)}"
    )
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=targeting_menu_keyboard())

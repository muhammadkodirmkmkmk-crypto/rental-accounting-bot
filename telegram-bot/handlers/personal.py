import asyncio
import logging
import re
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
from database import get_state, set_state, clear_state, get_user_settings
from handlers.start import personal_menu_keyboard

logger = logging.getLogger(__name__)

INCOME_CATEGORIES = [
    ("💼 Зарплата", "salary"),
    ("🖥 Фриланс", "freelance"),
    ("🏠 Аренда", "rental"),
    ("📈 Дивиденды", "dividends"),
    ("🎁 Другое", "other"),
]

EXPENSE_CATEGORIES = [
    ("🍔 Еда", "food"),
    ("🚗 Транспорт", "transport"),
    ("🎮 Развлечения", "entertainment"),
    ("👕 Одежда", "clothing"),
    ("💊 Здоровье", "health"),
    ("🏠 Жильё/ЖКХ", "housing"),
    ("📱 Связь/Интернет", "communication"),
    ("📚 Образование", "education"),
    ("💰 Сбережения", "savings"),
    ("📦 Другое", "other"),
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


async def personal_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    await msg.reply_text("💰 *Личные финансы*\n\nВыберите действие:",
                         parse_mode="Markdown", reply_markup=personal_menu_keyboard())


# ── Add income ────────────────────────────────────────────────

async def add_income_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    keyboard = [[InlineKeyboardButton(label, callback_data=f"prs_inc_cat_{val}")]
                for label, val in INCOME_CATEGORIES]
    clear_state(user_id)
    set_state(user_id, "prs_income_category", {})
    await msg.reply_text("📥 *Записать доход*\n\nВыберите категорию:",
                         parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def prs_inc_cat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    category = query.data.replace("prs_inc_cat_", "")
    _, data = get_state(user_id)
    data["category"] = category
    data["type"] = "income"
    settings = get_user_settings(user_id)
    data["symbol"] = settings.get("symbol", "$")
    set_state(user_id, "prs_income_amount", data)
    await query.edit_message_text(f"📥 Категория: *{category}*\n\nВведите сумму дохода:",
                                  parse_mode="Markdown")


async def prs_income_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "prs_income_amount":
        return
    try:
        amount = _parse_amount(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите сумму цифрами.")
        return
    data["amount"] = amount
    set_state(user_id, "prs_income_desc", data)
    await update.message.reply_text(
        "📝 Добавьте описание (или нажмите Пропустить):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="prs_desc_skip")]]),
    )


async def prs_desc_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state, data = get_state(user_id)
    data["description"] = ""
    await _save_personal(query.message, user_id, data)


async def prs_income_desc_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state not in ("prs_income_desc", "prs_expense_desc"):
        return
    data["description"] = update.message.text.strip()
    await _save_personal(update.message, user_id, data)


# ── Add personal expense ───────────────────────────────────────

async def add_personal_expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    keyboard = [[InlineKeyboardButton(label, callback_data=f"prs_exp_cat_{val}")]
                for label, val in EXPENSE_CATEGORIES]
    clear_state(user_id)
    set_state(user_id, "prs_expense_category", {})
    await msg.reply_text("📤 *Записать расход*\n\nВыберите категорию:",
                         parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def prs_exp_cat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    category = query.data.replace("prs_exp_cat_", "")
    _, data = get_state(user_id)
    data["category"] = category
    data["type"] = "expense"
    settings = get_user_settings(user_id)
    data["symbol"] = settings.get("symbol", "$")
    set_state(user_id, "prs_expense_amount", data)
    await query.edit_message_text(f"📤 Категория: *{category}*\n\nВведите сумму расхода:",
                                  parse_mode="Markdown")


async def prs_expense_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)
    if state != "prs_expense_amount":
        return
    try:
        amount = _parse_amount(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите сумму цифрами.")
        return
    data["amount"] = amount
    set_state(user_id, "prs_expense_desc", data)
    await update.message.reply_text(
        "📝 Добавьте описание (или нажмите Пропустить):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="prs_desc_skip")]]),
    )


async def _save_personal(msg, user_id: int, data: dict) -> None:
    row_data = {
        "type": data.get("type", "expense"),
        "category": data.get("category", "other"),
        "amount": data.get("amount", 0),
        "description": data.get("description", ""),
        "date": date.today().strftime("%d.%m.%Y"),
    }
    ok = await asyncio.to_thread(sheets.record_personal, row_data)
    clear_state(user_id)
    sym = data.get("symbol", "$")
    emoji = "📥" if data.get("type") == "income" else "📤"
    action = "доход" if data.get("type") == "income" else "расход"
    await msg.reply_text(
        f"{'Амирхон ака, ' + action + ' записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
        f"{emoji} {data.get('category')}: {sym}{data.get('amount', 0):.2f}\n"
        f"📝 {data.get('description') or '—'}\n"
        f"📅 {row_data['date']}",
        reply_markup=personal_menu_keyboard(),
    )


# ── Monthly report ────────────────────────────────────────────

async def personal_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    user_id = (update.effective_user or update.callback_query.from_user).id
    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    today = date.today()
    records = await asyncio.to_thread(sheets.get_personal_for_month, today.year, today.month)

    income_records = [r for r in records if r.get("type") == "income"]
    expense_records = [r for r in records if r.get("type") == "expense"]

    total_income = sum(float(r.get("amount", 0)) for r in income_records)
    total_expense = sum(float(r.get("amount", 0)) for r in expense_records)
    net = total_income - total_expense

    # Breakdown by category
    cat_totals: dict[str, float] = {}
    for r in expense_records:
        cat = r.get("category", "other")
        cat_totals[cat] = cat_totals.get(cat, 0) + float(r.get("amount", 0))

    expense_breakdown = "\n".join(
        f"  • {cat}: {sym}{amt:.2f}" for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1])
    ) or "  Нет расходов"

    month_name = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"][today.month - 1]

    text = (
        f"Амирхон ака, вот ваш отчёт по личным финансам 📊\n\n"
        f"💰 *{month_name} {today.year}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📥 Доходы: {sym}{total_income:.2f}\n"
        f"📤 Расходы: {sym}{total_expense:.2f}\n"
        f"{'📈' if net >= 0 else '📉'} Остаток: {sym}{net:.2f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Расходы по категориям:*\n{expense_breakdown}"
    )
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=personal_menu_keyboard())

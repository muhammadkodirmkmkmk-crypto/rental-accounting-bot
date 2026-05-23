import csv
import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes

import sheets
import analytics
from database import get_user_settings
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    year, month = now.year, now.month

    if context.args:
        text = " ".join(context.args).lower()
        parsed = _parse_month_arg(text)
        if parsed:
            month, year = parsed

    await _send_report(update, year, month)


async def _send_report(update: Update, year: int, month: int) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    settings = get_user_settings(update.effective_user.id)
    sym = settings.get("symbol", "$")

    report_text = analytics.build_monthly_report(year, month, sym)

    month_label = f"{MONTH_NAMES_RU.get(month, str(month))} {year}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Экспорт CSV — {month_label}", callback_data=f"csv_{year}_{month}")],
        [InlineKeyboardButton("◀ Главное меню", callback_data="menu_main")],
    ])
    await msg.reply_text(report_text, reply_markup=keyboard)


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    now = datetime.now()
    year, month = now.year, now.month
    settings = get_user_settings(update.effective_user.id)
    sym = settings.get("symbol", "$")

    payments = sheets.get_payments_for_month(year, month)
    expenses = sheets.get_expenses_for_month(year, month)
    objects = sheets.get_objects()

    total_income = sum(float(p.get("received_amount", 0)) for p in payments)
    total_expected = sum(float(o.get("rent_amount", 0)) for o in objects if o.get("status") == "rented")
    total_expenses = sum(float(e.get("amount", 0)) for e in expenses)
    net_profit = total_income - total_expenses

    occ = analytics.occupancy_rate()
    comp = analytics.monthly_income_comparison(year, month)
    late_tenants = analytics.tenants_with_late_pattern()

    arrow = "📈" if comp["change"] >= 0 else "📉"
    month_label = f"{MONTH_NAMES_RU.get(month, str(month))} {year}"

    text = (
        f"📊 *Сводка — {month_label}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Доход: *{sym}{total_income:.2f}* / {sym}{total_expected:.2f} ожидалось\n"
        f"🔧 Расходы: *{sym}{total_expenses:.2f}*\n"
        f"📈 Чистая прибыль: *{sym}{net_profit:.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏠 Заполняемость: *{occ['rate']}%* ({occ['rented']}/{occ['total']} объектов)\n"
        f"{arrow} По сравнению с прошлым месяцем: *{'+' if comp['change'] >= 0 else ''}{sym}{comp['change']:.2f}* ({comp['change_pct']:+.1f}%)\n"
    )

    if late_tenants:
        text += "\n⚠️ *Арендаторы с просрочками:*\n"
        for t in late_tenants:
            text += f"  • {t['tenant_name']} ({t['object_name']}): {t['late_count']}× просрочено\n"

    expiring = analytics.leases_expiring_soon(30)
    if expiring:
        text += "\n📋 *Истекающие договоры:*\n"
        for e in expiring:
            text += f"  • {e.get('name')}: осталось {e.get('days_left')} дн.\n"

    text += f"\n🔗 [Открыть таблицу]({sheets.spreadsheet_url()})"

    await msg.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())


async def csv_export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    year = int(parts[1])
    month = int(parts[2])

    payments = sheets.get_payments_for_month(year, month)
    expenses = sheets.get_expenses_for_month(year, month)

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["=== ПЛАТЕЖИ ==="])
    if payments:
        writer.writerow(list(payments[0].keys()))
        for p in payments:
            writer.writerow(list(p.values()))
    else:
        writer.writerow(["Платежи не найдены"])

    writer.writerow([])
    writer.writerow(["=== РАСХОДЫ ==="])
    if expenses:
        writer.writerow(list(expenses[0].keys()))
        for e in expenses:
            writer.writerow(list(e.values()))
    else:
        writer.writerow(["Расходы не найдены"])

    month_label = f"{MONTH_NAMES_RU.get(month, str(month))}_{year}"
    buf.seek(0)
    await query.message.reply_document(
        document=InputFile(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=f"отчёт_{month_label}.csv"),
        caption=f"📥 Отчёт за {MONTH_NAMES_RU.get(month, str(month))} {year}",
    )


def _parse_month_arg(text: str) -> tuple[int, int] | None:
    from nlp import extract_month_year
    return extract_month_year(text)


async def report_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    now = datetime.now()
    await _send_report(update, now.year, now.month)

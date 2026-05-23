import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
import analytics
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)


async def tenants_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    objects = await asyncio.to_thread(sheets.get_objects)
    if not objects:
        await msg.reply_text(
            "Арендаторы не найдены. Сначала добавьте объект!",
            reply_markup=main_menu_keyboard(),
        )
        return

    keyboard = []
    for obj in objects:
        tenant = obj.get("tenant_name", "Вакантно")
        status = "✅" if obj.get("status") == "rented" else "🔲"
        keyboard.append([InlineKeyboardButton(
            f"{status} {obj.get('name')} — {tenant}",
            callback_data=f"tenant_detail_{obj.get('id')}",
        )])

    await msg.reply_text(
        "👥 *Арендаторы*\n\nВыберите объект для просмотра:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def tenant_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    obj_id = query.data.replace("tenant_detail_", "")

    objects = await asyncio.to_thread(sheets.get_objects)
    obj = next((o for o in objects if str(o.get("id")) == str(obj_id)), None)
    if not obj:
        await query.edit_message_text("Объект не найден.")
        return

    rel = await asyncio.to_thread(analytics.payment_reliability, obj_id)
    payments = await asyncio.to_thread(sheets.get_all_records, "Payments")
    obj_payments = [p for p in payments if str(p.get("object_id")) == str(obj_id)]
    recent = sorted(obj_payments, key=lambda x: x.get("date", ""), reverse=True)[:5]

    text = (
        f"👤 *{obj.get('tenant_name')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📞 {obj.get('tenant_phone')}\n"
        f"🏠 {obj.get('name')} — {obj.get('address')}\n"
        f"💰 Аренда: {obj.get('rent_amount')} {obj.get('currency')}\n"
        f"📅 Договор: {obj.get('lease_start')} → {obj.get('lease_end')}\n\n"
        f"📊 *Надёжность платежей*\n"
        f"  Вовремя: {rel['on_time_pct']}%\n"
        f"  Просрочек: {rel['late_count']}/{rel['total']}\n"
        f"  Средняя задержка: {rel['avg_delay_days']} дн.\n"
    )

    if rel["late_count"] >= 3:
        text += "\n⚠️ *Этот арендатор систематически задерживает оплату!*\n"

    if recent:
        text += "\n📋 *Последние платежи:*\n"
        for p in recent:
            status_icon = "✅" if p.get("status") == "paid" else ("⚠️" if p.get("status") == "partial" else "❌")
            text += f"  {status_icon} {p.get('date')}: {p.get('received_amount')}\n"

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Назад к арендаторам", callback_data="menu_tenants")]
        ]),
    )

"""Personal reminders list — /my_reminders command and delete callback."""
import asyncio
import logging
from datetime import datetime

import pytz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import sheets
import scheduler as sched
from handlers.start import module_menu_keyboard

logger = logging.getLogger(__name__)

_TZ = pytz.timezone("Asia/Tashkent")

RECURRING_LABELS = {
    "none":    "однажды",
    "daily":   "ежедневно",
    "weekly":  "еженедельно",
    "monthly": "ежемесячно",
}


async def my_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all active personal reminders with delete buttons."""
    user_id = update.effective_user.id
    msg = update.effective_message

    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

    reminders = await asyncio.to_thread(sheets.get_reminders, user_id, "active")

    if not reminders:
        await msg.reply_text(
            "Амирхон ака, у вас нет активных напоминаний 📭\n\n"
            "Скажите мне голосом или текстом, и я напомню:\n"
            "• «Напомни завтра в 10:00 встреча с Алишером»\n"
            "• «Каждый понедельник в 9 — планёрка»\n"
            "• «Через 2 часа позвонить маме»",
            reply_markup=module_menu_keyboard(),
        )
        return

    now = datetime.now(_TZ)
    lines = [f"⏰ *Ваши напоминания* ({len(reminders)} шт.)\n"]
    keyboard = []

    for r in reminders:
        rid = str(r.get("id", ""))
        text = str(r.get("text", "Напоминание"))
        dt_str = str(r.get("datetime", ""))
        recurring = str(r.get("recurring", "none"))
        rec_label = RECURRING_LABELS.get(recurring, recurring)

        try:
            run_dt = _TZ.localize(datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M"))
            date_display = run_dt.strftime("%d.%m.%Y в %H:%M")
            is_past = run_dt < now and recurring == "none"
        except ValueError:
            date_display = dt_str or "—"
            is_past = False

        icon = "✅" if is_past else "⏳"
        rec_icon = "🔁 " if recurring != "none" else ""
        lines.append(f"{icon} {rec_icon}*{text}*\n   📅 {date_display} ({rec_label})")

        label = text[:28] + "…" if len(text) > 28 else text
        keyboard.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"rem_del_{rid}")])

    keyboard.append([InlineKeyboardButton("✖️ Закрыть", callback_data="rem_close")])

    await msg.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle rem_del_{id} — cancel scheduler job and mark as deleted in Sheets."""
    query = update.callback_query
    await query.answer()

    reminder_id = query.data.replace("rem_del_", "")
    user_id = query.from_user.id

    sched.cancel_reminder(f"rem_{user_id}_{reminder_id}")
    ok = await asyncio.to_thread(sheets.update_reminder_status, reminder_id, "deleted")

    await query.edit_message_text(
        "Амирхон ака, напоминание удалено ✅" if ok else
        "Амирхон ака, не удалось удалить напоминание ⚠️",
    )


async def close_reminders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle rem_close — just dismiss the keyboard."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Амирхон ака, список закрыт.")

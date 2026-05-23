import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_user_settings, save_user_settings
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)

TIMEZONE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("UTC", callback_data="tz_set_UTC"),
        InlineKeyboardButton("Europe/Moscow", callback_data="tz_set_Europe/Moscow"),
    ],
    [
        InlineKeyboardButton("Europe/London", callback_data="tz_set_Europe/London"),
        InlineKeyboardButton("America/New_York", callback_data="tz_set_America/New_York"),
    ],
    [
        InlineKeyboardButton("Asia/Dubai", callback_data="tz_set_Asia/Dubai"),
        InlineKeyboardButton("Asia/Almaty", callback_data="tz_set_Asia/Almaty"),
    ],
])


async def set_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    user_id = msg.chat.id if msg else update.effective_user.id
    settings = get_user_settings(user_id)
    tz = settings.get("timezone", "UTC")

    await msg.reply_text(
        f"⏰ *Настройка напоминаний*\n\n"
        f"Текущий часовой пояс: *{tz}*\n\n"
        f"Активные напоминания:\n"
        f"  • За 1 день до оплаты — 09:00\n"
        f"  • В день оплаты — 10:00\n"
        f"  • Через 3 дня после просрочки — 09:00\n"
        f"  • За 30 дней до окончания договора — 09:00\n"
        f"  • Ежемесячный отчёт (последний день) — 18:00\n\n"
        f"Изменить часовой пояс:",
        parse_mode="Markdown",
        reply_markup=TIMEZONE_KEYBOARD,
    )


async def set_timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    tz = query.data.replace("tz_set_", "")
    save_user_settings(user_id, timezone=tz)

    await query.edit_message_text(
        f"✅ Часовой пояс обновлён: *{tz}*\n\n"
        "Все напоминания теперь будут приходить по этому времени.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

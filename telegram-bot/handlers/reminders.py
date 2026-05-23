import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_user_settings, save_user_settings
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)

HOUR_OPTIONS = [7, 8, 9, 10, 11, 12, 13, 14]


def _hour_keyboard(callback_prefix: str, current: int) -> InlineKeyboardMarkup:
    """Build a row of hour buttons, marking the current selection with ✅."""
    row1 = []
    row2 = []
    for h in HOUR_OPTIONS[:4]:
        label = f"✅ {h:02d}:00" if h == current else f"{h:02d}:00"
        row1.append(InlineKeyboardButton(label, callback_data=f"{callback_prefix}{h}"))
    for h in HOUR_OPTIONS[4:]:
        label = f"✅ {h:02d}:00" if h == current else f"{h:02d}:00"
        row2.append(InlineKeyboardButton(label, callback_data=f"{callback_prefix}{h}"))
    return InlineKeyboardMarkup([row1, row2, [InlineKeyboardButton("◀ Главное меню", callback_data="menu_main")]])


async def set_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    user_id = msg.chat.id if msg else update.effective_user.id
    settings = get_user_settings(user_id)
    hour = settings.get("reminder_hour", 9)
    day_before = settings.get("reminder_day_before_hour", 9)

    text = (
        f"Амирхон ака, настройка напоминаний ⏰\n\n"
        f"🌏 Часовой пояс: *Asia/Tashkent* (UTC+5)\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Активные напоминания:*\n"
        f"  • В день оплаты — {hour:02d}:00\n"
        f"  • При просрочке — {hour:02d}:00\n"
        f"  • За 30 дней до конца договора — {hour:02d}:00\n"
        f"  • Ежемесячный отчёт (последний день) — 18:00\n\n"
        f"  • За 1 день до оплаты — {day_before:02d}:00\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 *Выберите время основных напоминаний:*"
    )

    await msg.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_hour_keyboard("rem_hour_", hour),
    )


async def set_day_before_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show time picker specifically for the 1-day-before reminder."""
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    user_id = msg.chat.id if msg else update.effective_user.id
    settings = get_user_settings(user_id)
    day_before = settings.get("reminder_day_before_hour", 9)

    await msg.reply_text(
        f"Амирхон ака, настройка напоминания «За 1 день до оплаты» ⏰\n\n"
        f"Сейчас: *{day_before:02d}:00*\n\n"
        f"Выберите, в котором часу напоминать:",
        parse_mode="Markdown",
        reply_markup=_hour_keyboard("rem_daybefore_", day_before),
    )


async def set_reminder_hour_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle rem_hour_N callback — update main reminder hour."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    hour = int(query.data.replace("rem_hour_", ""))
    settings = get_user_settings(user_id)
    day_before = settings.get("reminder_day_before_hour", 9)
    save_user_settings(user_id, reminder_hour=hour)

    # Reschedule APScheduler jobs with the new hour
    try:
        from scheduler import reschedule_reminders
        import asyncio
        await asyncio.to_thread(reschedule_reminders, context.application.bot, hour, day_before)
    except Exception as e:
        logger.warning("Could not reschedule: %s", e)

    await query.edit_message_text(
        f"Амирхон ака, время напоминаний обновлено ✅\n\n"
        f"🕐 Основные напоминания: *{hour:02d}:00*\n"
        f"🕐 За 1 день до оплаты: *{day_before:02d}:00*\n\n"
        f"Напоминания будут приходить по времени Asia/Tashkent.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏰ За 1 день до оплаты", callback_data="rem_set_daybefore")],
            [InlineKeyboardButton("◀ Назад к напоминаниям", callback_data="menu_set_reminder")],
        ]),
    )


async def set_day_before_hour_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle rem_daybefore_N callback — update day-before reminder hour."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    hour = int(query.data.replace("rem_daybefore_", ""))
    settings = get_user_settings(user_id)
    main_hour = settings.get("reminder_hour", 9)
    save_user_settings(user_id, reminder_day_before_hour=hour)

    try:
        from scheduler import reschedule_reminders
        import asyncio
        await asyncio.to_thread(reschedule_reminders, context.application.bot, main_hour, hour)
    except Exception as e:
        logger.warning("Could not reschedule: %s", e)

    await query.edit_message_text(
        f"Амирхон ака, время обновлено ✅\n\n"
        f"🕐 За 1 день до оплаты: *{hour:02d}:00*\n"
        f"🕐 Основные напоминания: *{main_hour:02d}:00*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ К напоминаниям", callback_data="menu_set_reminder")],
        ]),
    )

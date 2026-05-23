import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_user_settings, save_user_settings
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)

HOUR_OPTIONS = [7, 8, 9, 10, 11, 12, 13, 14]


def _reminder_keyboard(main_hour: int, day_before_hour: int) -> InlineKeyboardMarkup:
    """
    Single all-in-one keyboard:
      - label row  (non-clickable separator)
      - 2 rows of hour buttons for main reminders  (rem_hour_N)
      - label row
      - 2 rows of hour buttons for day-before      (rem_daybefore_N)
      - back button
    """
    def _row(hours, prefix, current):
        return [
            InlineKeyboardButton(
                f"✅ {h:02d}:00" if h == current else f"{h:02d}:00",
                callback_data=f"{prefix}{h}",
            )
            for h in hours
        ]

    rows = [
        # ── Main reminder label + buttons ─────────────────────
        [InlineKeyboardButton("🕐  Основные напоминания:", callback_data="rem_noop")],
        _row(HOUR_OPTIONS[:4], "rem_hour_", main_hour),
        _row(HOUR_OPTIONS[4:], "rem_hour_", main_hour),
        # ── Day-before label + buttons ─────────────────────────
        [InlineKeyboardButton("⏰  За 1 день до оплаты:", callback_data="rem_noop")],
        _row(HOUR_OPTIONS[:4], "rem_daybefore_", day_before_hour),
        _row(HOUR_OPTIONS[4:], "rem_daybefore_", day_before_hour),
        # ── Navigation ────────────────────────────────────────
        [InlineKeyboardButton("◀ Главное меню", callback_data="module_main")],
    ]
    return InlineKeyboardMarkup(rows)


def _reminder_text(main_hour: int, day_before_hour: int) -> str:
    return (
        "Амирхон ака, настройка напоминаний ⏰\n\n"
        "🌏 Часовой пояс: *Asia/Tashkent* (UTC+5)\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 *Активные напоминания:*\n"
        f"  • Основные — {main_hour:02d}:00\n"
        f"    (просрочка, день оплаты, конец договора)\n"
        f"  • За 1 день до оплаты — {day_before_hour:02d}:00\n"
        "  • Ежемесячный отчёт (последний день) — 18:00\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Выберите время для каждого типа 👇"
    )


async def set_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open (or refresh) the reminders screen — works from both /command and callback."""
    if update.callback_query:
        await update.callback_query.answer()
        user_id = update.callback_query.from_user.id
        settings = get_user_settings(user_id)
        hour = settings.get("reminder_hour", 9)
        day_before = settings.get("reminder_day_before_hour", 9)
        try:
            await update.callback_query.edit_message_text(
                _reminder_text(hour, day_before),
                parse_mode="Markdown",
                reply_markup=_reminder_keyboard(hour, day_before),
            )
        except Exception:
            await update.callback_query.message.reply_text(
                _reminder_text(hour, day_before),
                parse_mode="Markdown",
                reply_markup=_reminder_keyboard(hour, day_before),
            )
    else:
        msg = update.message
        user_id = msg.from_user.id
        settings = get_user_settings(user_id)
        hour = settings.get("reminder_hour", 9)
        day_before = settings.get("reminder_day_before_hour", 9)
        await msg.reply_text(
            _reminder_text(hour, day_before),
            parse_mode="Markdown",
            reply_markup=_reminder_keyboard(hour, day_before),
        )


async def set_reminder_hour_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """rem_hour_N — save main hour and refresh the same message."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    hour = int(query.data.replace("rem_hour_", ""))
    save_user_settings(user_id, reminder_hour=hour)

    settings = get_user_settings(user_id)
    day_before = settings.get("reminder_day_before_hour", 9)

    try:
        import asyncio
        from scheduler import reschedule_reminders
        await asyncio.to_thread(reschedule_reminders, context.application.bot, hour, day_before)
    except Exception as e:
        logger.warning("Could not reschedule: %s", e)

    await query.edit_message_text(
        _reminder_text(hour, day_before),
        parse_mode="Markdown",
        reply_markup=_reminder_keyboard(hour, day_before),
    )


async def set_day_before_hour_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """rem_daybefore_N — save day-before hour and refresh the same message."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    hour = int(query.data.replace("rem_daybefore_", ""))
    save_user_settings(user_id, reminder_day_before_hour=hour)

    settings = get_user_settings(user_id)
    main_hour = settings.get("reminder_hour", 9)

    try:
        import asyncio
        from scheduler import reschedule_reminders
        await asyncio.to_thread(reschedule_reminders, context.application.bot, main_hour, hour)
    except Exception as e:
        logger.warning("Could not reschedule: %s", e)

    await query.edit_message_text(
        _reminder_text(main_hour, hour),
        parse_mode="Markdown",
        reply_markup=_reminder_keyboard(main_hour, hour),
    )


async def reminder_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Label buttons — just acknowledge, do nothing."""
    await update.callback_query.answer()


# Keep this for backward compat (menu_set_reminder routes here)
set_day_before_command = set_reminder_command

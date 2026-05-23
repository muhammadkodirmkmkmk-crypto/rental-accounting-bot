"""
Pure-conversational rental accounting bot.
No wizards, no menus. Every message → Claude AI.
"""
import asyncio
import logging
import logging.handlers
import sys

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    filters,
)

import config
from database import init_db, get_state, clear_state, set_state
from sheets import init_sheets
from scheduler import start_scheduler, load_reminders_from_sheets

from handlers.start import start_command, restart_command
from handlers.personal_reminders import (
    my_reminders_command,
    delete_reminder_callback,
    close_reminders_callback,
)
from handlers.text import free_text_handler
from handlers.voice import voice_message_handler


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.WARNING)
    root.addHandler(fh)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    primary_owner = next(iter(config.ALLOWED_USER_IDS))
    if user_id != primary_owner:
        await update.message.reply_text("⛔ Только владелец бота может удалять все данные.")
        return
    clear_state(user_id)
    set_state(user_id, "confirm_delete", {})
    await update.message.reply_text(
        "Амирхон ака, вы уверены? ⚠️\n\n"
        "Это удалит *ВСЕ данные* из Google Sheets:\n"
        "• Все объекты аренды\n"
        "• Все платежи и расходы\n"
        "• Клиентов таргета\n"
        "• Личные финансы\n\n"
        "Напишите *ДА* для подтверждения или любое другое слово для отмены.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Амирхон ака, просто пишите или говорите что нужно — я всё пойму 👋\n\n"
        "*Примеры:*\n"
        "• «Офис заплатил 350 долларов»\n"
        "• «Запиши расход 200 на ремонт»\n"
        "• «Добавь новый объект»\n"
        "• «Напомни через 2 часа позвонить Алишеру»\n"
        "• «Каждый понедельник в 9 — планёрка»\n"
        "• «Отчёт за май»\n"
        "• «Покажи мои объекты»\n\n"
        "*Команды:*\n"
        "/my\\_reminders — список напоминаний\n"
        "/delete — сбросить все данные\n"
        "/help — эта справка\n\n"
        "Голосовые сообщения работают так же как текстовые 🎙",
        parse_mode="Markdown",
    )


async def main_callback_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""

    if data.startswith("rem_del_"):
        await delete_reminder_callback(update, context)
    elif data == "rem_close":
        await close_reminders_callback(update, context)
    else:
        await query.answer()


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in config.ALLOWED_USER_IDS:
        if update.message:
            await update.message.reply_text("⛔ Доступ запрещён.")
        elif update.callback_query:
            await update.callback_query.answer("⛔ Доступ запрещён.", show_alert=True)
        return False
    return True


BOT_COMMANDS = [
    BotCommand("start",        "👋 Начать / главное меню"),
    BotCommand("my_reminders", "📋 Мои личные напоминания"),
    BotCommand("help",         "❓ Справка и примеры"),
    BotCommand("delete",       "🗑️ Сбросить все данные"),
]


async def post_init(application: Application) -> None:
    bot = application.bot
    log = logging.getLogger(__name__)

    try:
        init_sheets()
        log.info("Google Sheets инициализирован")
    except Exception as e:
        log.error("Ошибка инициализации Sheets: %s", e)

    start_scheduler(bot, timezone=config.DEFAULT_TIMEZONE)

    try:
        load_reminders_from_sheets(bot)
    except Exception as e:
        log.warning("Could not restore reminders: %s", e)

    try:
        await bot.set_my_commands(BOT_COMMANDS)
        log.info("Команды бота зарегистрированы")
    except Exception as e:
        log.warning("Не удалось зарегистрировать команды: %s", e)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    # Commands
    app.add_handler(CommandHandler("start",        start_command))
    app.add_handler(CommandHandler("restart",      restart_command))
    app.add_handler(CommandHandler("delete",       delete_command))
    app.add_handler(CommandHandler("help",         help_command))
    app.add_handler(CommandHandler("my_reminders", my_reminders_command))

    # Media
    app.add_handler(MessageHandler(filters.VOICE, voice_message_handler))

    # Inline button callbacks (only for reminder list management)
    app.add_handler(CallbackQueryHandler(main_callback_dispatcher))

    # ALL text messages → Claude AI (no wizard routing)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        free_text_handler,
    ))

    return app


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Инициализация базы данных...")
    init_db()

    logger.info("Сборка приложения Telegram...")
    app = build_application()

    logger.info("Запуск бота (polling)...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

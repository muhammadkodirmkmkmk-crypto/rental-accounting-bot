"""
Бот учёта аренды недвижимости
"""

import asyncio
import logging
import logging.handlers
import sys
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    filters,
)

import config
from database import init_db
from sheets import init_sheets
from scheduler import start_scheduler

from handlers.start import (
    start_command,
    restart_command,
    setup_callback,
    setup_text_handler,
    main_menu_keyboard,
)
from handlers.objects import (
    add_object_command,
    objects_command,
    object_detail_callback,
    add_object_text_handler,
    obj_no_end_date_callback,
)
from handlers.payments import (
    record_payment_command,
    payment_obj_callback,
    payment_full_callback,
    payment_amount_text,
    payment_note_skip_callback,
    payment_note_text,
    confirm_payment_command,
    missed_payment_command,
)
from handlers.expenses import (
    record_expense_command,
    expense_obj_callback,
    expense_category_callback,
    expense_amount_text,
    expense_desc_skip_callback,
    expense_description_text,
)
from handlers.reports import (
    report_command,
    summary_command,
    csv_export_callback,
    report_nav_callback,
)
from handlers.tenants import tenants_command, tenant_detail_callback
from handlers.reminders import set_reminder_command, set_timezone_callback
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


async def main_text_dispatcher(update: Update, context) -> None:
    from database import get_state
    user_id = update.effective_user.id
    state, _ = get_state(user_id)

    if state in ("setup_timezone_custom",):
        await setup_text_handler(update, context)
    elif state and state.startswith("add_object_"):
        await add_object_text_handler(update, context)
    elif state == "record_payment_amount":
        await payment_amount_text(update, context)
    elif state == "record_payment_note":
        await payment_note_text(update, context)
    elif state == "record_expense_amount":
        await expense_amount_text(update, context)
    elif state == "record_expense_description":
        await expense_description_text(update, context)
    else:
        await free_text_handler(update, context)


async def main_callback_dispatcher(update: Update, context) -> None:
    query = update.callback_query
    data = query.data

    if data.startswith("menu_"):
        action = data.replace("menu_", "")
        if action == "objects":
            await objects_command(update, context)
        elif action == "add_object":
            await add_object_command(update, context)
        elif action == "record_payment":
            await record_payment_command(update, context)
        elif action == "record_expense":
            await record_expense_command(update, context)
        elif action == "report":
            await report_nav_callback(update, context)
        elif action == "summary":
            await summary_command(update, context)
        elif action == "tenants":
            await tenants_command(update, context)
        elif action == "set_reminder":
            await set_reminder_command(update, context)
        elif action == "main":
            await query.answer()
            await query.edit_message_text(
                "Что хотите сделать?",
                reply_markup=main_menu_keyboard(),
            )
    elif data.startswith("tz_") and not data.startswith("tz_set_"):
        await setup_callback(update, context)
    elif data.startswith("cur_"):
        await setup_callback(update, context)
    elif data in ("setup_add_object", "setup_skip"):
        await setup_callback(update, context)
    elif data.startswith("obj_detail_"):
        await object_detail_callback(update, context)
    elif data == "obj_no_end_date":
        await obj_no_end_date_callback(update, context)
    elif data.startswith("pay_obj_"):
        await payment_obj_callback(update, context)
    elif data.startswith("pay_full_"):
        await payment_full_callback(update, context)
    elif data == "pay_note_skip":
        await payment_note_skip_callback(update, context)
    elif data.startswith("exp_obj_"):
        await expense_obj_callback(update, context)
    elif data.startswith("exp_cat_"):
        await expense_category_callback(update, context)
    elif data == "exp_desc_skip":
        await expense_desc_skip_callback(update, context)
    elif data.startswith("csv_"):
        await csv_export_callback(update, context)
    elif data.startswith("tenant_detail_"):
        await tenant_detail_callback(update, context)
    elif data.startswith("tz_set_"):
        await set_timezone_callback(update, context)
    else:
        await query.answer("Неизвестное действие")


async def access_guard(update: Update, context) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in config.ALLOWED_USER_IDS:
        if update.message:
            await update.message.reply_text("⛔ Доступ запрещён.")
        elif update.callback_query:
            await update.callback_query.answer("⛔ Доступ запрещён.", show_alert=True)
        return False
    return True


async def post_init(application: Application) -> None:
    bot = application.bot
    try:
        init_sheets()
        logging.getLogger(__name__).info("Google Sheets инициализирован")
    except Exception as e:
        logging.getLogger(__name__).error("Ошибка инициализации Sheets: %s", e)

    start_scheduler(bot, timezone=config.DEFAULT_TIMEZONE)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("add_object", add_object_command))
    app.add_handler(CommandHandler("objects", objects_command))
    app.add_handler(CommandHandler("record_payment", record_payment_command))
    app.add_handler(CommandHandler("record_expense", record_expense_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("set_reminder", set_reminder_command))
    app.add_handler(CommandHandler("tenants", tenants_command))

    app.add_handler(MessageHandler(
        filters.Regex(r"^/confirm_\d+"),
        confirm_payment_command,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/missed_\d+"),
        missed_payment_command,
    ))

    app.add_handler(MessageHandler(filters.VOICE, voice_message_handler))

    app.add_handler(CallbackQueryHandler(main_callback_dispatcher))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        main_text_dispatcher,
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

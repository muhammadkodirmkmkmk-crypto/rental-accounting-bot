"""
Бот учёта: аренда, таргет-проекты, личные расходы
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
from database import init_db, get_state, clear_state, set_state
from sheets import init_sheets
from scheduler import start_scheduler

from handlers.start import (
    start_command,
    restart_command,
    setup_callback,
    setup_text_handler,
    module_menu_keyboard,
    main_menu_keyboard,
    targeting_menu_keyboard,
    personal_menu_keyboard,
    MODULE_MENU_KEYBOARD,
)
from handlers.objects import (
    add_object_command,
    objects_command,
    object_detail_callback,
    add_object_text_handler,
    obj_no_end_date_callback,
    obj_discount_yes_callback,
    obj_discount_no_callback,
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
from handlers.reminders import (
    set_reminder_command,
    set_day_before_command,
    set_reminder_hour_callback,
    set_day_before_hour_callback,
)
from handlers.text import free_text_handler
from handlers.voice import voice_message_handler
from handlers.targeting import (
    targeting_menu_command,
    add_client_command,
    clients_command,
    tgt_client_detail_callback,
    add_client_text_handler,
    record_tpayment_command,
    tgt_pay_callback,
    tgt_pay_full_callback,
    tgt_pay_amount_text,
    record_texpense_command,
    tgt_exp_cat_callback,
    tgt_exp_amount_text,
    tgt_report_command,
)
from handlers.personal import (
    personal_menu_command,
    add_income_command,
    add_personal_expense_command,
    prs_inc_cat_callback,
    prs_exp_cat_callback,
    prs_income_amount_text,
    prs_expense_amount_text,
    prs_income_desc_text,
    prs_desc_skip_callback,
    personal_report_command,
)


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


async def delete_command(update, context) -> None:
    user_id = update.effective_user.id
    # Only the primary owner (first in allowed list) can delete all data
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
        "Напишите *ДА* для подтверждения или любой другой текст для отмены.",
        parse_mode="Markdown",
    )


async def main_text_dispatcher(update: Update, context) -> None:
    user_id = update.effective_user.id
    state, _ = get_state(user_id)

    if state == "setup_timezone_custom":
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
    # Targeting states
    elif state and state.startswith("tgt_add_client_"):
        await add_client_text_handler(update, context)
    elif state == "tgt_pay_amount":
        await tgt_pay_amount_text(update, context)
    elif state == "tgt_exp_amount":
        await tgt_exp_amount_text(update, context)
    # Personal states
    elif state == "prs_income_amount":
        await prs_income_amount_text(update, context)
    elif state == "prs_expense_amount":
        await prs_expense_amount_text(update, context)
    elif state in ("prs_income_desc", "prs_expense_desc"):
        await prs_income_desc_text(update, context)
    else:
        await free_text_handler(update, context)


async def main_callback_dispatcher(update: Update, context) -> None:
    query = update.callback_query
    data = query.data

    # ── Module routing ────────────────────────────────────────
    if data == "module_main":
        await query.answer()
        await query.edit_message_text("Выберите раздел:", reply_markup=MODULE_MENU_KEYBOARD)
        return

    if data == "module_rental":
        await query.answer()
        await query.edit_message_text(
            "🏠 *Аренда квартир*\n\nЧто хотите сделать?",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "module_targeting":
        await query.answer()
        await query.edit_message_text(
            "🎯 *Таргет проекты*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=targeting_menu_keyboard(),
        )
        return

    if data == "module_personal":
        await query.answer()
        await query.edit_message_text(
            "💰 *Личные финансы*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=personal_menu_keyboard(),
        )
        return

    # ── Rental menu ───────────────────────────────────────────
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
                "🏠 *Аренда квартир*\n\nЧто хотите сделать?",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        return

    # ── Setup ─────────────────────────────────────────────────
    if (data.startswith("tz_") and not data.startswith("tz_set_")) or \
       data.startswith("cur_") or data in ("setup_add_object", "setup_skip"):
        await setup_callback(update, context)
        return

    # ── Object callbacks ──────────────────────────────────────
    if data.startswith("obj_detail_"):
        await object_detail_callback(update, context)
    elif data == "obj_no_end_date":
        await obj_no_end_date_callback(update, context)
    elif data == "obj_discount_yes":
        await obj_discount_yes_callback(update, context)
    elif data == "obj_discount_no":
        await obj_discount_no_callback(update, context)

    # ── Payment callbacks ─────────────────────────────────────
    elif data.startswith("pay_obj_"):
        await payment_obj_callback(update, context)
    elif data.startswith("pay_full_"):
        await payment_full_callback(update, context)
    elif data == "pay_note_skip":
        await payment_note_skip_callback(update, context)

    # ── Expense callbacks ─────────────────────────────────────
    elif data.startswith("exp_obj_"):
        await expense_obj_callback(update, context)
    elif data.startswith("exp_cat_"):
        await expense_category_callback(update, context)
    elif data == "exp_desc_skip":
        await expense_desc_skip_callback(update, context)

    # ── Reports ───────────────────────────────────────────────
    elif data.startswith("csv_"):
        await csv_export_callback(update, context)
    elif data.startswith("report_"):
        await report_nav_callback(update, context)

    # ── Tenants / Reminders ───────────────────────────────────
    elif data.startswith("tenant_detail_"):
        await tenant_detail_callback(update, context)
    elif data.startswith("rem_hour_"):
        await set_reminder_hour_callback(update, context)
    elif data.startswith("rem_daybefore_"):
        await set_day_before_hour_callback(update, context)
    elif data == "rem_set_daybefore":
        await set_day_before_command(update, context)

    # ── Targeting callbacks ───────────────────────────────────
    elif data == "tgt_clients":
        await clients_command(update, context)
    elif data == "tgt_add_client":
        await add_client_command(update, context)
    elif data == "tgt_record_payment":
        await record_tpayment_command(update, context)
    elif data == "tgt_record_expense":
        await record_texpense_command(update, context)
    elif data == "tgt_report":
        await tgt_report_command(update, context)
    elif data.startswith("tgt_client_"):
        await tgt_client_detail_callback(update, context)
    elif data.startswith("tgt_pay_full_"):
        await tgt_pay_full_callback(update, context)
    elif data.startswith("tgt_pay_"):
        await tgt_pay_callback(update, context)
    elif data.startswith("tgt_exp_cat_"):
        await tgt_exp_cat_callback(update, context)

    # ── Personal callbacks ────────────────────────────────────
    elif data == "prs_add_income":
        await add_income_command(update, context)
    elif data == "prs_add_expense":
        await add_personal_expense_command(update, context)
    elif data == "prs_report":
        await personal_report_command(update, context)
    elif data.startswith("prs_inc_cat_"):
        await prs_inc_cat_callback(update, context)
    elif data.startswith("prs_exp_cat_"):
        await prs_exp_cat_callback(update, context)
    elif data == "prs_desc_skip":
        await prs_desc_skip_callback(update, context)

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
    app.add_handler(CommandHandler("delete", delete_command))
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

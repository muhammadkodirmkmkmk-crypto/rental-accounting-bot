"""
Бот учёта: аренда, таргет-проекты, личные расходы
"""

import asyncio
import logging
import logging.handlers
import sys
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
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
    obj_skip_field_callback,
    obj_vacant_callback,
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
    set_reminder_hour_callback,
    set_day_before_hour_callback,
    reminder_noop_callback,
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
        await query.edit_message_text(
            "Амирхон ака, ассалому алайкум! 👋\n\nЧем могу помочь?",
            reply_markup=MODULE_MENU_KEYBOARD,
        )
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
    elif data == "obj_skip_field":
        await obj_skip_field_callback(update, context)
    elif data == "obj_vacant":
        await obj_vacant_callback(update, context)

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
    elif data == "rem_noop":
        await reminder_noop_callback(update, context)
    elif data == "rem_set_daybefore":
        await set_reminder_command(update, context)

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


BOT_COMMANDS = [
    BotCommand("start",        "🏠 Главное меню"),
    BotCommand("rent",         "🏠 Аренда квартир"),
    BotCommand("target",       "🎯 Таргет проекты"),
    BotCommand("personal",     "💰 Личные расходы"),
    BotCommand("add_property", "➕ Добавить объект"),
    BotCommand("add_client",   "➕ Добавить клиента (таргет)"),
    BotCommand("payment",      "💳 Записать оплату"),
    BotCommand("expense",      "📉 Записать расход"),
    BotCommand("report",       "📊 Отчёт за месяц"),
    BotCommand("report_year",  "📈 Отчёт за год"),
    BotCommand("objects",      "🏘️ Мои объекты"),
    BotCommand("clients",      "👥 Мои клиенты"),
    BotCommand("reminders",    "🔔 Настроить напоминания"),
    BotCommand("delete",       "🗑️ Сбросить все данные"),
    BotCommand("help",         "❓ Помощь"),
]

HELP_TEXT = (
    "Амирхон ака, вот список команд 📋\n\n"
    "🏠 *Аренда квартир*\n"
    "/rent — открыть меню аренды\n"
    "/add\\_property — добавить новый объект\n"
    "/objects — список всех объектов\n"
    "/payment — записать платёж\n"
    "/expense — записать расход\n"
    "/report — отчёт за текущий месяц\n"
    "/report\\_year — отчёт за текущий год\n\n"
    "🎯 *Таргет проекты*\n"
    "/target — открыть меню таргета\n"
    "/add\\_client — добавить клиента\n"
    "/clients — список клиентов\n\n"
    "💰 *Личные финансы*\n"
    "/personal — открыть меню личных расходов\n\n"
    "⚙️ *Управление*\n"
    "/reminders — настроить напоминания\n"
    "/delete — сбросить все данные\n"
    "/restart — перезапустить настройку\n"
    "/help — показать эту справку\n\n"
    "💡 Также можно писать голосом или текстом:\n"
    "«Квартира 1 заплатила 500» или «Расход ремонт 150$»"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=MODULE_MENU_KEYBOARD,
    )


async def rent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🏠 *Аренда квартир*\n\nЧто хотите сделать?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎯 *Таргет проекты*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=targeting_menu_keyboard(),
    )


async def personal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "💰 *Личные расходы*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=personal_menu_keyboard(),
    )


async def report_year_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import pytz
    from datetime import datetime
    import sheets as _sheets
    import analytics
    from database import get_user_settings as _gus

    tz = pytz.timezone(config.DEFAULT_TIMEZONE)
    now = datetime.now(tz)
    settings = _gus(update.effective_user.id)
    sym = settings.get("symbol", "$")

    month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
                   "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

    lines = [f"Амирхон ака, отчёт за {now.year} 📈\n"]
    total_income = 0.0
    total_expense = 0.0

    for month in range(1, now.month + 1):
        payments = await asyncio.to_thread(_sheets.get_payments_for_month, now.year, month)
        expenses = await asyncio.to_thread(_sheets.get_expenses_for_month, now.year, month)
        inc = sum(float(p.get("received_amount", 0)) for p in payments)
        exp = sum(float(e.get("amount", 0)) for e in expenses)
        total_income += inc
        total_expense += exp
        lines.append(f"  {month_names[month-1]}: доход {sym}{inc:.0f} / расход {sym}{exp:.0f}")

    lines.append(f"\n💰 Итого доход: *{sym}{total_income:.2f}*")
    lines.append(f"🔧 Итого расходы: *{sym}{total_expense:.2f}*")
    lines.append(f"📈 Чистая прибыль: *{sym}{total_income - total_expense:.2f}*")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def debug_sheets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — shows exact Sheets connection error."""
    lines = ["🔍 *Диагностика Google Sheets*\n"]

    # Check env vars
    creds = config.GOOGLE_CREDS_DICT
    sid = config.SPREADSHEET_ID
    lines.append(f"• GOOGLE\\_CREDS\\_JSON: {'✅ задан (' + str(len(creds)) + ' полей)' if creds else '❌ пустой или невалидный JSON'}")
    lines.append(f"• SPREADSHEET\\_ID: {'✅ ' + sid[:10] + '...' if sid else '❌ не задан'}")
    lines.append(f"• client\\_email: `{creds.get('client_email', '❌ нет')}`")
    lines.append(f"• type: `{creds.get('type', '❌ нет')}`\n")

    # Try connection
    try:
        import gspread as _gs
        gc = _gs.service_account_from_dict(creds)
        lines.append("• Аутентификация: ✅")
        try:
            ss = gc.open_by_key(sid)
            lines.append(f"• Открытие таблицы: ✅ ({ss.title})")
            try:
                ws = ss.worksheet("Objects")
                rows = ws.get_all_records()
                lines.append(f"• Чтение Objects: ✅ ({len(rows)} строк)")
            except Exception as e:
                lines.append(f"• Чтение Objects: ❌ {type(e).__name__}: {e}")
        except Exception as e:
            lines.append(f"• Открытие таблицы: ❌ {type(e).__name__}: {e}")
    except Exception as e:
        lines.append(f"• Аутентификация: ❌ {type(e).__name__}: {e}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def post_init(application: Application) -> None:
    bot = application.bot
    try:
        init_sheets()
        logging.getLogger(__name__).info("Google Sheets инициализирован")
    except Exception as e:
        logging.getLogger(__name__).error("Ошибка инициализации Sheets: %s", e)

    start_scheduler(bot, timezone=config.DEFAULT_TIMEZONE)

    # Register bot command menu (shows in Telegram "/" menu)
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        logging.getLogger(__name__).info("Команды бота зарегистрированы")
    except Exception as e:
        logging.getLogger(__name__).warning("Не удалось зарегистрировать команды: %s", e)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    app.add_handler(CommandHandler("start",         start_command))
    app.add_handler(CommandHandler("restart",       restart_command))
    app.add_handler(CommandHandler("delete",        delete_command))
    app.add_handler(CommandHandler("help",          help_command))
    # Module shortcuts
    app.add_handler(CommandHandler("rent",          rent_command))
    app.add_handler(CommandHandler("target",        target_command))
    app.add_handler(CommandHandler("personal",      personal_command))
    # Objects / clients
    app.add_handler(CommandHandler("add_object",    add_object_command))
    app.add_handler(CommandHandler("add_property",  add_object_command))
    app.add_handler(CommandHandler("objects",       objects_command))
    app.add_handler(CommandHandler("add_client",    add_client_command))
    app.add_handler(CommandHandler("clients",       clients_command))
    # Payments / expenses
    app.add_handler(CommandHandler("record_payment", record_payment_command))
    app.add_handler(CommandHandler("payment",       record_payment_command))
    app.add_handler(CommandHandler("record_expense", record_expense_command))
    app.add_handler(CommandHandler("expense",       record_expense_command))
    # Reports
    app.add_handler(CommandHandler("report",        report_command))
    app.add_handler(CommandHandler("report_year",   report_year_command))
    app.add_handler(CommandHandler("summary",       summary_command))
    # Reminders / tenants
    app.add_handler(CommandHandler("set_reminder",  set_reminder_command))
    app.add_handler(CommandHandler("reminders",     set_reminder_command))
    app.add_handler(CommandHandler("tenants",       tenants_command))
    app.add_handler(CommandHandler("debug_sheets",  debug_sheets_command))

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

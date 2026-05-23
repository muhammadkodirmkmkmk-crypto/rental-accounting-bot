import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_user_settings, save_user_settings, clear_state, set_state, get_state
import config

logger = logging.getLogger(__name__)

MODULE_MENU_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🏠 Аренда квартир", callback_data="module_rental")],
    [InlineKeyboardButton("🎯 Таргет проекты", callback_data="module_targeting")],
    [InlineKeyboardButton("💰 Личные расходы", callback_data="module_personal")],
])

RENTAL_MENU_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🏠 Мои объекты", callback_data="menu_objects"),
        InlineKeyboardButton("➕ Добавить объект", callback_data="menu_add_object"),
    ],
    [
        InlineKeyboardButton("💰 Записать платёж", callback_data="menu_record_payment"),
        InlineKeyboardButton("🔧 Записать расход", callback_data="menu_record_expense"),
    ],
    [
        InlineKeyboardButton("📊 Отчёт за месяц", callback_data="menu_report"),
        InlineKeyboardButton("📈 Сводка", callback_data="menu_summary"),
    ],
    [
        InlineKeyboardButton("👥 Арендаторы", callback_data="menu_tenants"),
        InlineKeyboardButton("⏰ Напоминания", callback_data="menu_set_reminder"),
    ],
    [InlineKeyboardButton("◀ Главное меню", callback_data="module_main")],
])

TARGETING_MENU_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("👥 Мои клиенты", callback_data="tgt_clients"),
        InlineKeyboardButton("➕ Добавить клиента", callback_data="tgt_add_client"),
    ],
    [
        InlineKeyboardButton("💰 Записать платёж", callback_data="tgt_record_payment"),
        InlineKeyboardButton("🔧 Записать расход", callback_data="tgt_record_expense"),
    ],
    [
        InlineKeyboardButton("📊 Отчёт", callback_data="tgt_report"),
    ],
    [InlineKeyboardButton("◀ Главное меню", callback_data="module_main")],
])

PERSONAL_MENU_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📥 Записать доход", callback_data="prs_add_income"),
        InlineKeyboardButton("📤 Записать расход", callback_data="prs_add_expense"),
    ],
    [InlineKeyboardButton("📊 Отчёт за месяц", callback_data="prs_report")],
    [InlineKeyboardButton("◀ Главное меню", callback_data="module_main")],
])

TIMEZONE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("UTC", callback_data="tz_UTC"),
        InlineKeyboardButton("Europe/Moscow", callback_data="tz_Europe/Moscow"),
    ],
    [
        InlineKeyboardButton("Europe/London", callback_data="tz_Europe/London"),
        InlineKeyboardButton("America/New_York", callback_data="tz_America/New_York"),
    ],
    [
        InlineKeyboardButton("Asia/Dubai", callback_data="tz_Asia/Dubai"),
        InlineKeyboardButton("Asia/Almaty", callback_data="tz_Asia/Almaty"),
    ],
    [InlineKeyboardButton("Другой (ввести вручную)", callback_data="tz_custom")],
])

CURRENCY_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("$ USD", callback_data="cur_USD_$"),
        InlineKeyboardButton("€ EUR", callback_data="cur_EUR_€"),
    ],
    [
        InlineKeyboardButton("₽ RUB", callback_data="cur_RUB_₽"),
        InlineKeyboardButton("£ GBP", callback_data="cur_GBP_£"),
    ],
    [
        InlineKeyboardButton("₸ KZT", callback_data="cur_KZT_₸"),
        InlineKeyboardButton("₴ UAH", callback_data="cur_UAH_₴"),
    ],
])


def module_menu_keyboard() -> InlineKeyboardMarkup:
    return MODULE_MENU_KEYBOARD


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return RENTAL_MENU_KEYBOARD


def targeting_menu_keyboard() -> InlineKeyboardMarkup:
    return TARGETING_MENU_KEYBOARD


def personal_menu_keyboard() -> InlineKeyboardMarkup:
    return PERSONAL_MENU_KEYBOARD


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    clear_state(user_id)

    if not settings.get("setup_done"):
        await update.message.reply_text(
            "👋 Добро пожаловать!\n\n"
            "Я помогу вам управлять арендой, таргет-проектами и личными финансами.\n\n"
            "Давайте выполним быструю настройку.\n\n"
            "💱 *Шаг 1/2:* Выберите валюту по умолчанию:",
            parse_mode="Markdown",
            reply_markup=CURRENCY_KEYBOARD,
        )
        set_state(user_id, "setup_currency", {"timezone": "Asia/Tashkent"})
    else:
        await update.message.reply_text(
            "👋 Привет! Выберите раздел:",
            reply_markup=MODULE_MENU_KEYBOARD,
        )


async def show_module_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text("Выберите раздел:", reply_markup=MODULE_MENU_KEYBOARD)


async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state, data = get_state(user_id)

    if state == "setup_currency":
        parts = query.data.replace("cur_", "").split("_")
        currency, symbol = parts[0], parts[1]
        data["currency"] = currency
        data["symbol"] = symbol
        await query.edit_message_text(
            f"✅ Валюта установлена: *{symbol} {currency}*\n\n"
            "🏠 *Шаг 2/2:* Хотите добавить первый объект аренды сейчас?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Да, добавить", callback_data="setup_add_object"),
                    InlineKeyboardButton("Пропустить", callback_data="setup_skip"),
                ]
            ]),
        )
        set_state(user_id, "setup_done_confirm", data)

    elif state == "setup_done_confirm":
        d = data
        save_user_settings(
            user_id,
            timezone=d.get("timezone", "Asia/Tashkent"),
            currency=d.get("currency", "USD"),
            symbol=d.get("symbol", "$"),
            setup_done=1,
        )
        if query.data == "setup_add_object":
            clear_state(user_id)
            await query.edit_message_text(
                "✅ Настройка завершена! Добавим первый объект.\n\n"
                "🏠 *Название объекта* (например: Квартира 1, Офис на Ленина):",
                parse_mode="Markdown",
            )
            set_state(user_id, "add_object_name", {})
        else:
            clear_state(user_id)
            await query.edit_message_text(
                "✅ Настройка завершена!\n\nВыберите раздел:",
                reply_markup=MODULE_MENU_KEYBOARD,
            )


async def setup_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)

    if state == "setup_timezone_custom":
        tz = update.message.text.strip()
        data["timezone"] = tz
        await update.message.reply_text(
            f"✅ Часовой пояс установлен: *{tz}*\n\n"
            "💱 *Шаг 2/3:* Выберите валюту по умолчанию:",
            parse_mode="Markdown",
            reply_markup=CURRENCY_KEYBOARD,
        )
        set_state(user_id, "setup_currency", data)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_state(user_id)
    save_user_settings(user_id, setup_done=0)
    await update.message.reply_text(
        "🔄 *Сброс настроек выполнен.*\n\n"
        "Все настройки сброшены. Запускаю мастер настройки заново...",
        parse_mode="Markdown",
    )
    await start_command(update, context)

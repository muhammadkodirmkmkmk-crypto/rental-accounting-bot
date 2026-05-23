import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_user_settings, save_user_settings, clear_state, set_state, get_state
import config

logger = logging.getLogger(__name__)

MAIN_MENU_KEYBOARD = InlineKeyboardMarkup([
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    clear_state(user_id)

    if not settings.get("setup_done"):
        await update.message.reply_text(
            "👋 Добро пожаловать в *бот учёта аренды недвижимости*!\n\n"
            "Я помогу вам управлять платежами, расходами и отчётами по вашим объектам.\n\n"
            "Давайте выполним быструю настройку.\n\n"
            "💱 *Шаг 1/2:* Выберите валюту по умолчанию:",
            parse_mode="Markdown",
            reply_markup=CURRENCY_KEYBOARD,
        )
        set_state(user_id, "setup_currency", {"timezone": "Asia/Tashkent"})
    else:
        sym = settings.get("symbol", "$")
        tz = settings.get("timezone", "UTC")
        await update.message.reply_text(
            f"👋 С возвращением!\n\n"
            f"🌍 Часовой пояс: *{tz}* | 💱 Валюта: *{sym}*\n\n"
            "Что хотите сделать?",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU_KEYBOARD,
        )


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
            "🏠 *Шаг 2/2:* Хотите добавить первый объект сейчас?",
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
                "✅ Настройка завершена! Можно начинать работу.\n\n"
                "Что хотите сделать?",
                reply_markup=MAIN_MENU_KEYBOARD,
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
        "Все настройки аккаунта сброшены. Запускаю мастер настройки заново...",
        parse_mode="Markdown",
    )
    await start_command(update, context)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return MAIN_MENU_KEYBOARD

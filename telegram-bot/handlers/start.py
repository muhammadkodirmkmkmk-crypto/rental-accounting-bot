"""
Start command — pure conversational greeting, no setup wizard.
Auto-configures USD + Asia/Tashkent for new users on first /start.
"""
import logging
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_user_settings, save_user_settings, clear_state
import config

logger = logging.getLogger(__name__)

# ── Stub keyboards kept for compatibility (not used in responses) ──
MODULE_MENU_KEYBOARD = InlineKeyboardMarkup([])
RENTAL_MENU_KEYBOARD = InlineKeyboardMarkup([])
TARGETING_MENU_KEYBOARD = InlineKeyboardMarkup([])
PERSONAL_MENU_KEYBOARD = InlineKeyboardMarkup([])


def module_menu_keyboard() -> InlineKeyboardMarkup:
    return MODULE_MENU_KEYBOARD


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return RENTAL_MENU_KEYBOARD


def targeting_menu_keyboard() -> InlineKeyboardMarkup:
    return TARGETING_MENU_KEYBOARD


def personal_menu_keyboard() -> InlineKeyboardMarkup:
    return PERSONAL_MENU_KEYBOARD


GREETING = (
    "Амирхон ака, ассалому алайкум! 👋\n\n"
    "Я ваш личный ассистент. Просто говорите или пишите что нужно сделать — я всё пойму.\n\n"
    "Примеры:\n"
    "• «Офис заплатил 350 долларов»\n"
    "• «Запиши новый объект»\n"
    "• «Напомни через 30 минут позвонить Алишеру»\n"
    "• «Покажи отчёт за май»\n"
    "• «Каждый понедельник в 9 — планёрка»\n\n"
    "Голосовые сообщения тоже работают 🎙"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_state(user_id)

    settings = get_user_settings(user_id)
    if not settings.get("setup_done"):
        # Auto-configure with defaults — no wizard needed
        save_user_settings(
            user_id,
            timezone="Asia/Tashkent",
            currency="USD",
            symbol="$",
            setup_done=1,
        )
        logger.info("Auto-configured new user %d with USD / Asia/Tashkent", user_id)

    await update.message.reply_text(GREETING)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_state(user_id)
    save_user_settings(user_id, setup_done=0)
    await update.message.reply_text("🔄 Настройки сброшены.")
    await start_command(update, context)


# ── Legacy stubs (no longer active, kept to avoid import errors) ──

async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()


async def setup_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pass


async def show_module_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(GREETING)

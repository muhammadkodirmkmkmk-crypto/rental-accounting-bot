import logging
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
import nlp
import analytics
from database import get_state, get_user_settings
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)


async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None) -> None:
    user_id = update.effective_user.id
    state, _ = get_state(user_id)

    if state and text is None:
        return

    msg_text = text or update.message.text.strip()
    known_objects = [o.get("name", "") for o in sheets.get_objects()]
    parsed = nlp.parse_free_text(msg_text, known_objects)

    if not parsed:
        reply_msg = update.message
        await reply_msg.reply_text(
            "Не понял команду. Используйте меню или голосовое сообщение.\n\n"
            "Примеры команд:\n"
            "• /record_payment — записать платёж\n"
            "• /record_expense — записать расход\n"
            "• /report — отчёт\n"
            "• /summary — сводка",
            reply_markup=main_menu_keyboard(),
        )
        return

    intent = parsed.get("intent")
    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    reply_msg = update.message

    if intent == "record_payment":
        obj_name = parsed.get("object_name")
        amount = parsed.get("amount")

        if not obj_name or not amount:
            await reply_msg.reply_text(
                "Понял, что речь о платеже, но нужно указать объект и сумму.\n"
                "Пример: \"Квартира 1 заплатила 500\" или /record_payment",
                reply_markup=main_menu_keyboard(),
            )
            return

        objects = sheets.get_objects()
        obj = next(
            (o for o in objects if obj_name.lower() in o.get("name", "").lower()),
            None,
        )
        if not obj:
            await reply_msg.reply_text(
                f"Объект «{obj_name}» не найден. Используйте /objects для просмотра списка.",
                reply_markup=main_menu_keyboard(),
            )
            return

        data = {
            "object_id": obj.get("id"),
            "object_name": obj.get("name"),
            "expected_amount": obj.get("rent_amount"),
            "received_amount": amount,
            "note": "Записано через текст/голос",
            "date": date.today().strftime("%d.%m.%Y"),
        }
        ok = sheets.record_payment(data)
        diff = amount - float(obj.get("rent_amount", 0))
        diff_text = ""
        if diff < 0:
            diff_text = f"\n⚠️ Недоплата: {sym}{abs(diff):.2f}"
        await reply_msg.reply_text(
            f"{'✅ Платёж записан!' if ok else '⚠️ Сохранено локально.'}\n\n"
            f"🏠 {obj.get('name')}: {sym}{amount:.2f}{diff_text}\n"
            f"📅 {data['date']}",
            reply_markup=main_menu_keyboard(),
        )

    elif intent == "record_expense":
        obj_name = parsed.get("object_name")
        amount = parsed.get("amount")
        category = parsed.get("category", "other")

        if not amount:
            await reply_msg.reply_text(
                "Понял, что речь о расходе, но нужно указать сумму.\n"
                "Пример: \"Ремонт в офисе 150$\" или /record_expense",
                reply_markup=main_menu_keyboard(),
            )
            return

        objects = sheets.get_objects()
        obj = None
        if obj_name:
            obj = next(
                (o for o in objects if obj_name.lower() in o.get("name", "").lower()),
                None,
            )

        data = {
            "object_id": obj.get("id") if obj else "general",
            "object_name": obj.get("name") if obj else "Общие",
            "category": category,
            "amount": amount,
            "description": "Записано через текст/голос",
            "date": date.today().strftime("%d.%m.%Y"),
        }
        ok = sheets.record_expense(data)
        await reply_msg.reply_text(
            f"{'✅ Расход записан!' if ok else '⚠️ Сохранено локально.'}\n\n"
            f"📂 {category}: {sym}{amount:.2f}\n"
            f"🏠 {data['object_name']}\n"
            f"📅 {data['date']}",
            reply_markup=main_menu_keyboard(),
        )

    elif intent == "report":
        month = parsed.get("month")
        year = parsed.get("year")
        report = analytics.build_monthly_report(year, month, sym)
        await reply_msg.reply_text(report, reply_markup=main_menu_keyboard())

    else:
        await reply_msg.reply_text(
            "Используйте меню для навигации:",
            reply_markup=main_menu_keyboard(),
        )

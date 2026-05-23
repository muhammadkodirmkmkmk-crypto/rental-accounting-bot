import asyncio
import logging
import re
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
import nlp
import analytics
from database import get_state, get_user_settings, clear_state, save_user_settings
from handlers.start import main_menu_keyboard, module_menu_keyboard

logger = logging.getLogger(__name__)

GREETING_PATTERN = re.compile(
    r"^(привет|прив|салом|hello|hi|hey|хай|здравствуй|добрый день|добрый вечер|доброе утро|меню|menu|старт|start)[\s,!.]*$",
    re.IGNORECASE,
)


async def free_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None
) -> None:
    user_id = update.effective_user.id
    state, _ = get_state(user_id)

    msg_text = text or (update.message.text.strip() if update.message and update.message.text else "")

    # ── /delete confirmation — must run BEFORE any early-return guard ──
    if state == "confirm_delete":
        if msg_text.upper() in ("ДА", "DA", "YES"):
            await update.message.reply_text("Амирхон ака, удаляю все данные... 🗑")
            ok = await asyncio.to_thread(sheets.clear_all_data)
            clear_state(user_id)
            save_user_settings(user_id, setup_done=0)
            from handlers.start import start_command
            await update.message.reply_text(
                f"{'Амирхон ака, все данные удалены ✅' if ok else 'Амирхон ака, частично удалено ⚠️ (ошибка Sheets).'}\n\n"
                "Запускаю мастер настройки заново..."
            )
            await start_command(update, context)
        else:
            clear_state(user_id)
            await update.message.reply_text(
                "Амирхон ака, удаление отменено ❌",
                reply_markup=module_menu_keyboard(),
            )
        return

    # ── Skip NLP/greeting if we're inside a known wizard state ──
    if state and text is None:
        return

    # ── Greeting → module menu ────────────────────────────────
    if GREETING_PATTERN.match(msg_text):
        settings = get_user_settings(user_id)
        if not settings.get("setup_done"):
            from handlers.start import start_command
            await start_command(update, context)
        else:
            await update.message.reply_text(
                "Амирхон ака, ассалому алайкум! 👋\n\nЧем могу помочь?",
                reply_markup=module_menu_keyboard(),
            )
        return

    # ── NLP free text ─────────────────────────────────────────
    objects = await asyncio.to_thread(sheets.get_objects)
    known_objects = [o.get("name", "") for o in objects]
    parsed = nlp.parse_free_text(msg_text, known_objects)

    if not parsed:
        await update.message.reply_text(
            "Амирхон ака, не понял команду. Используйте меню или голосовое сообщение.\n\n"
            "Примеры:\n"
            "• «Квартира 1 заплатила 500»\n"
            "• «Расход ремонт 150$»\n"
            "• «Отчёт за март»",
            reply_markup=module_menu_keyboard(),
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
                "Пример: «Квартира 1 заплатила 500» или /record_payment",
                reply_markup=main_menu_keyboard(),
            )
            return

        obj = next(
            (o for o in objects if obj_name.lower() in o.get("name", "").lower()),
            None,
        )
        if not obj:
            await reply_msg.reply_text(
                f"Объект «{obj_name}» не найден. Используйте /objects для просмотра.",
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
        ok = await asyncio.to_thread(sheets.record_payment, data)
        diff = amount - float(obj.get("rent_amount", 0))
        diff_text = f"\n⚠️ Недоплата: {sym}{abs(diff):.2f}" if diff < 0 else ""
        await reply_msg.reply_text(
            f"{'Амирхон ака, платёж записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
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
                "Понял расход, но нужна сумма.\nПример: «Ремонт в офисе 150$»",
                reply_markup=main_menu_keyboard(),
            )
            return

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
        ok = await asyncio.to_thread(sheets.record_expense, data)
        await reply_msg.reply_text(
            f"{'Амирхон ака, расход записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"📂 {category}: {sym}{amount:.2f}\n"
            f"🏠 {data['object_name']}\n"
            f"📅 {data['date']}",
            reply_markup=main_menu_keyboard(),
        )

    elif intent == "report":
        month = parsed.get("month")
        year = parsed.get("year")
        report = await asyncio.to_thread(analytics.build_monthly_report, year, month, sym)
        await reply_msg.reply_text(report, reply_markup=main_menu_keyboard())

    else:
        await reply_msg.reply_text(
            "Используйте меню для навигации:",
            reply_markup=module_menu_keyboard(),
        )

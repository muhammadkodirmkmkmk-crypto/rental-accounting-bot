import asyncio
import logging
import re
from datetime import datetime
import pytz
from telegram import Bot, Update
from telegram.ext import ContextTypes

import sheets
import analytics
import claude_ai
import scheduler as sched
from database import get_state, get_user_settings, clear_state, save_user_settings
from handlers.start import main_menu_keyboard, module_menu_keyboard, targeting_menu_keyboard

logger = logging.getLogger(__name__)

_TZ = pytz.timezone("Asia/Tashkent")

GREETING_PATTERN = re.compile(
    r"^(привет|прив|салом|hello|hi|hey|хай|здравствуй|добрый день|добрый вечер|доброе утро|меню|menu|старт|start)[\s,!.]*$",
    re.IGNORECASE,
)

# Month name → number for show_report period parsing
_MONTH_MAP = {
    "jan": 1, "january": 1, "январь": 1, "января": 1, "янв": 1,
    "feb": 2, "february": 2, "февраль": 2, "февраля": 2, "фев": 2,
    "mar": 3, "march": 3, "март": 3, "марта": 3,
    "apr": 4, "april": 4, "апрель": 4, "апреля": 4, "апр": 4,
    "may": 5, "май": 5, "мая": 5,
    "jun": 6, "june": 6, "июнь": 6, "июня": 6,
    "jul": 7, "july": 7, "июль": 7, "июля": 7,
    "aug": 8, "august": 8, "август": 8, "августа": 8, "авг": 8,
    "sep": 9, "september": 9, "сентябрь": 9, "сентября": 9, "сен": 9,
    "oct": 10, "october": 10, "октябрь": 10, "октября": 10, "окт": 10,
    "nov": 11, "november": 11, "ноябрь": 11, "ноября": 11, "ноя": 11,
    "dec": 12, "december": 12, "декабрь": 12, "декабря": 12, "дек": 12,
}


def _parse_period(period: str) -> tuple[int, int]:
    """Parse period string like 'may_2026' or 'may2026' → (month, year)."""
    now = datetime.now(_TZ)
    period = period.lower().replace("-", "_").replace(" ", "_")
    # Extract year
    year_m = re.search(r"(20\d{2})", period)
    year = int(year_m.group(1)) if year_m else now.year
    # Extract month name
    for name, num in _MONTH_MAP.items():
        if name in period:
            return num, year
    # Fallback: look for month number
    num_m = re.search(r"_(\d{1,2})_", f"_{period}_")
    if num_m:
        m = int(num_m.group(1))
        if 1 <= m <= 12:
            return m, year
    return now.month, year


async def _get_context() -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch objects, clients, and current-month payments in parallel."""
    now = datetime.now(_TZ)
    objects, clients, payments = await asyncio.gather(
        asyncio.to_thread(sheets.get_objects),
        asyncio.to_thread(sheets.get_target_clients),
        asyncio.to_thread(sheets.get_payments_for_month, now.year, now.month),
    )
    return objects, clients, payments


async def _dispatch_action(
    action_data: dict,
    objects: list[dict],
    clients: list[dict],
    reply_msg,
    settings: dict,
    bot: Bot,
    user_id: int,
) -> None:
    """Route Claude's action to the correct handler."""
    action = action_data.get("action", "reply")
    sym = settings.get("symbol", "$")
    now = datetime.now(_TZ)
    today_str = now.strftime("%d.%m.%Y")

    # ── Simple text reply ──────────────────────────────────────
    if action == "reply":
        text = action_data.get("text", "Амирхон ака, чем могу помочь?")
        await reply_msg.reply_text(text, reply_markup=module_menu_keyboard())

    # ── Record rental payment ──────────────────────────────────
    elif action == "record_payment":
        obj_name = str(action_data.get("object") or action_data.get("object_name") or "")
        amount = action_data.get("amount")

        if not obj_name or not amount:
            await reply_msg.reply_text(
                "Амирхон ака, уточните объект и сумму платежа.",
                reply_markup=main_menu_keyboard(),
            )
            return

        obj = _find_object(objects, obj_name)
        if not obj:
            names = "\n".join(f"• {o.get('name')}" for o in objects) or "(нет объектов)"
            await reply_msg.reply_text(
                f"Амирхон ака, объект «{obj_name}» не найден.\n\nДоступные:\n{names}",
                reply_markup=main_menu_keyboard(),
            )
            return

        data = {
            "object_id": obj.get("id"),
            "object_name": obj.get("name"),
            "expected_amount": obj.get("rent_amount", 0),
            "received_amount": amount,
            "note": "Записано через AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_payment, data)
        diff = float(amount) - float(obj.get("rent_amount", 0))
        diff_text = (f"\n⚠️ Недоплата: {sym}{abs(diff):.0f}" if diff < 0 else
                     f"\n➕ Переплата: {sym}{diff:.0f}" if diff > 0 else "")
        await reply_msg.reply_text(
            f"{'Амирхон ака, платёж записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🏠 {obj.get('name')}: {sym}{float(amount):.0f}{diff_text}\n"
            f"📅 {today_str}",
            reply_markup=main_menu_keyboard(),
        )

    # ── Record rental expense ──────────────────────────────────
    elif action == "record_expense":
        obj_name = action_data.get("object") or action_data.get("object_name")
        amount = action_data.get("amount")
        category = action_data.get("category", "прочее")

        if not amount:
            await reply_msg.reply_text(
                "Амирхон ака, уточните сумму расхода.",
                reply_markup=main_menu_keyboard(),
            )
            return

        obj = _find_object(objects, obj_name) if obj_name and obj_name != "null" else None
        data = {
            "object_id": obj.get("id") if obj else "general",
            "object_name": obj.get("name") if obj else "Общие",
            "category": category,
            "amount": amount,
            "description": "Записано через AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_expense, data)
        await reply_msg.reply_text(
            f"{'Амирхон ака, расход записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🏠 {data['object_name']}\n"
            f"📂 {category}: {sym}{float(amount):.0f}\n"
            f"📅 {today_str}",
            reply_markup=main_menu_keyboard(),
        )

    # ── Add rental object ──────────────────────────────────────
    elif action == "add_object":
        name = action_data.get("name", "")
        rent = action_data.get("rent") or action_data.get("rent_amount")

        if not name or not rent:
            await reply_msg.reply_text(
                "Амирхон ака, уточните название объекта и сумму аренды.",
                reply_markup=main_menu_keyboard(),
            )
            return

        data = {
            "name": name,
            "address": action_data.get("address", ""),
            "tenant_name": action_data.get("tenant_name", ""),
            "tenant_phone": action_data.get("tenant_phone", ""),
            "rent_amount": rent,
            "payment_day": action_data.get("payment_day", 1),
            "lease_start": today_str,
            "lease_end": action_data.get("lease_end", ""),
            "status": "rented",
        }
        ok = await asyncio.to_thread(sheets.add_object, data)
        await reply_msg.reply_text(
            f"{'Амирхон ака, объект добавлен! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🏠 *{name}*\n"
            f"💰 {sym}{rent}/мес\n"
            f"📅 День оплаты: {data['payment_day']} числа",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )

    # ── Add targeting client ───────────────────────────────────
    elif action == "add_client":
        name = action_data.get("name", "")
        fee = action_data.get("fee") or action_data.get("monthly_fee")

        if not name or not fee:
            await reply_msg.reply_text(
                "Амирхон ака, уточните имя клиента и ежемесячную сумму.",
                reply_markup=targeting_menu_keyboard(),
            )
            return

        data = {
            "name": name,
            "monthly_fee": fee,
            "payment_day": action_data.get("payment_day", 1),
            "start_date": today_str,
            "status": "active",
        }
        ok = await asyncio.to_thread(sheets.add_target_client, data)
        await reply_msg.reply_text(
            f"{'Амирхон ака, клиент добавлен! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🎯 *{name}*\n"
            f"💰 {sym}{fee}/мес",
            parse_mode="Markdown",
            reply_markup=targeting_menu_keyboard(),
        )

    # ── Record targeting payment ───────────────────────────────
    elif action == "record_target_payment":
        client_name = str(action_data.get("client") or action_data.get("client_name") or "")
        amount = action_data.get("amount")

        if not client_name or not amount:
            await reply_msg.reply_text(
                "Амирхон ака, уточните имя клиента и сумму платежа.",
                reply_markup=targeting_menu_keyboard(),
            )
            return

        client = _find_client(clients, client_name)
        expected = float(client.get("monthly_fee", 0)) if client else float(amount)
        received = float(amount)
        diff = received - expected

        row_data = {
            "client_id": client.get("id", "") if client else "",
            "client_name": client.get("name", client_name) if client else client_name,
            "expected_amount": expected,
            "received_amount": received,
            "difference": diff,
            "status": "paid" if diff >= 0 else ("partial" if received > 0 else "missed"),
            "note": "Записано через AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_target_payment, row_data)
        await reply_msg.reply_text(
            f"{'Амирхон ака, платёж записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🎯 {row_data['client_name']}: {sym}{received:.0f}\n"
            f"📅 {today_str}",
            reply_markup=targeting_menu_keyboard(),
        )

    # ── Show report ────────────────────────────────────────────
    elif action in ("show_report", "get_report"):
        period = action_data.get("period", "")
        if period:
            month, year = _parse_period(period)
        else:
            month = int(action_data.get("month", now.month))
            year = int(action_data.get("year", now.year))
        report = await asyncio.to_thread(analytics.build_monthly_report, year, month, sym)
        await reply_msg.reply_text(report, reply_markup=main_menu_keyboard())

    # ── Show objects ───────────────────────────────────────────
    elif action in ("show_objects", "list_objects"):
        if not objects:
            await reply_msg.reply_text("Амирхон ака, объекты не найдены.", reply_markup=main_menu_keyboard())
            return
        lines = ["Амирхон ака, вот ваши объекты 🏠\n"]
        for o in objects:
            lines.append(
                f"🏠 *{o.get('name')}*\n"
                f"   👤 {o.get('tenant_name','—')} | 📞 {o.get('tenant_phone','—')}\n"
                f"   💰 {sym}{o.get('rent_amount',0)}/мес | 📅 {o.get('payment_day','?')} числа"
            )
        await reply_msg.reply_text("\n\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_keyboard())

    # ── Show targeting clients ─────────────────────────────────
    elif action in ("show_clients", "list_tenants", "list_clients"):
        if not clients:
            await reply_msg.reply_text("Амирхон ака, клиенты не найдены.", reply_markup=targeting_menu_keyboard())
            return
        lines = ["Амирхон ака, вот ваши клиенты таргетинга 🎯\n"]
        for c in clients:
            lines.append(
                f"🎯 *{c.get('name')}*\n"
                f"   💰 {sym}{c.get('monthly_fee',0)}/мес | 📅 {c.get('payment_day','?')} числа"
            )
        await reply_msg.reply_text("\n\n".join(lines), parse_mode="Markdown", reply_markup=targeting_menu_keyboard())

    # ── Show summary ───────────────────────────────────────────
    elif action in ("show_summary", "get_summary"):
        report = await asyncio.to_thread(analytics.build_monthly_report, now.year, now.month, sym)
        await reply_msg.reply_text(
            f"Амирхон ака, сводка за текущий месяц 📊\n\n{report}",
            reply_markup=main_menu_keyboard(),
        )

    # ── Set one-time reminder ──────────────────────────────────
    elif action == "set_reminder":
        # Support both "datetime" field and separate "date"+"time" fields
        dt_str = str(action_data.get("datetime") or "").strip()
        if not dt_str:
            date_part = str(action_data.get("date", "")).strip()
            time_part = str(action_data.get("time", "09:00")).strip()
            dt_str = f"{date_part} {time_part}".strip()

        message_text = str(action_data.get("text") or action_data.get("message", "Напоминание")).strip()
        obj_name = action_data.get("object") or action_data.get("object_name")

        if not dt_str or len(dt_str) < 10:
            await reply_msg.reply_text(
                "Амирхон ака, уточните дату и время напоминания.",
                reply_markup=module_menu_keyboard(),
            )
            return

        # Ensure HH:MM if only date given
        if len(dt_str) == 10:
            dt_str += " 09:00"

        try:
            run_dt_naive = datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M")
            run_dt = _TZ.localize(run_dt_naive)
        except ValueError:
            await reply_msg.reply_text(
                f"Амирхон ака, не смог разобрать дату «{dt_str}».\n"
                "Формат: 2026-05-25 14:30",
                reply_markup=module_menu_keyboard(),
            )
            return

        if run_dt <= datetime.now(_TZ):
            await reply_msg.reply_text(
                "Амирхон ака, это время уже прошло. Укажите будущую дату.",
                reply_markup=module_menu_keyboard(),
            )
            return

        prefix = f"🏠 {obj_name}\n" if obj_name and str(obj_name).lower() != "null" else ""
        full_message = f"⏰ *Напоминание*\n\n{prefix}📝 {message_text}"

        ok = sched.schedule_one_time_reminder(bot, user_id, run_dt, full_message)
        date_display = run_dt.strftime("%d.%m.%Y в %H:%M")
        obj_part = f"\n🏠 {obj_name}" if obj_name and str(obj_name).lower() != "null" else ""

        await reply_msg.reply_text(
            f"{'Амирхон ака, напоминание установлено ✅' if ok else 'Амирхон ака, не удалось установить напоминание ⚠️'}\n\n"
            f"📅 {date_display}{obj_part}\n"
            f"📝 {message_text}",
            reply_markup=module_menu_keyboard(),
        )

    # ── Unknown ────────────────────────────────────────────────
    else:
        await reply_msg.reply_text(
            "Амирхон ака, чем могу помочь?",
            reply_markup=module_menu_keyboard(),
        )


def _find_object(objects: list[dict], name: str) -> dict | None:
    """Fuzzy-match an object by name."""
    if not name:
        return None
    name_l = name.lower()
    # Exact match first
    for o in objects:
        if o.get("name", "").lower() == name_l:
            return o
    # Substring match
    for o in objects:
        if name_l in o.get("name", "").lower() or o.get("name", "").lower() in name_l:
            return o
    return None


def _find_client(clients: list[dict], name: str) -> dict | None:
    """Fuzzy-match a targeting client by name."""
    if not name:
        return None
    name_l = name.lower()
    for c in clients:
        if c.get("name", "").lower() == name_l:
            return c
    for c in clients:
        if name_l in c.get("name", "").lower() or c.get("name", "").lower() in name_l:
            return c
    return None


async def free_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None
) -> None:
    user_id = update.effective_user.id
    state, _ = get_state(user_id)

    msg_text = text or (
        update.message.text.strip() if update.message and update.message.text else ""
    )

    # ── /delete confirmation ──────────────────────────────────
    if state == "confirm_delete":
        if msg_text.upper() in ("ДА", "DA", "YES"):
            await update.message.reply_text("Амирхон ака, удаляю все данные... 🗑")
            ok = await asyncio.to_thread(sheets.clear_all_data)
            clear_state(user_id)
            save_user_settings(user_id, setup_done=0)
            from handlers.start import start_command
            await update.message.reply_text(
                f"{'Амирхон ака, все данные удалены ✅' if ok else 'Амирхон ака, частично удалено ⚠️'}\n\n"
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

    # ── Skip AI if inside a wizard step ─────────────────────
    if state and text is None:
        return

    # ── Greetings ─────────────────────────────────────────────
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

    # ── Claude AI ─────────────────────────────────────────────
    settings = get_user_settings(user_id)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    objects, clients, payments = await _get_context()

    result = await asyncio.to_thread(
        claude_ai.process_message,
        msg_text,
        objects,
        clients,
        payments,
    )

    await _dispatch_action(
        result,
        objects,
        clients,
        reply_msg=update.message,
        settings=settings,
        bot=context.bot,
        user_id=user_id,
    )

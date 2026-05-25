"""
Pure-conversational AI handler.
Every text or voice message → Claude AI (with conversation history) → action.
No wizards, no menus, no keyboards.
"""
import asyncio
import logging
import re
import json
from datetime import datetime
import pytz
from telegram import Bot, Update
from telegram.ext import ContextTypes

import sheets
import analytics
import claude_ai
import scheduler as sched
from database import get_state, get_user_settings, clear_state, save_user_settings
from utils import safe_float

logger = logging.getLogger(__name__)

_TZ = pytz.timezone("Asia/Tashkent")

GREETING_PATTERN = re.compile(
    r"^(привет|прив|салом|hello|hi|hey|хай|здравствуй|добрый день|добрый вечер|доброе утро|меню|menu|старт|start)[\s,!.]*$",
    re.IGNORECASE,
)

RECURRING_LABELS = {
    "none":    "разовое",
    "daily":   "ежедневно",
    "weekly":  "еженедельно",
    "monthly": "ежемесячно",
}

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

# ── Conversation history ───────────────────────────────────────
# Stored as Claude API format: [{"role": "user"/"assistant", "content": "..."}]
_history: dict[int, list[dict]] = {}
MAX_HISTORY = 20  # 10 exchanges (user+assistant pairs)


def _get_history(user_id: int) -> list[dict]:
    return list(_history.get(user_id, []))


def _add_to_history(user_id: int, role: str, content: str) -> None:
    hist = _history.setdefault(user_id, [])
    hist.append({"role": role, "content": content})
    while len(hist) > MAX_HISTORY:
        hist.pop(0)


def _clear_history(user_id: int) -> None:
    _history.pop(user_id, None)


# ── Context fetchers ───────────────────────────────────────────

def _parse_period(period: str) -> tuple[int, int]:
    now = datetime.now(_TZ)
    period = period.lower().replace("-", "_").replace(" ", "_")
    year_m = re.search(r"(20\d{2})", period)
    year = int(year_m.group(1)) if year_m else now.year
    for name, num in _MONTH_MAP.items():
        if name in period:
            return num, year
    num_m = re.search(r"_(\d{1,2})_", f"_{period}_")
    if num_m:
        m = int(num_m.group(1))
        if 1 <= m <= 12:
            return m, year
    return now.month, year


async def _get_context() -> tuple[list[dict], list[dict], list[dict]]:
    now = datetime.now(_TZ)
    objects, clients, payments = await asyncio.gather(
        asyncio.to_thread(sheets.get_objects),
        asyncio.to_thread(sheets.get_target_clients),
        asyncio.to_thread(sheets.get_payments_for_month, now.year, now.month),
    )
    return objects, clients, payments


# ── Action dispatcher ──────────────────────────────────────────

async def _dispatch_action(
    action_data: dict,
    objects: list[dict],
    clients: list[dict],
    reply_msg,
    settings: dict,
    bot: Bot,
    user_id: int,
) -> str:
    """
    Execute Claude's action and send response to user.
    Returns the bot's response text (for storing in history).
    """
    action = action_data.get("action", "reply")
    sym = settings.get("symbol", "$")
    now = datetime.now(_TZ)
    today_str = now.strftime("%d.%m.%Y")

    # ── Simple text reply ──────────────────────────────────────
    if action == "reply":
        text = action_data.get("text", "Амирхон ака, чем могу помочь?")
        await reply_msg.reply_text(text)
        return text

    # ── Record rental payment ──────────────────────────────────
    elif action == "record_payment":
        obj_name = str(action_data.get("object") or action_data.get("object_name") or "")
        amount = action_data.get("amount")

        if not obj_name or not amount:
            msg = "Амирхон ака, уточните объект и сумму платежа."
            await reply_msg.reply_text(msg)
            return msg

        obj = _find_object(objects, obj_name)
        if not obj:
            names = "\n".join(f"• {o.get('name')}" for o in objects) or "(нет объектов)"
            msg = f"Амирхон ака, объект «{obj_name}» не найден.\n\nДоступные:\n{names}"
            await reply_msg.reply_text(msg)
            return msg

        data = {
            "object_id": obj.get("id"),
            "object_name": obj.get("name"),
            "expected_amount": obj.get("rent_amount", 0),
            "received_amount": amount,
            "note": "AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_payment, data)
        diff = safe_float(amount) - safe_float(obj.get("rent_amount"))
        diff_text = (f"\n⚠️ Недоплата: {sym}{abs(diff):.0f}" if diff < 0 else
                     f"\n➕ Переплата: {sym}{diff:.0f}" if diff > 0 else "")
        msg = (
            f"✅ Амирхон ака, платёж записан!\n\n"
            f"🏠 {obj.get('name')}: {sym}{safe_float(amount):.0f}{diff_text}\n"
            f"📅 {today_str}"
        )
        if not ok:
            msg += "\n⚠️ Сохранено в очередь (нет связи с Sheets)"
        await reply_msg.reply_text(msg)
        return msg

    # ── Record rental expense ──────────────────────────────────
    elif action == "record_expense":
        obj_name = action_data.get("object") or action_data.get("object_name")
        amount = action_data.get("amount")
        category = action_data.get("category", "прочее")

        if not amount:
            msg = "Амирхон ака, уточните сумму расхода."
            await reply_msg.reply_text(msg)
            return msg

        obj = _find_object(objects, obj_name) if obj_name and str(obj_name).lower() != "null" else None
        data = {
            "object_id": obj.get("id") if obj else "general",
            "object_name": obj.get("name") if obj else "Общие",
            "category": category,
            "amount": amount,
            "description": "AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_expense, data)
        msg = (
            f"✅ Амирхон ака, расход записан!\n\n"
            f"🏠 {data['object_name']}\n"
            f"📂 {category}: {sym}{safe_float(amount):.0f}\n"
            f"📅 {today_str}"
        )
        if not ok:
            msg += "\n⚠️ Сохранено в очередь"
        await reply_msg.reply_text(msg)
        return msg

    # ── Add rental object ──────────────────────────────────────
    elif action == "add_object":
        name = action_data.get("name", "")
        rent = action_data.get("rent") or action_data.get("rent_amount")

        if not name or not rent:
            msg = "Амирхон ака, уточните название объекта и сумму аренды."
            await reply_msg.reply_text(msg)
            return msg

        data = {
            "name": name,
            "address": action_data.get("address", ""),
            "tenant_name": action_data.get("tenant_name", ""),
            "tenant_phone": action_data.get("tenant_phone", ""),
            "tenant_telegram": action_data.get("tenant_telegram", ""),
            "rent_amount": rent,
            "payment_day": action_data.get("payment_day", 1),
            "lease_start": today_str,
            "status": "rented",
        }
        ok = await asyncio.to_thread(sheets.add_object, data)
        tg = data.get("tenant_telegram", "")
        tenant_line = (
            f"👤 {data['tenant_name']}"
            + (f" — {data['tenant_phone']}" if data["tenant_phone"] else "")
            + (f" ({tg})" if tg else "")
        )
        address_line = f"📍 {data['address']}\n" if data["address"] else ""
        msg = (
            f"✅ Амирхон ака, объект сохранён!\n\n"
            f"🏠 {name}\n"
            f"{address_line}"
            f"💰 {sym}{rent}/месяц, {data['payment_day']}-го числа\n"
            f"{tenant_line if data['tenant_name'] else ''}"
        ).strip()
        if not ok:
            msg += "\n⚠️ Сохранено в очередь"
        await reply_msg.reply_text(msg)
        return msg

    # ── Add targeting client ───────────────────────────────────
    elif action == "add_client":
        name = action_data.get("name", "")
        fee = action_data.get("fee") or action_data.get("monthly_fee")

        if not name or not fee:
            msg = "Амирхон ака, уточните имя клиента и ежемесячную сумму."
            await reply_msg.reply_text(msg)
            return msg

        data = {
            "name": name,
            "monthly_fee": fee,
            "payment_day": action_data.get("payment_day", 1),
            "start_date": today_str,
            "status": "active",
        }
        ok = await asyncio.to_thread(sheets.add_target_client, data)
        msg = (
            f"✅ Амирхон ака, клиент добавлен!\n\n"
            f"🎯 {name}\n"
            f"💰 {sym}{fee}/месяц"
        )
        if not ok:
            msg += "\n⚠️ Сохранено в очередь"
        await reply_msg.reply_text(msg)
        return msg

    # ── Record targeting payment ───────────────────────────────
    elif action == "record_target_payment":
        client_name = str(action_data.get("client") or action_data.get("client_name") or "")
        amount = action_data.get("amount")

        if not client_name or not amount:
            msg = "Амирхон ака, уточните имя клиента и сумму платежа."
            await reply_msg.reply_text(msg)
            return msg

        client = _find_client(clients, client_name)
        expected = safe_float(client.get("monthly_fee")) if client else safe_float(amount)
        received = safe_float(amount)
        diff = received - expected

        row_data = {
            "client_id": client.get("id", "") if client else "",
            "client_name": client.get("name", client_name) if client else client_name,
            "expected_amount": expected,
            "received_amount": received,
            "difference": diff,
            "status": "paid" if diff >= 0 else ("partial" if received > 0 else "missed"),
            "note": "AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_target_payment, row_data)
        msg = (
            f"✅ Амирхон ака, платёж записан!\n\n"
            f"🎯 {row_data['client_name']}: {sym}{received:.0f}\n"
            f"📅 {today_str}"
        )
        if not ok:
            msg += "\n⚠️ Сохранено в очередь"
        await reply_msg.reply_text(msg)
        return msg

    # ── Show report ────────────────────────────────────────────
    elif action in ("show_report", "get_report"):
        period = action_data.get("period", "")
        if period:
            month, year = _parse_period(period)
        else:
            month = int(action_data.get("month", now.month))
            year = int(action_data.get("year", now.year))
        report = await asyncio.to_thread(analytics.build_monthly_report, year, month, sym)
        await reply_msg.reply_text(report)
        return report

    # ── Show objects ───────────────────────────────────────────
    elif action in ("show_objects", "list_objects"):
        # Filter out empty/corrupt rows
        objects = [o for o in objects if o.get("name", "").strip()]
        if not objects:
            msg = "Амирхон ака, объекты не найдены. Добавьте первый объект!"
            await reply_msg.reply_text(msg)
            return msg
        lines = ["Амирхон ака, ваши объекты 🏠\n"]
        for o in objects:
            tg = o.get("tenant_telegram", "")
            tg_part = f" {tg}" if tg else ""
            lines.append(
                f"🏠 {o.get('name')}\n"
                f"   👤 {o.get('tenant_name','—')} {o.get('tenant_phone','')}{tg_part}\n"
                f"   💰 {sym}{o.get('rent_amount',0)}/мес | {o.get('payment_day','?')} числа"
            )
        msg = "\n\n".join(lines)
        await reply_msg.reply_text(msg)
        return msg

    # ── Show targeting clients ─────────────────────────────────
    elif action in ("show_clients", "list_tenants", "list_clients"):
        if not clients:
            msg = "Амирхон ака, клиенты таргетинга не найдены."
            await reply_msg.reply_text(msg)
            return msg
        lines = ["Амирхон ака, ваши клиенты таргетинга 🎯\n"]
        for c in clients:
            lines.append(
                f"🎯 {c.get('name')}\n"
                f"   💰 {sym}{c.get('monthly_fee',0)}/мес | {c.get('payment_day','?')} числа"
            )
        msg = "\n\n".join(lines)
        await reply_msg.reply_text(msg)
        return msg

    # ── Show summary ───────────────────────────────────────────
    elif action in ("show_summary", "get_summary"):
        report = await asyncio.to_thread(analytics.build_monthly_report, now.year, now.month, sym)
        msg = f"Амирхон ака, сводка за текущий месяц 📊\n\n{report}"
        await reply_msg.reply_text(msg)
        return msg

    # ── Set reminder ───────────────────────────────────────────
    elif action == "set_reminder":
        dt_str = str(action_data.get("datetime") or "").strip()
        if not dt_str:
            date_part = str(action_data.get("date", "")).strip()
            time_part = str(action_data.get("time", "09:00")).strip()
            dt_str = f"{date_part} {time_part}".strip()

        message_text = str(action_data.get("text") or action_data.get("message", "Напоминание")).strip()
        obj_name = action_data.get("object") or action_data.get("object_name")
        recurring = str(action_data.get("recurring", "none")).strip().lower()
        if recurring not in ("none", "daily", "weekly", "monthly"):
            recurring = "none"

        if not dt_str or len(dt_str) < 10:
            msg = "Амирхон ака, уточните дату и время напоминания."
            await reply_msg.reply_text(msg)
            return msg

        if len(dt_str) == 10:
            dt_str += " 09:00"

        try:
            run_dt = _TZ.localize(datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M"))
        except ValueError:
            msg = f"Амирхон ака, не смог разобрать дату «{dt_str}».\nФормат: 2026-05-25 14:30"
            await reply_msg.reply_text(msg)
            return msg

        if recurring == "none" and run_dt <= datetime.now(_TZ):
            msg = "Амирхон ака, это время уже прошло. Укажите будущую дату."
            await reply_msg.reply_text(msg)
            return msg

        obj_prefix = f"🏠 {obj_name}\n" if obj_name and str(obj_name).lower() not in ("", "null") else ""
        fire_text = f"{obj_prefix}📝 {message_text}"

        reminder_id = await asyncio.to_thread(
            sheets.add_reminder, user_id, dt_str[:16], message_text, recurring
        )
        ok = sched.schedule_reminder(bot, user_id, reminder_id or "x", run_dt, fire_text, recurring)

        date_display = run_dt.strftime("%d.%m.%Y в %H:%M")
        rec_label = RECURRING_LABELS.get(recurring, recurring)
        obj_part = f"\n🏠 {obj_name}" if obj_name and str(obj_name).lower() not in ("", "null") else ""

        msg = (
            f"✅ Амирхон ака, напомню!\n\n"
            f"📅 {date_display}{obj_part}\n"
            f"📝 {message_text}\n"
            f"🔁 {rec_label}"
        ) if ok else (
            f"⚠️ Амирхон ака, не удалось установить напоминание.\n"
            f"📅 {date_display}\n📝 {message_text}"
        )
        await reply_msg.reply_text(msg)
        return msg

    # ── Unknown / fallback ─────────────────────────────────────
    else:
        msg = "Амирхон ака, чем могу помочь?"
        await reply_msg.reply_text(msg)
        return msg


def _find_object(objects: list[dict], name: str) -> dict | None:
    if not name:
        return None
    name_l = str(name).lower()
    for o in objects:
        if o.get("name", "").lower() == name_l:
            return o
    for o in objects:
        if name_l in o.get("name", "").lower() or o.get("name", "").lower() in name_l:
            return o
    return None


def _find_client(clients: list[dict], name: str) -> dict | None:
    if not name:
        return None
    name_l = str(name).lower()
    for c in clients:
        if c.get("name", "").lower() == name_l:
            return c
    for c in clients:
        if name_l in c.get("name", "").lower() or c.get("name", "").lower() in name_l:
            return c
    return None


# ── Main entry point ───────────────────────────────────────────

async def free_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None
) -> None:
    user_id = update.effective_user.id
    state, _ = get_state(user_id)

    msg_text = text or (
        update.message.text.strip() if update.message and update.message.text else ""
    )

    if not msg_text:
        logger.warning("free_text_handler: empty message, skipping")
        return

    logger.info(
        "free_text_handler: user=%d state=%r source=%s msg=%r",
        user_id, state, "voice" if text else "text", msg_text[:80],
    )

    # ── /delete confirmation ───────────────────────────────────
    if state == "confirm_delete":
        if msg_text.upper() in ("ДА", "DA", "YES"):
            await update.message.reply_text("Амирхон ака, удаляю все данные... 🗑")
            ok = await asyncio.to_thread(sheets.clear_all_data)
            clear_state(user_id)
            save_user_settings(user_id, setup_done=0)
            _clear_history(user_id)
            await update.message.reply_text(
                f"{'✅ Амирхон ака, все данные удалены.' if ok else '⚠️ Амирхон ака, частично удалено.'}\n\n"
                "Напишите /start чтобы начать заново."
            )
        else:
            clear_state(user_id)
            await update.message.reply_text("Амирхон ака, удаление отменено ❌")
        return

    # ── Clear any leftover wizard state ───────────────────────
    if state:
        logger.info("Clearing leftover wizard state=%r for user=%d", state, user_id)
        clear_state(user_id)

    # ── Greetings → clear history, show welcome ────────────────
    if GREETING_PATTERN.match(msg_text):
        _clear_history(user_id)
        await update.message.reply_text(
            "Амирхон ака, ассалому алайкум! 👋\n\nЧем могу помочь? Говорите или пишите."
        )
        return

    # ── Fetch context + call Claude ───────────────────────────
    settings = get_user_settings(user_id)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    objects, clients, payments = await _get_context()
    history = _get_history(user_id)

    # Add user message to history before Claude call
    _add_to_history(user_id, "user", msg_text)

    logger.info(
        "→ Claude: user=%d history_len=%d msg=%r",
        user_id, len(history), msg_text[:80],
    )

    try:
        result = await asyncio.to_thread(
            claude_ai.process_message,
            msg_text,
            objects,
            clients,
            payments,
            history,
        )
    except Exception as e:
        logger.error("claude_ai.process_message exception: %s", e, exc_info=True)
        await update.message.reply_text("Амирхон ака, временная ошибка AI. Попробуйте ещё раз.")
        return

    logger.info(
        "← Claude: user=%d action=%r result=%r",
        user_id, result.get("action"), str(result)[:200],
    )

    # Dispatch action and get bot's response text
    bot_response = await _dispatch_action(
        result,
        objects,
        clients,
        reply_msg=update.message,
        settings=settings,
        bot=context.bot,
        user_id=user_id,
    )

    # Store assistant response in history
    # For non-reply actions, store a JSON summary so Claude knows what was executed
    if result.get("action") == "reply":
        _add_to_history(user_id, "assistant", bot_response)
    else:
        _add_to_history(user_id, "assistant", json.dumps(result, ensure_ascii=False))

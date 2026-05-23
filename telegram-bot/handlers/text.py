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
from handlers.start import main_menu_keyboard, module_menu_keyboard

logger = logging.getLogger(__name__)

_TZ = pytz.timezone("Asia/Tashkent")

GREETING_PATTERN = re.compile(
    r"^(привет|прив|салом|hello|hi|hey|хай|здравствуй|добрый день|добрый вечер|доброе утро|меню|menu|старт|start)[\s,!.]*$",
    re.IGNORECASE,
)


async def _get_context() -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch objects, clients, and current-month payments in parallel for Claude context."""
    now = datetime.now(_TZ)
    objects, clients, payments = await asyncio.gather(
        asyncio.to_thread(sheets.get_objects),
        asyncio.to_thread(sheets.get_target_clients),
        asyncio.to_thread(sheets.get_payments_for_month, now.year, now.month),
    )
    return objects, clients, payments


async def _handle_action(
    action_data: dict,
    objects: list[dict],
    reply_msg,
    settings: dict,
    bot: Bot,
    user_id: int,
) -> None:
    """Execute a structured action returned by Claude."""
    action = action_data.get("action")
    sym = settings.get("symbol", "$")
    today_str = datetime.now(_TZ).strftime("%d.%m.%Y")
    today = datetime.now(_TZ)

    # ── Record rental payment ──────────────────────────────────
    if action == "record_payment":
        obj_name = str(action_data.get("object_name") or "")
        amount = action_data.get("amount")

        if not obj_name or not amount:
            await reply_msg.reply_text(
                "Амирхон ака, уточните название объекта и сумму платежа.",
                reply_markup=main_menu_keyboard(),
            )
            return

        obj = next(
            (o for o in objects
             if obj_name.lower() in o.get("name", "").lower()
             or o.get("name", "").lower() in obj_name.lower()),
            None,
        )
        if not obj:
            names = "\n".join(f"• {o.get('name')}" for o in objects) or "(нет объектов)"
            await reply_msg.reply_text(
                f"Амирхон ака, объект «{obj_name}» не найден.\n\n"
                f"Доступные объекты:\n{names}",
                reply_markup=main_menu_keyboard(),
            )
            return

        data = {
            "object_id": obj.get("id"),
            "object_name": obj.get("name"),
            "expected_amount": obj.get("rent_amount"),
            "received_amount": amount,
            "note": "Записано через AI",
            "date": today_str,
        }
        ok = await asyncio.to_thread(sheets.record_payment, data)
        diff = float(amount) - float(obj.get("rent_amount", 0))
        if diff < 0:
            diff_text = f"\n⚠️ Недоплата: {sym}{abs(diff):.0f}"
        elif diff > 0:
            diff_text = f"\n➕ Переплата: {sym}{diff:.0f}"
        else:
            diff_text = ""
        await reply_msg.reply_text(
            f"{'Амирхон ака, платёж записан! ✅' if ok else 'Амирхон ака, сохранено локально ⚠️'}\n\n"
            f"🏠 {obj.get('name')}: {sym}{float(amount):.0f}{diff_text}\n"
            f"📅 {today_str}",
            reply_markup=main_menu_keyboard(),
        )

    # ── Record expense ─────────────────────────────────────────
    elif action == "record_expense":
        obj_name = action_data.get("object_name")
        amount = action_data.get("amount")
        category = action_data.get("category", "other")

        if not amount:
            await reply_msg.reply_text(
                "Амирхон ака, уточните сумму расхода.",
                reply_markup=main_menu_keyboard(),
            )
            return

        obj = None
        if obj_name:
            obj = next(
                (o for o in objects
                 if str(obj_name).lower() in o.get("name", "").lower()
                 or o.get("name", "").lower() in str(obj_name).lower()),
                None,
            )
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

    # ── Monthly report ─────────────────────────────────────────
    elif action == "get_report":
        month = int(action_data.get("month", today.month))
        year = int(action_data.get("year", today.year))
        report = await asyncio.to_thread(analytics.build_monthly_report, year, month, sym)
        await reply_msg.reply_text(report, reply_markup=main_menu_keyboard())

    # ── Summary ────────────────────────────────────────────────
    elif action == "get_summary":
        report = await asyncio.to_thread(
            analytics.build_monthly_report, today.year, today.month, sym
        )
        await reply_msg.reply_text(
            f"Амирхон ака, вот сводка за текущий месяц 📊\n\n{report}",
            reply_markup=main_menu_keyboard(),
        )

    # ── List objects ───────────────────────────────────────────
    elif action == "list_objects":
        if not objects:
            await reply_msg.reply_text(
                "Амирхон ака, объекты не найдены.",
                reply_markup=main_menu_keyboard(),
            )
            return
        lines = ["Амирхон ака, вот ваши объекты 🏠\n"]
        for o in objects:
            lines.append(
                f"🏠 *{o.get('name')}*\n"
                f"   👤 {o.get('tenant_name', '—')} | 📞 {o.get('tenant_phone', '—')}\n"
                f"   💰 {sym}{o.get('rent_amount', 0)}/мес | 📅 {o.get('payment_day', '?')} числа"
            )
        await reply_msg.reply_text(
            "\n\n".join(lines),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )

    # ── List tenants ───────────────────────────────────────────
    elif action == "list_tenants":
        tenants = [o for o in objects if o.get("tenant_name")]
        if not tenants:
            await reply_msg.reply_text(
                "Амирхон ака, арендаторы не найдены.",
                reply_markup=main_menu_keyboard(),
            )
            return
        lines = ["Амирхон ака, вот ваши арендаторы 👥\n"]
        for o in tenants:
            lines.append(
                f"👤 *{o.get('tenant_name')}*\n"
                f"   🏠 {o.get('name')} | 📞 {o.get('tenant_phone', '—')}\n"
                f"   💰 {sym}{o.get('rent_amount', 0)}/мес"
            )
        await reply_msg.reply_text(
            "\n\n".join(lines),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )

    # ── Set one-time reminder ──────────────────────────────────
    elif action == "set_reminder":
        date_str = str(action_data.get("date", "")).strip()
        time_str = str(action_data.get("time", "09:00")).strip()
        message_text = str(action_data.get("message", "Напоминание")).strip()
        obj_name = action_data.get("object_name")

        if not date_str:
            await reply_msg.reply_text(
                "Амирхон ака, уточните дату напоминания (например: 25 мая или 2026-05-25).",
                reply_markup=module_menu_keyboard(),
            )
            return

        # Ensure time has HH:MM format
        if ":" not in time_str:
            time_str = "09:00"

        try:
            run_dt_naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            run_dt = _TZ.localize(run_dt_naive)
        except ValueError:
            await reply_msg.reply_text(
                f"Амирхон ака, не смог разобрать дату «{date_str}» или время «{time_str}».\n"
                "Уточните, например: «завтра в 10:00» или «25 мая в 14:30».",
                reply_markup=module_menu_keyboard(),
            )
            return

        if run_dt <= today:
            await reply_msg.reply_text(
                "Амирхон ака, это время уже прошло. Укажите будущую дату.",
                reply_markup=module_menu_keyboard(),
            )
            return

        prefix = f"🏠 {obj_name}\n" if obj_name and obj_name != "null" else ""
        full_message = f"⏰ *Напоминание*\n\n{prefix}📝 {message_text}"

        ok = sched.schedule_one_time_reminder(bot, user_id, run_dt, full_message)

        date_display = run_dt.strftime("%d.%m.%Y в %H:%M")
        if ok:
            obj_part = f"\n🏠 Объект: {obj_name}" if obj_name and obj_name != "null" else ""
            await reply_msg.reply_text(
                f"Амирхон ака, напоминание установлено ✅\n\n"
                f"📅 {date_display}{obj_part}\n"
                f"📝 {message_text}",
                reply_markup=module_menu_keyboard(),
            )
        else:
            await reply_msg.reply_text(
                "Амирхон ака, не удалось установить напоминание — планировщик не запущен. "
                "Попробуйте позже.",
                reply_markup=module_menu_keyboard(),
            )

    # ── Unknown action ─────────────────────────────────────────
    else:
        await reply_msg.reply_text(
            f"Амирхон ака, не могу выполнить действие «{action}».",
            reply_markup=module_menu_keyboard(),
        )


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

    # ── Skip AI if inside an active wizard step ──────────────
    if state and text is None:
        return

    # ── Greeting ──────────────────────────────────────────────
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

    # Show typing while Claude processes
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # Fetch Sheets context and call Claude concurrently
    objects, clients, payments = await _get_context()

    result = await asyncio.to_thread(
        claude_ai.process_message,
        msg_text,
        objects,
        clients,
        payments,
    )

    reply_msg = update.message

    if result["type"] == "action":
        await _handle_action(
            result, objects, reply_msg, settings,
            bot=context.bot, user_id=user_id,
        )
    elif result["type"] == "text":
        await reply_msg.reply_text(
            result["content"],
            reply_markup=module_menu_keyboard(),
        )
    else:
        await reply_msg.reply_text(
            f"Амирхон ака, {result.get('content', 'произошла ошибка AI')}.\n\n"
            "Используйте меню или команды:",
            reply_markup=module_menu_keyboard(),
        )

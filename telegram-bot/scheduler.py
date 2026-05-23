import asyncio
import logging
from datetime import datetime, date, timedelta
import pytz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot

import analytics
import sheets
import database
import config

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_tz: pytz.BaseTzInfo = pytz.timezone("Asia/Tashkent")  # updated by start_scheduler


def _today() -> date:
    """Return today's date in the bot's configured timezone (not server UTC)."""
    return datetime.now(_tz).date()


def _get_all_user_ids() -> list[int]:
    with database.get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM user_settings WHERE setup_done = 1").fetchall()
        return [r["user_id"] for r in rows]


async def _send_to_all(bot: Bot, text: str) -> None:
    for uid in _get_all_user_ids():
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Не удалось отправить сообщение пользователю %d: %s", uid, e)


async def check_payment_reminders(bot: Bot, days_ahead: int = 1) -> None:
    """Send reminders for payments due in `days_ahead` days."""
    today = _today()
    due = await asyncio.to_thread(analytics.payments_due_in_days, days_ahead)
    for obj in due:
        sym = config.CURRENCY_SYMBOL
        payment_day = int(obj.get("payment_day", 1))
        due_date = today.replace(day=payment_day)
        if due_date < today:
            if today.month == 12:
                due_date = due_date.replace(year=today.year + 1, month=1)
            else:
                due_date = due_date.replace(month=today.month + 1)

        from handlers.objects import get_current_rent
        effective_amount = get_current_rent(obj)

        if days_ahead == 3:
            msg = (
                f"Амирхон ака, напоминаю: через 3 дня оплата ⏰\n\n"
                f"🏠 *{obj.get('name')}*\n"
                f"💰 Сумма: {sym}{effective_amount}\n"
                f"📅 Дата: {due_date.strftime('%d.%m.%Y')}\n"
                f"👤 {obj.get('tenant_name')} {obj.get('tenant_phone')}"
            )
        else:
            msg = (
                f"Амирхон ака, завтра оплата ⏰\n\n"
                f"🏠 *{obj.get('name')}*\n"
                f"💰 Сумма: {sym}{effective_amount}\n"
                f"📅 Дата: {due_date.strftime('%d.%m.%Y')}\n"
                f"👤 {obj.get('tenant_name')} {obj.get('tenant_phone')}"
            )
        await _send_to_all(bot, msg)
    if due:
        logger.info("Отправлено %d напоминаний (%d дней до оплаты)", len(due), days_ahead)


async def check_payment_day(bot: Bot) -> None:
    due_today = await asyncio.to_thread(analytics.payments_due_in_days, 0)
    for obj in due_today:
        obj_id = obj.get("id")
        from handlers.objects import get_current_rent
        effective_amount = get_current_rent(obj)
        sym = config.CURRENCY_SYMBOL
        msg = (
            f"Амирхон ака, сегодня день оплаты 💰\n\n"
            f"🏠 *{obj.get('name')}*\n"
            f"💰 Сумма: {sym}{effective_amount}\n"
            f"👤 {obj.get('tenant_name')}\n\n"
            f"Оплачено?\n"
            f"👉 /confirm_{obj_id} — Да, оплачено\n"
            f"👉 /missed_{obj_id} — Нет, не оплачено"
        )
        await _send_to_all(bot, msg)
    if due_today:
        logger.info("Отправлено %d напоминаний (день оплаты)", len(due_today))


async def check_overdue_payments(bot: Bot) -> None:
    overdue = await asyncio.to_thread(analytics.payments_overdue, 3)
    for obj in overdue:
        sym = config.CURRENCY_SYMBOL
        from handlers.objects import get_current_rent
        effective_amount = get_current_rent(obj)
        msg = (
            f"Амирхон ака, платёж просрочен на 3 дня ⚠️\n\n"
            f"🏠 *{obj.get('name')}*\n"
            f"💰 Сумма: {sym}{effective_amount}\n"
            f"👤 {obj.get('tenant_name')} {obj.get('tenant_phone')}\n\n"
            "Примите меры!"
        )
        await _send_to_all(bot, msg)


async def check_lease_expirations(bot: Bot) -> None:
    expiring = await asyncio.to_thread(analytics.leases_expiring_soon, 30)
    for obj in expiring:
        msg = (
            f"Амирхон ака, договор скоро истекает 📋\n\n"
            f"🏠 *{obj.get('name')}*\n"
            f"📅 Дата окончания: {obj.get('lease_end')} (через {obj.get('days_left')} дн.)\n"
            f"👤 {obj.get('tenant_name')}\n\n"
            "Продлите договор или найдите нового арендатора."
        )
        await _send_to_all(bot, msg)


async def send_monthly_summary(bot: Bot) -> None:
    today = _today()
    sym = config.CURRENCY_SYMBOL
    report = await asyncio.to_thread(analytics.build_monthly_report, today.year, today.month, sym)
    month_names = ["январь", "февраль", "март", "апрель", "май", "июнь",
                   "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    month_name = month_names[today.month - 1]
    await _send_to_all(bot, f"Амирхон ака, отчёт за {month_name} готов 📊\n\n{report}")
    logger.info("Месячный отчёт отправлен")


async def retry_queued_writes() -> None:
    pending = database.pop_queued_writes()
    if not pending:
        return
    logger.info("Повтор %d отложенных записей в таблицу", len(pending))
    for item in pending:
        import json
        try:
            ok = sheets.append_row(item["sheet_name"], json.loads(item["row_data"]))
            if ok:
                database.delete_queued_write(item["id"])
                logger.info("Запись %d синхронизирована с таблицей", item["id"])
            else:
                database.increment_queue_retries(item["id"])
        except Exception as e:
            logger.error("Ошибка повтора записи %d: %s", item["id"], e)
            database.increment_queue_retries(item["id"])


def start_scheduler(bot: Bot, timezone: str = "Asia/Tashkent") -> AsyncIOScheduler:
    global _scheduler, _tz
    if _scheduler and _scheduler.running:
        return _scheduler

    try:
        _tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        _tz = pytz.timezone("Asia/Tashkent")
        logger.warning("Неизвестный часовой пояс '%s', используется Asia/Tashkent", timezone)

    _scheduler = AsyncIOScheduler(timezone=_tz)

    # 3-day reminder at 09:00 Tashkent
    _scheduler.add_job(
        check_payment_reminders,
        CronTrigger(hour=9, minute=0, timezone=_tz),
        args=[bot, 3],
        id="check_payment_reminders",
        replace_existing=True,
    )
    # 1-day reminder at 10:00 Tashkent
    _scheduler.add_job(
        check_payment_reminders,
        CronTrigger(hour=10, minute=0, timezone=_tz),
        args=[bot, 1],
        id="day_before_reminder",
        replace_existing=True,
    )
    # On payment day at 11:00 Tashkent
    _scheduler.add_job(
        check_payment_day,
        CronTrigger(hour=11, minute=0, timezone=_tz),
        args=[bot],
        id="payment_day_reminder",
        replace_existing=True,
    )
    # Overdue check at 09:30 Tashkent
    _scheduler.add_job(
        check_overdue_payments,
        CronTrigger(hour=9, minute=30, timezone=_tz),
        args=[bot],
        id="overdue_reminder",
        replace_existing=True,
    )
    # Lease expiry check at 09:00 Tashkent
    _scheduler.add_job(
        check_lease_expirations,
        CronTrigger(hour=9, minute=0, timezone=_tz),
        args=[bot],
        id="lease_expiry_reminder",
        replace_existing=True,
    )
    # Monthly summary on last day at 18:00 Tashkent
    _scheduler.add_job(
        send_monthly_summary,
        CronTrigger(day="last", hour=18, minute=0, timezone=_tz),
        args=[bot],
        id="monthly_summary",
        replace_existing=True,
    )
    # Retry queued writes every N seconds
    _scheduler.add_job(
        retry_queued_writes,
        "interval",
        seconds=config.SHEETS_RETRY_INTERVAL,
        id="retry_queue",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Планировщик запущен, часовой пояс: %s", timezone)
    return _scheduler


def reschedule_reminders(bot: Bot, hour: int, day_before_hour: int) -> None:
    """Reschedule reminder jobs with updated hours. Called when user changes reminder time."""
    global _scheduler
    if not _scheduler or not _scheduler.running:
        logger.warning("Scheduler not running — cannot reschedule")
        return

    # 3-day reminder keeps its fixed 09:00 schedule (not user-configurable)
    _scheduler.reschedule_job(
        "check_payment_reminders",
        trigger=CronTrigger(hour=9, minute=0, timezone=_tz),
    )
    # 1-day reminder uses user-chosen day_before_hour
    _scheduler.reschedule_job(
        "day_before_reminder",
        trigger=CronTrigger(hour=day_before_hour, minute=0, timezone=_tz),
    )
    # Payment day notification uses user-chosen hour
    _scheduler.reschedule_job(
        "payment_day_reminder",
        trigger=CronTrigger(hour=hour, minute=0, timezone=_tz),
    )
    _scheduler.reschedule_job(
        "overdue_reminder",
        trigger=CronTrigger(hour=hour, minute=30, timezone=_tz),
    )
    _scheduler.reschedule_job(
        "lease_expiry_reminder",
        trigger=CronTrigger(hour=hour, minute=0, timezone=_tz),
    )
    logger.info("Напоминания перенастроены: основные=%d:00, за день до=%d:00", hour, day_before_hour)


def schedule_one_time_reminder(
    bot: Bot,
    user_id: int,
    run_at: "datetime",
    message_text: str,
) -> bool:
    """
    Schedule a one-time reminder for a specific user at a specific datetime (timezone-aware).
    Returns True if scheduled successfully, False if scheduler is not running.
    """
    global _scheduler
    if not _scheduler or not _scheduler.running:
        logger.warning("Scheduler not running — cannot schedule one-time reminder")
        return False

    job_id = f"reminder_{user_id}_{int(run_at.timestamp())}"

    async def _send(chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Не удалось отправить разовое напоминание %s: %s", job_id, e)

    _scheduler.add_job(
        _send,
        "date",
        run_date=run_at,
        args=[user_id, message_text],
        id=job_id,
        replace_existing=True,
    )
    logger.info("Разовое напоминание запланировано: %s → %s", job_id, run_at.isoformat())
    return True


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")


# ── Personal reminders ────────────────────────────────────────

async def _fire_reminder(
    bot: Bot,
    user_id: int,
    reminder_id: str,
    text: str,
    recurring: str,
) -> None:
    """Send a personal reminder and mark one-time reminders as sent."""
    now = datetime.now(_tz)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⏰ *Амирхон ака, напоминание!*\n\n"
                f"{text}\n\n"
                f"🕐 {now.strftime('%H:%M')}  📅 {now.strftime('%d.%m.%Y')}"
            ),
            parse_mode="Markdown",
        )
        logger.info("Reminder %s fired for user %d", reminder_id, user_id)
    except Exception as e:
        logger.error("Reminder %s send failed: %s", reminder_id, e)
        return

    if recurring == "none":
        try:
            await asyncio.to_thread(sheets.update_reminder_status, reminder_id, "sent")
        except Exception as e:
            logger.error("Mark reminder %s as sent failed: %s", reminder_id, e)


def schedule_reminder(
    bot: Bot,
    user_id: int,
    reminder_id: str,
    run_dt: datetime,
    message_text: str,
    recurring: str = "none",
) -> bool:
    """
    Schedule a personal reminder (one-time or recurring).
    run_dt must be timezone-aware (Asia/Tashkent).
    recurring: 'none' | 'daily' | 'weekly' | 'monthly'
    """
    global _scheduler
    if not _scheduler or not _scheduler.running:
        logger.warning("Scheduler not running — cannot schedule reminder %s", reminder_id)
        return False

    job_id = f"rem_{user_id}_{reminder_id}"
    args = [bot, user_id, reminder_id, message_text, recurring]

    try:
        if recurring == "none":
            _scheduler.add_job(
                _fire_reminder,
                "date",
                run_date=run_dt,
                args=args,
                id=job_id,
                replace_existing=True,
            )
        elif recurring == "daily":
            _scheduler.add_job(
                _fire_reminder,
                CronTrigger(hour=run_dt.hour, minute=run_dt.minute, timezone=_tz),
                args=args,
                id=job_id,
                replace_existing=True,
            )
        elif recurring == "weekly":
            _scheduler.add_job(
                _fire_reminder,
                CronTrigger(
                    day_of_week=run_dt.weekday(),
                    hour=run_dt.hour,
                    minute=run_dt.minute,
                    timezone=_tz,
                ),
                args=args,
                id=job_id,
                replace_existing=True,
            )
        elif recurring == "monthly":
            _scheduler.add_job(
                _fire_reminder,
                CronTrigger(
                    day=run_dt.day,
                    hour=run_dt.hour,
                    minute=run_dt.minute,
                    timezone=_tz,
                ),
                args=args,
                id=job_id,
                replace_existing=True,
            )
        else:
            logger.warning("Unknown recurring type '%s', scheduling as one-time", recurring)
            _scheduler.add_job(
                _fire_reminder,
                "date",
                run_date=run_dt,
                args=args,
                id=job_id,
                replace_existing=True,
            )

        logger.info(
            "Personal reminder scheduled: %s | recurring=%s | run_dt=%s",
            job_id, recurring, run_dt.isoformat(),
        )
        return True

    except Exception as e:
        logger.error("schedule_reminder error for %s: %s", job_id, e)
        return False


def cancel_reminder(job_id: str) -> bool:
    """Remove a scheduled reminder job. Returns True if removed."""
    global _scheduler
    if not _scheduler or not _scheduler.running:
        return False
    try:
        _scheduler.remove_job(job_id)
        logger.info("Reminder cancelled: %s", job_id)
        return True
    except Exception as e:
        logger.warning("cancel_reminder %s: %s", job_id, e)
        return False


def load_reminders_from_sheets(bot: Bot) -> None:
    """
    Restore all active reminders from Google Sheets on bot startup.
    Called synchronously from post_init (before the event loop is busy).
    """
    try:
        all_reminders = sheets.get_reminders(status="active")
        now = datetime.now(_tz)
        scheduled = 0
        expired = 0

        for r in all_reminders:
            rid = str(r.get("id", "")).strip()
            uid_str = str(r.get("user_id", "")).strip()
            dt_str = str(r.get("datetime", "")).strip()
            text = str(r.get("text", "Напоминание"))
            recurring = str(r.get("recurring", "none"))

            if not rid or not uid_str or not dt_str:
                continue
            try:
                user_id = int(uid_str)
            except ValueError:
                continue
            try:
                run_dt = _tz.localize(datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M"))
            except ValueError:
                logger.warning("Bad datetime format in reminder %s: %s", rid, dt_str)
                continue

            if recurring == "none" and run_dt <= now:
                try:
                    sheets.update_reminder_status(rid, "sent")
                except Exception:
                    pass
                expired += 1
                continue

            ok = schedule_reminder(bot, user_id, rid, run_dt, text, recurring)
            if ok:
                scheduled += 1

        logger.info(
            "Reminders restored: %d scheduled, %d expired/skipped",
            scheduled, expired,
        )
    except Exception as e:
        logger.error("load_reminders_from_sheets error: %s", e)

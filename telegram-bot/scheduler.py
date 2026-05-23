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
    due = analytics.payments_due_in_days(days_ahead)
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
    due_today = analytics.payments_due_in_days(0)
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
    overdue = analytics.payments_overdue(3)
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
    expiring = analytics.leases_expiring_soon(30)
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
    report = analytics.build_monthly_report(today.year, today.month, sym)
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
    _scheduler.add_job(
        check_overdue_payments,
        CronTrigger(hour=9, minute=30, timezone=_tz),
        args=[bot],
        id="overdue_reminder",
        replace_existing=True,
    )
    _scheduler.add_job(
        check_lease_expirations,
        CronTrigger(hour=9, minute=0, timezone=_tz),
        args=[bot],
        id="lease_expiry_reminder",
        replace_existing=True,
    )
    _scheduler.add_job(
        send_monthly_summary,
        CronTrigger(day="last", hour=18, minute=0, timezone=_tz),
        args=[bot],
        id="monthly_summary",
        replace_existing=True,
    )
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

    _scheduler.reschedule_job(
        "check_payment_reminders",
        trigger=CronTrigger(hour=day_before_hour, minute=0, timezone=_tz),
    )
    _scheduler.reschedule_job(
        "day_before_reminder",
        trigger=CronTrigger(hour=day_before_hour, minute=0, timezone=_tz),
    )
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


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")

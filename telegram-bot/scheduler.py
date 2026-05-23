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


async def check_payment_reminders(bot: Bot) -> None:
    today = date.today()
    due_tomorrow = analytics.payments_due_in_days(1)
    for obj in due_tomorrow:
        sym = config.CURRENCY_SYMBOL
        payment_day = int(obj.get("payment_day", 1))
        due_date = today.replace(day=payment_day) + timedelta(days=1)
        msg = (
            f"⏰ *Напоминание:* Аренда за *{obj.get('name')}* "
            f"(*{sym}{obj.get('rent_amount')}*) должна поступить завтра "
            f"({due_date.strftime('%d.%m.%Y')}).\n"
            f"Арендатор: {obj.get('tenant_name')} {obj.get('tenant_phone')}"
        )
        await _send_to_all(bot, msg)
    if due_tomorrow:
        logger.info("Отправлено %d напоминаний (день до оплаты)", len(due_tomorrow))


async def check_payment_day(bot: Bot) -> None:
    due_today = analytics.payments_due_in_days(0)
    for obj in due_today:
        obj_id = obj.get("id")
        msg = (
            f"💰 Сегодня день оплаты по *{obj.get('name')}*.\n"
            f"Оплатил ли {obj.get('tenant_name')}?\n\n"
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
        msg = (
            f"⚠️ Платёж по *{obj.get('name')}* просрочен на *3 дня*.\n"
            f"Сумма: {sym}{obj.get('rent_amount')}\n"
            f"Арендатор: {obj.get('tenant_name')} {obj.get('tenant_phone')}\n\n"
            "Примите меры!"
        )
        await _send_to_all(bot, msg)


async def check_lease_expirations(bot: Bot) -> None:
    expiring = analytics.leases_expiring_soon(30)
    for obj in expiring:
        msg = (
            f"📋 Договор по *{obj.get('name')}* истекает через *{obj.get('days_left')} дн.* "
            f"({obj.get('lease_end')}).\n"
            f"Арендатор: {obj.get('tenant_name')}\n\n"
            "Продлите договор или найдите нового арендатора."
        )
        await _send_to_all(bot, msg)


async def send_monthly_summary(bot: Bot) -> None:
    today = date.today()
    sym = config.CURRENCY_SYMBOL
    report = analytics.build_monthly_report(today.year, today.month, sym)
    await _send_to_all(bot, f"📊 *Автоматический месячный отчёт*\n\n{report}")
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


def start_scheduler(bot: Bot, timezone: str = "UTC") -> AsyncIOScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    try:
        tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.UTC
        logger.warning("Неизвестный часовой пояс '%s', используется UTC", timezone)

    _scheduler = AsyncIOScheduler(timezone=tz)

    _scheduler.add_job(
        check_payment_reminders,
        CronTrigger(hour=9, minute=0, timezone=tz),
        args=[bot],
        id="day_before_reminder",
        replace_existing=True,
    )
    _scheduler.add_job(
        check_payment_day,
        CronTrigger(hour=10, minute=0, timezone=tz),
        args=[bot],
        id="payment_day_reminder",
        replace_existing=True,
    )
    _scheduler.add_job(
        check_overdue_payments,
        CronTrigger(hour=9, minute=0, timezone=tz),
        args=[bot],
        id="overdue_reminder",
        replace_existing=True,
    )
    _scheduler.add_job(
        check_lease_expirations,
        CronTrigger(hour=9, minute=0, timezone=tz),
        args=[bot],
        id="lease_expiry_reminder",
        replace_existing=True,
    )
    _scheduler.add_job(
        send_monthly_summary,
        CronTrigger(day="last", hour=18, minute=0, timezone=tz),
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


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")

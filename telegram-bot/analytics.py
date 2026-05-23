from datetime import datetime, date, timedelta
from collections import defaultdict
import logging
import pytz

import sheets

logger = logging.getLogger(__name__)

_TZ = pytz.timezone("Asia/Tashkent")


def _today() -> date:
    """Return today's date in Asia/Tashkent, not server UTC."""
    return datetime.now(_TZ).date()


def payment_reliability(object_id: str) -> dict:
    all_payments = sheets.get_all_records("Payments")
    obj_payments = [p for p in all_payments if str(p.get("object_id")) == str(object_id)]
    if not obj_payments:
        return {"total": 0, "on_time_pct": 0, "avg_delay_days": 0, "late_count": 0}

    total = len(obj_payments)
    paid_on_time = sum(
        1 for p in obj_payments if p.get("status") in ("paid", "partial")
        and float(p.get("received_amount", 0)) >= float(p.get("expected_amount", 0)) * 0.95
    )
    on_time_pct = round(paid_on_time / total * 100, 1) if total else 0

    objects = sheets.get_objects()
    obj = next((o for o in objects if str(o.get("id")) == str(object_id)), {})
    payment_day = int(obj.get("payment_day", 1))

    delay_days_list = []
    for p in obj_payments:
        try:
            pay_date = datetime.strptime(str(p.get("date", "")), "%d.%m.%Y")
            expected_date = pay_date.replace(day=payment_day)
            if pay_date > expected_date:
                delay_days_list.append((pay_date - expected_date).days)
        except (ValueError, OverflowError):
            pass

    avg_delay = round(sum(delay_days_list) / len(delay_days_list), 1) if delay_days_list else 0
    late_count = sum(1 for p in obj_payments if p.get("status") in ("partial", "missed"))

    return {
        "total": total,
        "on_time_pct": on_time_pct,
        "avg_delay_days": avg_delay,
        "late_count": late_count,
    }


def tenants_with_late_pattern(threshold: int = 3) -> list[dict]:
    objects = sheets.get_objects()
    alerts = []
    for obj in objects:
        rel = payment_reliability(obj.get("id", ""))
        if rel["late_count"] >= threshold:
            alerts.append({
                "object_name": obj.get("name"),
                "tenant_name": obj.get("tenant_name"),
                "late_count": rel["late_count"],
            })
    return alerts


def monthly_income_comparison(year: int, month: int) -> dict:
    sym = _symbol()
    current = _month_income(year, month)
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    prev = _month_income(prev_year, prev_month)
    change = current - prev
    change_pct = round(change / prev * 100, 1) if prev else 0
    return {
        "current": current,
        "previous": prev,
        "change": change,
        "change_pct": change_pct,
        "symbol": sym,
    }


def _month_income(year: int, month: int) -> float:
    payments = sheets.get_payments_for_month(year, month)
    return sum(float(p.get("received_amount", 0)) for p in payments)


def occupancy_rate() -> dict:
    objects = sheets.get_objects()
    total = len(objects)
    rented = sum(1 for o in objects if o.get("status", "").lower() == "rented")
    vacant = total - rented
    rate = round(rented / total * 100, 1) if total else 0
    return {"total": total, "rented": rented, "vacant": vacant, "rate": rate}


def leases_expiring_soon(days: int = 30) -> list[dict]:
    today = _today()
    threshold = today + timedelta(days=days)
    objects = sheets.get_objects()
    expiring = []
    for obj in objects:
        try:
            lease_end_str = str(obj.get("lease_end", ""))
            if not lease_end_str:
                continue
            lease_end = datetime.strptime(lease_end_str, "%d.%m.%Y").date()
            days_left = (lease_end - today).days
            if 0 <= days_left <= days:
                expiring.append({
                    **obj,
                    "days_left": days_left,
                    "lease_end": lease_end_str,
                })
        except ValueError:
            pass
    return expiring


def payments_due_in_days(days_ahead: int) -> list[dict]:
    today = _today()
    target = today + timedelta(days=days_ahead)
    objects = sheets.get_active_objects()
    due = []
    for obj in objects:
        try:
            payment_day = int(obj.get("payment_day", 1))
            if target.day == payment_day:
                due.append(obj)
        except (ValueError, TypeError):
            pass
    return due


def payments_overdue(days_overdue: int) -> list[dict]:
    today = _today()
    objects = sheets.get_active_objects()
    overdue = []
    for obj in objects:
        try:
            payment_day = int(obj.get("payment_day", 1))
            current_month_due = today.replace(day=payment_day)
            if today > current_month_due:
                delta = (today - current_month_due).days
                if delta == days_overdue:
                    payments = sheets.get_payments_for_month(today.year, today.month)
                    obj_paid = any(
                        str(p.get("object_id")) == str(obj.get("id"))
                        for p in payments
                        if p.get("status") in ("paid", "partial")
                    )
                    if not obj_paid:
                        overdue.append({**obj, "days_overdue": delta})
        except (ValueError, TypeError):
            pass
    return overdue


MONTH_NAMES_RU = {
    1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ",
    5: "МАЙ", 6: "ИЮНЬ", 7: "ИЮЛЬ", 8: "АВГУСТ",
    9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ",
}


def build_monthly_report(year: int, month: int, symbol: str = "$") -> str:
    month_name = f"{MONTH_NAMES_RU.get(month, str(month))} {year}"
    objects = sheets.get_objects()
    payments = sheets.get_payments_for_month(year, month)
    expenses = sheets.get_expenses_for_month(year, month)

    lines = [f"📊 ОТЧЁТ {month_name}", "━" * 18]

    total_received = 0.0
    total_expected = 0.0
    total_expenses = sum(float(e.get("amount", 0)) for e in expenses)

    for obj in objects:
        obj_id = str(obj.get("id", ""))
        obj_payments = [p for p in payments if str(p.get("object_id")) == obj_id]
        if obj_payments:
            p = obj_payments[-1]
            received = float(p.get("received_amount", 0))
            expected = float(p.get("expected_amount", 0))
            diff = received - expected
            total_received += received
            total_expected += expected
            tenant = obj.get("tenant_name", "?")
            if diff < 0:
                lines.append(
                    f"🏠 {obj.get('name')} (Арендатор: {tenant}): "
                    f"{symbol}{received:.0f} ✅ (ожидалось {symbol}{expected:.0f}, разница: {symbol}{diff:.0f})"
                )
            else:
                lines.append(
                    f"🏠 {obj.get('name')} (Арендатор: {tenant}): {symbol}{received:.0f} ✅"
                )
        else:
            expected = float(obj.get("rent_amount", 0))
            total_expected += expected
            if obj.get("status", "").lower() == "rented":
                lines.append(f"🏠 {obj.get('name')}: ❌ НЕ ОПЛАЧЕНО")
            else:
                lines.append(f"🏠 {obj.get('name')}: 🔲 ВАКАНТНО")

    net = total_received - total_expenses
    collection_rate = round(total_received / total_expected * 100, 1) if total_expected else 0

    lines += [
        "━" * 18,
        f"💰 Доход: {symbol}{total_received:.0f} / {symbol}{total_expected:.0f} ожидалось",
        f"🔧 Расходы: {symbol}{total_expenses:.0f}",
        f"📈 Чистая прибыль: {symbol}{net:.0f}",
        f"📉 Собираемость: {collection_rate}%",
    ]

    comp = monthly_income_comparison(year, month)
    arrow = "📈" if comp["change"] >= 0 else "📉"
    lines.append(
        f"{arrow} По сравнению с прошлым месяцем: {'+' if comp['change'] >= 0 else ''}"
        f"{symbol}{comp['change']:.0f} ({comp['change_pct']:+.1f}%)"
    )

    occ = occupancy_rate()
    lines.append(f"🏠 Заполняемость: {occ['rate']}% ({occ['rented']}/{occ['total']} объектов)")

    return "\n".join(lines)


def _symbol() -> str:
    import config
    return config.CURRENCY_SYMBOL

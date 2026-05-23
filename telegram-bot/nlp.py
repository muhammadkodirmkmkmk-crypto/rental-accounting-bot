"""Simple bilingual keyword-based NLP for Russian and English input."""
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MONTH_MAP = {
    "january": 1, "jan": 1, "январь": 1, "января": 1, "янв": 1,
    "february": 2, "feb": 2, "февраль": 2, "февраля": 2, "фев": 2,
    "march": 3, "mar": 3, "март": 3, "марта": 3, "мар": 3,
    "april": 4, "apr": 4, "апрель": 4, "апреля": 4, "апр": 4,
    "may": 5, "май": 5, "мая": 5,
    "june": 6, "jun": 6, "июнь": 6, "июня": 6,
    "july": 7, "jul": 7, "июль": 7, "июля": 7,
    "august": 8, "aug": 8, "август": 8, "августа": 8, "авг": 8,
    "september": 9, "sep": 9, "сентябрь": 9, "сентября": 9, "сен": 9,
    "october": 10, "oct": 10, "октябрь": 10, "октября": 10, "окт": 10,
    "november": 11, "nov": 11, "ноябрь": 11, "ноября": 11, "ноя": 11,
    "december": 12, "dec": 12, "декабрь": 12, "декабря": 12, "дек": 12,
}

PAYMENT_KEYWORDS = [
    "paid", "payment", "заплатила", "заплатил", "оплатил", "оплатила",
    "платёж", "платеж", "внёс", "внес", "погасил",
]

EXPENSE_KEYWORDS = [
    "expense", "расход", "расходы", "трата", "потратил", "потратила",
    "записать расход", "затрата",
]

REPORT_KEYWORDS = [
    "report", "отчёт", "отчет", "отчёта", "отчета",
]

OBJECT_KEYWORDS = [
    "квартира", "apt", "apartment", "офис", "office", "studio", "студия",
    "комната", "room", "дом", "house",
]

EXPENSE_CATEGORIES = [
    "ремонт", "repair", "maintenance",
    "коммунальные", "utilities",
    "налог", "tax",
    "страховка", "insurance",
    "управление", "management",
    "реклама", "advertising",
    "прочее", "other",
]


def extract_amount(text: str) -> float | None:
    patterns = [
        r"(\d[\d\s,]*\.?\d*)\s*(?:долларов|доллара|доллар|\$|usd|€|евро|руб|₽)",
        r"(?:\$|€|₽)\s*(\d[\d\s,]*\.?\d*)",
        r"(\d[\d\s,]*\.?\d*)\s*(?:тыс|k|к)\b",
        r"\b(\d[\d\s,]*\.?\d*)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(" ", "").replace(",", ".")
            try:
                val = float(raw)
                if "тыс" in text.lower() or re.search(r"\b[kк]\b", text, re.IGNORECASE):
                    val *= 1000
                return val
            except ValueError:
                pass
    return None


def extract_month_year(text: str) -> tuple[int, int] | None:
    text_lower = text.lower()
    for month_str, month_num in MONTH_MAP.items():
        if month_str in text_lower:
            year_match = re.search(r"\b(20\d{2})\b", text)
            year = int(year_match.group(1)) if year_match else datetime.now().year
            return month_num, year
    return None


def extract_object_name(text: str, known_objects: list[str]) -> str | None:
    text_lower = text.lower()
    for name in known_objects:
        if name.lower() in text_lower:
            return name
    for kw in OBJECT_KEYWORDS:
        m = re.search(rf"{kw}\s*(\d+)", text_lower)
        if m:
            return f"{kw.capitalize()} {m.group(1)}"
    return None


def extract_expense_category(text: str) -> str:
    text_lower = text.lower()
    for cat in EXPENSE_CATEGORIES:
        if cat in text_lower:
            return cat
    return "other"


def classify_intent(text: str) -> str | None:
    text_lower = text.lower()
    if any(kw in text_lower for kw in PAYMENT_KEYWORDS):
        return "record_payment"
    if any(kw in text_lower for kw in EXPENSE_KEYWORDS):
        return "record_expense"
    if any(kw in text_lower for kw in REPORT_KEYWORDS):
        return "report"
    return None


def parse_free_text(text: str, known_objects: list[str]) -> dict | None:
    intent = classify_intent(text)
    if not intent:
        return None

    result: dict = {"intent": intent}

    obj_name = extract_object_name(text, known_objects)
    if obj_name:
        result["object_name"] = obj_name

    amount = extract_amount(text)
    if amount is not None:
        result["amount"] = amount

    if intent == "report":
        month_year = extract_month_year(text)
        if month_year:
            result["month"], result["year"] = month_year
        else:
            now = datetime.now()
            result["month"] = now.month
            result["year"] = now.year

    if intent == "record_expense":
        result["category"] = extract_expense_category(text)

    return result

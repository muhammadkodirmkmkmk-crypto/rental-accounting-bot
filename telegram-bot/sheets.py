import logging
import json
import time
from datetime import datetime, date
from typing import Any

import gspread
from gspread.exceptions import APIError
from oauth2client.service_account import ServiceAccountCredentials

import config
from database import enqueue_sheet_write

logger = logging.getLogger(__name__)

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADERS = {
    "Objects": [
        "id", "name", "address", "tenant_name", "tenant_phone",
        "rent_amount", "currency", "payment_day", "lease_start", "lease_end", "status",
    ],
    "Payments": [
        "date", "object_id", "object_name", "expected_amount",
        "received_amount", "difference", "status", "note",
    ],
    "Expenses": ["date", "object_id", "category", "amount", "description"],
    "Summary": ["month", "object", "income", "expenses", "net_profit", "occupancy_rate"],
}

_gc: gspread.Client | None = None
_spreadsheet: gspread.Spreadsheet | None = None


def _get_client() -> gspread.Client:
    global _gc
    if _gc is None:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            config.GOOGLE_CREDS_DICT, SCOPE
        )
        _gc = gspread.authorize(creds)
    return _gc


def _get_spreadsheet() -> gspread.Spreadsheet:
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = _get_client().open_by_key(config.SPREADSHEET_ID)
    return _spreadsheet


def _get_or_create_sheet(name: str) -> gspread.Worksheet:
    ss = _get_spreadsheet()
    try:
        ws = ss.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=name, rows=1000, cols=20)
        ws.append_row(SHEET_HEADERS[name])
        logger.info("Created sheet '%s'", name)
    return ws


def init_sheets() -> None:
    for name in SHEET_HEADERS:
        _get_or_create_sheet(name)
    logger.info("All sheets initialised")


def _retry_write(fn, *args, retries: int = 3, delay: float = 2.0) -> Any:
    for attempt in range(retries):
        try:
            return fn(*args)
        except APIError as e:
            logger.warning("Sheets API error (attempt %d): %s", attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise


def append_row(sheet_name: str, row: list) -> bool:
    try:
        ws = _get_or_create_sheet(sheet_name)
        _retry_write(ws.append_row, row)
        return True
    except Exception as e:
        logger.error("Failed to append row to '%s': %s", sheet_name, e)
        enqueue_sheet_write(sheet_name, row)
        return False


def get_all_records(sheet_name: str) -> list[dict]:
    try:
        ws = _get_or_create_sheet(sheet_name)
        return ws.get_all_records()
    except Exception as e:
        logger.error("Failed to read '%s': %s", sheet_name, e)
        return []


def update_cell_by_key(sheet_name: str, key_col: str, key_val: str,
                       update_col: str, update_val: str) -> bool:
    try:
        ws = _get_or_create_sheet(sheet_name)
        records = ws.get_all_records()
        headers = ws.row_values(1)
        key_idx = headers.index(key_col) + 1
        upd_idx = headers.index(update_col) + 1
        for i, row in enumerate(records, start=2):
            if str(row.get(key_col)) == str(key_val):
                ws.update_cell(i, upd_idx, update_val)
                return True
        return False
    except Exception as e:
        logger.error("Failed to update cell in '%s': %s", sheet_name, e)
        return False


def get_objects() -> list[dict]:
    return get_all_records("Objects")


def get_active_objects() -> list[dict]:
    return [o for o in get_objects() if o.get("status", "").lower() == "rented"]


def add_object(data: dict) -> bool:
    objects = get_objects()
    new_id = str(len(objects) + 1)
    row = [
        new_id,
        data.get("name", ""),
        data.get("address", ""),
        data.get("tenant_name", ""),
        data.get("tenant_phone", ""),
        data.get("rent_amount", ""),
        data.get("currency", config.DEFAULT_CURRENCY),
        data.get("payment_day", ""),
        data.get("lease_start", ""),
        data.get("lease_end", ""),
        data.get("status", "rented"),
    ]
    return append_row("Objects", row)


def record_payment(data: dict) -> bool:
    expected = float(data.get("expected_amount", 0))
    received = float(data.get("received_amount", 0))
    diff = received - expected
    status = "paid" if diff >= 0 else ("partial" if received > 0 else "missed")
    row = [
        data.get("date", date.today().strftime("%d.%m.%Y")),
        data.get("object_id", ""),
        data.get("object_name", ""),
        expected,
        received,
        diff,
        status,
        data.get("note", ""),
    ]
    return append_row("Payments", row)


def record_expense(data: dict) -> bool:
    row = [
        data.get("date", date.today().strftime("%d.%m.%Y")),
        data.get("object_id", ""),
        data.get("category", ""),
        data.get("amount", ""),
        data.get("description", ""),
    ]
    return append_row("Expenses", row)


def get_payments_for_month(year: int, month: int) -> list[dict]:
    all_payments = get_all_records("Payments")
    result = []
    for p in all_payments:
        try:
            d = datetime.strptime(str(p.get("date", "")), "%d.%m.%Y")
            if d.year == year and d.month == month:
                result.append(p)
        except ValueError:
            pass
    return result


def get_expenses_for_month(year: int, month: int) -> list[dict]:
    all_expenses = get_all_records("Expenses")
    result = []
    for e in all_expenses:
        try:
            d = datetime.strptime(str(e.get("date", "")), "%d.%m.%Y")
            if d.year == year and d.month == month:
                result.append(e)
        except ValueError:
            pass
    return result


def spreadsheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}"

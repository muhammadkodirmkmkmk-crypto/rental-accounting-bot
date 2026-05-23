import logging
import json
import time
from datetime import datetime, date
from typing import Any

import gspread
from gspread.exceptions import APIError

import config
from database import enqueue_sheet_write

logger = logging.getLogger(__name__)

SHEET_HEADERS: dict[str, list[str]] = {
    "Objects": [
        "id", "name", "address", "tenant_name", "tenant_phone",
        "rent_amount", "initial_price", "discount_end",
        "currency", "payment_day", "lease_start", "lease_end", "status",
    ],
    "Payments": [
        "date", "object_id", "object_name", "expected_amount",
        "received_amount", "difference", "status", "note",
    ],
    "Expenses": ["date", "object_id", "object_name", "category", "amount", "description"],
    "Summary": ["month", "object", "income", "expenses", "net_profit", "occupancy_rate"],
    "Targeting_Clients": [
        "id", "name", "monthly_fee", "currency", "payment_day", "start_date", "status",
    ],
    "Targeting_Payments": [
        "date", "client_id", "client_name", "expected_amount",
        "received_amount", "difference", "status", "note",
    ],
    "Targeting_Expenses": ["date", "client_id", "category", "amount", "description"],
    "Personal": ["date", "type", "category", "amount", "description"],
}

_gc: gspread.Client | None = None
_spreadsheet: gspread.Spreadsheet | None = None


def _get_client() -> gspread.Client:
    global _gc
    if _gc is None:
        _gc = gspread.service_account_from_dict(config.GOOGLE_CREDS_DICT)
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
        ws = ss.add_worksheet(title=name, rows=1000, cols=25)
        headers = SHEET_HEADERS.get(name, [])
        if headers:
            ws.append_row(headers)
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


_last_read_error: dict[str, bool] = {}


def had_read_error(sheet_name: str) -> bool:
    """Returns True if the most recent get_all_records call for this sheet failed."""
    return _last_read_error.get(sheet_name, False)


def get_all_records(sheet_name: str) -> list[dict]:
    global _spreadsheet
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            ws = _get_or_create_sheet(sheet_name)
            result = ws.get_all_records(expected_headers=[], numericise_ignore=["all"])
            _last_read_error[sheet_name] = False
            return result
        except Exception as e:
            last_exc = e
            logger.warning(
                "Sheets read error '%s' (attempt %d/3): %s", sheet_name, attempt + 1, e
            )
            _spreadsheet = None  # force reconnect on next attempt
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    logger.error("Failed to read '%s' after 3 attempts: %s", sheet_name, last_exc)
    _last_read_error[sheet_name] = True
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


def clear_all_data() -> bool:
    """Clear all data rows from all sheets (keep headers). Used by /delete command."""
    global _spreadsheet
    _spreadsheet = None  # force reconnect
    success = True
    for name in SHEET_HEADERS:
        try:
            ws = _get_or_create_sheet(name)
            all_vals = ws.get_all_values()
            if len(all_vals) > 1:
                ws.delete_rows(2, len(all_vals))
                logger.info("Cleared sheet '%s' (%d rows)", name, len(all_vals) - 1)
        except Exception as e:
            logger.error("Failed to clear sheet '%s': %s", name, e)
            success = False
    return success


# ── Objects ───────────────────────────────────────────────────

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
        data.get("initial_price", ""),
        data.get("discount_end", ""),
        data.get("currency", config.DEFAULT_CURRENCY),
        data.get("payment_day", ""),
        data.get("lease_start", ""),
        data.get("lease_end", ""),
        data.get("status", "rented"),
    ]
    return append_row("Objects", row)


# ── Payments ──────────────────────────────────────────────────

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


# ── Expenses ──────────────────────────────────────────────────

def record_expense(data: dict) -> bool:
    row = [
        data.get("date", date.today().strftime("%d.%m.%Y")),
        data.get("object_id", ""),
        data.get("object_name", ""),
        data.get("category", ""),
        data.get("amount", ""),
        data.get("description", ""),
    ]
    return append_row("Expenses", row)


# ── Targeting ─────────────────────────────────────────────────

def get_target_clients() -> list[dict]:
    return get_all_records("Targeting_Clients")


def add_target_client(data: dict) -> bool:
    clients = get_target_clients()
    new_id = str(len(clients) + 1)
    row = [
        new_id,
        data.get("name", ""),
        data.get("monthly_fee", ""),
        data.get("currency", config.DEFAULT_CURRENCY),
        data.get("payment_day", ""),
        data.get("start_date", ""),
        data.get("status", "active"),
    ]
    return append_row("Targeting_Clients", row)


def record_target_payment(data: dict) -> bool:
    row = [
        data.get("date", date.today().strftime("%d.%m.%Y")),
        data.get("client_id", ""),
        data.get("client_name", ""),
        data.get("expected_amount", 0),
        data.get("received_amount", 0),
        data.get("difference", 0),
        data.get("status", "paid"),
        data.get("note", ""),
    ]
    return append_row("Targeting_Payments", row)


def record_target_expense(data: dict) -> bool:
    row = [
        data.get("date", date.today().strftime("%d.%m.%Y")),
        data.get("client_id", ""),
        data.get("category", ""),
        data.get("amount", 0),
        data.get("description", ""),
    ]
    return append_row("Targeting_Expenses", row)


def get_target_payments_for_month(year: int, month: int) -> list[dict]:
    all_p = get_all_records("Targeting_Payments")
    result = []
    for p in all_p:
        try:
            d = datetime.strptime(str(p.get("date", "")), "%d.%m.%Y")
            if d.year == year and d.month == month:
                result.append(p)
        except ValueError:
            pass
    return result


def get_target_expenses_for_month(year: int, month: int) -> list[dict]:
    all_e = get_all_records("Targeting_Expenses")
    result = []
    for e in all_e:
        try:
            d = datetime.strptime(str(e.get("date", "")), "%d.%m.%Y")
            if d.year == year and d.month == month:
                result.append(e)
        except ValueError:
            pass
    return result


# ── Personal ──────────────────────────────────────────────────

def record_personal(data: dict) -> bool:
    row = [
        data.get("date", date.today().strftime("%d.%m.%Y")),
        data.get("type", "expense"),
        data.get("category", "other"),
        data.get("amount", 0),
        data.get("description", ""),
    ]
    return append_row("Personal", row)


def get_personal_for_month(year: int, month: int) -> list[dict]:
    all_r = get_all_records("Personal")
    result = []
    for r in all_r:
        try:
            d = datetime.strptime(str(r.get("date", "")), "%d.%m.%Y")
            if d.year == year and d.month == month:
                result.append(r)
        except ValueError:
            pass
    return result


# ── Shared helpers ────────────────────────────────────────────

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

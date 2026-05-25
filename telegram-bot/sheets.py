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
        "rent_amount", "currency", "payment_day", "lease_start", "lease_end", "status",
        "tenant_telegram",
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
    "Reminders": ["id", "user_id", "datetime", "text", "recurring", "status"],
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


def _sync_headers(ws: gspread.Worksheet, sheet_name: str) -> None:
    """
    Ensure the first row of the worksheet exactly matches SHEET_HEADERS.
    If it differs (e.g. old schema with extra columns), rewrite it in-place.
    """
    expected = SHEET_HEADERS.get(sheet_name, [])
    if not expected:
        return
    try:
        current = ws.row_values(1)
        if current == expected:
            return  # already correct
        logger.warning(
            "Sheet '%s' header mismatch — current=%s expected=%s — rewriting row 1",
            sheet_name, current, expected,
        )
        # Pad expected with empty strings to cover any extra columns in the old header
        padded = expected + [""] * max(0, len(current) - len(expected))
        ws.update("1:1", [padded])
        logger.info("Sheet '%s': header row updated to %s", sheet_name, expected)
    except Exception as e:
        logger.error("Failed to sync headers for '%s': %s", sheet_name, e)


def init_sheets() -> None:
    for name in SHEET_HEADERS:
        ws = _get_or_create_sheet(name)
        _sync_headers(ws, name)
    logger.info("All sheets initialised and headers verified")


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


def clean_empty_name_rows(sheet_name: str, name_col_index: int = 1) -> int:
    """
    Delete data rows where the name column (default col B, index 1) is empty or whitespace.
    Returns the number of rows deleted.
    Rows are deleted bottom-up to preserve indices.
    """
    try:
        ws = _get_or_create_sheet(sheet_name)
        all_vals = ws.get_all_values()
        if len(all_vals) <= 1:
            return 0  # header only or empty
        deleted = 0
        # Iterate backwards so row indices stay valid as we delete
        for i in range(len(all_vals) - 1, 0, -1):
            row = all_vals[i]
            cell = row[name_col_index].strip() if len(row) > name_col_index else ""
            if not cell:
                ws.delete_rows(i + 1)  # gspread rows are 1-indexed
                deleted += 1
        if deleted:
            logger.info("clean_empty_name_rows('%s'): removed %d empty rows", sheet_name, deleted)
        return deleted
    except Exception as e:
        logger.error("clean_empty_name_rows('%s') error: %s", sheet_name, e)
        return 0


def clean_all_sheets() -> dict[str, int]:
    """
    Remove rows with empty name/key column from every relevant sheet.
    Returns a dict of {sheet_name: rows_deleted}.
    Column indices (0-based): Objects B=1, Payments date=0 (skip), Targeting_Clients B=1
    """
    results: dict[str, int] = {}
    # Sheets with a meaningful "name" column at index 1
    for sheet in ("Objects", "Targeting_Clients"):
        results[sheet] = clean_empty_name_rows(sheet, name_col_index=1)
    # Payments/Expenses: clean rows where date (col A) is empty
    for sheet in ("Payments", "Expenses", "Targeting_Payments", "Targeting_Expenses", "Personal"):
        results[sheet] = clean_empty_name_rows(sheet, name_col_index=0)
    return results


# ── Objects ───────────────────────────────────────────────────

def get_objects() -> list[dict]:
    return [o for o in get_all_records("Objects") if str(o.get("name", "")).strip()]


def get_active_objects() -> list[dict]:
    return [o for o in get_objects() if o.get("status", "").lower() == "rented"]


def add_object(data: dict) -> bool:
    objects = get_objects()
    new_id = str(len(objects) + 1)
    # Columns: A=id B=name C=address D=tenant_name E=tenant_phone
    #          F=rent_amount G=currency H=payment_day I=lease_start J=lease_end K=status L=tenant_telegram
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
        data.get("tenant_telegram", ""),
    ]
    return append_row("Objects", row)


# ── Payments ──────────────────────────────────────────────────

def record_payment(data: dict) -> bool:
    from utils import safe_float
    expected = safe_float(data.get("expected_amount"))
    received = safe_float(data.get("received_amount"))
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


def delete_object(name: str) -> bool:
    """Delete an object row from the Objects sheet by name."""
    try:
        ws = _get_or_create_sheet("Objects")
        rows = ws.get_all_values()
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) > 1 and row[1].strip().lower() == name.strip().lower():
                ws.delete_rows(i + 1)
                logger.info("Deleted object '%s' (row %d)", name, i + 1)
                return True
        logger.warning("delete_object: '%s' not found", name)
        return False
    except Exception as e:
        logger.error("delete_object error: %s", e)
        return False


def update_object(name: str, fields: dict) -> bool:
    """Update specified fields of an object row found by name (case-insensitive)."""
    header_to_col = {h: i + 1 for i, h in enumerate(SHEET_HEADERS["Objects"])}
    try:
        ws = _get_or_create_sheet("Objects")
        rows = ws.get_all_values()
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) > 1 and row[1].strip().lower() == name.strip().lower():
                row_num = i + 1
                for field, value in fields.items():
                    col = header_to_col.get(field)
                    if col:
                        ws.update_cell(row_num, col, str(value) if value is not None else "")
                        logger.info("Updated object '%s' field '%s'='%s'", name, field, value)
                    # Handle alias: "name" → update column B (index 2)
                    elif field == "new_name":
                        ws.update_cell(row_num, 2, str(value))
                        logger.info("Renamed object '%s' → '%s'", name, value)
                return True
        logger.warning("update_object: '%s' not found", name)
        return False
    except Exception as e:
        logger.error("update_object error: %s", e)
        return False


def spreadsheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}"


# ── Reminders ─────────────────────────────────────────────────

def add_reminder(user_id: int, datetime_str: str, text: str, recurring: str = "none") -> str:
    """Append a reminder row and return its ID (empty string on failure)."""
    reminders = get_all_records("Reminders")
    new_id = str(len(reminders) + 1)
    row = [new_id, str(user_id), datetime_str, text, recurring, "active"]
    ok = append_row("Reminders", row)
    return new_id if ok else ""


def get_reminders(user_id: int | None = None, status: str = "active") -> list[dict]:
    """Return reminders filtered by user_id and/or status."""
    all_r = get_all_records("Reminders")
    result = []
    for r in all_r:
        if status and str(r.get("status", "active")) != status:
            continue
        if user_id is not None and str(r.get("user_id", "")) != str(user_id):
            continue
        result.append(r)
    return result


def update_reminder_status(reminder_id: str, new_status: str) -> bool:
    """Update the status cell of a specific reminder row."""
    try:
        ws = _get_or_create_sheet("Reminders")
        rows = ws.get_all_values()
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if row and str(row[0]) == str(reminder_id):
                ws.update_cell(i + 1, 6, new_status)
                return True
        logger.warning("Reminder id=%s not found for status update", reminder_id)
        return False
    except Exception as e:
        logger.error("update_reminder_status error: %s", e)
        return False

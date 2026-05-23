"""Shared utility helpers."""


def safe_float(value, default: float = 0.0) -> float:
    """Convert a value from Google Sheets to float, returning default on empty/invalid."""
    try:
        return float(value) if value and str(value).strip() else default
    except (ValueError, TypeError):
        return default


def safe_int(value, default: int = 0) -> int:
    """Convert a value from Google Sheets to int, returning default on empty/invalid."""
    try:
        return int(float(value)) if value and str(value).strip() else default
    except (ValueError, TypeError):
        return default

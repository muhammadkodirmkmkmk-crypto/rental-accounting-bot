import os
import json
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]

_raw_creds = os.environ.get("GOOGLE_CREDS_JSON", "")
try:
    GOOGLE_CREDS_DICT: dict = json.loads(_raw_creds)
except (json.JSONDecodeError, ValueError):
    GOOGLE_CREDS_DICT = {}

SPREADSHEET_ID: str = os.environ.get("SPREADSHEET_ID", "")

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

DEFAULT_TIMEZONE: str = os.environ.get("BOT_TIMEZONE", "Asia/Tashkent")
DEFAULT_CURRENCY: str = os.environ.get("DEFAULT_CURRENCY", "USD")
CURRENCY_SYMBOL: str = os.environ.get("CURRENCY_SYMBOL", "$")

SHEETS_RETRY_INTERVAL: int = 300
RATE_LIMIT_SECONDS: float = 3.0

LOG_FILE: str = "errors.log"
DB_PATH: str = "bot_state.db"

WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base")

ALLOWED_USER_IDS: set[int] = {5448612638, 7871931220}

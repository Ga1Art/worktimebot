import os
from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


BOT_TOKEN = os.getenv("BOT_TOKEN")
VK_TOKEN = os.getenv("VK_TOKEN")
ENABLE_TELEGRAM_BOT = env_bool("ENABLE_TELEGRAM_BOT", True)
ENABLE_VK_BOT = env_bool("ENABLE_VK_BOT", True)

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT")

API_KEY = os.getenv("API_KEY")
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
GOOGLE_SHEETS_CURRENT_SHEET = os.getenv("GOOGLE_SHEETS_CURRENT_SHEET", "current_month")
GOOGLE_SHEETS_RATES_SHEET = os.getenv("GOOGLE_SHEETS_RATES_SHEET", "Rates")
GOOGLE_SHEETS_BONUSES_SHEET = os.getenv("GOOGLE_SHEETS_BONUSES_SHEET", "Bonuses")
GOOGLE_SHEETS_PENALTIES_SHEET = os.getenv("GOOGLE_SHEETS_PENALTIES_SHEET", "Penalties")
GOOGLE_SHEETS_PROJECTS_SHEET = os.getenv("GOOGLE_SHEETS_PROJECTS_SHEET", "Projects")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
STANDFLOW_API_BASE_URL = os.getenv("STANDFLOW_API_BASE_URL", "http://localhost:8000")
STANDFLOW_BOT_API_TOKEN = os.getenv("STANDFLOW_BOT_API_TOKEN")

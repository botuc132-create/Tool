"""Bot config."""
import os

# Token & admin defaults (override via env)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8693798009:AAFsAoQDdL3JCGI1I8wD2p7RXn5rHJf0C8c")
_admin_env = os.getenv("ADMIN_IDS", "7776471599")
ADMIN_IDS = {int(x.strip()) for x in _admin_env.split(",") if x.strip()}

# Telethon API (giữ nguyên như source gốc)
API_ID = int(os.getenv("API_ID", "31013160"))
API_HASH = os.getenv("API_HASH", "16cd203faf218319e61e175d129bfd38")

SESSIONS_DIR = "sessions"
LOGS_DIR = "logs"
DATA_DIR = "data"

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

"""Bot config.

Mọi giá trị bí mật PHẢI được cấu hình qua biến môi trường
(Railway → Variables, hoặc file .env khi chạy local). Không ghi token vào source.
"""
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _admin_env.split(",") if x.strip()}

# Telethon API
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()

_missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("API_ID", API_ID),
                           ("API_HASH", API_HASH), ("ADMIN_IDS", ADMIN_IDS)) if not v]
if _missing:
    raise SystemExit("Thiếu biến môi trường: " + ", ".join(_missing))

# Thư mục lưu trữ. Trên Railway gắn Volume tại /app/storage và đặt
# STORAGE_DIR=/app/storage để session + lịch chạy không mất khi redeploy.
STORAGE_DIR = os.getenv("STORAGE_DIR", "").strip()
_base = STORAGE_DIR or "."
SESSIONS_DIR = os.getenv("SESSIONS_DIR") or os.path.join(_base, "sessions")
LOGS_DIR = os.getenv("LOGS_DIR") or os.path.join(_base, "logs")
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(_base, "data")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

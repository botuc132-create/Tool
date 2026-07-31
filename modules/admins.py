"""Admin & permission management (persisted JSON).

Owners = ADMIN_IDS từ env/config (full quyền, không bao giờ hết hạn).
Admins thêm runtime = lưu data/admins.json kèm dict quyền + hạn dùng.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from config import DATA_DIR, ADMIN_IDS

ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")

# Vietnam timezone (UTC+7, no DST)
VN_TZ = timezone(timedelta(hours=7))

# Danh sách quyền chi tiết (key, label hiển thị)
PERMS = [
    ("CHECK",      "🛡 CHECK JOIN"),
    ("AUTO",       "🤖 AUTO"),
    ("RAI",        "📢 TOOL RẢI"),
    ("REF",        "🎯 REF"),
    ("SPAM",       "💬 SEX SPAM"),
    ("JOIN",       "🚪 JOIN GROUP"),
    ("DUYET",      "✅ DUYỆT NẠP"),
    ("MANAGE_ACC", "👤 Quản lý TK"),
    ("READ_MSG",   "📩 Đọc tin nhắn TK"),
    ("STOP",       "⛔ Stop task"),
    ("LOGS",       "📜 Logs/Status"),
    ("USE_ALL_ACCS", "🗂 Sử dụng tất cả ACC"),
    ("ADD_ADMIN",  "➕ Thêm admin"),
    ("DEL_ADMIN",  "🗑 Xoá admin"),
    ("EDIT_ADMIN", "✏️ Sửa quyền admin"),
]
PERM_KEYS = [k for k, _ in PERMS]
PERM_LABEL = {k: v for k, v in PERMS}


def _default_perms(value: bool = True) -> Dict[str, bool]:
    return {k: value for k in PERM_KEYS}


def _load() -> dict:
    if not os.path.exists(ADMINS_FILE):
        return {"admins": {}}
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            if "admins" not in d:
                d["admins"] = {}
            return d
    except Exception:
        return {"admins": {}}


def _save(d: dict):
    tmp = ADMINS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ADMINS_FILE)


# ─── duration parser ────────────────────────────────────
_DUR_RE = re.compile(r"^\s*(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|hour|hours|d|day|days)\s*$", re.I)

def parse_duration(text: str) -> Optional[int]:
    """Parse '1day', '1h', '30m', '45s' -> seconds. Return None on fail."""
    if not text:
        return None
    m = _DUR_RE.match(text.strip())
    if not m:
        # cho phép chỉ số (mặc định = giờ)
        try:
            v = int(text.strip())
            return v * 3600
        except Exception:
            return None
    n = int(m.group(1))
    u = m.group(2).lower()
    if u.startswith("s"):
        return n
    if u.startswith("m"):
        return n * 60
    if u.startswith("h"):
        return n * 3600
    if u.startswith("d"):
        return n * 86400
    return None


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


def is_owner(uid: int) -> bool:
    return uid in ADMIN_IDS


def _expires_at(rec: dict) -> Optional[datetime]:
    s = rec.get("expires_at")
    if not s:
        return None
    try:
        # ISO with tz
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VN_TZ)
        return dt.astimezone(VN_TZ)
    except Exception:
        return None


def is_expired(uid: int) -> bool:
    if is_owner(uid):
        return False
    d = _load()
    rec = d["admins"].get(str(uid))
    if not rec:
        return True
    exp = _expires_at(rec)
    if exp is None:
        return False  # không set hạn = vô thời hạn
    return now_vn() >= exp


def is_admin(uid: int) -> bool:
    if is_owner(uid):
        return True
    d = _load()
    if str(uid) not in d["admins"]:
        return False
    return not is_expired(uid)


def has_perm(uid: int, perm: str) -> bool:
    if is_owner(uid):
        return True
    if is_expired(uid):
        return False
    d = _load()
    rec = d["admins"].get(str(uid))
    if not rec:
        return False
    return bool(rec.get("perms", {}).get(perm, False))


def expiry_seconds_left(uid: int) -> Optional[int]:
    """Số giây còn lại. None nếu vô thời hạn / owner. <=0 nếu hết."""
    if is_owner(uid):
        return None
    d = _load()
    rec = d["admins"].get(str(uid))
    if not rec:
        return 0
    exp = _expires_at(rec)
    if exp is None:
        return None
    delta = (exp - now_vn()).total_seconds()
    return int(delta)


def format_remaining(uid: int) -> str:
    if is_owner(uid):
        return "♾ Vô thời hạn (Owner)"
    sec = expiry_seconds_left(uid)
    if sec is None:
        return "♾ Vô thời hạn"
    if sec <= 0:
        return "❌ Đã hết hạn"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d} ngày")
    if h: parts.append(f"{h} giờ")
    if m and not d: parts.append(f"{m} phút")
    if not parts: parts.append(f"{s} giây")
    return " ".join(parts)


def format_expiry_at(uid: int) -> str:
    if is_owner(uid):
        return "—"
    d = _load()
    rec = d["admins"].get(str(uid))
    if not rec:
        return "—"
    exp = _expires_at(rec)
    if exp is None:
        return "Vô thời hạn"
    return exp.strftime("%H:%M %d/%m/%Y (GMT+7)")


def granted_perm_labels(uid: int) -> List[str]:
    """Trả về list label các quyền chức năng đang bật (không tính quyền admin meta)."""
    feature_keys = ["CHECK", "AUTO", "REF", "SPAM", "JOIN",
                    "MANAGE_ACC", "READ_MSG", "USE_ALL_ACCS"]
    if is_owner(uid):
        return [PERM_LABEL[k] for k in feature_keys]
    out = []
    for k in feature_keys:
        if has_perm(uid, k):
            out.append(PERM_LABEL[k])
    return out


def list_admins() -> List[dict]:
    out = []
    for oid in sorted(ADMIN_IDS):
        out.append({"uid": oid, "owner": True, "name": "Owner",
                    "perms": _default_perms(True), "expires_at": None})
    d = _load()
    for uid_s, rec in d["admins"].items():
        try:
            uid = int(uid_s)
        except Exception:
            continue
        if uid in ADMIN_IDS:
            continue
        out.append({
            "uid": uid,
            "owner": False,
            "name": rec.get("name", ""),
            "perms": {**_default_perms(False), **rec.get("perms", {})},
            "expires_at": rec.get("expires_at"),
        })
    return out


def get_admin(uid: int) -> Optional[dict]:
    for a in list_admins():
        if a["uid"] == uid:
            return a
    return None


def add_admin(uid: int, name: str = "", duration_seconds: Optional[int] = None) -> bool:
    if uid in ADMIN_IDS:
        return False
    d = _load()
    if str(uid) in d["admins"]:
        return False
    rec = {"name": name, "perms": _default_perms(True)}
    if duration_seconds and duration_seconds > 0:
        exp = now_vn() + timedelta(seconds=duration_seconds)
        rec["expires_at"] = exp.isoformat()
    d["admins"][str(uid)] = rec
    _save(d)
    return True


def set_expiry(uid: int, duration_seconds: Optional[int]) -> bool:
    """duration_seconds=None -> vô thời hạn."""
    if uid in ADMIN_IDS:
        return False
    d = _load()
    rec = d["admins"].get(str(uid))
    if not rec:
        return False
    if duration_seconds is None:
        rec.pop("expires_at", None)
    else:
        exp = now_vn() + timedelta(seconds=duration_seconds)
        rec["expires_at"] = exp.isoformat()
    d["admins"][str(uid)] = rec
    _save(d)
    return True


def remove_admin(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return False
    d = _load()
    if str(uid) not in d["admins"]:
        return False
    del d["admins"][str(uid)]
    _save(d)
    return True


def toggle_perm(uid: int, perm: str) -> Optional[bool]:
    if uid in ADMIN_IDS:
        return None
    if perm not in PERM_KEYS:
        return None
    d = _load()
    rec = d["admins"].get(str(uid))
    if not rec:
        return None
    perms = {**_default_perms(False), **rec.get("perms", {})}
    perms[perm] = not perms.get(perm, False)
    rec["perms"] = perms
    d["admins"][str(uid)] = rec
    _save(d)
    return perms[perm]


# ─── session ownership ──────────────────────────────────
SESSION_OWNERS_FILE = os.path.join(DATA_DIR, "session_owners.json")


def _load_owners() -> dict:
    if not os.path.exists(SESSION_OWNERS_FILE):
        return {}
    try:
        with open(SESSION_OWNERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_owners(d: dict):
    tmp = SESSION_OWNERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSION_OWNERS_FILE)


def set_session_owner(name: str, uid: int):
    d = _load_owners()
    d[name] = uid
    _save_owners(d)


def get_session_owner(name: str) -> Optional[int]:
    d = _load_owners()
    v = d.get(name)
    return int(v) if v is not None else None


def remove_session_owner(name: str):
    d = _load_owners()
    if name in d:
        del d[name]
        _save_owners(d)

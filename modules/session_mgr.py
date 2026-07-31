"""Session manager – Telethon clients per session file."""
import os
from telethon import TelegramClient
from config import API_ID, API_HASH, SESSIONS_DIR

# Lazy import to avoid circular
def _admins():
    from modules import admins as a
    return a


def list_sessions():
    return sorted(
        f[:-len(".session")]
        for f in os.listdir(SESSIONS_DIR)
        if f.endswith(".session")
    )


def list_sessions_for(uid: int):
    """Trả về session mà uid được phép dùng.
    - Owner / admin có quyền USE_ALL_ACCS  -> toàn bộ.
    - Admin khác                          -> chỉ session do chính họ thêm.
    """
    a = _admins()
    all_s = list_sessions()
    if a.is_owner(uid) or a.has_perm(uid, "USE_ALL_ACCS"):
        return all_s
    return [n for n in all_s if a.get_session_owner(n) == uid]


def session_path(name: str) -> str:
    return os.path.join(SESSIONS_DIR, name)


def make_client(name: str) -> TelegramClient:
    return TelegramClient(session_path(name), API_ID, API_HASH)


def delete_session(name: str):
    for ext in (".session", ".session-journal"):
        p = session_path(name) + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    try:
        _admins().remove_session_owner(name)
    except Exception:
        pass

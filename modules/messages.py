"""Đọc tin nhắn từ 1 account (Telethon)."""
from .session_mgr import make_client


async def list_dialogs(session_name: str, limit: int = 20):
    """Trả về list [{id, name}] gần đây."""
    client = make_client(session_name)
    out = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return out
        async for d in client.iter_dialogs(limit=limit):
            out.append({
                "id": d.id,
                "name": (d.name or "(no name)")[:40],
            })
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return out


async def read_messages(session_name: str, dialog_id: int, limit: int = 30):
    """Trả về list str dòng tin nhắn (mới nhất ở dưới)."""
    client = make_client(session_name)
    lines = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return ["❌ Session die"]
        msgs = []
        async for m in client.iter_messages(dialog_id, limit=limit):
            msgs.append(m)
        msgs.reverse()
        for m in msgs:
            who = ""
            try:
                if m.sender_id:
                    who = f"[{m.sender_id}] "
            except Exception:
                pass
            ts = m.date.strftime("%m-%d %H:%M") if m.date else ""
            text = (m.message or m.text or "").replace("\n", " ")
            if not text and m.media:
                text = "<media>"
            lines.append(f"{ts} {who}{text[:200]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return lines or ["(không có tin nhắn)"]
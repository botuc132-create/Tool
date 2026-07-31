"""JOIN GROUP tool + auto-leave scheduler (persisted).

Lưu lịch leave trong data/join_schedule.json. Khi bot restart,
restore_scheduler() được gọi trong main() để lập lại các task leave còn hạn.
"""
import asyncio
import json
import os
import time
from typing import List, Optional

from telethon import errors
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest

from config import DATA_DIR
from .session_mgr import make_client

SCHED_FILE = os.path.join(DATA_DIR, "join_schedule.json")


def parse_leave_time(s: str) -> Optional[float]:
    """Trả về số giây sau khi join sẽ leave.
    None = không rời (stay/0).
    -1 = parse fail.
    """
    if s is None:
        return -1
    t = s.strip().lower()
    if t in ("stay", "0", "0s", "0m", "0h", "0d", "0day"):
        return None
    try:
        if t.endswith("day"):
            return float(t[:-3]) * 86400
        if t.endswith("d"):
            return float(t[:-1]) * 86400
        if t.endswith("h"):
            return float(t[:-1]) * 3600
        if t.endswith("m"):
            return float(t[:-1]) * 60
        if t.endswith("s"):
            return float(t[:-1])
        # số trần coi là giây
        return float(t)
    except Exception:
        return -1


# ─── persistence ────────────────────────────────────────
def _load() -> list:
    if not os.path.exists(SCHED_FILE):
        return []
    try:
        with open(SCHED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list):
    tmp = SCHED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SCHED_FILE)


def _add_schedule(session: str, entity: str, leave_at: float):
    items = _load()
    items.append({"session": session, "entity": entity, "leave_at": leave_at})
    _save(items)


def _remove_schedule(session: str, entity: str, leave_at: float):
    items = _load()
    items = [x for x in items
             if not (x["session"] == session and x["entity"] == entity
                     and abs(x["leave_at"] - leave_at) < 1)]
    _save(items)


# ─── join / leave core ──────────────────────────────────
async def _resolve_and_join(client, link: str):
    """Join channel/group bằng public username hoặc invite link.
    Trả về entity đã join."""
    link = link.strip()
    if "joinchat/" in link or "/+" in link:
        # invite link
        hash_ = link.split("+")[-1].split("/")[-1].split("?")[0]
        try:
            res = await client(ImportChatInviteRequest(hash_))
            chat = res.chats[0]
            return chat
        except errors.UserAlreadyParticipantError:
            inv = await client(CheckChatInviteRequest(hash_))
            return getattr(inv, "chat", None)
    # public
    username = link.rstrip("/").split("/")[-1].replace("@", "").split("?")[0]
    try:
        res = await client(JoinChannelRequest(username))
        return res.chats[0] if res.chats else username
    except errors.UserAlreadyParticipantError:
        return username


async def _leave(client, entity):
    try:
        await client(LeaveChannelRequest(entity))
        return True
    except Exception:
        return False


async def _entity_ref(entity):
    """Convert chat object/username thành ref serializable (string)."""
    if isinstance(entity, str):
        return entity
    try:
        if getattr(entity, "username", None):
            return entity.username
        return str(entity.id)
    except Exception:
        return str(entity)


async def join_one(session: str, link: str, leave_after: Optional[float], log,
                   app_loop=None):
    """Join 1 account. Nếu leave_after != None -> schedule leave."""
    client = make_client(session)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{session}] ❌ Session die")
            return
        try:
            entity = await _resolve_and_join(client, link)
            await log(f"[{session}] ✅ Joined {link}")
        except errors.FloodWaitError as e:
            await log(f"[{session}] ⏳ FloodWait {e.seconds}s")
            return
        except Exception as e:
            await log(f"[{session}] ❌ Join fail: {e}")
            return

        if leave_after is None:
            return  # stay
        ent_ref = await _entity_ref(entity)
        leave_at = time.time() + leave_after
        _add_schedule(session, ent_ref, leave_at)
        await log(f"[{session}] 🕒 Sẽ leave sau {int(leave_after)}s")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_join(sessions: List[str], link: str, leave_after: Optional[float],
                   log):
    await log("🚪 JOIN START")
    for s in sessions:
        await join_one(s, link, leave_after, log)
    await log("✅ JOIN DONE")
    # bắt đầu loop scheduler ngay để xử lý leave đã add
    asyncio.create_task(scheduler_loop(log))


# ─── scheduler ──────────────────────────────────────────
_scheduler_started = False


async def scheduler_loop(log=None):
    """Loop kiểm tra định kỳ và leave những entity đến hạn."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    try:
        while True:
            items = _load()
            now = time.time()
            due = [x for x in items if x["leave_at"] <= now]
            for it in due:
                session = it["session"]; entity = it["entity"]
                client = make_client(session)
                ok = False
                try:
                    await client.connect()
                    if await client.is_user_authorized():
                        ok = await _leave(client, entity)
                except Exception:
                    ok = False
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                if log:
                    await log(f"[{session}] {'✅' if ok else '⚠'} leave {entity}")
                _remove_schedule(session, entity, it["leave_at"])
            await asyncio.sleep(15)
    finally:
        _scheduler_started = False


def pending_schedule() -> list:
    return _load()
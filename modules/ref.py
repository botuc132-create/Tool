"""REF – port từ ref.py, no input()."""
import asyncio, re
from datetime import datetime, timezone
from telethon import errors
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import Channel
from .session_mgr import make_client


async def _clean_old_groups(client):
    dialogs = await client.get_dialogs()
    old = []
    for d in dialogs:
        e = d.entity
        if isinstance(e, Channel):
            try:
                msgs = await client.get_messages(e, limit=1)
                if not msgs:
                    continue
                days = (datetime.now(timezone.utc) - msgs[0].date).days
                if days >= 14:
                    old.append(e)
            except Exception:
                pass
    leave = 0
    for g in old:
        try:
            await client(LeaveChannelRequest(g))
            leave += 1
            await asyncio.sleep(1)
            if leave >= 10:
                break
        except Exception:
            pass


async def _ref_one(session_name, deep_link, log):
    client = make_client(session_name)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{session_name}] ❌ Session die")
            return
        try:
            bot_username = deep_link.split("/")[-1].split("?")[0]
            start_param = deep_link.split("start=")[-1]
            await client.send_message(bot_username, f"/start {start_param}")
            await asyncio.sleep(3)
            msgs = await client.get_messages(bot_username, limit=10)
            found, joined = [], []
            for m in msgs:
                t = m.message or ""
                for l in re.findall(r"(https://t\.me/[^\s]+)", t):
                    if l not in found:
                        found.append(l)
                for u in re.findall(r"@([a-zA-Z0-9_]+)", t):
                    link = f"https://t.me/{u}"
                    if link not in found:
                        found.append(link)
                if m.buttons:
                    for row in m.buttons:
                        for b in row:
                            u = getattr(b, "url", None)
                            if u and u not in found:
                                found.append(u)
            if not found:
                await log(f"[{session_name}] ❌ Không tìm thấy nhóm")
                return
            for link in found:
                try:
                    if "t.me/+" in link:
                        continue
                    u = link.split("/")[-1].split("?")[0]
                    if not u:
                        continue
                    try:
                        await client(JoinChannelRequest(u))
                    except errors.UserAlreadyParticipantError:
                        pass
                    except errors.ChannelsTooMuchError:
                        await _clean_old_groups(client)
                        await asyncio.sleep(3)
                        await client(JoinChannelRequest(u))
                    joined.append(u)
                    await asyncio.sleep(2)
                except Exception:
                    pass
            await asyncio.sleep(5)
            msgs = await client.get_messages(bot_username, limit=15)
            clicked = False
            keys = ["xác minh", "xác nhận", "tôi đã tham gia",
                    "tham gia", "verify", "check", "confirm"]
            for m in msgs:
                if not m.buttons:
                    continue
                for row in m.buttons:
                    for b in row:
                        try:
                            txt = str(getattr(b, "text", "")).lower()
                            if "✅" not in txt:
                                continue
                            if any(k in txt for k in keys):
                                await log(f"[{session_name}] 🔘 {txt}")
                                await m.click(text=b.text)
                                clicked = True
                                await asyncio.sleep(5)
                                break
                        except Exception:
                            pass
                    if clicked:
                        break
                if clicked:
                    break
            if not clicked:
                await log(f"[{session_name}] ❌ Không thấy nút xác minh")
            else:
                for g in joined:
                    try:
                        ent = await client.get_entity(g)
                        await client(LeaveChannelRequest(ent))
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                await log(f"[{session_name}] ✅ Thành công")
        except Exception as e:
            await log(f"[{session_name}] ❌ {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_ref(sessions, deep_link, times, log):
    await log(f"🚀 REF START | {len(sessions)} acc | x{times}")
    for i in range(times):
        if times > 1:
            await log(f"━━━ LẦN {i+1}/{times} ━━━")
        await asyncio.gather(*[_ref_one(s, deep_link, log) for s in sessions])
    await log("✅ REF DONE")

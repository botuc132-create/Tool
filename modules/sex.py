"""SEX SPAM – port từ sex.py, no input()."""
import asyncio, random
from telethon import errors
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from .session_mgr import make_client


async def _join_group(client, link, log, session_name):
    try:
        if "https://t.me/" in link:
            username = link.split("/")[-1]
        else:
            username = link.replace("@", "")
        try:
            await client(JoinChannelRequest(username))
        except errors.UserAlreadyParticipantError:
            pass
        return username
    except Exception as e:
        await log(f"[{session_name}] ❌ Lỗi vào nhóm: {e}")
        return None


async def _spam_one(session_name, mode, target, message, total, delay,
                    delete_delay, start_event, stop_event, log):
    client = make_client(session_name)
    joined_group = False
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{session_name}] ❌ Session die")
            return
        try:
            if mode == "group":
                username = await _join_group(client, target, log, session_name)
                if not username:
                    return
                entity = await client.get_entity(username)
                joined_group = True
            else:
                entity = await client.get_entity(target)
        except Exception as e:
            await log(f"[{session_name}] ❌ entity: {e}")
            return

        await log(f"[{session_name}] READY")
        await start_event.wait()

        sent_ids = []
        for i in range(total):
            if stop_event.is_set():
                break
            try:
                async with client.action(entity, "typing"):
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                m = await client.send_message(entity, message)
                sent_ids.append(m.id)
                await log(f"[{session_name}] ✓ {i+1}/{total}")
            except errors.FloodWaitError as e:
                await log(f"[{session_name}] ⏳ FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                await log(f"[{session_name}] ❌ {e}")
            await asyncio.sleep(delay)

        if sent_ids and delete_delay > 0:
            await log(f"[{session_name}] ⏳ chờ {delete_delay}s rồi xóa")
            await asyncio.sleep(delete_delay)
            try:
                await client.delete_messages(entity, sent_ids, revoke=True)
                await log(f"[{session_name}] 🗑 xóa {len(sent_ids)} tin")
            except Exception as e:
                await log(f"[{session_name}] ❌ xóa: {e}")

        if joined_group:
            try:
                await client(LeaveChannelRequest(entity))
                await log(f"[{session_name}] 🚪 rời nhóm")
            except Exception:
                pass
        await log(f"[{session_name}] ✅ Done")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_spam(sessions, mode, target, message, total, delay,
                   delete_delay, stop_event, log):
    start_event = asyncio.Event()
    await log(f"🚀 SPAM START | {len(sessions)} acc | mode={mode}")
    tasks = [
        asyncio.create_task(_spam_one(
            s, mode, target, message, total, delay,
            delete_delay, start_event, stop_event, log
        )) for s in sessions
    ]
    await asyncio.sleep(2)
    start_event.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await log("✅ SPAM DONE")

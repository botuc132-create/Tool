"""TOOL RẢI – gửi 1 nội dung tới nhiều nhóm với 1 acc.

Không hỏi delay gửi/xoá. Mỗi nhóm gửi `per_group` tin nhắn rồi sang nhóm kế tiếp.
"""
import asyncio
from telethon import errors
from .session_mgr import make_client


async def run_rai(session_name, groups, content, per_group, stop_event, log):
    """
    session_name: tên 1 session
    groups: list link/username nhóm
    content: nội dung cần rải
    per_group: số tin mỗi nhóm
    """
    await log(f"🚀 RẢI START | acc={session_name} | {len(groups)} nhóm × {per_group} tin")
    client = make_client(session_name)
    total_sent = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{session_name}] ❌ Session die")
            return
        for gi, group in enumerate(groups, 1):
            if stop_event.is_set():
                break
            await log(f"➡ [{gi}/{len(groups)}] Nhóm: {group}")
            try:
                entity = await client.get_entity(group)
            except Exception as e:
                await log(f"   ❌ Không lấy được nhóm: {e}")
                continue
            for i in range(per_group):
                if stop_event.is_set():
                    break
                try:
                    await client.send_message(entity, content)
                    total_sent += 1
                    await log(f"   ✔ [{i+1}/{per_group}] đã gửi")
                    await asyncio.sleep(1)
                except errors.FloodWaitError as e:
                    await log(f"   ⚠ FloodWait {e.seconds}s")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    await log(f"   ❌ {e}")
                    await asyncio.sleep(2)
        await log(f"✅ RẢI DONE | tổng gửi = {total_sent}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

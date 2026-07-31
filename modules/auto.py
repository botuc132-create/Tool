"""AUTO – port từ auto.py, dùng nhiều acc, no input()."""
import asyncio, random, string
from telethon import errors
from telethon.tl.functions.account import UpdateProfileRequest
from .session_mgr import make_client

TEXTS = [
    "đúng là quả trứng vô ơn",
    "hôm nay tự nhiên thấy mọi thứ chán thật",
    "có nhiều chuyện nhìn vậy thôi chứ khó nói lắm",
    "đêm nay chắc lại ngủ muộn nữa rồi",
    "nhiều lúc chỉ muốn im lặng cả ngày",
    "đúng người nhưng sai thời điểm thì cũng vậy thôi",
    "cảm giác chờ đợi đúng là khó chịu thật",
    "đôi khi càng nghĩ càng thấy mệt",
    "trời hôm nay đẹp mà lòng không vui",
    "có những chuyện không giải thích được",
    "ngồi một lúc tự nhiên thấy đời lạ thật",
    "nhiều chuyện tưởng dễ mà khó ghê",
    "hết chuyện này tới chuyện khác luôn",
    "đúng là không biết nói gì thêm",
    "nhiều lúc chỉ muốn biến mất vài hôm",
    "thức khuya riết giờ ngủ không nổi",
    "không hiểu sao hôm nay mệt ngang",
    "nghĩ nhiều quá cũng chẳng giải quyết được gì",
    "càng lớn càng thấy ít vui hơn",
    "đúng là chuyện gì cũng có lý do",
    "đôi khi im lặng lại tốt hơn",
    "mọi thứ rồi cũng sẽ qua thôi",
    "đúng là khó nói thật",
    "lại một ngày bình thường",
]


def _rand_name():
    return "".join(random.choice(string.ascii_lowercase) for _ in range(8))


async def _auto_one(session_name, group, send_delay, delete_delay, total, stop_event, log):
    client = make_client(session_name)
    sent = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{session_name}] ❌ Session die")
            return
        for i in range(total):
            if stop_event.is_set():
                break
            try:
                try:
                    new_name = _rand_name()
                    await client(UpdateProfileRequest(first_name=new_name))
                except Exception:
                    pass
                text = random.choice(TEXTS)
                msg = await client.send_message(group, text)
                sent += 1
                await log(f"[{session_name}] ✔ [{i+1}/{total}] {text[:40]}")
                await asyncio.sleep(delete_delay)
                try:
                    await client.delete_messages(group, msg.id)
                except Exception:
                    pass
                await asyncio.sleep(send_delay)
            except errors.FloodWaitError as e:
                await log(f"[{session_name}] ⚠ FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                await log(f"[{session_name}] ❌ {e}")
                await asyncio.sleep(3)
        await log(f"[{session_name}] ✅ Done, sent={sent}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_auto(sessions, group, send_delay, delete_delay, total, stop_event, log):
    await log(f"🚀 AUTO START | {len(sessions)} acc | group={group}")
    await asyncio.gather(*[
        _auto_one(s, group, send_delay, delete_delay, total, stop_event, log)
        for s in sessions
    ])
    await log("✅ AUTO DONE")

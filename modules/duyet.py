"""DUYỆT NẠP – auto bấm nút "✅ Duyệt" trong tin nhắn của bot nạp tiền.

Cách hoạt động:
- Mở session telethon đã chọn.
- Lắng nghe NewMessage + MessageEdited liên tục cho đến khi user bấm STOP.
- Chỉ xử lý tin có timestamp >= thời điểm BẬT TOOL (không quét tin cũ).
- Khi text chứa trigger -> chờ `delay` giây, refetch message,
  tìm inline button có chữ "Duyệt" và bấm.
- Tự reconnect nếu Telethon rớt mạng tạm thời (không thoát tool).
"""
import asyncio
from datetime import datetime, timezone

from telethon import events, TelegramClient
from telethon.errors import FloodWaitError

from .session_mgr import session_path
from config import API_ID, API_HASH


async def _click_duyet(client, msg, button_text, log, tag):
    try:
        fresh = await client.get_messages(msg.chat_id, ids=msg.id)
    except Exception as e:
        await log(f"[{tag}] ❌ Lỗi refetch msg_id={msg.id}: {e}")
        return
    if fresh is None:
        await log(f"[{tag}] ⚠ Tin đã bị xoá, bỏ qua msg_id={msg.id}")
        return
    if not fresh.buttons:
        await log(f"[{tag}] ⚠ Tin không có nút bấm (msg_id={fresh.id})")
        return
    for row in fresh.buttons:
        for btn in row:
            if button_text.lower() in (btn.text or "").lower():
                try:
                    res = await btn.click()
                    await log(f"[{tag}] ✅ Đã bấm '{btn.text}' (msg_id={fresh.id})")
                    return res
                except FloodWaitError as e:
                    await log(f"[{tag}] ⏳ FloodWait {e.seconds}s khi bấm nút")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    await log(f"[{tag}] ❌ Lỗi bấm nút: {e}")
                    return
    await log(f"[{tag}] ⚠ Không tìm thấy nút chứa '{button_text}' (msg_id={fresh.id})")


async def run_duyet(session_name, bot_username, delay, trigger, button_text,
                    stop_event, log):
    tag = session_name
    # Bật auto-reconnect để khi rớt mạng tạm thời, Telethon tự kết nối lại
    # và update handlers vẫn tiếp tục nhận tin (không thoát tool).
    client = TelegramClient(
        session_path(session_name), API_ID, API_HASH,
        connection_retries=None,        # retry vô hạn
        retry_delay=2,
        auto_reconnect=True,
        request_retries=5,
    )
    pending: set[asyncio.Task] = set()
    handled_ids: set[int] = set()  # dedupe NewMessage + MessageEdited
    start_ts = datetime.now(timezone.utc)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{tag}] ❌ Session die")
            return

        bot_uname = bot_username.lstrip("@")
        try:
            bot_entity = await client.get_entity(bot_uname)
            bot_id = bot_entity.id
        except Exception as e:
            await log(f"[{tag}] ❌ Không tìm thấy bot @{bot_uname}: {e}")
            return

        await log(f"[{tag}] 🚀 DUYỆT NẠP đang chạy")
        await log(f"[{tag}] 👀 Bot: @{bot_uname} (id={bot_id}) | "
                  f"trigger='{trigger}' | delay={delay}s | button='{button_text}'")
        await log(f"[{tag}] ⏱ Chỉ duyệt tin nhận từ {start_ts.strftime('%H:%M:%S')} UTC trở đi")

        async def schedule(msg):
            await log(f"[{tag}] ⏳ Phát hiện lệnh nạp (msg_id={msg.id}), "
                      f"chờ {delay}s rồi bấm Duyệt…")
            try:
                slept = 0.0
                step = 0.5
                while slept < delay:
                    if stop_event.is_set():
                        return
                    await asyncio.sleep(min(step, delay - slept))
                    slept += step
                await _click_duyet(client, msg, button_text, log, tag)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                await log(f"[{tag}] ❌ {e}")

        async def _process(event, source: str):
            try:
                msg = event.message
                if msg.date and msg.date < start_ts:
                    return
                sender_id = getattr(msg, "sender_id", None)
                if sender_id != bot_id:
                    return
                text = msg.message or ""
                if trigger.lower() not in text.lower():
                    return
                if msg.id in handled_ids:
                    return
                handled_ids.add(msg.id)
                await log(f"[{tag}] 🔔 Khớp trigger qua {source} (msg_id={msg.id})")
                t = asyncio.create_task(schedule(msg))
                pending.add(t)
                t.add_done_callback(pending.discard)
            except Exception as e:
                await log(f"[{tag}] ❌ handler {source}: {e}")

        @client.on(events.NewMessage())
        async def _on_new(event):
            await _process(event, "NewMessage")

        @client.on(events.MessageEdited())
        async def _on_edit(event):
            await _process(event, "MessageEdited")

        # CHỈ thoát khi user bấm STOP. Nếu rớt mạng, auto_reconnect sẽ tự nối lại.
        await stop_event.wait()

        for t in list(pending):
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await log(f"[{tag}] ⛔ DUYỆT NẠP đã dừng")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

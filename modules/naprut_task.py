"""Tác vụ điểm danh hằng ngày trên @naprutclmmnew_bot.

Dùng lại session Telethon có sẵn (modules.session_mgr). Chỉ thực hiện:
  1) gửi /start
  2) bấm nút có chữ "điểm danh" (hoặc gửi đúng chữ đó nếu bot dùng bàn phím trả lời)
Tuyệt đối KHÔNG bấm các nút nạp / rút / thanh toán / xác nhận giao dịch.
Không ghi session, số điện thoại, mã OTP vào log.
"""
import asyncio
import logging
import unicodedata

from telethon import errors as tele_errors
from telethon.tl.types import KeyboardButton, ReplyKeyboardMarkup

# Telethon >= 1.40 đã xoá KeyboardButtonCallback khỏi tl.types.
# Nhận diện nút inline bằng thuộc tính `data` để tương thích mọi phiên bản.
try:
    from telethon.tl.types import KeyboardButtonCallback
except ImportError:  # Telethon mới
    KeyboardButtonCallback = None


def _is_inline_button(btn) -> bool:
    if KeyboardButtonCallback is not None and isinstance(btn, KeyboardButtonCallback):
        return True
    # Fallback: nút inline luôn có trường `data` (bytes), nút reply thì không.
    return getattr(btn, "data", None) is not None

from modules.session_mgr import make_client

log = logging.getLogger("naprut_task")

TARGET_BOT = "naprutclmmnew_bot"
REPLY_TIMEOUT = 25        # giây chờ bot đích trả lời mỗi bước
CONNECT_TIMEOUT = 30

CHECKIN_WORDS = ("diem danh", "checkin", "check in", "check-in")
FORBIDDEN_WORDS = ("nap", "rut", "thanh toan", "chuyen khoan", "xac nhan",
                   "deposit", "withdraw", "bank", "mua", "dat cuoc", "cuoc")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFD", text or "").lower()
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.replace("đ", "d")


def _is_checkin(text: str) -> bool:
    n = _norm(text)
    if any(w in n for w in FORBIDDEN_WORDS) and not any(w in n for w in CHECKIN_WORDS[:1]):
        return False
    return any(w in n for w in CHECKIN_WORDS)


def _is_forbidden(text: str) -> bool:
    n = _norm(text)
    return any(w in n for w in FORBIDDEN_WORDS) and "diem danh" not in n


async def _wait_reply(client, entity, after_id: int, timeout=REPLY_TIMEOUT):
    """Chờ tin nhắn mới (hoặc tin đã được sửa) từ bot đích sau after_id."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        msgs = await client.get_messages(entity, limit=5)
        for m in msgs:
            if m.id > after_id and not m.out:
                return m
        await asyncio.sleep(2)
    return None


def _find_checkin_button(msg):
    """Trả về (kiểu, nút) — 'inline' hoặc 'reply'; None nếu không có."""
    if not msg or not msg.reply_markup or not getattr(msg.reply_markup, "rows", None):
        return None
    is_reply_kb = isinstance(msg.reply_markup, ReplyKeyboardMarkup)
    for row in msg.reply_markup.rows:
        for btn in row.buttons:
            label = getattr(btn, "text", "") or ""
            if _is_forbidden(label) or not _is_checkin(label):
                continue
            if _is_inline_button(btn):
                return ("inline", btn)
            if is_reply_kb or isinstance(btn, KeyboardButton):
                return ("reply", btn)
    return None


def _short(text: str, n=160) -> str:
    t = " ".join((text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


async def run_checkin(account_name: str) -> str:
    """Chạy 1 lần điểm danh. Trả về mô tả ngắn; raise khi lỗi."""
    client = make_client(account_name)
    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            raise RuntimeError("Không kết nối được Telegram (lỗi mạng)") from e

        if not await client.is_user_authorized():
            raise RuntimeError(
                f"Tài khoản {account_name} chưa đăng nhập hoặc session đã hết hạn. "
                "Vào QUẢN LÝ ACC để thêm lại.")

        try:
            entity = await client.get_entity(TARGET_BOT)
        except (ValueError, tele_errors.UsernameInvalidError,
                tele_errors.UsernameNotOccupiedError) as e:
            raise RuntimeError(f"Không tìm thấy bot @{TARGET_BOT}") from e

        # Bước 1: /start
        sent = await client.send_message(entity, "/start")
        reply = await _wait_reply(client, entity, sent.id)
        if not reply:
            raise RuntimeError("Bot đích không phản hồi /start")

        # Tìm nút điểm danh trong vài tin gần nhất
        found = _find_checkin_button(reply)
        source_msg = reply
        if not found:
            for m in await client.get_messages(entity, limit=5):
                found = _find_checkin_button(m)
                if found:
                    source_msg = m
                    break
        if not found:
            raise RuntimeError("Không thấy nút 'Điểm danh' trong menu bot đích")

        kind, btn = found
        last_id = (await client.get_messages(entity, limit=1))[0].id
        # Bước 2: bấm nút điểm danh (một lần duy nhất)
        if kind == "inline":
            try:
                res = await source_msg.click(data=btn.data)
                answer = getattr(res, "message", None) if res else None
            except tele_errors.BotResponseTimeoutError:
                answer = None
            if answer:
                return _short(answer)
        else:
            sent2 = await client.send_message(entity, btn.text)
            last_id = sent2.id

        after = await _wait_reply(client, entity, last_id)
        if after:
            return _short(after.raw_text or "Đã bấm điểm danh")
        # tin cũ có thể đã bị bot sửa
        edited = await client.get_messages(entity, ids=source_msg.id)
        if edited and edited.edit_date:
            return _short(edited.raw_text or "Đã bấm điểm danh")
        return "Đã bấm điểm danh (bot đích không gửi phản hồi)"
    except tele_errors.FloodWaitError as e:
        raise RuntimeError(f"Telegram yêu cầu chờ {e.seconds}s (FloodWait)") from e
    except (tele_errors.AuthKeyUnregisteredError, tele_errors.SessionRevokedError,
            tele_errors.UserDeactivatedError) as e:
        raise RuntimeError(
            f"Session của {account_name} đã hết hạn/bị thu hồi. Hãy đăng nhập lại.") from e
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

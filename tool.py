"""
tool.py — Chức năng TIKTOK cho bot Telegram.

Tính năng:
  1. User gửi video vào bot  -> lưu video vào thư mục data/tiktok/videos
  2. Bot hỏi caption          -> user nhập caption
  3. Lưu {video, caption}     -> hàng đợi (queue) data/tiktok/queue.json
  4. 07:00 sáng mỗi ngày      -> lấy 1 video đầu hàng đợi, dùng Playwright
                                 mở https://www.tiktok.com/upload, upload video,
                                 điền caption, bấm Post
  5. Đăng xong                -> xoá video khỏi queue (và xoá file)

Cách tích hợp (đã làm sẵn trong bot.py):
    import tool as mod_tiktok
    ...
    mod_tiktok.register(app)          # trong main()
    mod_tiktok.start_scheduler(app)   # trong _post_init()

Lệnh Telegram:
    /tiktok            – hướng dẫn + trạng thái
    /tiktok_queue      – xem hàng đợi
    /tiktok_login      – mở trình duyệt để đăng nhập TikTok thủ công (lần đầu)
    /tiktok_post       – đăng ngay video đầu hàng đợi (test)
    /tiktok_clear      – xoá toàn bộ hàng đợi

Yêu cầu:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("tiktok")

# ─── cấu hình (có thể sửa qua biến môi trường) ─────────────────────────
BASE_DIR = os.getenv("TIKTOK_DIR", os.path.join("data", "tiktok"))
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
QUEUE_FILE = os.path.join(BASE_DIR, "queue.json")
STATE_FILE = os.path.join(BASE_DIR, "session.json")   # session TikTok (cookie)
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_run.json")

POST_HOUR = int(os.getenv("TIKTOK_POST_HOUR", "7"))    # 07:00
POST_MINUTE = int(os.getenv("TIKTOK_POST_MINUTE", "0"))
TZ = timezone(timedelta(hours=int(os.getenv("TIKTOK_TZ_OFFSET", "7"))))  # giờ VN
HEADLESS = os.getenv("TIKTOK_HEADLESS", "1") not in ("0", "false", "False")
UPLOAD_URL = "https://www.tiktok.com/upload?lang=vi-VN"

os.makedirs(VIDEO_DIR, exist_ok=True)

# user đang được hỏi caption: {user_id: {"file": path, "chat_id": int}}
_PENDING: dict[int, dict[str, Any]] = {}
_LOCK = asyncio.Lock()


# ─── helper: đọc/ghi JSON ──────────────────────────────────────────────
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ─── QUEUE ─────────────────────────────────────────────────────────────
def load_queue() -> list[dict]:
    q = _read_json(QUEUE_FILE, [])
    return q if isinstance(q, list) else []


def save_queue(queue: list[dict]) -> None:
    _write_json(QUEUE_FILE, queue)


def queue_add(file_path: str, caption: str, user_id: int) -> dict:
    item = {
        "id": uuid.uuid4().hex[:8],
        "file": file_path,
        "caption": caption,
        "user_id": user_id,
        "added_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    q = load_queue()
    q.append(item)
    save_queue(q)
    log.info("[tiktok] queue += %s (%s)", item["id"], os.path.basename(file_path))
    return item


def queue_pop_done(item_id: str) -> None:
    """Xoá item khỏi queue + xoá file video."""
    q = load_queue()
    keep, removed = [], None
    for it in q:
        if it.get("id") == item_id and removed is None:
            removed = it
        else:
            keep.append(it)
    save_queue(keep)
    if removed:
        try:
            os.remove(removed["file"])
        except Exception:
            pass
        log.info("[tiktok] đã xoá khỏi queue: %s", item_id)


# ─── PLAYWRIGHT: đăng TikTok ───────────────────────────────────────────
async def tiktok_login() -> str:
    """Mở trình duyệt (không headless) để user đăng nhập thủ công, rồi lưu session."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None
        )
        page = await ctx.new_page()
        await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        log.info("[tiktok] Đang chờ đăng nhập thủ công (tối đa 5 phút)…")
        # chờ tới khi vào được trang upload (đã login)
        deadline = time.time() + 300
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
                if "login" not in page.url:
                    break
            except Exception:
                pass
        await ctx.storage_state(path=STATE_FILE)
        await browser.close()
    return STATE_FILE


async def post_to_tiktok(video_path: str, caption: str) -> tuple[bool, str]:
    """Upload 1 video lên TikTok. Trả về (ok, message)."""
    if not os.path.exists(video_path):
        return False, f"Không tìm thấy file: {video_path}"
    if not os.path.exists(STATE_FILE):
        return False, "Chưa đăng nhập TikTok. Dùng /tiktok_login trước."

    from playwright.async_api import async_playwright

    log.info("[tiktok] Bắt đầu đăng: %s", os.path.basename(video_path))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            ctx = await browser.new_context(storage_state=STATE_FILE)
            page = await ctx.new_page()
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            if "login" in page.url:
                await browser.close()
                return False, "Session hết hạn — chạy lại /tiktok_login."

            # Trang upload nằm trong iframe (tuỳ phiên bản)
            frame = page
            for f in page.frames:
                if "upload" in (f.url or "") and f != page.main_frame:
                    frame = f
                    break

            # 1) chọn file
            log.info("[tiktok] Chọn file video…")
            file_input = frame.locator("input[type=file]").first
            await file_input.wait_for(state="attached", timeout=60000)
            await file_input.set_input_files(video_path)

            # 2) chờ video xử lý xong
            log.info("[tiktok] Đang chờ TikTok xử lý video…")
            await page.wait_for_timeout(20000)

            # 3) điền caption
            log.info("[tiktok] Điền caption…")
            editor = frame.locator("div[contenteditable='true']").first
            await editor.wait_for(state="visible", timeout=60000)
            await editor.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.keyboard.type(caption, delay=25)
            await page.wait_for_timeout(2000)

            # 4) bấm Post
            log.info("[tiktok] Bấm nút Post…")
            post_btn = frame.get_by_role("button", name="Post", exact=False).first
            try:
                await post_btn.wait_for(state="visible", timeout=30000)
            except Exception:
                post_btn = frame.locator("button:has-text('Đăng'), button:has-text('Post')").last
            await post_btn.click()

            # 5) chờ đăng xong
            await page.wait_for_timeout(30000)
            await ctx.storage_state(path=STATE_FILE)
            await browser.close()

        log.info("[tiktok] ✅ Đăng xong: %s", os.path.basename(video_path))
        return True, "Đăng thành công"
    except Exception as e:  # noqa: BLE001
        log.exception("[tiktok] ❌ Lỗi khi đăng")
        return False, f"Lỗi: {e}"


async def post_next(app: Optional[Application] = None) -> str:
    """Lấy video đầu hàng đợi và đăng. Trả về mô tả kết quả."""
    async with _LOCK:
        q = load_queue()
        if not q:
            return "📭 Hàng đợi trống — không có gì để đăng."
        item = q[0]
        ok, msg = await post_to_tiktok(item["file"], item.get("caption", ""))
        if ok:
            queue_pop_done(item["id"])
            result = f"✅ Đã đăng TikTok: <code>{item['id']}</code>\n📝 {item.get('caption','')}"
        else:
            result = f"❌ Đăng thất bại ({item['id']}): {msg}"

    if app is not None:
        uid = item.get("user_id")
        if uid:
            try:
                await app.bot.send_message(uid, result, parse_mode="HTML")
            except Exception:
                pass
    return result


# ─── SCHEDULER 07:00 hằng ngày ─────────────────────────────────────────
def _seconds_until_next_run() -> float:
    now = datetime.now(TZ)
    target = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _already_ran_today() -> bool:
    data = _read_json(LAST_RUN_FILE, {})
    return data.get("date") == datetime.now(TZ).strftime("%Y-%m-%d")


def _mark_ran_today() -> None:
    _write_json(LAST_RUN_FILE, {"date": datetime.now(TZ).strftime("%Y-%m-%d")})


async def _scheduler_loop(app: Application) -> None:
    log.info("[tiktok] Scheduler bật — đăng lúc %02d:%02d mỗi ngày (UTC+7)",
             POST_HOUR, POST_MINUTE)
    while True:
        try:
            wait = _seconds_until_next_run()
            log.info("[tiktok] Lần đăng kế tiếp sau %.0f phút", wait / 60)
            await asyncio.sleep(wait)
            if _already_ran_today():
                await asyncio.sleep(60)
                continue
            _mark_ran_today()
            result = await post_next(app)
            log.info("[tiktok] Kết quả job hằng ngày: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("[tiktok] Scheduler lỗi: %s", e)
            await asyncio.sleep(60)


def start_scheduler(app: Application) -> None:
    """Gọi trong post_init của bot."""
    asyncio.create_task(_scheduler_loop(app))


# ─── HANDLERS TELEGRAM ─────────────────────────────────────────────────
async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    vid = msg.video or (msg.document if (msg.document and (msg.document.mime_type or "").startswith("video/")) else None)
    if not vid:
        return

    await msg.reply_text("⏬ Đang tải video về…")
    try:
        f = await ctx.bot.get_file(vid.file_id)
        name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}.mp4"
        path = os.path.join(VIDEO_DIR, name)
        await f.download_to_drive(path)
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(f"❌ Không tải được video: {e}")
        raise ApplicationHandlerStop

    _PENDING[uid] = {"file": path, "chat_id": update.effective_chat.id}
    log.info("[tiktok] Đã lưu video %s từ user %s", name, uid)
    await msg.reply_text(
        "✅ Đã lưu video.\n\n📝 Hãy <b>nhập caption</b> cho video này "
        "(gửi /cancel_tiktok để huỷ).",
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop


async def on_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chỉ xử lý khi user đang ở bước nhập caption, còn lại nhường handler cũ."""
    uid = update.effective_user.id
    if uid not in _PENDING:
        return  # không chặn — bot cũ xử lý bình thường

    text = (update.effective_message.text or "").strip()
    if text.startswith("/cancel_tiktok"):
        info = _PENDING.pop(uid)
        try:
            os.remove(info["file"])
        except Exception:
            pass
        await update.effective_message.reply_text("❌ Đã huỷ video.")
        raise ApplicationHandlerStop
    if not text:
        return

    info = _PENDING.pop(uid)
    item = queue_add(info["file"], text, uid)
    pos = len(load_queue())
    await update.effective_message.reply_text(
        f"✅ Đã thêm vào hàng đợi TikTok!\n"
        f"🆔 <code>{item['id']}</code>\n"
        f"📝 {text}\n"
        f"📦 Vị trí: <b>{pos}</b> — đăng lúc {POST_HOUR:02d}:{POST_MINUTE:02d} mỗi ngày.",
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop


async def cmd_tiktok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = load_queue()
    logged = "✅ Đã đăng nhập" if os.path.exists(STATE_FILE) else "❌ Chưa đăng nhập"
    await update.effective_message.reply_text(
        "🎵 <b>TIKTOK AUTO POST</b>\n"
        f"🔐 Session: {logged}\n"
        f"📦 Hàng đợi: <b>{len(q)}</b> video\n"
        f"⏰ Đăng lúc <b>{POST_HOUR:02d}:{POST_MINUTE:02d}</b> mỗi ngày (giờ VN)\n\n"
        "➤ Gửi 1 video vào bot rồi nhập caption để thêm vào hàng đợi.\n"
        "➤ /tiktok_queue – xem hàng đợi\n"
        "➤ /tiktok_login – đăng nhập TikTok (lần đầu)\n"
        "➤ /tiktok_post – đăng ngay video đầu hàng đợi\n"
        "➤ /tiktok_clear – xoá hàng đợi",
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = load_queue()
    if not q:
        await update.effective_message.reply_text("📭 Hàng đợi trống.")
        raise ApplicationHandlerStop
    lines = [
        f"{i+1}. <code>{it['id']}</code> — {(it.get('caption') or '')[:60]}"
        for i, it in enumerate(q[:30])
    ]
    await update.effective_message.reply_text(
        "📦 <b>HÀNG ĐỢI TIKTOK</b>\n" + "\n".join(lines), parse_mode="HTML")
    raise ApplicationHandlerStop


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🌐 Đang mở trình duyệt để đăng nhập TikTok…\n"
        "Hãy đăng nhập thủ công trong cửa sổ vừa mở (tối đa 5 phút). "
        "Session sẽ được lưu lại để dùng cho các lần sau."
    )
    try:
        path = await tiktok_login()
        await update.effective_message.reply_text(f"✅ Đã lưu session: <code>{path}</code>",
                                                  parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f"❌ Lỗi đăng nhập: {e}")
    raise ApplicationHandlerStop


async def cmd_post_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("🚀 Đang đăng video đầu hàng đợi…")
    result = await post_next(None)
    await update.effective_message.reply_text(result, parse_mode="HTML")
    raise ApplicationHandlerStop


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for it in load_queue():
        try:
            os.remove(it["file"])
        except Exception:
            pass
    save_queue([])
    await update.effective_message.reply_text("🗑 Đã xoá toàn bộ hàng đợi TikTok.")
    raise ApplicationHandlerStop


def register(app: Application) -> None:
    """Gắn toàn bộ handler TikTok vào bot hiện tại (group=-1 để chạy trước)."""
    g = -1
    app.add_handler(CommandHandler("tiktok", cmd_tiktok), group=g)
    app.add_handler(CommandHandler("tiktok_queue", cmd_queue), group=g)
    app.add_handler(CommandHandler("tiktok_login", cmd_login), group=g)
    app.add_handler(CommandHandler("tiktok_post", cmd_post_now), group=g)
    app.add_handler(CommandHandler("tiktok_clear", cmd_clear), group=g)
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video), group=g)
    app.add_handler(MessageHandler(filters.TEXT, on_caption), group=g)
    log.info("[tiktok] Đã gắn handler TikTok vào bot.")

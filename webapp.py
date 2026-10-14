"""Telegram Mini App server — chạy CHUNG tiến trình & event loop với bot.

- Không tạo polling thứ hai: server aiohttp được khởi động trong post_init
  của python-telegram-bot, dùng chung asyncio loop với run_polling().
- Phục vụ giao diện tĩnh tại "/" (thư mục ./webapp) và API tại "/api/*".
- Mọi API đều xác thực initData của Telegram Web Apps bằng HMAC-SHA256
  với BOT_TOKEN (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import Chat, Message, Update, User
from telegram.error import Forbidden
from telegram.ext import Application, CallbackContext

from config import BOT_TOKEN
from modules import admins as mod_admins
from modules.session_mgr import list_sessions_for

log = logging.getLogger("webapp")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
INIT_DATA_MAX_AGE = int(os.getenv("WEBAPP_AUTH_MAX_AGE", "86400"))  # giây

# Các action Mini App được phép gọi (đều là action có sẵn của bot)
ALLOWED_ACTIONS = {
    "m:home", "m:check", "m:auto", "m:rai", "m:ref", "m:spam", "m:join",
    "m:duyet", "m:naprut", "m:acc", "m:admins", "m:logs", "m:status", "m:stop",
}


def get_webapp_url() -> Optional[str]:
    """URL HTTPS công khai của Mini App.
    Ưu tiên WEBAPP_URL, sau đó RAILWAY_PUBLIC_DOMAIN (Railway tự cấp khi Generate Domain)."""
    url = os.getenv("WEBAPP_URL", "").strip()
    if not url:
        dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if dom:
            url = f"https://{dom}"
    if not url:
        return None
    if not url.startswith("https://"):
        url = "https://" + url.split("://", 1)[-1]
    return url.rstrip("/") + "/"


# ─── initData validation ────────────────────────────────
def validate_init_data(init_data: str) -> Optional[dict]:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    recv_hash = pairs.pop("hash", None)
    if not recv_hash:
        return None
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if INIT_DATA_MAX_AGE > 0 and time.time() - auth_date > INIT_DATA_MAX_AGE:
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


class Hooks:
    """Các hàm của bot.py được truyền vào (tránh import vòng với __main__)."""

    def __init__(self, dispatch: Callable[..., Awaitable[Any]],
                 get_state: Callable[[int], Any],
                 get_np_status: Callable[[], dict],
                 schedule_expiry: Callable[[Application, int, int], Any],
                 rent_contact: str):
        self.dispatch = dispatch
        self.get_state = get_state
        self.get_np_status = get_np_status
        self.schedule_expiry = schedule_expiry
        self.rent_contact = rent_contact


def _make_update(app: Application, tg_user: dict) -> Update:
    """Tạo Update giả lập 1 tin nhắn private từ user, để tái sử dụng
    nguyên vẹn dispatch_action() của bot (kết quả gửi vào chat với bot)."""
    uid = int(tg_user["id"])
    user = User(id=uid, is_bot=False,
                first_name=tg_user.get("first_name") or "User",
                last_name=tg_user.get("last_name"),
                username=tg_user.get("username"))
    chat = Chat(id=uid, type=Chat.PRIVATE)
    # FIX: python-telegram-bot chỉ cho dùng shortcut (chat.send_message,
    # message.reply_text, user.send_message…) khi CHÍNH object đó có bot.
    # Trước đây chỉ Message/Update được set_bot → update.effective_chat.send_message()
    # ném "This object has no bot associated with it. Shortcuts cannot be used."
    # Phải gắn bot cho User và Chat TRƯỚC khi lồng vào Message.
    user.set_bot(app.bot)
    chat.set_bot(app.bot)
    msg = Message(message_id=0, date=datetime.now(timezone.utc), chat=chat,
                  from_user=user, text="")
    msg.set_bot(app.bot)
    upd = Update(update_id=0, message=msg)
    upd.set_bot(app.bot)
    # Kiểm tra an toàn: mọi object shortcut phải có bot
    for obj in (upd.effective_chat, upd.effective_user, upd.effective_message):
        if obj is not None:
            try:
                obj.get_bot()
            except RuntimeError:
                obj.set_bot(app.bot)
    return upd


async def _read_auth(request: web.Request) -> tuple[dict, dict]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    init_data = body.get("initData") or request.headers.get("X-Telegram-Init-Data", "")
    user = validate_init_data(init_data)
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid_init_data"}),
                                   content_type="application/json")
    return user, body


def build_web_app(app: Application, hooks: Hooks) -> web.Application:
    wa = web.Application()

    async def api_me(request: web.Request):
        user, _ = await _read_auth(request)
        uid = int(user["id"])
        base = {"uid": uid, "first_name": user.get("first_name", ""),
                "username": user.get("username"), "rent_contact": hooks.rent_contact,
                "bot_username": getattr(app.bot, "username", None)}
        if not mod_admins.is_admin(uid):
            return web.json_response({**base, "admin": False})
        s = hooks.get_state(uid)
        perms = {k: mod_admins.has_perm(uid, k) for k in mod_admins.PERM_KEYS}
        task_running = bool(s.task and not s.task.done())
        return web.json_response({
            **base,
            "admin": True,
            "owner": mod_admins.is_owner(uid),
            "remaining": mod_admins.format_remaining(uid),
            "expiry_at": mod_admins.format_expiry_at(uid),
            "perms": perms,
            "perm_labels": mod_admins.granted_perm_labels(uid),
            "sessions": list_sessions_for(uid),
            "status": s.status,
            "step": s.step,
            "task_running": task_running,
            "logs": list(s.log_buf)[-60:],
            "naprut": hooks.get_np_status() or {},
        })

    async def api_action(request: web.Request):
        user, body = await _read_auth(request)
        uid = int(user["id"])
        if not mod_admins.is_admin(uid):
            return web.json_response({"ok": False, "error": "not_admin"}, status=403)
        action = str(body.get("action", ""))
        if action not in ALLOWED_ACTIONS:
            return web.json_response({"ok": False, "error": "bad_action"}, status=400)
        upd = _make_update(app, user)
        ctx = CallbackContext.from_update(upd, app)
        try:
            if action == "m:home":
                hooks.schedule_expiry(app, uid, uid)
            await hooks.dispatch(upd, ctx, action)
        except Forbidden:
            # User chưa /start bot hoặc đã chặn bot → bot không thể nhắn vào chat
            log.warning("Mini App action %s: user %s chưa start/đã chặn bot", action, uid)
            return web.json_response({"ok": False, "error": "bot_blocked",
                                      "bot_username": getattr(app.bot, "username", None)},
                                     status=409)
        except Exception as e:  # không để lỗi làm sập bot
            log.exception("Mini App action %s failed", action)
            return web.json_response({"ok": False, "error": str(e),
                                      "bot_username": getattr(app.bot, "username", None)},
                                     status=500)
        return web.json_response({"ok": True})

    async def index(_request: web.Request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"),
                                headers={"Cache-Control": "no-cache"})

    async def health(_request: web.Request):
        return web.json_response({"ok": True})

    wa.router.add_get("/", index)
    wa.router.add_get("/health", health)
    wa.router.add_post("/api/me", api_me)
    wa.router.add_post("/api/action", api_action)
    wa.router.add_static("/static/", WEB_DIR, show_index=False)
    return wa


async def start_web_server(app: Application, hooks: Hooks) -> web.AppRunner:
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(build_web_app(app, hooks), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Mini App server listening on 0.0.0.0:%s (url=%s)", port, get_webapp_url())
    return runner

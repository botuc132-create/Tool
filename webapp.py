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
import asyncio
import contextvars
import inspect
import re
import secrets
from pathlib import Path
import itertools
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import Bot, Chat, InlineKeyboardMarkup, InputFile, Message, Update, User
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


# ─── Chat mirror: chạy bot NGAY TRONG Mini App ─────────────────────────
# Mọi tin nhắn bot gửi/sửa cho 1 chat được ghi lại để Mini App hiển thị như
# một khung chat (kèm nút inline). User trả lời / bấm nút ngay trong Mini App
# → tạo Update tương ứng và đưa vào app.process_update(), nên toàn bộ logic,
# step nhập liệu, task chạy nền… giữ NGUYÊN như khi dùng ngoài chat.
# Task chạy trên server (asyncio) nên đóng Mini App / thoát Telegram vẫn chạy.
MIRROR_MAX = int(os.getenv("WEBAPP_MIRROR_MAX", "60"))
_MIRROR: dict[int, "OrderedDict[str, dict]"] = {}
_SEQ = itertools.count(1)
_FAKE_ID = itertools.count(1)
_TOASTS: dict[int, list] = {}


def _kb_to_json(markup) -> list:
    rows = []
    try:
        for row in (getattr(markup, "inline_keyboard", None) or []):
            r = []
            for b in row:
                r.append({"text": b.text, "data": b.callback_data, "url": b.url})
            rows.append(r)
    except Exception:
        pass
    return rows


def _record(msg: Any, outgoing_text: Optional[str] = None) -> None:
    if not isinstance(msg, Message) or msg.chat is None:
        return
    cid = int(msg.chat.id)
    box = _MIRROR.setdefault(cid, OrderedDict())
    key = str(msg.message_id)
    try:
        html = msg.text_html if msg.text else (msg.caption_html if msg.caption else "")
    except Exception:
        html = msg.text or msg.caption or ""
    doc = None
    if msg.document:
        doc = msg.document.file_name or "file"
    elif msg.photo:
        doc = "📷 Ảnh"
    item = box.get(key) or {"id": msg.message_id, "from": "bot"}
    item.update({"html": html, "kb": _kb_to_json(msg.reply_markup), "doc": doc,
                 "seq": next(_SEQ), "ts": int(time.time())})
    box[key] = item
    box.move_to_end(key)
    while len(box) > MIRROR_MAX:
        box.popitem(last=False)


def _record_user(cid: int, text: str) -> None:
    box = _MIRROR.setdefault(cid, OrderedDict())
    key = f"u{next(_FAKE_ID)}"
    box[key] = {"id": key, "from": "user", "html": _esc(text), "kb": [], "doc": None,
                "seq": next(_SEQ), "ts": int(time.time())}
    while len(box) > MIRROR_MAX:
        box.popitem(last=False)


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _drop(chat_id: Any, message_id: Any) -> None:
    try:
        box = _MIRROR.get(int(chat_id))
        if box:
            box.pop(str(message_id), None)
    except Exception:
        pass


_PATCHED = False

# Khi xử lý thao tác đến TỪ Mini App, biến này = uid của user. Mọi tin bot gửi
# cho chính user đó (kể cả từ task chạy nền tạo ra trong lúc ấy) sẽ CHỈ hiện
# trong Mini App, không gửi ra chat Telegram. Thao tác từ chat Telegram thì
# vẫn trả lời trong chat như cũ.
ORIGIN: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("wa_origin", default=None)
_FAKE_MSG_BASE = 1_900_000_000          # id tin nhắn "ảo" chỉ tồn tại trong Mini App
_FAKE_MSG = itertools.count(_FAKE_MSG_BASE)
_FILES: "OrderedDict[str, tuple]" = OrderedDict()   # token -> (filename, bytes, uid)
FILES_MAX = 30


def _is_fake_id(mid: Any) -> bool:
    try:
        return int(mid) >= _FAKE_MSG_BASE
    except Exception:
        return False


def _to_app(chat_id: Any) -> bool:
    """True nếu tin nhắn tới chat này phải hiển thị trong Mini App thay vì Telegram."""
    uid = ORIGIN.get()
    try:
        return uid is not None and int(chat_id) == int(uid)
    except Exception:
        return False


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _html_of(text: Optional[str], parse_mode: Any) -> str:
    if not text:
        return ""
    pm = str(parse_mode or "").upper()
    if pm.endswith("HTML"):
        return text
    return _esc(text)


def _app_message(bot: Bot, chat_id: int, message_id: int, html: str, markup: Any,
                 doc: Optional[str] = None, file_token: Optional[str] = None,
                 new: bool = True) -> Message:
    """Ghi tin nhắn vào khung chat Mini App và trả về Message giả (để code bot dùng tiếp)."""
    cid = int(chat_id)
    box = _MIRROR.setdefault(cid, OrderedDict())
    key = str(message_id)
    item = box.get(key) or {"id": message_id, "from": "bot", "html": "", "kb": [], "doc": None}
    if html is not None:
        item["html"] = html
    if markup is not None or new:
        item["kb"] = _kb_to_json(markup)
    if doc:
        item["doc"] = doc
    if file_token:
        item["file"] = file_token
    item["seq"] = next(_SEQ)
    item["ts"] = int(time.time())
    box[key] = item
    box.move_to_end(key)
    while len(box) > MIRROR_MAX:
        box.popitem(last=False)
    chat = Chat(id=cid, type=Chat.PRIVATE)
    me = getattr(bot, "_bot_user", None)
    msg = Message(message_id=int(message_id), date=datetime.now(timezone.utc), chat=chat,
                  from_user=me, text=_plain(item["html"]) or None,
                  reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None)
    msg.set_bot(bot)
    chat.set_bot(bot)
    return msg


def _read_file(obj: Any, filename: Optional[str]) -> tuple:
    name = filename
    data = b""
    try:
        if isinstance(obj, InputFile):
            data = obj.input_file_content
            name = name or obj.filename
        elif isinstance(obj, (bytes, bytearray)):
            data = bytes(obj)
        elif hasattr(obj, "read"):
            data = obj.read()
            name = name or os.path.basename(getattr(obj, "name", "") or "")
        elif isinstance(obj, (str, Path)) and os.path.isfile(str(obj)):
            with open(str(obj), "rb") as f:
                data = f.read()
            name = name or os.path.basename(str(obj))
    except Exception:
        log.exception("Mini App: không đọc được file")
    return (name or "file"), data


def _store_file(uid: int, name: str, data: bytes) -> Optional[str]:
    if not data:
        return None
    token = secrets.token_urlsafe(18)
    _FILES[token] = (name, data, uid)
    while len(_FILES) > FILES_MAX:
        _FILES.popitem(last=False)
    return token


def install_mirror() -> None:
    """Bọc các hàm gửi tin của Bot (ExtBot gọi super() nên bọc lớp gốc là đủ).
    - Thao tác từ Mini App  → tin nhắn chỉ hiện trong Mini App.
    - Thao tác từ chat     → gửi Telegram như cũ (và cũng hiện trong Mini App)."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def args_of(orig, self, a, kw) -> dict:
        try:
            b = inspect.signature(orig).bind_partial(self, *a, **kw)
            d = dict(b.arguments)
            d.update(d.pop("kwargs", {}) or {})
            return d
        except Exception:
            return dict(kw)

    def wrap(name, app_handler):
        orig = getattr(Bot, name)

        async def w(self, *a, **kw):
            p = args_of(orig, self, a, kw)
            if app_handler is not None:
                try:
                    res = app_handler(self, p)
                except Exception:
                    log.exception("Mini App intercept %s failed", name)
                    res = None
                if res is not None:
                    return res
            res = await orig(self, *a, **kw)
            try:
                _record(res)
            except Exception:
                log.debug("mirror record failed", exc_info=True)
            return res
        setattr(Bot, name, w)

    def h_send_message(bot, p):
        if not _to_app(p.get("chat_id")):
            return None
        return _app_message(bot, p["chat_id"], next(_FAKE_MSG),
                            _html_of(p.get("text"), p.get("parse_mode")), p.get("reply_markup"))

    def h_edit_text(bot, p):
        cid, mid = p.get("chat_id"), p.get("message_id")
        if cid is None or mid is None:
            return None
        if not (_to_app(cid) or _is_fake_id(mid)):
            return None
        return _app_message(bot, cid, int(mid), _html_of(p.get("text"), p.get("parse_mode")),
                            p.get("reply_markup"), new=False)

    def h_edit_caption(bot, p):
        cid, mid = p.get("chat_id"), p.get("message_id")
        if cid is None or mid is None or not (_to_app(cid) or _is_fake_id(mid)):
            return None
        return _app_message(bot, cid, int(mid), _html_of(p.get("caption"), p.get("parse_mode")),
                            p.get("reply_markup"), new=False)

    def h_edit_markup(bot, p):
        cid, mid = p.get("chat_id"), p.get("message_id")
        if cid is None or mid is None or not (_to_app(cid) or _is_fake_id(mid)):
            return None
        return _app_message(bot, cid, int(mid), None, p.get("reply_markup") or InlineKeyboardMarkup([]),
                            new=False)

    def h_media(field, label):
        def h(bot, p):
            cid = p.get("chat_id")
            if not _to_app(cid):
                return None
            name, data = _read_file(p.get(field), p.get("filename"))
            token = _store_file(int(cid), name, data)
            return _app_message(bot, cid, next(_FAKE_MSG),
                                _html_of(p.get("caption"), p.get("parse_mode")),
                                p.get("reply_markup"), doc=(name if label is None else label),
                                file_token=token)
        return h

    def h_chat_action(bot, p):
        return True if _to_app(p.get("chat_id")) else None

    table = {
        "send_message": h_send_message,
        "edit_message_text": h_edit_text,
        "edit_message_caption": h_edit_caption,
        "edit_message_reply_markup": h_edit_markup,
        "send_document": h_media("document", None),
        "send_photo": h_media("photo", "📷 Ảnh"),
        "send_video": h_media("video", "🎬 Video"),
        "send_audio": h_media("audio", None),
        "send_chat_action": h_chat_action,
    }
    for n, h in table.items():
        if hasattr(Bot, n):
            wrap(n, h)

    orig_del = Bot.delete_message

    async def w_del(self, chat_id, message_id, *a, **kw):
        if _is_fake_id(message_id) or _to_app(chat_id) and str(message_id) in (_MIRROR.get(int(chat_id)) or {}):
            _drop(chat_id, message_id)
            if _is_fake_id(message_id):
                return True
        res = await orig_del(self, chat_id, message_id, *a, **kw)
        _drop(chat_id, message_id)
        return res
    Bot.delete_message = w_del

    orig_ans = Bot.answer_callback_query

    async def w_ans(self, callback_query_id, text=None, show_alert=None, *a, **kw):
        # Callback giả lập từ Mini App: không gọi Telegram, chuyển text thành toast
        if str(callback_query_id).startswith("wa_"):
            try:
                uid = int(str(callback_query_id).split("_")[1])
                if text:
                    _TOASTS.setdefault(uid, []).append(text)
            except Exception:
                pass
            return True
        return await orig_ans(self, callback_query_id, text, show_alert, *a, **kw)
    Bot.answer_callback_query = w_ans


def _user_dict(tg_user: dict) -> dict:
    d = {"id": int(tg_user["id"]), "is_bot": False,
         "first_name": tg_user.get("first_name") or "User"}
    for k in ("last_name", "username", "language_code"):
        if tg_user.get(k):
            d[k] = tg_user[k]
    return d


def _fake_text_update(app: Application, tg_user: dict, text: str) -> Update:
    u = _user_dict(tg_user)
    data = {"update_id": 0, "message": {
        "message_id": -next(_FAKE_ID), "date": int(time.time()),
        "chat": {"id": u["id"], "type": "private", "first_name": u["first_name"]},
        "from": u, "text": text}}
    if text.startswith("/"):
        cmd = text.split()[0]
        data["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
    return Update.de_json(data, app.bot)


def _fake_callback_update(app: Application, tg_user: dict, message_id: int,
                          cb_data: str, html: str = "") -> Update:
    u = _user_dict(tg_user)
    data = {"update_id": 0, "callback_query": {
        "id": f"wa_{u['id']}_{next(_FAKE_ID)}", "from": u, "chat_instance": "webapp",
        "data": cb_data,
        "message": {"message_id": int(message_id), "date": int(time.time()),
                    "chat": {"id": u["id"], "type": "private", "first_name": u["first_name"]},
                    "from": {"id": app.bot.id, "is_bot": True,
                             "first_name": app.bot.first_name or "bot"},
                    "text": html or " "}}}
    return Update.de_json(data, app.bot)


def _run_bg(app: Application, upd: Update, label: str) -> None:
    uid = upd.effective_user.id if upd.effective_user else None

    async def runner():
        ORIGIN.set(uid)  # task con tạo ra từ đây cũng thừa hưởng → chạy trong Mini App
        try:
            await app.process_update(upd)
        except Exception:
            log.exception("Mini App %s failed", label)
    asyncio.create_task(runner())


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
            # Chạy nền: chức năng dài (task) không làm treo Mini App
            async def _go():
                ORIGIN.set(uid)  # kết quả chỉ hiện trong Mini App
                try:
                    await hooks.dispatch(upd, ctx, action)
                except Exception as e:
                    log.exception("Mini App action %s failed", action)
                    _TOASTS.setdefault(uid, []).append(f"❌ {e}")
            asyncio.create_task(_go())
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

    async def api_chat(request: web.Request):
        user, body = await _read_auth(request)
        uid = int(user["id"])
        since = int(body.get("since") or 0)
        box = _MIRROR.get(uid) or {}
        items = sorted(box.values(), key=lambda x: x["seq"])
        s = hooks.get_state(uid) if mod_admins.is_admin(uid) else None
        return web.json_response({
            "messages": items,
            "changed": any(x["seq"] > since for x in items),
            "last": max([x["seq"] for x in items] or [0]),
            "toasts": _TOASTS.pop(uid, []),
            "step": getattr(s, "step", None),
            "status": getattr(s, "status", None),
            "task_running": bool(s and s.task and not s.task.done()),
        })

    async def api_send(request: web.Request):
        user, body = await _read_auth(request)
        text = str(body.get("text") or "").strip()[:4096]
        if not text:
            return web.json_response({"ok": False, "error": "empty"}, status=400)
        _record_user(int(user["id"]), text)
        _run_bg(app, _fake_text_update(app, user, text), "text")
        return web.json_response({"ok": True})

    async def api_press(request: web.Request):
        user, body = await _read_auth(request)
        uid = int(user["id"])
        cb = str(body.get("data") or "")
        mid = body.get("message_id")
        box = _MIRROR.get(uid) or {}
        item = box.get(str(mid))
        # Chỉ cho bấm nút thật sự có trên tin nhắn bot đã gửi cho user này
        if not item or not any(b.get("data") == cb for row in item["kb"] for b in row):
            return web.json_response({"ok": False, "error": "button_not_found"}, status=400)
        _run_bg(app, _fake_callback_update(app, user, int(mid), cb, item.get("html", "")), "press")
        return web.json_response({"ok": True})

    async def get_file(request: web.Request):
        f = _FILES.get(request.match_info["token"])
        if not f:
            raise web.HTTPNotFound()
        name, data, _uid = f
        return web.Response(body=data, content_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    async def index(_request: web.Request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"),
                                headers={"Cache-Control": "no-cache"})

    async def health(_request: web.Request):
        return web.json_response({"ok": True})

    wa.router.add_get("/", index)
    wa.router.add_get("/health", health)
    wa.router.add_post("/api/me", api_me)
    wa.router.add_post("/api/action", api_action)
    wa.router.add_post("/api/chat", api_chat)
    wa.router.add_post("/api/send", api_send)
    wa.router.add_post("/api/press", api_press)
    wa.router.add_get("/f/{token}", get_file)
    wa.router.add_static("/static/", WEB_DIR, show_index=False)
    return wa


async def start_web_server(app: Application, hooks: Hooks) -> web.AppRunner:
    install_mirror()
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(build_web_app(app, hooks), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Mini App server listening on 0.0.0.0:%s (url=%s)", port, get_webapp_url())
    return runner

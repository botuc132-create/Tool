"""
FULL TOOL Bot – button menu, conversation flow.
Token & admin có thể override bằng env BOT_TOKEN / ADMIN_IDS.
"""
import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from telethon import TelegramClient, errors as tele_errors

from config import BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH, SESSIONS_DIR
from modules.session_mgr import (
    list_sessions, list_sessions_for, make_client, delete_session, session_path,
)

# Liên hệ thuê bot hiển thị cho user chưa có quyền / hết hạn
RENT_CONTACT = "@huybuwin"
RENT_MESSAGE = f"💼 Liên Hệ Admin Để Thuê: {RENT_CONTACT}"
from modules import check as mod_check
from modules import auto as mod_auto
from modules import rai as mod_rai
from modules import ref as mod_ref
from modules import sex as mod_spam
from modules import admins as mod_admins
from modules import join as mod_join
from modules import messages as mod_msg
from modules import duyet as mod_duyet

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

SEP = "━━━━━━━━━━━━━━━━━━"

# ─── per-user runtime state ─────────────────────────────
@dataclass
class UserState:
    # conversation step e.g. "check.link", "auto.delay", "addacc.code"
    step: Optional[str] = None
    data: dict = field(default_factory=dict)
    # login flow
    login_client: Optional[TelegramClient] = None
    login_phone: Optional[str] = None
    # active task
    task: Optional[asyncio.Task] = None
    stop_event: Optional[asyncio.Event] = None
    log_buf: deque = field(default_factory=lambda: deque(maxlen=200))
    log_msg_id: Optional[int] = None
    log_chat_id: Optional[int] = None
    last_log_flush: float = 0.0
    status: str = "IDLE"  # IDLE / RUNNING / STOPPED / ERROR / FLOODWAIT
    # Nhắc / kick khi hết hạn
    expiry_warn_task: Optional[asyncio.Task] = None
    expiry_kick_task: Optional[asyncio.Task] = None
    warned_10m: bool = False


STATES: dict[int, UserState] = {}


def st(uid: int) -> UserState:
    if uid not in STATES:
        STATES[uid] = UserState()
    return STATES[uid]


def is_admin(uid: int) -> bool:
    return mod_admins.is_admin(uid)


def has_perm(uid: int, perm: str) -> bool:
    return mod_admins.has_perm(uid, perm)


# ─── keyboards ──────────────────────────────────────────
def kb_main(uid: int | None = None):
    # Reply keyboard ở bàn phím — vuốt xuống là ẩn được
    rows = [
        ["🛡 CHECK JOIN", "🤖 AUTO"],
        ["📢 TOOL RẢI", "🎯 REF"],
        ["💬 SEX SPAM", "🚪 JOIN GROUP"],
        ["✅ DUYỆT NẠP"],
        ["👤 QUẢN LÝ ACC"],
    ]
    # Chỉ Owner mới thấy ô QUẢN LÝ ADMIN
    if uid is not None and mod_admins.is_owner(uid):
        rows.append(["🛠 QUẢN LÝ ADMIN"])
    rows += [
        ["📜 LOGS", "📊 STATUS"],
        ["⛔ STOP TASK"],
    ]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=False,
        input_field_placeholder="Chọn chức năng hoặc nhập lệnh…",
    )


# Giữ map cũ để tương thích (nếu user vẫn còn reply keyboard cũ)
REPLY_TEXT_MAP = {
    "🛡 CHECK JOIN": "m:check",
    "🤖 AUTO":       "m:auto",
    "📢 TOOL RẢI":   "m:rai",
    "🎯 REF":        "m:ref",
    "💬 SEX SPAM":   "m:spam",
    "🚪 JOIN GROUP": "m:join",
    "✅ DUYỆT NẠP":  "m:duyet",
    "👤 QUẢN LÝ ACC": "m:acc",
    "🛠 QUẢN LÝ ADMIN": "m:admins",
    "📜 LOGS":   "m:logs",
    "📊 STATUS": "m:status",
    "⛔ STOP TASK": "m:stop",
    "🏠 HOME": "m:home",
}

# Các step đang chờ user nhập NỘI DUNG TỰ DO (cho phép cả /command)
FREEFORM_STEPS = {"spam.msg", "rai.msg"}



def kb_acc():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ THÊM ACC",     callback_data="acc:add"),
         InlineKeyboardButton("📋 DANH SÁCH",    callback_data="acc:list")],
        [InlineKeyboardButton("🗑 XOÁ ACC",      callback_data="acc:del"),
         InlineKeyboardButton("💓 CHECK LIVE",   callback_data="acc:live")],
        [InlineKeyboardButton("📥 IMPORT SESSION", callback_data="acc:import"),
         InlineKeyboardButton("📤 EXPORT SESSION", callback_data="acc:export")],
        [InlineKeyboardButton("📩 ĐỌC TIN NHẮN TK", callback_data="acc:read")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="m:home")],
    ])


def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ HUỶ",  callback_data="ctrl:cancel"),
         InlineKeyboardButton("🏠 HOME", callback_data="m:home")],
    ])


def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ XÁC NHẬN", callback_data="ctrl:yes"),
         InlineKeyboardButton("❌ HUỶ",      callback_data="ctrl:cancel")],
    ])


def kb_back_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ BACK", callback_data="m:home")],
    ])


# ─── helpers ────────────────────────────────────────────
async def send(update: Update, text: str, kb=None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await update.effective_chat.send_message(
        text, reply_markup=kb, parse_mode=ParseMode.HTML)


def banner(title: str, body: str) -> str:
    return f"{SEP}\n<b>{title}</b>\n{SEP}\n\n{body}"


def home_banner(uid: int) -> str:
    # Format menu chính theo yêu cầu
    owner_id = next(iter(sorted(ADMIN_IDS))) if ADMIN_IDS else "—"
    remain = mod_admins.format_remaining(uid)
    perms = mod_admins.granted_perm_labels(uid)
    perms_txt = ", ".join(perms) if perms else "(chưa có)"
    return (
        f"{SEP}\n"
        f"🧰 <b>FULL TOOL — MENU</b>\n"
        f"{SEP}\n\n"
        f"👮 Admin: <code>{owner_id}</code>\n\n"
        f"⭕️ Thời Gian Hết Hạn Còn: <b>{remain}</b>\n"
        f"⭕️ Các Chức năng Được Sử Dụng: {perms_txt}\n\n"
        f"🏵️ Admin: {RENT_CONTACT}"
    )


async def show_home(update: Update):
    uid = update.effective_user.id
    text = home_banner(uid)
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    await update.effective_chat.send_message(
        text, reply_markup=kb_main(uid), parse_mode=ParseMode.HTML)



# ─── log streaming ──────────────────────────────────────
def _format_log(s: UserState) -> str:
    lines = list(s.log_buf)[-30:]
    body = "\n".join(lines) if lines else "(chưa có log)"
    return banner(f"📜 LIVE LOG — {s.status}", f"<pre>{_html_escape(body)}</pre>")


def _html_escape(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def make_logger(app: Application, uid: int, chat_id: int):
    s = st(uid)
    s.log_buf.clear()
    s.last_log_flush = 0
    msg = await app.bot.send_message(
        chat_id, _format_log(s),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ STOP", callback_data="m:stop"),
             InlineKeyboardButton("🏠 HOME", callback_data="m:home")]
        ]),
        parse_mode=ParseMode.HTML,
    )
    s.log_msg_id = msg.message_id
    s.log_chat_id = chat_id

    async def _log(text: str):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        s.log_buf.append(line)
        log.info(text)
        now = time.time()
        if now - s.last_log_flush >= 1.2:
            s.last_log_flush = now
            try:
                await app.bot.edit_message_text(
                    _format_log(s), chat_id=chat_id,
                    message_id=s.log_msg_id, parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⛔ STOP", callback_data="m:stop"),
                         InlineKeyboardButton("🏠 HOME", callback_data="m:home")]
                    ]),
                )
            except Exception:
                pass
    return _log


async def finalize_log(app: Application, uid: int):
    s = st(uid)
    if s.log_msg_id and s.log_chat_id:
        try:
            await app.bot.edit_message_text(
                _format_log(s), chat_id=s.log_chat_id,
                message_id=s.log_msg_id, parse_mode=ParseMode.HTML,
                reply_markup=kb_back_home(),
            )
        except Exception:
            pass


# ─── expiry scheduling ──────────────────────────────────
async def _expiry_watcher(app: Application, uid: int, chat_id: int):
    """Cảnh báo trước 10 phút và thông báo khi hết hạn."""
    try:
        s = st(uid)
        while True:
            sec = mod_admins.expiry_seconds_left(uid)
            if sec is None:
                return  # vô thời hạn
            if sec <= 0:
                try:
                    await app.bot.send_message(
                        chat_id,
                        "❌ Tài khoản của bạn đã hết hạn.\n"
                        f"💼 Liên Hệ Admin Để Thuê: {RENT_CONTACT}",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass
                return
            if sec <= 600 and not s.warned_10m:
                s.warned_10m = True
                try:
                    await app.bot.send_message(
                        chat_id,
                        "⏰ Bạn Sắp Hết Thời Gian Sử Dụng "
                        f"Liên Hệ Admin Để Thuê Thêm: {RENT_CONTACT}",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            # Ngủ tới mốc cảnh báo / hết hạn
            if sec > 600:
                await asyncio.sleep(min(sec - 600, 300))
            else:
                await asyncio.sleep(min(sec, 30))
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("expiry watcher error")


def schedule_expiry(app: Application, uid: int, chat_id: int):
    s = st(uid)
    if s.expiry_warn_task and not s.expiry_warn_task.done():
        return
    s.warned_10m = False
    s.expiry_warn_task = asyncio.create_task(_expiry_watcher(app, uid, chat_id))


# ─── /start ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            f"{RENT_MESSAGE}\n\nUser ID: <code>{uid}</code>",
            parse_mode=ParseMode.HTML)
        return
    s = st(uid)
    # Nếu đang ở step nhập nội dung tự do thì coi /start là text user gõ
    if s.step in FREEFORM_STEPS:
        await on_text(update, ctx)
        return
    schedule_expiry(ctx.application, uid, update.effective_chat.id)
    await show_home(update)



# ─── dispatch (dùng chung cho callback + reply keyboard) ─
async def dispatch_action(update, ctx, action: str):
    uid = update.effective_user.id
    s = st(uid)
    if action == "m:home":
        s.step = None; s.data.clear()
        await show_home(update); return
    if action == "m:stop":
        if not has_perm(uid, "STOP"):
            await send(update, "❌ Không có quyền STOP", kb_main(uid)); return
        await handle_stop(update, ctx); return
    if action == "m:status":
        if not has_perm(uid, "LOGS"):
            await send(update, "❌ Không có quyền", kb_main(uid)); return
        await send(update, banner("📊 STATUS",
            f"Trạng thái: <b>{s.status}</b>\n"
            f"Step hiện tại: <code>{s.step or '-'}</code>\n"
            f"Sessions: <b>{len(list_sessions_for(update.effective_user.id))}</b>"), kb_back_home()); return
    if action == "m:logs":
        if not has_perm(uid, "LOGS"):
            await send(update, "❌ Không có quyền", kb_main(uid)); return
        await send(update, _format_log(s), kb_back_home()); return
    if action == "m:check":
        if not has_perm(uid, "CHECK"):
            await send(update, "❌ Không có quyền CHECK", kb_main(uid)); return
        await flow_check_start(update, ctx); return
    if action == "m:auto":
        if not has_perm(uid, "AUTO"):
            await send(update, "❌ Không có quyền AUTO", kb_main(uid)); return
        await flow_auto_start(update, ctx); return
    if action == "m:rai":
        if not has_perm(uid, "RAI"):
            await send(update, "❌ Không có quyền TOOL RẢI", kb_main(uid)); return
        await flow_rai_start(update, ctx); return
    if action == "m:ref":
        if not has_perm(uid, "REF"):
            await send(update, "❌ Không có quyền REF", kb_main(uid)); return
        await flow_ref_start(update, ctx); return
    if action == "m:spam":
        if not has_perm(uid, "SPAM"):
            await send(update, "❌ Không có quyền SPAM", kb_main(uid)); return
        await flow_spam_start(update, ctx); return
    if action == "m:join":
        if not has_perm(uid, "JOIN"):
            await send(update, "❌ Không có quyền JOIN", kb_main(uid)); return
        await flow_join_start(update, ctx); return
    if action == "m:duyet":
        if not has_perm(uid, "DUYET"):
            await send(update, "❌ Không có quyền DUYỆT NẠP", kb_main(uid)); return
        await flow_duyet_start(update, ctx); return
    if action == "m:admins":
        if not mod_admins.is_owner(uid):
            await send(update, "❌ Chỉ Owner mới được quản lý admin", kb_main(uid)); return
        await admins_home(update, ctx); return
    if action == "m:acc":
        if not has_perm(uid, "MANAGE_ACC"):
            await send(update, "❌ Không có quyền Quản lý TK", kb_main(uid)); return
        await send(update, banner("👤 QUẢN LÝ ACC",
            f"Sessions hiện có: <b>{len(list_sessions_for(uid))}</b>"), kb_acc())
        return


# ─── menu callback ──────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer(RENT_MESSAGE, show_alert=True)
        try:
            await update.effective_chat.send_message(RENT_MESSAGE)
        except Exception:
            pass
        return
    await q.answer()
    data = q.data
    s = st(uid)

    # control
    if data == "ctrl:cancel":
        s.step = None
        s.data.clear()
        await show_home(update)
        return
    if data == "m:home":
        s.step = None
        s.data.clear()
        await show_home(update)
        return
    if data == "m:stop":
        if not has_perm(uid, "STOP"):
            await q.answer("❌ Không có quyền STOP", show_alert=True); return
        await handle_stop(update, ctx)
        return
    if data == "m:status":
        if not has_perm(uid, "LOGS"):
            await q.answer("❌ Không có quyền", show_alert=True); return
        await send(update, banner("📊 STATUS",
            f"Trạng thái: <b>{s.status}</b>\n"
            f"Step hiện tại: <code>{s.step or '-'}</code>\n"
            f"Sessions: <b>{len(list_sessions_for(uid))}</b>"), kb_back_home())
        return
    if data == "m:logs":
        if not has_perm(uid, "LOGS"):
            await q.answer("❌ Không có quyền", show_alert=True); return
        await send(update, _format_log(s), kb_back_home())
        return

    # main menu entries
    if data == "m:check":
        if not has_perm(uid, "CHECK"):
            await q.answer("❌ Không có quyền CHECK", show_alert=True); return
        await flow_check_start(update, ctx); return
    if data == "m:auto":
        if not has_perm(uid, "AUTO"):
            await q.answer("❌ Không có quyền AUTO", show_alert=True); return
        await flow_auto_start(update, ctx); return
    if data == "m:rai":
        if not has_perm(uid, "RAI"):
            await q.answer("❌ Không có quyền TOOL RẢI", show_alert=True); return
        await flow_rai_start(update, ctx); return
    if data == "m:ref":
        if not has_perm(uid, "REF"):
            await q.answer("❌ Không có quyền REF", show_alert=True); return
        await flow_ref_start(update, ctx); return
    if data == "m:spam":
        if not has_perm(uid, "SPAM"):
            await q.answer("❌ Không có quyền SPAM", show_alert=True); return
        await flow_spam_start(update, ctx); return
    if data == "m:join":
        if not has_perm(uid, "JOIN"):
            await q.answer("❌ Không có quyền JOIN", show_alert=True); return
        await flow_join_start(update, ctx); return
    if data == "m:duyet":
        if not has_perm(uid, "DUYET"):
            await q.answer("❌ Không có quyền DUYỆT NẠP", show_alert=True); return
        await flow_duyet_start(update, ctx); return
    if data.startswith("duyet:pick:"):
        if not has_perm(uid, "DUYET"):
            await q.answer("❌ Không có quyền DUYỆT NẠP", show_alert=True); return
        await on_duyet_pick(update, ctx); return
    if data == "m:admins":
        if not mod_admins.is_owner(uid):
            await q.answer("❌ Chỉ Owner", show_alert=True); return
        await admins_home(update, ctx); return
    if data.startswith("adm:"):
        if not mod_admins.is_owner(uid):
            await q.answer("❌ Chỉ Owner", show_alert=True); return
        await on_admin_cb(update, ctx); return
    if data == "m:acc":
        if not has_perm(uid, "MANAGE_ACC"):
            await q.answer("❌ Không có quyền Quản lý TK", show_alert=True); return
        await send(update, banner("👤 QUẢN LÝ ACC",
            f"Sessions hiện có: <b>{len(list_sessions_for(uid))}</b>"), kb_acc())
        return

    # AUTO pick acc
    if data.startswith("auto:pick:"):
        if not has_perm(uid, "AUTO"):
            await q.answer("❌ Không có quyền AUTO", show_alert=True); return
        await on_auto_pick(update, ctx); return

    # RẢI handlers
    if data == "rai:done":
        if not has_perm(uid, "RAI"):
            await q.answer("❌ Không có quyền TOOL RẢI", show_alert=True); return
        await on_rai_done(update, ctx); return
    if data.startswith("rai:pick:"):
        if not has_perm(uid, "RAI"):
            await q.answer("❌ Không có quyền TOOL RẢI", show_alert=True); return
        await on_rai_pick(update, ctx); return


    # account ops
    if data == "acc:add":    await acc_add_start(update, ctx); return
    if data == "acc:list":   await acc_list(update, ctx); return
    if data == "acc:del":    await acc_del_start(update, ctx); return
    if data == "acc:live":   await acc_live(update, ctx); return
    if data == "acc:read":
        if not has_perm(uid, "READ_MSG"):
            await q.answer("❌ Không có quyền đọc tin nhắn", show_alert=True); return
        await read_pick_acc(update, ctx); return
    if data.startswith("readacc:"):
        await on_read_cb(update, ctx); return
    if data.startswith("join:"):
        await on_join_cb(update, ctx); return
    if data == "acc:import":
        s.step = "import.wait"
        await send(update, banner("📥 IMPORT SESSION",
            "Gửi file <code>.session</code> vào chat này."), kb_cancel())
        return
    if data == "acc:export": await acc_export(update, ctx); return

    # confirms for flows
    if data == "ctrl:yes":
        if s.step == "check.confirm":
            await launch_check(update, ctx); return
        if s.step == "auto.confirm":
            await launch_auto(update, ctx); return
        if s.step == "rai.confirm":
            await launch_rai(update, ctx); return
        if s.step == "ref.confirm":
            await launch_ref(update, ctx); return
        if s.step == "spam.confirm":
            await launch_spam(update, ctx); return
        if s.step == "join.confirm":
            await launch_join(update, ctx); return
        if s.step == "duyet.confirm":
            await launch_duyet(update, ctx); return
        if s.step and s.step.startswith("acc.del.confirm:"):
            name = s.step.split(":", 1)[1]
            delete_session(name)
            s.step = None
            await send(update, banner("🗑 XOÁ ACC", f"Đã xoá <code>{name}</code>"), kb_acc())
            return
        if s.step and s.step.startswith("adm.del.confirm:"):
            tid = int(s.step.split(":", 1)[1])
            ok = mod_admins.remove_admin(tid)
            s.step = None
            await send(update, banner("🗑 XOÁ ADMIN",
                "✅ Đã xoá" if ok else "❌ Không xoá được (owner hoặc không tồn tại)"),
                kb_back_home())
            return


# ─── stop handler ───────────────────────────────────────
async def handle_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(update.effective_user.id)
    if s.task and not s.task.done():
        if s.stop_event:
            s.stop_event.set()
        s.task.cancel()
        s.status = "STOPPED"
        await send(update, banner("⛔ STOP", "Đã yêu cầu dừng task."), kb_back_home())
    else:
        await send(update, banner("⛔ STOP", "Không có task nào đang chạy."), kb_back_home())


# ─── flow: CHECK JOIN ───────────────────────────────────
async def flow_check_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions_for(update.effective_user.id):
        await send(update, banner("⚠ CHECK JOIN",
            "Chưa có acc nào. Vào QUẢN LÝ ACC để thêm."), kb_back_home()); return
    s.step = "check.link"
    s.data = {}
    await send(update, banner("⚙️ CHECK JOIN",
        "📌 Vui lòng nhập <b>link nhóm</b>\n\n"
        "Ví dụ:\n<code>https://t.me/xxxx</code>\n\n➤ Nhập link:"),
        kb_cancel())


async def step_check(update: Update, ctx, text: str):
    s = st(update.effective_user.id)
    if s.step == "check.link":
        s.data["link"] = text.strip()
        s.step = "check.delay"
        await update.message.reply_text(banner("⏱ CÀI ĐẶT DELAY",
            "Delay = thời gian nghỉ giữa mỗi lượt check\n\n"
            "Ví dụ:\n"
            "<code>1</code> = nhanh\n"
            "<code>3</code> = trung bình\n"
            "<code>5</code> = an toàn\n\n➤ Nhập delay (giây):"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "check.delay":
        try: s.data["delay"] = float(text)
        except: await update.message.reply_text("❌ Sai số. Nhập lại:"); return
        s.step = "check.times"
        await update.message.reply_text(banner("🔁 SỐ LẦN",
            "Số vòng check muốn thực hiện.\n\n➤ Nhập số lần:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "check.times":
        try: s.data["times"] = int(text)
        except: await update.message.reply_text("❌ Sai. Nhập lại số nguyên:"); return
        s.step = "check.confirm"
        d = s.data
        await update.message.reply_text(banner("✅ XÁC NHẬN CHECK JOIN",
            f"🔗 Link  : <code>{_html_escape(d['link'])}</code>\n"
            f"⏱ Delay : <b>{d['delay']}s</b>\n"
            f"🔁 Số lần: <b>{d['times']}</b>\n"
            f"👥 Acc   : <b>{len(list_sessions_for(update.effective_user.id))}</b>\n\n"
            "Bấm <b>XÁC NHẬN</b> để bắt đầu."),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_check(update, ctx):
    s = st(update.effective_user.id)
    s.step = None
    s.status = "RUNNING"
    s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    sessions = list_sessions_for(update.effective_user.id)
    d = s.data

    async def runner():
        try:
            await mod_check.run_check(sessions, d["link"], d["delay"], d["times"], logger)
            s.status = "DONE"
        except asyncio.CancelledError:
            s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"
            await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)

    s.task = asyncio.create_task(runner())


# ─── flow: AUTO ─────────────────────────────────────────
# Mặc định: chạy đến khi STOP, không hỏi delay/số tin
AUTO_DEFAULT_SEND_DELAY = 5.0       # giây giữa mỗi tin
AUTO_DEFAULT_DELETE_DELAY = 8.0     # giây trước khi xoá tin
AUTO_DEFAULT_TOTAL = 10**9          # chạy "vô hạn" cho đến khi STOP


def kb_auto_pick(uid: int):
    sessions = list_sessions_for(uid)
    rows = [[InlineKeyboardButton(f"🚀 TẤT CẢ ({len(sessions)} acc)",
                                  callback_data="auto:pick:__ALL__")]]
    row = []
    for i, name in enumerate(sessions, 1):
        row.append(InlineKeyboardButton(f"👤 {name}",
                                        callback_data=f"auto:pick:{name}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ HUỶ", callback_data="ctrl:cancel"),
                 InlineKeyboardButton("🏠 HOME", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


async def flow_auto_start(update, ctx):
    s = st(update.effective_user.id)
    sessions = list_sessions_for(update.effective_user.id)
    if not sessions:
        await send(update, banner("⚠ AUTO",
            "Chưa có acc. Hãy thêm acc trước."), kb_back_home()); return
    s.step = "auto.pick"; s.data = {}
    await send(update, banner("🤖 AUTO",
        f"📌 Chọn acc muốn dùng (tổng <b>{len(sessions)}</b>)\n\n"
        "• Bấm <b>TẤT CẢ</b> để dùng toàn bộ acc\n"
        "• Hoặc bấm 1 acc cụ thể"), kb_auto_pick(update.effective_user.id))


async def on_auto_pick(update, ctx):
    q = update.callback_query
    s = st(q.from_user.id)
    name = q.data.split(":", 2)[2]
    if name == "__ALL__":
        s.data["sessions"] = list_sessions_for(update.effective_user.id)
        label = f"TẤT CẢ ({len(s.data['sessions'])} acc)"
    else:
        s.data["sessions"] = [name]
        label = name
    s.step = "auto.link"
    await send(update, banner("🔗 LINK / USERNAME",
        f"Đã chọn: <b>{_html_escape(label)}</b>\n\n"
        "Gửi link nhóm / @username / số điện thoại.\n"
        "Ví dụ: <code>https://t.me/xxxx</code>\n\n"
        "➤ Nhập link:"),
        kb_cancel())


async def step_auto(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "auto.link":
        d["link"] = text.strip()
        s.step = "auto.send_delay"
        await send(update, banner("⏱ DELAY GỬI",
            f"Mặc định: <b>{AUTO_DEFAULT_SEND_DELAY}s</b>\n\n"
            "➤ Nhập số giây giữa mỗi lần gửi (hoặc gửi <b>0</b> để dùng mặc định):"),
            kb_cancel())
        return
    if s.step == "auto.send_delay":
        try:
            v = float(text.strip().replace(",", "."))
            d["send_delay"] = v if v > 0 else AUTO_DEFAULT_SEND_DELAY
        except Exception:
            d["send_delay"] = AUTO_DEFAULT_SEND_DELAY
        s.step = "auto.del_delay"
        await send(update, banner("🗑 THỜI GIAN XOÁ",
            f"Mặc định: <b>{AUTO_DEFAULT_DELETE_DELAY}s</b>\n\n"
            "➤ Nhập số giây sau khi gửi sẽ xoá tin (hoặc gửi <b>0</b> để dùng mặc định):"),
            kb_cancel())
        return
    if s.step == "auto.del_delay":
        try:
            v = float(text.strip().replace(",", "."))
            d["del_delay"] = v if v > 0 else AUTO_DEFAULT_DELETE_DELAY
        except Exception:
            d["del_delay"] = AUTO_DEFAULT_DELETE_DELAY
        await launch_auto(update, ctx)
        return


async def launch_auto(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data
    sessions = d.get("sessions") or list_sessions_for(update.effective_user.id)

    async def runner():
        try:
            await mod_auto.run_auto(
                sessions, d["link"],
                d.get("send_delay", AUTO_DEFAULT_SEND_DELAY),
                d.get("del_delay", AUTO_DEFAULT_DELETE_DELAY),
                AUTO_DEFAULT_TOTAL,
                s.stop_event, logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())


# ─── flow: TOOL RẢI ─────────────────────────────────────
RAI_MAX_GROUPS = 20


def kb_rai_link():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ XONG NHẬP NHÓM", callback_data="rai:done")],
        [InlineKeyboardButton("❌ HUỶ", callback_data="ctrl:cancel"),
         InlineKeyboardButton("🏠 HOME", callback_data="m:home")],
    ])


def kb_rai_pick(uid: int):
    sessions = list_sessions_for(uid)
    rows = []
    row = []
    for name in sessions:
        row.append(InlineKeyboardButton(f"👤 {name}",
                                        callback_data=f"rai:pick:{name}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ HUỶ", callback_data="ctrl:cancel"),
                 InlineKeyboardButton("🏠 HOME", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


async def flow_rai_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions_for(update.effective_user.id):
        await send(update, banner("⚠ TOOL RẢI",
            "Chưa có acc. Hãy thêm acc trước."), kb_back_home()); return
    s.step = "rai.link"
    s.data = {"groups": []}
    await send(update, banner("📢 TOOL RẢI",
        f"📌 Gửi link nhóm <b>thứ 1</b> (tối đa {RAI_MAX_GROUPS} nhóm).\n\n"
        "Ví dụ: <code>https://t.me/xxxx</code> hoặc <code>@username</code>\n\n"
        "➤ Nhập link nhóm:"), kb_cancel())


async def step_rai(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "rai.link":
        link = text.strip()
        if not link:
            await send(update, "❌ Link rỗng, gửi lại.", kb_cancel()); return
        d.setdefault("groups", []).append(link)
        n = len(d["groups"])
        if n >= RAI_MAX_GROUPS:
            await _rai_ask_msg(update, ctx); return
        await send(update, banner("📢 TOOL RẢI",
            f"✔ Đã lưu nhóm <b>{n}</b>: <code>{_html_escape(link)}</code>\n\n"
            f"➤ Gửi link nhóm <b>thứ {n+1}</b> (tối đa {RAI_MAX_GROUPS}),\n"
            "hoặc bấm <b>XONG NHẬP NHÓM</b> để tiếp tục."),
            kb_rai_link())
        return
    if s.step == "rai.msg":
        d["content"] = text
        await _rai_ask_acc(update, ctx); return
    if s.step == "rai.count":
        try:
            n = int(text.strip())
            if n <= 0:
                raise ValueError
        except Exception:
            await send(update, "❌ Số không hợp lệ, nhập lại số nguyên dương:",
                kb_cancel()); return
        d["per_group"] = n
        await _rai_ask_confirm(update, ctx); return


async def on_rai_done(update, ctx):
    s = st(update.effective_user.id)
    if s.step != "rai.link":
        return
    if not s.data.get("groups"):
        await send(update, "❌ Chưa có nhóm nào, gửi link trước.", kb_cancel()); return
    await _rai_ask_msg(update, ctx)


async def _rai_ask_msg(update, ctx):
    s = st(update.effective_user.id)
    s.step = "rai.msg"
    groups = s.data.get("groups", [])
    lst = "\n".join(f"• <code>{_html_escape(g)}</code>" for g in groups)
    await send(update, banner("📝 NỘI DUNG",
        f"Đã có <b>{len(groups)}</b> nhóm:\n{lst}\n\n"
        "➤ Gửi <b>nội dung</b> muốn rải<code>/</code>):"),
        kb_cancel())


async def _rai_ask_acc(update, ctx):
    s = st(update.effective_user.id)
    s.step = "rai.pick"
    await send(update, banner("👤 CHỌN ACC",
        "Chọn <b>1 acc</b> để chạy RẢI (tối đa 1):"),
        kb_rai_pick(update.effective_user.id))


async def on_rai_pick(update, ctx):
    q = update.callback_query
    s = st(q.from_user.id)
    if s.step != "rai.pick":
        return
    name = q.data.split(":", 2)[2]
    s.data["session"] = name
    s.step = "rai.count"
    await send(update, banner("🔢 SỐ TIN / NHÓM",
        f"Acc đã chọn: <b>{_html_escape(name)}</b>\n\n"
        "➤ Mỗi nhóm gửi bao nhiêu tin? (nhập số nguyên dương):"),
        kb_cancel())


async def _rai_ask_confirm(update, ctx):
    s = st(update.effective_user.id); d = s.data
    s.step = "rai.confirm"
    groups = d.get("groups", [])
    lst = "\n".join(f"• <code>{_html_escape(g)}</code>" for g in groups)
    preview = d.get("content", "")
    if len(preview) > 200:
        preview = preview[:200] + "…"
    await send(update, banner("✅ XÁC NHẬN",
        f"👤 Acc: <b>{_html_escape(d.get('session',''))}</b>\n"
        f"📦 Số nhóm: <b>{len(groups)}</b>\n"
        f"🔢 Mỗi nhóm: <b>{d.get('per_group')}</b> tin\n\n"
        f"📝 Nội dung:\n<pre>{_html_escape(preview)}</pre>\n\n"
        f"Danh sách nhóm:\n{lst}"),
        kb_confirm())


async def launch_rai(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data

    async def runner():
        try:
            await mod_rai.run_rai(
                d["session"], d["groups"], d["content"], d["per_group"],
                s.stop_event, logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())



# ─── flow: REF ──────────────────────────────────────────
async def flow_ref_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions_for(update.effective_user.id):
        await send(update, banner("⚠ REF", "Chưa có acc."), kb_back_home()); return
    s.step = "ref.link"; s.data = {}
    await send(update, banner("🎯 REF",
        "📌 Dán <b>link ref bot</b>\n\n"
        "Ví dụ: <code>https://t.me/botname?start=xxxxxx</code>\n\n"
        "➤ Nhập link:"), kb_cancel())


async def step_ref(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "ref.link":
        d["link"] = text.strip(); s.step = "ref.times"
        await update.message.reply_text(banner("🔁 SỐ REF",
            "Mỗi acc sẽ chạy bao nhiêu lượt ref\n\n➤ Nhập số:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "ref.times":
        try: d["times"] = int(text)
        except: await update.message.reply_text("❌ Sai."); return
        s.step = "ref.confirm"
        await update.message.reply_text(banner("✅ XÁC NHẬN REF",
            f"🔗 Link : <code>{_html_escape(d['link'])}</code>\n"
            f"🔁 Lần  : <b>{d['times']}</b>\n"
            f"👥 Acc  : <b>{len(list_sessions_for(update.effective_user.id))}</b>"),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_ref(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data; sessions = list_sessions_for(update.effective_user.id)

    async def runner():
        try:
            await mod_ref.run_ref(sessions, d["link"], d["times"], logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())


# ─── flow: SEX SPAM ─────────────────────────────────────
async def flow_spam_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions_for(update.effective_user.id):
        await send(update, banner("⚠ SEX SPAM", "Chưa có acc."), kb_back_home()); return
    s.step = "spam.target"; s.data = {}
    await send(update, banner("💬 SEX SPAM",
        "📌 Username/SĐT người nhận, hoặc link nhóm\n\n"
        "Ví dụ:\n<code>@username</code>\n<code>https://t.me/groupx</code>\n\n"
        "➤ Nhập target:"), kb_cancel())


async def step_spam(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "spam.target":
        d["target"] = text.strip()
        d["mode"] = "group" if ("t.me/" in d["target"] and "+" not in d["target"]) else "private"
        if d["target"].startswith("https://t.me/+"):
            d["mode"] = "group"
        s.step = "spam.msg"
        await update.message.reply_text(banner("✉️ NỘI DUNG",
            "Nhập nội dung tin nhắn muốn gửi\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.msg":
        d["msg"] = text; s.step = "spam.total"
        await update.message.reply_text(banner("🔁 SỐ LẦN",
            "Số tin mỗi acc sẽ gửi\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.total":
        try: d["total"] = int(text)
        except: await update.message.reply_text("❌ Sai."); return
        s.step = "spam.delay"
        await update.message.reply_text(banner("⏱ DELAY",
            "Giây giữa mỗi tin (>=0.3)\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.delay":
        try:
            v = float(text)
            if v < 0.3: raise ValueError
            d["delay"] = v
        except: await update.message.reply_text("❌ >=0.3, nhập lại:"); return
        s.step = "spam.deldelay"
        await update.message.reply_text(banner("🗑 DELAY XOÁ",
            "Sau bao nhiêu giây sẽ xoá (0 = không xoá)\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.deldelay":
        try: d["deldelay"] = int(text)
        except: await update.message.reply_text("❌ Sai."); return
        s.step = "spam.confirm"
        await update.message.reply_text(banner("✅ XÁC NHẬN SPAM",
            f"🎯 Target : <code>{_html_escape(d['target'])}</code>\n"
            f"📦 Mode   : <b>{d['mode']}</b>\n"
            f"✉️ Msg    : <code>{_html_escape(d['msg'][:60])}</code>\n"
            f"🔁 Số tin : <b>{d['total']}</b>\n"
            f"⏱ Delay  : <b>{d['delay']}s</b>\n"
            f"🗑 Xoá   : <b>{d['deldelay']}s</b>"),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_spam(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data; sessions = list_sessions_for(update.effective_user.id)

    async def runner():
        try:
            await mod_spam.run_spam(sessions, d["mode"], d["target"], d["msg"],
                                    d["total"], d["delay"], d["deldelay"],
                                    s.stop_event, logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())


# ─── ACCOUNT: ADD (login Telethon) ──────────────────────
async def acc_add_start(update, ctx):
    s = st(update.effective_user.id)
    s.step = "addacc.phone"; s.data = {}
    await send(update, banner("📱 THÊM TÀI KHOẢN",
        "➤ Nhập số điện thoại (kèm mã quốc gia, ví dụ <code>+84…</code>):"),
        kb_cancel())


async def step_addacc(update, ctx, text: str):
    uid = update.effective_user.id; s = st(uid)
    if s.step == "addacc.phone":
        phone = text.strip()
        if phone.startswith("0"): phone = "+84" + phone[1:]
        elif not phone.startswith("+"): phone = "+" + phone
        s.login_phone = phone
        s.login_client = make_client(phone)
        try:
            await s.login_client.connect()
            await s.login_client.send_code_request(phone)
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi gửi OTP: {e}",
                                            reply_markup=kb_cancel())
            try: await s.login_client.disconnect()
            except: pass
            s.login_client = None; s.step = None
            return
        s.step = "addacc.code"
        await update.message.reply_text(banner("📩 OTP",
            f"Đã gửi OTP tới <b>{_html_escape(phone)}</b>\n\n"
            "➤ Nhập mã OTP (có thể chèn dấu cách giữa các số):"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "addacc.code":
        code = "".join(c for c in text if c.isdigit())
        try:
            await s.login_client.sign_in(s.login_phone, code)
        except tele_errors.SessionPasswordNeededError:
            s.step = "addacc.2fa"
            await update.message.reply_text(banner("🔐 2FA",
                "Tài khoản có bật 2FA.\n➤ Nhập mật khẩu 2FA:"),
                reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Sai OTP: {e}\nNhập lại:",
                                            reply_markup=kb_cancel())
            return
        await _finish_login(update, ctx)
    elif s.step == "addacc.2fa":
        try:
            await s.login_client.sign_in(password=text.strip())
        except Exception as e:
            await update.message.reply_text(f"❌ 2FA sai: {e}\nNhập lại:",
                                            reply_markup=kb_cancel())
            return
        await _finish_login(update, ctx)


async def _finish_login(update, ctx):
    uid = update.effective_user.id; s = st(uid)
    try:
        me = await s.login_client.get_me()
        info = f"✅ Đăng nhập thành công\n\n👤 {me.first_name or ''} {me.last_name or ''}\n📱 {me.phone}"
    except Exception as e:
        info = f"⚠ Lỗi lấy thông tin: {e}"
    try: await s.login_client.disconnect()
    except: pass
    # Đăng ký chủ sở hữu session = uid này (theo yêu cầu: acc admin thêm
    # luôn được lưu về kho session chung). Nếu admin không có quyền
    # USE_ALL_ACCS thì họ vẫn thấy được acc do chính mình thêm.
    phone = s.login_phone
    if phone:
        mod_admins.set_session_owner(phone, uid)
    s.login_client = None; s.login_phone = None; s.step = None
    await update.message.reply_text(banner("📱 THÊM TÀI KHOẢN", info),
                                    reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


# ─── ACCOUNT: LIST / DELETE / LIVE / EXPORT ─────────────
async def acc_list(update, ctx):
    sessions = list_sessions_for(update.effective_user.id)
    if not sessions:
        await send(update, banner("📋 DANH SÁCH ACC", "Chưa có acc nào."), kb_acc())
        return
    rows = []
    for i, name in enumerate(sessions, 1):
        rows.append(f"<b>{i}.</b> <code>{_html_escape(name)}</code>")
    await send(update, banner("📋 DANH SÁCH ACC",
        f"Tổng: <b>{len(sessions)}</b>\n\n" + "\n".join(rows)),
        kb_acc())


async def acc_del_start(update, ctx):
    sessions = list_sessions_for(update.effective_user.id)
    if not sessions:
        await send(update, banner("🗑 XOÁ ACC", "Không có acc."), kb_acc()); return
    rows = [[InlineKeyboardButton(f"🗑 {n}", callback_data=f"acc:delpick:{n}")]
            for n in sessions]
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="m:acc")])
    await send(update, banner("🗑 XOÁ ACC", "Chọn acc cần xoá:"),
               InlineKeyboardMarkup(rows))


async def acc_live(update, ctx):
    sessions = list_sessions_for(update.effective_user.id)
    if not sessions:
        await send(update, banner("💓 CHECK LIVE", "Không có acc."), kb_acc()); return
    await send(update, banner("💓 CHECK LIVE", "Đang kiểm tra..."), kb_acc())
    results = []
    for name in sessions:
        c = make_client(name); alive = False
        try:
            await c.connect()
            alive = await c.is_user_authorized()
        except Exception:
            alive = False
        finally:
            try: await c.disconnect()
            except: pass
        results.append(f"{'✅' if alive else '❌'} <code>{_html_escape(name)}</code>")
    await update.effective_chat.send_message(
        banner("💓 CHECK LIVE — KẾT QUẢ", "\n".join(results)),
        reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


async def acc_export(update, ctx):
    sessions = list_sessions_for(update.effective_user.id)
    if not sessions:
        await send(update, banner("📤 EXPORT", "Không có acc."), kb_acc()); return
    for name in sessions:
        path = session_path(name) + ".session"
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    await update.effective_chat.send_document(
                        InputFile(f, filename=f"{name}.session"))
            except Exception as e:
                await update.effective_chat.send_message(f"❌ {name}: {e}")
    await update.effective_chat.send_message(
        banner("📤 EXPORT", "Hoàn tất."),
        reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


# ─── delete pick callback ───────────────────────────────
async def on_del_pick(update: Update, ctx):
    q = update.callback_query; uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("❌", show_alert=True); return
    await q.answer()
    name = q.data.split(":", 2)[2]
    s = st(uid); s.step = f"acc.del.confirm:{name}"
    await send(update, banner("🗑 XÁC NHẬN XOÁ",
        f"Xoá <code>{_html_escape(name)}</code>?"), kb_confirm())


# ─── document handler (IMPORT SESSION) ──────────────────
async def on_document(update: Update, ctx):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(RENT_MESSAGE)
        return
    s = st(uid)
    if s.step != "import.wait": return
    doc = update.message.document
    if not doc.file_name.endswith(".session"):
        await update.message.reply_text("❌ Phải là file .session"); return
    f = await doc.get_file()
    dest = os.path.join(SESSIONS_DIR, doc.file_name)
    await f.download_to_drive(dest)
    sess_name = doc.file_name[:-len(".session")]
    mod_admins.set_session_owner(sess_name, uid)
    s.step = None
    await update.message.reply_text(
        banner("📥 IMPORT SESSION", f"✅ Đã thêm <code>{doc.file_name}</code>"),
        reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


# ─── text router ────────────────────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(RENT_MESSAGE)
        return
    s = st(uid)
    txt = (update.message.text or "")

    # Nếu đang ở step nhập nội dung tự do -> nhận nguyên văn (kể cả "/start")
    if s.step in FREEFORM_STEPS:
        try:
            if s.step.startswith("spam."):
                await step_spam(update, ctx, txt)
            elif s.step.startswith("rai."):
                await step_rai(update, ctx, txt)
        except Exception as e:
            log.exception("freeform step error")
            await update.message.reply_text(f"❌ Lỗi: {e}")
            s.step = None
        return

    txt_s = txt.strip()
    # Bàn phím cũ (nếu còn) – huỷ step hiện tại
    if txt_s in REPLY_TEXT_MAP:
        action = REPLY_TEXT_MAP[txt_s]
        s.step = None
        s.data.clear()
        await dispatch_action(update, ctx, action)
        return

    if not s.step:
        # Bỏ qua các /command rời rạc khi không có step
        if txt_s.startswith("/"):
            return
        await show_home(update)
        return


    try:
        if s.step.startswith("check."):   await step_check(update, ctx, txt)
        elif s.step.startswith("auto."):  await step_auto(update, ctx, txt)
        elif s.step.startswith("rai."):   await step_rai(update, ctx, txt)
        elif s.step.startswith("ref."):   await step_ref(update, ctx, txt)
        elif s.step.startswith("spam."):  await step_spam(update, ctx, txt)
        elif s.step.startswith("addacc."): await step_addacc(update, ctx, txt)
        elif s.step.startswith("join."):  await step_join(update, ctx, txt)
        elif s.step.startswith("duyet."): await step_duyet(update, ctx, txt)
        elif s.step.startswith("admadd."): await step_adm_add(update, ctx, txt)
        elif s.step.startswith("admexp."): await step_adm_exp(update, ctx, txt)
        elif s.step.startswith("admdel."): await step_adm_del(update, ctx, txt)
    except Exception as e:
        log.exception("step error")
        await update.message.reply_text(f"❌ Lỗi: {e}", reply_markup=kb_main(uid))
        s.step = None


# ════════════════════════════════════════════════════════
# ADMIN MANAGEMENT
# ════════════════════════════════════════════════════════
def kb_admins_home(uid: int):
    rows = []
    for a in mod_admins.list_admins():
        tag = "👑" if a["owner"] else "🛡"
        label = f"{tag} {a['uid']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm:view:{a['uid']}")])
    btns = []
    if has_perm(uid, "ADD_ADMIN"):
        btns.append(InlineKeyboardButton("➕ THÊM ADMIN", callback_data="adm:add"))
    if has_perm(uid, "DEL_ADMIN"):
        btns.append(InlineKeyboardButton("🗑 XOÁ ADMIN", callback_data="adm:del"))
    if btns:
        rows.append(btns)
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


async def admins_home(update, ctx):
    uid = update.effective_user.id
    admins = mod_admins.list_admins()
    lines = []
    for a in admins:
        tag = "👑 Owner" if a["owner"] else "🛡 Admin"
        lines.append(f"{tag} — <code>{a['uid']}</code>")
    await send(update, banner("🛠 QUẢN LÝ ADMIN",
        f"Tổng: <b>{len(admins)}</b>\n\n" + "\n".join(lines)),
        kb_admins_home(uid))


def kb_admin_view(target_uid: int, viewer_uid: int):
    a = mod_admins.get_admin(target_uid)
    if not a:
        return kb_back_home()
    rows = []
    if not a["owner"] and has_perm(viewer_uid, "EDIT_ADMIN"):
        # toggle buttons - 2 per row
        cur = []
        for key, label in mod_admins.PERMS:
            mark = "✅" if a["perms"].get(key, False) else "❌"
            cur.append(InlineKeyboardButton(f"{mark} {label}",
                callback_data=f"adm:tog:{target_uid}:{key}"))
            if len(cur) == 2:
                rows.append(cur); cur = []
        if cur: rows.append(cur)
    if not a["owner"] and has_perm(viewer_uid, "EDIT_ADMIN"):
        rows.append([InlineKeyboardButton("⏰ SỬA THỜI HẠN",
            callback_data=f"adm:exp:{target_uid}")])
    if not a["owner"] and has_perm(viewer_uid, "DEL_ADMIN"):
        rows.append([InlineKeyboardButton("🗑 XOÁ ADMIN NÀY",
            callback_data=f"adm:delone:{target_uid}")])
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="m:admins")])
    return InlineKeyboardMarkup(rows)


async def admin_view(update, ctx, target_uid: int):
    viewer = update.effective_user.id
    a = mod_admins.get_admin(target_uid)
    if not a:
        await send(update, banner("🛠 ADMIN", "Không tìm thấy."), kb_back_home())
        return
    if a["owner"]:
        body = "👑 <b>Owner</b> — full quyền, không thể chỉnh."
    else:
        lines = []
        for key, label in mod_admins.PERMS:
            mark = "✅" if a["perms"].get(key, False) else "❌"
            lines.append(f"{mark} {label}")
        body = ("🛡 <b>Admin</b>\n"
                f"⏰ Hết hạn: <b>{mod_admins.format_remaining(target_uid)}</b>\n"
                f"📅 Lúc: <code>{mod_admins.format_expiry_at(target_uid)}</code>\n\n"
                + "\n".join(lines))
        if has_perm(viewer, "EDIT_ADMIN"):
            body += "\n\nBấm nút bên dưới để bật/tắt quyền."
    await send(update, banner(f"ADMIN <code>{target_uid}</code>", body),
               kb_admin_view(target_uid, viewer))


async def on_admin_cb(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    s = st(uid)
    # adm:add | adm:del | adm:view:UID | adm:tog:UID:KEY | adm:delone:UID
    action = parts[1]
    if action == "add":
        if not has_perm(uid, "ADD_ADMIN"):
            await q.answer("❌", show_alert=True); return
        s.step = "admadd.uid"; s.data = {}
        await send(update, banner("➕ THÊM ADMIN",
            "➤ Nhập <b>User ID</b> của admin mới (số nguyên):"),
            kb_cancel())
    elif action == "del":
        if not has_perm(uid, "DEL_ADMIN"):
            await q.answer("❌", show_alert=True); return
        admins = [a for a in mod_admins.list_admins() if not a["owner"]]
        if not admins:
            await send(update, banner("🗑 XOÁ ADMIN", "Không có admin nào để xoá."),
                       kb_back_home()); return
        rows = [[InlineKeyboardButton(f"🗑 {a['uid']}",
                  callback_data=f"adm:delone:{a['uid']}")] for a in admins]
        rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="m:admins")])
        await send(update, banner("🗑 XOÁ ADMIN", "Chọn admin cần xoá:"),
                   InlineKeyboardMarkup(rows))
    elif action == "view":
        await admin_view(update, ctx, int(parts[2]))
    elif action == "tog":
        if not has_perm(uid, "EDIT_ADMIN"):
            await q.answer("❌", show_alert=True); return
        target = int(parts[2]); key = parts[3]
        mod_admins.toggle_perm(target, key)
        await admin_view(update, ctx, target)
    elif action == "delone":
        if not has_perm(uid, "DEL_ADMIN"):
            await q.answer("❌", show_alert=True); return
        target = int(parts[2])
        s.step = f"adm.del.confirm:{target}"
        await send(update, banner("🗑 XÁC NHẬN",
            f"Xoá quyền admin của <code>{target}</code>?"), kb_confirm())
    elif action == "exp":
        if not has_perm(uid, "EDIT_ADMIN"):
            await q.answer("❌", show_alert=True); return
        target = int(parts[2])
        s.step = f"admexp.dur:{target}"
        await send(update, banner("⏰ SỬA THỜI HẠN",
            f"Admin: <code>{target}</code>\n"
            f"Hiện tại: <b>{mod_admins.format_remaining(target)}</b>\n\n"
            "Nhập thời gian mới (tính từ bây giờ, giờ VN):\n"
            "<code>30s</code> / <code>15m</code> / <code>1h</code> / "
            "<code>1day</code> / <code>-</code> = vô thời hạn"),
            kb_cancel())


async def step_adm_add(update, ctx, text: str):
    uid = update.effective_user.id; s = st(uid)
    if s.step == "admadd.uid":
        try:
            tid = int(text.strip())
        except Exception:
            await update.message.reply_text("❌ ID phải là số nguyên. Nhập lại:")
            return
        s.data["uid"] = tid; s.step = "admadd.name"
        await update.message.reply_text(banner("➕ THÊM ADMIN",
            "➤ Nhập tên ghi chú (hoặc gửi <code>-</code> để bỏ qua):"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "admadd.name":
        name = text.strip()
        if name == "-": name = ""
        s.data["name"] = name; s.step = "admadd.dur"
        await update.message.reply_text(banner("⏰ THỜI GIAN SỬ DỤNG",
            "Nhập thời gian được phép dùng (theo giờ VN):\n"
            "<code>30s</code> = 30 giây\n"
            "<code>15m</code> = 15 phút\n"
            "<code>1h</code>  = 1 giờ\n"
            "<code>1day</code> hoặc <code>1d</code> = 1 ngày\n"
            "<code>-</code> hoặc <code>0</code> = vô thời hạn\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "admadd.dur":
        t = text.strip().lower()
        dur = None
        if t not in ("-", "0", ""):
            dur = mod_admins.parse_duration(t)
            if dur is None or dur <= 0:
                await update.message.reply_text(
                    "❌ Sai định dạng. VD: 1day / 1h / 30m / -")
                return
        ok = mod_admins.add_admin(s.data["uid"], s.data.get("name", ""), dur)
        s.step = None
        if ok:
            await update.message.reply_text(banner("➕ THÊM ADMIN",
                f"✅ Đã thêm <code>{s.data['uid']}</code>\n"
                f"⏰ Thời hạn: <b>{mod_admins.format_remaining(s.data['uid'])}</b>\n"
                "Mặc định bật tất cả quyền. Vào QUẢN LÝ ADMIN để chỉnh."),
                reply_markup=kb_back_home(), parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(banner("➕ THÊM ADMIN",
                "❌ Không thêm được (đã là owner hoặc đã tồn tại)."),
                reply_markup=kb_back_home(), parse_mode=ParseMode.HTML)


async def step_adm_exp(update, ctx, text: str):
    uid = update.effective_user.id; s = st(uid)
    if not s.step or not s.step.startswith("admexp.dur:"):
        return
    target = int(s.step.split(":", 1)[1])
    t = text.strip().lower()
    dur = None
    if t not in ("-", "0", ""):
        dur = mod_admins.parse_duration(t)
        if dur is None or dur <= 0:
            await update.message.reply_text(
                "❌ Sai định dạng. VD: 1day / 1h / 30m / -")
            return
    ok = mod_admins.set_expiry(target, dur)
    s.step = None
    if ok:
        # reset warning flag của user đó để cảnh báo lại nếu cần
        st(target).warned_10m = False
        await update.message.reply_text(banner("⏰ SỬA THỜI HẠN",
            f"✅ Đã cập nhật cho <code>{target}</code>\n"
            f"⏰ Còn lại: <b>{mod_admins.format_remaining(target)}</b>\n"
            f"📅 Lúc: <code>{mod_admins.format_expiry_at(target)}</code>"),
            reply_markup=kb_back_home(), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Không sửa được.",
            reply_markup=kb_back_home())


async def step_adm_del(update, ctx, text: str):
    # giữ chỗ - hiện flow xoá dùng callback button
    pass


# ════════════════════════════════════════════════════════
# READ MESSAGES
# ════════════════════════════════════════════════════════
async def read_pick_acc(update, ctx):
    sessions = list_sessions_for(update.effective_user.id)
    if not sessions:
        await send(update, banner("📩 ĐỌC TIN NHẮN", "Không có acc."), kb_acc())
        return
    rows = [[InlineKeyboardButton(f"📩 {n}", callback_data=f"readacc:dlg:{n}")]
            for n in sessions]
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="m:acc")])
    await send(update, banner("📩 ĐỌC TIN NHẮN",
        "Chọn account để đọc tin nhắn:"), InlineKeyboardMarkup(rows))


async def on_read_cb(update, ctx):
    q = update.callback_query; uid = q.from_user.id
    if not has_perm(uid, "READ_MSG"):
        await q.answer("❌", show_alert=True); return
    parts = q.data.split(":")
    # readacc:dlg:NAME | readacc:msg:NAME:DIALOGID
    sub = parts[1]
    s = st(uid)
    if sub == "dlg":
        name = parts[2]
        await q.edit_message_text(f"⏳ Đang tải dialogs của <code>{name}</code>...",
                                  parse_mode=ParseMode.HTML)
        dialogs = await mod_msg.list_dialogs(name, limit=20)
        if not dialogs:
            await send(update, banner("📩 ĐỌC TIN NHẮN",
                f"Không lấy được dialog (session die?)."), kb_acc()); return
        rows = []
        for d in dialogs:
            label = f"{d['name']}"[:40]
            rows.append([InlineKeyboardButton(label,
                callback_data=f"readacc:msg:{name}:{d['id']}")])
        rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="acc:read")])
        await send(update, banner(f"📩 {name}",
            f"Dialog gần đây ({len(dialogs)}):"), InlineKeyboardMarkup(rows))
    elif sub == "msg":
        name = parts[2]; did = int(parts[3])
        await q.edit_message_text("⏳ Đang đọc tin nhắn...", parse_mode=ParseMode.HTML)
        lines = await mod_msg.read_messages(name, did, limit=30)
        body = "\n".join(_html_escape(l) for l in lines[-30:])
        text = banner(f"📩 {name}", f"<pre>{body}</pre>")
        if len(text) > 3800:
            text = text[:3800] + "...</pre>"
        await send(update, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ BACK", callback_data=f"readacc:dlg:{name}")],
            [InlineKeyboardButton("🏠 HOME", callback_data="m:home")],
        ]))


# ════════════════════════════════════════════════════════
# JOIN GROUP FLOW
# ════════════════════════════════════════════════════════
async def flow_join_start(update, ctx):
    uid = update.effective_user.id; s = st(uid)
    if not list_sessions_for(update.effective_user.id):
        await send(update, banner("⚠ JOIN GROUP",
            "Chưa có acc nào."), kb_back_home()); return
    s.step = "join.mode"; s.data = {}
    await send(update, banner("🚪 JOIN GROUP",
        f"Tổng acc: <b>{len(list_sessions_for(update.effective_user.id))}</b>\n\nChọn chế độ:"),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 TẤT CẢ ACC", callback_data="join:all")],
            [InlineKeyboardButton("🎯 CHỌN ACC",   callback_data="join:pick")],
            [InlineKeyboardButton("❌ HUỶ",        callback_data="ctrl:cancel")],
        ]))


async def on_join_cb(update, ctx):
    q = update.callback_query; uid = q.from_user.id
    s = st(uid)
    parts = q.data.split(":")
    sub = parts[1]
    sessions = list_sessions_for(update.effective_user.id)
    if sub == "all":
        s.data["mode"] = "all"
        s.data["exclude"] = []
        s.step = "join.exclude"
        await send(update, banner("🚪 JOIN GROUP — NGOẠI LỆ",
            "Nhập tên các acc ngoại lệ (cách nhau bằng dấu phẩy)\n"
            "Hoặc gửi <code>-</code> để bỏ qua\n\n"
            f"Danh sách acc: <code>{', '.join(sessions)}</code>"),
            kb_cancel())
    elif sub == "pick":
        s.data["mode"] = "pick"
        s.data["picked"] = []
        await _join_render_pick(update, ctx)
    elif sub == "togpick":
        name = parts[2]
        picked = s.data.setdefault("picked", [])
        if name in picked: picked.remove(name)
        else: picked.append(name)
        await _join_render_pick(update, ctx)
    elif sub == "donepick":
        if not s.data.get("picked"):
            await q.answer("Chưa chọn acc nào", show_alert=True); return
        s.step = "join.link"
        await send(update, banner("🔗 LINK NHÓM/KÊNH",
            "Nhập link Telegram (public hoặc invite)\n\n"
            "Ví dụ:\n<code>https://t.me/xxxx</code>\n"
            "<code>https://t.me/+abcdEFG</code>\n\n➤ Nhập:"),
            kb_cancel())


async def _join_render_pick(update, ctx):
    uid = update.effective_user.id; s = st(uid)
    sessions = list_sessions_for(update.effective_user.id)
    picked = s.data.get("picked", [])
    rows = []
    cur = []
    for n in sessions:
        mark = "✅" if n in picked else "▫️"
        cur.append(InlineKeyboardButton(f"{mark} {n}",
            callback_data=f"join:togpick:{n}"))
        if len(cur) == 2: rows.append(cur); cur = []
    if cur: rows.append(cur)
    rows.append([InlineKeyboardButton(f"✅ XONG ({len(picked)})",
                  callback_data="join:donepick"),
                 InlineKeyboardButton("❌ HUỶ", callback_data="ctrl:cancel")])
    await send(update, banner("🚪 CHỌN ACC JOIN",
        f"Đã chọn: <b>{len(picked)}/{len(sessions)}</b>"),
        InlineKeyboardMarkup(rows))


async def step_join(update, ctx, text: str):
    uid = update.effective_user.id; s = st(uid); d = s.data
    if s.step == "join.exclude":
        t = text.strip()
        if t == "-":
            d["exclude"] = []
        else:
            d["exclude"] = [x.strip() for x in t.split(",") if x.strip()]
        s.step = "join.link"
        await update.message.reply_text(banner("🔗 LINK NHÓM/KÊNH",
            "Nhập link Telegram (public hoặc invite)\n\n"
            "Ví dụ:\n<code>https://t.me/xxxx</code>\n"
            "<code>https://t.me/+abcdEFG</code>\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "join.link":
        d["link"] = text.strip()
        s.step = "join.leave"
        await update.message.reply_text(banner("⏰ THỜI GIAN AUTO LEAVE",
            "Định dạng:\n"
            "<code>20s</code> = 20 giây\n"
            "<code>1m</code>  = 1 phút\n"
            "<code>1h</code>  = 1 giờ\n"
            "<code>1day</code> hoặc <code>1d</code> = 1 ngày\n"
            "<code>stay</code> hoặc <code>0</code> = không rời\n\n"
            "➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "join.leave":
        v = mod_join.parse_leave_time(text)
        if v == -1:
            await update.message.reply_text("❌ Sai định dạng. VD: 20s, 1m, 1h, 1day, stay")
            return
        d["leave_after"] = v
        # build target sessions
        all_s = list_sessions_for(update.effective_user.id)
        if d.get("mode") == "all":
            targets = [x for x in all_s if x not in d.get("exclude", [])]
        else:
            targets = [x for x in d.get("picked", []) if x in all_s]
        d["targets"] = targets
        s.step = "join.confirm"
        leave_txt = "stay (không rời)" if v is None else f"{int(v)}s"
        await update.message.reply_text(banner("✅ XÁC NHẬN JOIN",
            f"🔗 Link    : <code>{_html_escape(d['link'])}</code>\n"
            f"👥 Acc     : <b>{len(targets)}</b>\n"
            f"⏰ Auto leave: <b>{leave_txt}</b>\n"
            f"🚫 Loại trừ: <code>{', '.join(d.get('exclude', [])) or '-'}</code>"),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_join(update, ctx):
    uid = update.effective_user.id; s = st(uid); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, uid, update.effective_chat.id)
    d = s.data
    targets = d.get("targets", [])

    async def runner():
        try:
            await mod_join.run_join(targets, d["link"], d["leave_after"], logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, uid)
    s.task = asyncio.create_task(runner())


# ════════════════════════════════════════════════════════
# DUYỆT NẠP FLOW
# ════════════════════════════════════════════════════════
DUYET_DEFAULT_DELAY = 10.0
DUYET_DEFAULT_TRIGGER = "YÊU CẦU NẠP TIỀN"
DUYET_DEFAULT_BUTTON = "Duyệt"


def kb_duyet_pick(uid: int):
    sessions = list_sessions_for(uid)
    rows = []
    row = []
    for name in sessions:
        row.append(InlineKeyboardButton(f"👤 {name}",
                                        callback_data=f"duyet:pick:{name}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ HUỶ", callback_data="ctrl:cancel"),
                 InlineKeyboardButton("🏠 HOME", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


async def flow_duyet_start(update, ctx):
    uid = update.effective_user.id
    s = st(uid)
    sessions = list_sessions_for(uid)
    if not sessions:
        await send(update, banner("⚠ DUYỆT NẠP",
            "Chưa có acc nào. Vào QUẢN LÝ ACC để thêm trước."),
            kb_back_home()); return
    s.step = "duyet.pick"; s.data = {}
    await send(update, banner("✅ DUYỆT NẠP",
        "📌 Chọn <b>1 acc</b> để chạy auto duyệt:\n\n"
        f"(Tổng acc của bạn: <b>{len(sessions)}</b>)"),
        kb_duyet_pick(uid))


async def on_duyet_pick(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    s = st(uid)
    if s.step != "duyet.pick":
        return
    name = q.data.split(":", 2)[2]
    s.data["session"] = name
    s.step = "duyet.bot"
    await send(update, banner("🤖 USERNAME BOT NẠP",
        f"Acc đã chọn: <b>{_html_escape(name)}</b>\n\n"
        "➤ Gửi <b>username của bot</b> sẽ gửi lệnh nạp\n"
        "Ví dụ: <code>SIC88_BOT</code> hoặc <code>@SIC88_BOT</code>"),
        kb_cancel())


async def step_duyet(update, ctx, text: str):
    uid = update.effective_user.id
    s = st(uid); d = s.data
    if s.step == "duyet.bot":
        bot = text.strip().lstrip("@")
        if not bot:
            await update.message.reply_text("❌ Username rỗng, gửi lại:")
            return
        d["bot"] = bot
        s.step = "duyet.delay"
        await send(update, banner("⏱ THỜI GIAN CHỜ",
            f"Bot: <b>@{_html_escape(bot)}</b>\n\n"
            f"➤ Nhập số giây chờ trước khi bấm <b>Duyệt</b>\n"
            f"(Mặc định: <b>{DUYET_DEFAULT_DELAY}</b>s. Gửi <code>0</code> để dùng mặc định)"),
            kb_cancel())
        return
    if s.step == "duyet.delay":
        try:
            v = float(text.strip().replace(",", "."))
            d["delay"] = v if v > 0 else DUYET_DEFAULT_DELAY
        except Exception:
            d["delay"] = DUYET_DEFAULT_DELAY
        d.setdefault("trigger", DUYET_DEFAULT_TRIGGER)
        d.setdefault("button", DUYET_DEFAULT_BUTTON)
        s.step = "duyet.confirm"
        await send(update, banner("✅ XÁC NHẬN DUYỆT NẠP",
            f"👤 Acc      : <b>{_html_escape(d['session'])}</b>\n"
            f"🤖 Bot      : <b>@{_html_escape(d['bot'])}</b>\n"
            f"⏱ Chờ      : <b>{d['delay']}s</b>\n"
            f"🔎 Trigger  : <code>{_html_escape(d['trigger'])}</code>\n"
            f"🔘 Nút bấm  : <code>{_html_escape(d['button'])}</code>\n\n"
            "Tool sẽ chạy liên tục đến khi bấm ⛔ STOP TASK.\n\n"
            "Bấm <b>XÁC NHẬN</b> để bắt đầu."),
            kb_confirm())


async def launch_duyet(update, ctx):
    uid = update.effective_user.id
    s = st(uid); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, uid, update.effective_chat.id)
    d = s.data

    async def runner():
        try:
            await mod_duyet.run_duyet(
                d["session"], d["bot"], d["delay"],
                d.get("trigger", DUYET_DEFAULT_TRIGGER),
                d.get("button", DUYET_DEFAULT_BUTTON),
                s.stop_event, logger)
            s.status = "DONE"
        except asyncio.CancelledError:
            s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, uid)
    s.task = asyncio.create_task(runner())


# ─── main ───────────────────────────────────────────────
async def _post_init(app: Application):
    # Khôi phục scheduler auto-leave sau khi Railway restart
    log.info("Restoring join auto-leave scheduler...")
    asyncio.create_task(mod_join.scheduler_loop())


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_del_pick, pattern=r"^acc:delpick:"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    # Nhận cả /command để các step nhập nội dung tự do (vd SEX SPAM) không bị mất
    app.add_handler(MessageHandler(filters.TEXT, on_text))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

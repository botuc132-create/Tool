"""CHECK JOIN – port từ check.py gốc, no input()."""
import asyncio
from telethon import errors
from telethon.tl.functions.channels import (
    JoinChannelRequest, GetParticipantRequest,
)
from .session_mgr import make_client


async def check_account(session_name: str, group_link: str, log):
    client = make_client(session_name)
    join_ok, msg_ok = False, False
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await log(f"[{session_name}] ❌ Session die")
            return
        username = group_link.split("/")[-1].split("?")[0].replace("@", "")
        try:
            await client(JoinChannelRequest(username))
            join_ok = True
        except errors.UserAlreadyParticipantError:
            join_ok = True
        except Exception:
            join_ok = False

        if join_ok:
            try:
                me = await client.get_me()
                p = await client(GetParticipantRequest(channel=username, participant=me.id))
                banned = getattr(p.participant, "banned_rights", None)
                msg_ok = not (banned and banned.send_messages)
            except Exception:
                msg_ok = False

        await log(
            f"[{session_name}] "
            + ("✅ vào được(🚀)" if join_ok else "❌ không vào được")
            + " | "
            + ("✅ chat được" if msg_ok else "❌ không chat được")
        )
    except Exception as e:
        await log(f"[{session_name}] ❌ {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_check(sessions, group_link, delay, times, log):
    await log("🚀 CHECK START")
    for i in range(times):
        if times > 1:
            await log(f"━━━ LẦN {i+1}/{times} ━━━")
        await asyncio.gather(*[check_account(s, group_link, log) for s in sessions])
        if i < times - 1:
            await asyncio.sleep(delay)
    await log("✅ CHECK DONE")


import asyncio
import json
import logging
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Múi giờ Việt Nam
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

try:
    from config import DATA_DIR as _CFG_DATA_DIR
    DATA_DIR = Path(_CFG_DATA_DIR)
except Exception:  # chạy độc lập, không có biến môi trường
    DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "naprut_schedule.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("naprut_checkin")


def load_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.exception("Không đọc được file lịch chạy")
        return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp_file.replace(STATE_FILE)


def parse_run_time(value):
    """Nhận giờ theo định dạng HH:MM."""
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M").time()
        return parsed
    except ValueError as exc:
        raise ValueError(
            "Giờ không hợp lệ. Hãy nhập theo dạng 08:30 hoặc 20:15."
        ) from exc


def next_run_datetime(run_time):
    now = datetime.now(TZ)
    target = datetime.combine(now.date(), run_time, tzinfo=TZ)

    if target <= now:
        target += timedelta(days=1)

    return target


async def run_daily_task(account_name):
    """
    Điểm tích hợp tác vụ hằng ngày.

    Hàm này hiện chỉ ghi log. Hãy kết nối với handler an toàn
    trong bot hiện tại nếu cần; chưa tự động thao tác giao dịch.
    """
    log.info(
        "Đến lịch của tài khoản %s. "
        "Hãy kiểm tra bot đích và thực hiện thao tác cần thiết.",
        account_name
    )


class SevenDayScheduler:
    def __init__(self, task_callback=run_daily_task, notify=None):
        self.state = load_state()
        self.task_callback = task_callback
        # notify(text) – coroutine tuỳ chọn để báo kết quả về Telegram
        self.notify = notify
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._worker = None

    def status(self):
        return self.state

    async def _notify(self, text):
        if self.notify:
            try:
                await self.notify(text)
            except Exception:
                log.exception("Không gửi được thông báo")

    async def start(self, account_name, time_text, owner_id=None):
        run_time = parse_run_time(time_text)

        async with self._lock:
            if self.state.get("active"):
                raise RuntimeError("Lịch đang chạy. Hãy dừng lịch cũ trước.")

            now = datetime.now(TZ)
            first_run = next_run_datetime(run_time)

            self.state = {
                "active": True,
                "account": account_name,
                "daily_time": run_time.strftime("%H:%M"),
                "started_at": now.isoformat(),
                "expires_at": (now + timedelta(days=7)).isoformat(),
                "first_run": first_run.isoformat(),
                "last_run_date": None,
                "completed_dates": [],
                "status": "scheduled",
                "owner_id": owner_id,
                "last_result": None,
            }
            save_state(self.state)

            self._stop_event = asyncio.Event()
            self._worker = asyncio.create_task(self._run_loop())

        return self.state.copy()

    async def stop(self):
        async with self._lock:
            self.state["active"] = False
            self.state["status"] = "stopped"
            save_state(self.state)
            self._stop_event.set()

            worker = self._worker
            self._worker = None

        if worker and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        return self.state.copy()

    async def restore(self):
        """Gọi khi bot khởi động để khôi phục lịch còn hiệu lực."""
        async with self._lock:
            if not self.state.get("active"):
                return False

            expires = datetime.fromisoformat(self.state["expires_at"])
            if datetime.now(TZ) >= expires:
                self.state["active"] = False
                self.state["status"] = "completed"
                save_state(self.state)
                return False

            if self._worker and not self._worker.done():
                return True

            self._stop_event = asyncio.Event()
            self._worker = asyncio.create_task(self._run_loop())
            return True

    async def _run_loop(self):
        try:
            while self.state.get("active"):
                now = datetime.now(TZ)
                expires = datetime.fromisoformat(
                    self.state["expires_at"]
                )

                if now >= expires:
                    self.state["active"] = False
                    self.state["status"] = "completed"
                    save_state(self.state)
                    log.info("Lịch 7 ngày đã kết thúc.")
                    await self._notify(
                        "🏁 Lịch điểm danh 7 ngày đã hoàn tất và tự dừng."
                    )
                    return

                # Chưa tới lần chạy đầu tiên (giờ hôm bật đã qua -> từ mai)
                first_run = self.state.get("first_run")
                if first_run and now < datetime.fromisoformat(first_run):
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=20
                        )
                        return
                    except asyncio.TimeoutError:
                        continue

                run_time = parse_run_time(self.state["daily_time"])
                target = datetime.combine(
                    now.date(), run_time, tzinfo=TZ
                )

                completed = set(self.state.get("completed_dates", []))
                today = now.date().isoformat()

                # Chạy ngay khi đến giờ, nhưng chỉ một lần mỗi ngày.
                if now >= target and today not in completed:
                    # Ghi ngày trước khi gọi callback để tránh chạy trùng
                    # nếu tiến trình khởi động lại ngay sau đó.
                    self.state["completed_dates"].append(today)
                    self.state["last_run_date"] = today
                    self.state["status"] = "running"
                    save_state(self.state)

                    try:
                        result = await self.task_callback(
                            self.state["account"]
                        )
                        self.state["status"] = "scheduled"
                        self.state["last_result"] = str(result or "OK")[:300]
                        log.info("Đã xử lý lịch ngày %s.", today)
                        await self._notify(
                            f"✅ Điểm danh ngày {today}: {result or 'OK'}"
                        )
                    except Exception as exc:
                        self.state["status"] = "task_error"
                        self.state["last_result"] = (
                            f"Lỗi: {type(exc).__name__}: {exc}"[:300]
                        )
                        log.error("Tác vụ hằng ngày gặp lỗi: %s",
                                  type(exc).__name__)
                        # Không thử lại liên tục: ngày này đã được đánh dấu.
                        await self._notify(
                            f"❌ Điểm danh ngày {today} lỗi: "
                            f"{self.state['last_result']}\n"
                            "Sẽ thử lại vào lịch ngày mai."
                        )

                    save_state(self.state)

                # Kiểm tra mỗi 20 giây để phản hồi nút dừng.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=20
                    )
                    return
                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            raise


async def main():
    scheduler = SevenDayScheduler()
    await scheduler.restore()

    if scheduler.status().get("active"):
        print("Đã khôi phục lịch đang chạy.")
        print(json.dumps(
            scheduler.status(), ensure_ascii=False, indent=2
        ))
        await asyncio.Event().wait()
        return

    accounts_dir = Path("sessions")
    accounts = sorted(accounts_dir.glob("*.session"))

    if not accounts:
        print("Chưa tìm thấy session trong thư mục sessions/.")
        print("Hãy cấu hình tài khoản qua hệ thống bot hiện có.")
        return

    print("Danh sách tài khoản:")
    for i, account in enumerate(accounts, 1):
        print(f"{i}. {account.stem}")

    try:
        choice = int(input("Chọn tài khoản: "))
        if not 1 <= choice <= len(accounts):
            print("Lựa chọn không hợp lệ.")
            return

        account_name = accounts[choice - 1].stem
        run_time = input("Nhập giờ chạy mỗi ngày (HH:MM): ")

        state = await scheduler.start(account_name, run_time)
        print("Đã bật lịch 7 ngày:")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        print("Nhấn Ctrl+C để thoát tiến trình.")
        print("Lịch được lưu trong data/naprut_schedule.json.")

        await asyncio.Event().wait()

    except (ValueError, RuntimeError) as exc:
        print(f"Lỗi: {exc}")
    except KeyboardInterrupt:
        print("Tiến trình kết thúc.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
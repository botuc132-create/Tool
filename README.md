# FULL TOOL Telegram Bot + Mini App

Bot dạng menu nút bấm (check / auto / rải / ref / spam / join / duyệt / naprut) kèm **Telegram Mini App**.
Bot và Mini App chạy chung **một tiến trình** (`python bot.py`), một vòng polling duy nhất.

## Biến môi trường (Railway → Variables)
| Tên | Bắt buộc | Ghi chú |
|---|---|---|
| `BOT_TOKEN`, `API_ID`, `API_HASH`, `ADMIN_IDS` | có | giữ nguyên như cũ |
| `STORAGE_DIR` | nên có | `/app/storage` (gắn Volume) |
| `WEBAPP_URL` | không | URL HTTPS của Mini App. Bỏ trống thì tự dùng `RAILWAY_PUBLIC_DOMAIN` |
| `PORT` | không | Railway tự cấp, đừng tự đặt |
| `WEBAPP_AUTH_MAX_AGE` | không | Số giây initData còn hợp lệ (mặc định 86400) |

## Chạy trên máy
```
pip install -r requirements.txt
export BOT_TOKEN=... API_ID=... API_HASH=... ADMIN_IDS=...
python bot.py          # giao diện ở http://localhost:8080
```
Telegram chỉ mở Mini App qua HTTPS. Muốn thử trên máy: `ngrok http 8080` rồi `export WEBAPP_URL=https://xxxx.ngrok-free.app`.
**Tắt bot trên Railway trước khi chạy trên máy** (cùng token mà polling 2 nơi sẽ báo lỗi Conflict).

## Deploy Railway
1. Đẩy code lên repo đang nối với Railway, Railway tự build lại.
2. Service → **Settings → Networking → Generate Domain** (cổng 8080 hoặc để mặc định theo `PORT`).
3. (Tuỳ chọn) Variables → `WEBAPP_URL=https://<domain>.up.railway.app`.
4. Giữ **1 replica** (`railway.json` đã đặt `numReplicas: 1`).
5. Xem Deploy Logs, phải thấy: `Mini App server listening` và `Mini App menu button set`.

## Mở Mini App trong Telegram
- Nút **Mini App** cạnh ô nhập tin (bot tự đặt lúc khởi động), hoặc
- Lệnh `/app`, hoặc ô **📱 MINI APP** trên bàn phím menu.
- (Tuỳ chọn) BotFather → `/mybots` → bot → Bot Settings → Configure Mini App → dán URL.

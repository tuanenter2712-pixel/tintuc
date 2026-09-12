# News → Telegram Bot

Tự động lấy tin mới từ **TechCrunch**, **Engadget**, **PhoneArena** mỗi 30 phút
và gửi về Telegram của bạn — miễn phí, chạy trên GitHub Actions (không cần
máy chủ, không cần để máy tính bật liên tục).

## Cách hoạt động

- `news_to_telegram.py`: đọc RSS của từng trang, so với `seen.json` (danh
  sách tin đã gửi), tin nào mới thì gửi qua Telegram.
- `.github/workflows/news-bot.yml`: bảo GitHub tự chạy file trên **mỗi 30
  phút**, kể cả khi bạn tắt máy tính.
- Lần chạy đầu tiên sẽ **không** gửi tin (chỉ lưu mốc), để tránh dội một lúc
  hàng chục bài viết cũ về điện thoại. Từ lần chạy thứ 2 trở đi mới bắt đầu
  báo tin mới.

## Bước 1 — Tạo Telegram Bot

1. Mở Telegram, tìm và chat với **@BotFather**.
2. Gõ `/newbot`, đặt tên bot theo hướng dẫn.
3. BotFather sẽ trả về một đoạn dạng:
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   → Đây là **TELEGRAM_BOT_TOKEN**, lưu lại.
4. Vào Telegram, tìm đúng bot bạn vừa tạo (theo username đã đặt) và bấm
   **Start** / gửi thử một tin bất kỳ cho nó (bắt buộc, để bot "biết" chat
   với bạn).

## Bước 2 — Lấy Chat ID của bạn

Mở trình duyệt, truy cập (thay `<TOKEN>` bằng token ở bước 1):

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Bạn sẽ thấy một đoạn JSON có chứa `"chat":{"id":123456789,...}`.
→ Con số đó là **TELEGRAM_CHAT_ID**.

(Nếu không thấy gì, nhắn lại vài tin cho bot rồi tải lại trang trên.)

## Bước 3 — Đưa code này lên GitHub

1. Tạo một repository mới trên GitHub (Public hay Private đều được).
2. Tải toàn bộ các file trong thư mục này lên repo đó (kéo-thả trên GitHub
   web, hoặc dùng `git push` nếu quen dùng Git).

## Bước 4 — Khai báo 2 "Secrets"

Trong repo GitHub vừa tạo:

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

Thêm 2 secret:

| Tên | Giá trị |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token lấy ở Bước 1 |
| `TELEGRAM_CHAT_ID` | chat id lấy ở Bước 2 |

## Bước 5 — Chạy thử

Vào tab **Actions** trên GitHub → chọn workflow **News to Telegram** →
bấm **Run workflow** để chạy thử ngay (không cần đợi 30 phút).

Sau đó workflow sẽ tự động chạy lại mỗi 30 phút — không cần làm gì thêm.

## Thêm / bớt trang web muốn theo dõi

Mở file `news_to_telegram.py`, sửa danh sách `SOURCES` ở đầu file. Ưu tiên
tìm RSS chính thức của trang (thường là `<domain>/feed/` hoặc `<domain>/rss.xml`).
Nếu trang không có RSS, có thể dùng mẹo Google News:

```
https://news.google.com/rss/search?q=site:tenmiengtrangweb.com&hl=en-US&gl=US&ceid=US:en
```

## Lưu ý

- GitHub Actions free có thể chạy trễ vài phút vào giờ cao điểm — đây là
  giới hạn chung, không phải lỗi script.
- Không chia sẻ `TELEGRAM_BOT_TOKEN` cho ai — ai có token đó đều gửi được
  tin nhân danh bot của bạn.

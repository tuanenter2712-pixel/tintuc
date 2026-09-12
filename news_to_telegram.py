"""
News -> Telegram bot
--------------------
Đọc tin mới từ danh sách nguồn RSS bên dưới, so sánh với những tin đã gửi
trước đó (lưu trong seen.json), rồi gửi các tin MỚI về Telegram.

Chạy file này định kỳ (khuyến nghị: GitHub Actions mỗi 30 phút, xem
.github/workflows/news-bot.yml đi kèm).
"""

import json
import os
import time
from pathlib import Path

import feedparser
import requests

# ----------------------------------------------------------------------
# 1. CẤU HÌNH NGUỒN TIN — thêm/bớt/sửa thoải mái ở đây
# ----------------------------------------------------------------------
SOURCES = [
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "Engadget", "url": "https://www.engadget.com/rss.xml"},
    # PhoneArena không có RSS chính thức -> dùng Google News RSS lọc theo site
    {
        "name": "PhoneArena",
        "url": "https://news.google.com/rss/search?q=site:phonearena.com&hl=en-US&gl=US&ceid=US:en",
    },
]

# Mỗi lần chạy chỉ gửi tối đa từng này tin MỚI cho mỗi nguồn,
# để tránh spam nếu vì lý do gì đó bị "sót" nhiều lần chạy liên tiếp.
MAX_NEW_PER_SOURCE = 8

# Giữ lại tối đa từng này link trong lịch sử "đã gửi" (tránh file phình to mãi)
MAX_SEEN_HISTORY = 800

STATE_FILE = Path(__file__).parent / "seen.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ----------------------------------------------------------------------
# 2. STATE (lịch sử các link đã gửi) — đọc/ghi ra seen.json
# ----------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # initialized: những nguồn đã chạy lần đầu (để không spam toàn bộ feed cũ)
    return {"initialized": [], "seen": []}


def save_state(state):
    # chỉ giữ lại N link gần nhất để file không phình to theo thời gian
    state["seen"] = state["seen"][-MAX_SEEN_HISTORY:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ----------------------------------------------------------------------
# 3. GỬI TELEGRAM
# ----------------------------------------------------------------------
def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID (đặt trong biến môi trường / GitHub Secrets)."
        )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"[LỖI TELEGRAM] {resp.status_code}: {resp.text}")
    return resp.ok


# ----------------------------------------------------------------------
# 4. LOGIC CHÍNH
# ----------------------------------------------------------------------
def check_source(source, state, seen_set):
    name = source["name"]
    url = source["url"]

    print(f"-> Đang kiểm tra: {name}")
    parsed = feedparser.parse(url)

    if parsed.bozo and not parsed.entries:
        print(f"   Không đọc được feed của {name}: {parsed.bozo_exception}")
        return []

    entries = parsed.entries or []
    first_run = name not in state["initialized"]

    if first_run:
        # Lần đầu tiên chạy với nguồn này: chỉ đánh dấu "đã thấy", không gửi
        # (tránh việc mới thiết lập là dội cả chục tin cũ về điện thoại)
        for e in entries:
            link = e.get("link")
            if link:
                seen_set.add(link)
        state["initialized"].append(name)
        print(f"   Lần đầu chạy với {name}: đã lưu {len(entries)} tin làm mốc, chưa gửi.")
        return []

    new_items = []
    for e in entries:
        link = e.get("link")
        title = e.get("title", "(không có tiêu đề)")
        if not link or link in seen_set:
            continue
        new_items.append({"name": name, "title": title, "link": link})

    # feed thường liệt kê tin mới nhất trước -> đảo lại để gửi theo thứ tự thời gian
    new_items.reverse()
    return new_items[-MAX_NEW_PER_SOURCE:] if len(new_items) > MAX_NEW_PER_SOURCE else new_items


def main():
    state = load_state()
    seen_set = set(state["seen"])

    all_new = []
    for source in SOURCES:
        try:
            new_items = check_source(source, state, seen_set)
            all_new.extend(new_items)
        except Exception as exc:  # không để 1 nguồn lỗi làm hỏng cả lượt chạy
            print(f"   Lỗi khi xử lý {source['name']}: {exc}")

    if not all_new:
        print("Không có tin mới.")
    else:
        print(f"Có {len(all_new)} tin mới, đang gửi Telegram...")
        for item in all_new:
            text = f"🗞 <b>{item['name']}</b>\n{item['title']}\n{item['link']}"
            ok = send_telegram_message(text)
            if ok:
                seen_set.add(item["link"])
                print(f"   Đã gửi: {item['title'][:70]}")
            time.sleep(1)  # tránh gửi dồn dập bị Telegram giới hạn tốc độ

    state["seen"] = list(seen_set)
    save_state(state)


if __name__ == "__main__":
    main()

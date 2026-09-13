"""
News -> Telegram bot (6 nguồn, chỉ lấy tin trong ngày, lọc chủ đề + dịch tiếng Việt bằng Claude)
--------------------------------------------------------------------------------------------------
Đọc tin từ 6 nguồn RSS bên dưới, CHỈ giữ lại tin được đăng TRONG NGÀY HÔM NAY
(theo giờ Hà Nội) và chưa từng gửi trước đó (lưu trong seen.json). Với các tin
đạt yêu cầu, gửi cho Claude (Anthropic API) để:
  1) Lọc: chỉ giữ tin về smartphone / AI / công nghệ, bỏ tin quảng cáo,
     khuyến mãi, tài trợ (sponsored), hoặc không liên quan công nghệ.
  2) Dịch tiêu đề + tóm tắt sang tiếng Việt: chính xác với nội dung gốc
     nhưng viết theo văn phong hấp dẫn, lôi cuốn người đọc.
Sau đó gửi các tin đã lọc + đã dịch về Telegram.

Lịch chạy: mỗi 60 phút, chỉ trong khung giờ 07:00-23:00 giờ Hà Nội
(xem .github/workflows/news-bot.yml — cron chỉ định nghĩa khung giờ chạy,
KHÔNG cần xử lý gì thêm trong code cho việc tạm dừng ban đêm).
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from anthropic import Anthropic

# ----------------------------------------------------------------------
# 1. CẤU HÌNH NGUỒN TIN — thêm/bớt/sửa thoải mái ở đây
# ----------------------------------------------------------------------
SOURCES = [
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Engadget", "url": "https://www.engadget.com/rss.xml"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Android Authority", "url": "http://feed.androidauthority.com/"},
    {"name": "VnExpress - Khoa học công nghệ", "url": "https://vnexpress.net/rss/khoa-hoc-cong-nghe.rss"},
]

# Chỉ lấy tin được đăng TRONG NGÀY HÔM NAY theo giờ Hà Nội — không lấy tin cũ.
HANOI_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Mỗi lần chạy chỉ xét tối đa từng này tin MỚI cho mỗi nguồn,
# để tránh spam / tốn phí API nếu vì lý do gì đó bị "sót" nhiều lần chạy liên tiếp.
MAX_NEW_PER_SOURCE = 8

# Giữ lại tối đa từng này link trong lịch sử "đã gửi" (tránh file phình to mãi)
MAX_SEEN_HISTORY = 800

# Model Claude dùng để lọc + dịch — Haiku là bản rẻ & nhanh, đủ dùng cho việc này.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

STATE_FILE = Path(__file__).parent / "seen.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


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
# 4. LỌC CHỦ ĐỀ + DỊCH BẰNG CLAUDE
# ----------------------------------------------------------------------
def clean_summary(raw_html: str, limit: int = 300) -> str:
    """Feed RSS thường có HTML trong phần tóm tắt -> bỏ thẻ HTML, cắt bớt độ dài."""
    if not raw_html:
        return ""
    text = re.sub("<[^<]+?>", "", raw_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_published_today_hanoi(entry) -> bool:
    """
    Chỉ chấp nhận tin có ngày đăng = ngày hôm nay theo giờ Hà Nội.
    Nếu feed không có thông tin ngày đăng -> bỏ qua (an toàn hơn là lỡ lấy tin cũ).
    """
    struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct_time:
        return False
    # feedparser trả về struct_time đã chuẩn hoá theo UTC
    published_utc = datetime(*struct_time[:6], tzinfo=ZoneInfo("UTC"))
    published_hanoi_date = published_utc.astimezone(HANOI_TZ).date()
    today_hanoi_date = datetime.now(HANOI_TZ).date()
    return published_hanoi_date == today_hanoi_date


def classify_and_translate(items, client):
    """
    Gửi 1 lần cho Claude toàn bộ danh sách tin mới, nhận về JSON gồm:
    relevant (có nên gửi không), title_vi (tiêu đề tiếng Việt),
    summary_vi (tóm tắt ngắn tiếng Việt).
    """
    if not items:
        return []

    numbered = "\n".join(
        f'{i + 1}. Nguồn: {it["name"]} | Tiêu đề: {it["title"]} | Tóm tắt: {it["summary"] or "(không có)"}'
        for i, it in enumerate(items)
    )

    prompt = f"""Bạn là bộ lọc + biên tập tin tức công nghệ cho một kênh Telegram tiếng Việt. Dưới đây là {len(items)} tin.

Với MỖI tin, hãy xác định:
- "relevant": true nếu tin thuộc chủ đề smartphone, AI (trí tuệ nhân tạo), hoặc công nghệ nói chung (phần cứng, phần mềm, chip, startup công nghệ, ứng dụng...). Trả về false nếu đây là: bài quảng cáo, tin khuyến mãi/giảm giá/deal mua sắm, nội dung được tài trợ (sponsored/partner content), hoặc chủ đề không liên quan công nghệ (chính trị, giải trí đơn thuần, thể thao...).
- "title_vi": biên tập lại tiêu đề bằng tiếng Việt sao cho NGẮN GỌN, HẤP DẪN, thu hút người đọc bấm vào xem — nhưng phải giữ ĐÚNG sự thật, không giật gân sai lệch so với nội dung gốc. Nếu tin gốc đã bằng tiếng Việt (ví dụ từ VnExpress), chỉ biên tập lại cho súc tích/hấp dẫn hơn nếu cần, không cần dịch.
- "summary_vi": viết tóm tắt 2-3 câu bằng tiếng Việt, văn phong lôi cuốn, dễ đọc, nhưng phải CHÍNH XÁC với các sự kiện/số liệu trong tóm tắt gốc — không thêm thắt, không suy diễn.

Danh sách tin:
{numbered}

CHỈ trả lời bằng một JSON array hợp lệ, đúng thứ tự với danh sách trên, không thêm bất kỳ chữ nào khác, markdown, hay giải thích. Định dạng:
[{{"relevant": true, "title_vi": "...", "summary_vi": "..."}}, ...]"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text.strip()

    # phòng khi Claude lỡ bọc code fence ```json ... ```
    raw_text = re.sub(r"^```(json)?|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()

    results = json.loads(raw_text)
    if len(results) != len(items):
        raise ValueError(
            f"Số kết quả trả về ({len(results)}) không khớp số tin gửi đi ({len(items)})."
        )
    return results


# ----------------------------------------------------------------------
# 5. LOGIC CHÍNH
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
    skipped_seen = 0
    skipped_old = 0
    for e in entries:
        link = e.get("link")
        title = e.get("title", "(không có tiêu đề)")
        if not link or link in seen_set:
            skipped_seen += 1
            continue
        if not is_published_today_hanoi(e):
            skipped_old += 1
            continue  # bỏ qua tin không phải đăng trong ngày hôm nay
        summary = clean_summary(e.get("summary", ""))
        new_items.append({"name": name, "title": title, "link": link, "summary": summary})

    print(
        f"   Tổng {len(entries)} bài trong feed | "
        f"đã thấy trước đó: {skipped_seen} | "
        f"không phải hôm nay: {skipped_old} | "
        f"đạt điều kiện: {len(new_items)}"
    )

    # feed thường liệt kê tin mới nhất trước -> đảo lại để xử lý theo thứ tự thời gian
    new_items.reverse()
    return new_items[-MAX_NEW_PER_SOURCE:] if len(new_items) > MAX_NEW_PER_SOURCE else new_items


def main():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "Thiếu ANTHROPIC_API_KEY (đặt trong biến môi trường / GitHub Secrets)."
        )
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

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
        print("Không có tin mới trong ngày hôm nay.")
        state["seen"] = list(seen_set)
        save_state(state)
        return

    print(f"Có {len(all_new)} tin mới trong ngày, đang lọc + dịch bằng Claude...")
    try:
        results = classify_and_translate(all_new, client)
    except Exception as exc:
        # Nếu Claude lỗi (mạng, hết quota, JSON sai...) -> không để mất tin:
        # coi như mọi tin đều relevant, gửi tạm bản gốc tiếng Anh, đánh dấu đã gửi.
        print(f"   [CẢNH BÁO] Lọc/dịch bằng Claude thất bại ({exc}). Gửi tạm bản gốc.")
        results = [{"relevant": True, "title_vi": None, "summary_vi": None} for _ in all_new]

    sent_count = 0
    for item, result in zip(all_new, results):
        if not result.get("relevant", True):
            # tin bị coi là quảng cáo / không liên quan -> vẫn đánh dấu đã xem để không hỏi lại,
            # nhưng KHÔNG gửi về Telegram.
            seen_set.add(item["link"])
            print(f"   Bỏ qua (không liên quan/quảng cáo): {item['title'][:70]}")
            continue

        title_vi = result.get("title_vi") or item["title"]
        summary_vi = result.get("summary_vi") or ""

        text = f"🗞 <b>{item['name']}</b>\n{title_vi}"
        if summary_vi:
            text += f"\n{summary_vi}"
        text += f"\n{item['link']}"

        ok = send_telegram_message(text)
        if ok:
            seen_set.add(item["link"])
            sent_count += 1
            print(f"   Đã gửi: {title_vi[:70]}")
        time.sleep(1)  # tránh gửi dồn dập bị Telegram giới hạn tốc độ

    print(f"Hoàn tất: gửi {sent_count}/{len(all_new)} tin.")
    state["seen"] = list(seen_set)
    save_state(state)


if __name__ == "__main__":
    main()

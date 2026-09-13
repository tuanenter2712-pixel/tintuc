"""
News -> Telegram bot (8 nguồn, chạy mỗi giờ, lọc chủ đề + dịch tiếng Việt bằng Claude)
--------------------------------------------------------------------------------------------------
Đọc tin từ 8 nguồn RSS bên dưới, CHỈ giữ lại tin được đăng trong vòng
LOOKBACK_HOURS giờ gần nhất (mặc định 24h) và chưa từng gửi trước đó (lưu
trong seen.json). Với các tin đạt yêu cầu, gửi cho Claude (Anthropic API) để:
  1) Lọc: chỉ giữ tin về smartphone / AI / công nghệ, bỏ tin quảng cáo,
     khuyến mãi, tài trợ (sponsored), hoặc không liên quan công nghệ.
  2) Dịch tiêu đề + tóm tắt sang tiếng Việt: chính xác với nội dung gốc
     nhưng viết theo văn phong hấp dẫn, lôi cuốn người đọc.
  3) Gộp nhóm: nếu nhiều nguồn cùng đưa tin về MỘT sự kiện/tin tức giống nhau
     (kể cả khi viết bằng ngôn ngữ khác nhau) -> gộp lại thành 1 tin nhắn duy
     nhất, liệt kê đủ các nguồn, KHÔNG gửi lặp lại nhiều tin nhắn cho cùng 1
     câu chuyện.
Sau đó gửi các tin đã lọc + đã dịch + đã gộp về Telegram.

Lịch chạy: mỗi giờ, 07:00-23:00 giờ Hà Nội (xem .github/workflows/news-bot.yml).
Nhờ lọc theo LOOKBACK_HOURS = 24 giờ gần nhất (thay vì "đúng ngày hôm nay"
như trước) và luôn nhớ tin đã gửi qua seen.json, lần chạy 07:00 sáng sẽ tự
động nhặt được mọi tin phát sinh từ 23h đêm hôm trước mà chưa từng gửi,
không cần logic riêng và không bị trùng lặp.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
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
    {"name": "GenK", "url": "https://genk.vn/rss/home.rss"},
    {"name": "VnReview", "url": "https://vnreview.vn/forums/-/index.rss"},
]

# Chỉ lấy tin được đăng trong vòng LOOKBACK_HOURS giờ gần nhất — không lấy tin cũ hơn.
# Mặc định 24h để lần chạy 7h sáng bắt trọn tin đăng suốt đêm hôm trước (kể cả
# tin đăng 23h-24h, vẫn thuộc "ngày hôm qua" theo lịch) mà không cần logic riêng.
HANOI_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
LOOKBACK_HOURS = 24

# Mỗi lần chạy chỉ xét tối đa từng này tin MỚI cho mỗi nguồn. Đặt cao hơn mức
# cần cho 1 giờ bình thường để có khoảng đệm an toàn (ví dụ lần 7h sáng gộp
# nhiều tin hơn, hoặc lỡ có lần chạy bị GitHub bỏ lỡ).
MAX_NEW_PER_SOURCE = 40

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
# 4. LỌC CHỦ ĐỀ + DỊCH + GỘP TIN TRÙNG BẰNG CLAUDE
# ----------------------------------------------------------------------
def clean_summary(raw_html: str, limit: int = 300) -> str:
    """Feed RSS thường có HTML trong phần tóm tắt -> bỏ thẻ HTML, cắt bớt độ dài."""
    if not raw_html:
        return ""
    text = re.sub("<[^<]+?>", "", raw_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_recent_enough(entry) -> bool:
    """
    Chỉ chấp nhận tin được đăng trong vòng LOOKBACK_HOURS giờ gần nhất tính đến
    lúc chạy — thay cho cách lọc "đúng ngày hôm nay theo lịch" trước đây (cách cũ
    sẽ bỏ sót tin đăng cuối đêm hôm trước, vì lúc đó ngày đăng đã là "hôm qua").
    Nếu feed không có thông tin ngày đăng -> bỏ qua (an toàn hơn là lỡ lấy tin cũ).
    """
    struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct_time:
        return False
    # feedparser trả về struct_time đã chuẩn hoá theo UTC
    published_utc = datetime(*struct_time[:6], tzinfo=ZoneInfo("UTC"))
    now_utc = datetime.now(ZoneInfo("UTC"))
    return (now_utc - published_utc) <= timedelta(hours=LOOKBACK_HOURS)


def classify_and_translate(items, client):
    """
    Gửi 1 lần cho Claude toàn bộ danh sách tin mới, nhận về JSON gồm:
    relevant (có nên gửi không), duplicate_group (số nhóm để gộp các tin
    trùng nội dung đến từ nhiều nguồn khác nhau), title_vi (tiêu đề tiếng
    Việt), summary_vi (tóm tắt ngắn tiếng Việt).
    """
    if not items:
        return []

    numbered = "\n".join(
        f'{i + 1}. Nguồn: {it["name"]} | Tiêu đề: {it["title"]} | Tóm tắt: {it["summary"] or "(không có)"}'
        for i, it in enumerate(items)
    )

    prompt = f"""Bạn là bộ lọc + biên tập tin tức công nghệ cho một kênh Telegram tiếng Việt. Dưới đây là {len(items)} tin, lấy từ nhiều nguồn khác nhau (có thể có nguồn tiếng Anh và nguồn tiếng Việt).

Với MỖI tin, hãy xác định:
- "relevant": true nếu tin thuộc chủ đề smartphone, AI (trí tuệ nhân tạo), hoặc công nghệ nói chung (phần cứng, phần mềm, chip, startup công nghệ, ứng dụng...). Trả về false nếu đây là: bài quảng cáo, tin khuyến mãi/giảm giá/deal mua sắm, nội dung được tài trợ (sponsored/partner content), bài thuộc diễn đàn không phải tin tức (hỏi đáp, review cá nhân không có giá trị tin tức), hoặc chủ đề không liên quan công nghệ (chính trị, giải trí đơn thuần, thể thao...).
- "duplicate_group": một số nguyên. Nếu 2 tin trở lên (dù khác nguồn, khác ngôn ngữ, cách viết tiêu đề khác nhau) đang nói về CÙNG MỘT sự kiện/tin tức thực tế (ví dụ: cùng đưa tin về việc ra mắt 1 sản phẩm, cùng 1 vụ rò rỉ, cùng 1 thông báo của 1 công ty) -> gán cho chúng CÙNG một số duplicate_group. Các tin không trùng với tin nào khác thì mỗi tin nhận một số duplicate_group riêng, không được trùng với số của tin khác. Chỉ những tin có "relevant": true mới cần xét gộp nhóm.
- "title_vi": biên tập lại tiêu đề bằng tiếng Việt sao cho NGẮN GỌN, HẤP DẪN, thu hút người đọc bấm vào xem — nhưng phải giữ ĐÚNG sự thật, không giật gân sai lệch so với nội dung gốc. Nếu tin gốc đã bằng tiếng Việt (ví dụ từ VnExpress, GenK, VnReview), chỉ biên tập lại cho súc tích/hấp dẫn hơn nếu cần, không cần dịch. Nếu tin này bị gộp nhóm (duplicate_group trùng với tin khác), hãy viết title_vi là tiêu đề chung, đầy đủ nhất cho CẢ NHÓM (không cần viết riêng cho từng tin trong nhóm).
- "summary_vi": viết tóm tắt 2-3 câu bằng tiếng Việt, văn phong lôi cuốn, dễ đọc, nhưng phải CHÍNH XÁC với các sự kiện/số liệu trong tóm tắt gốc — không thêm thắt, không suy diễn. Nếu tin bị gộp nhóm, hãy tổng hợp thông tin từ TẤT CẢ các tin trong nhóm đó thành 1 bản tóm tắt chung, đầy đủ nhất.

Danh sách tin:
{numbered}

CHỈ trả lời bằng một JSON array hợp lệ, đúng thứ tự với danh sách trên, không thêm bất kỳ chữ nào khác, markdown, hay giải thích. Định dạng:
[{{"relevant": true, "duplicate_group": 1, "title_vi": "...", "summary_vi": "..."}}, ...]"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
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
    for e in entries:
        link = e.get("link")
        title = e.get("title", "(không có tiêu đề)")
        if not link or link in seen_set:
            continue
        if not is_recent_enough(e):
            continue  # bỏ qua tin đăng quá lâu (ngoài khung LOOKBACK_HOURS giờ gần nhất)
        summary = clean_summary(e.get("summary", ""))
        new_items.append({"name": name, "title": title, "link": link, "summary": summary})

    # feed thường liệt kê tin mới nhất trước -> đảo lại để xử lý theo thứ tự thời gian
    new_items.reverse()
    return new_items[-MAX_NEW_PER_SOURCE:] if len(new_items) > MAX_NEW_PER_SOURCE else new_items


def group_relevant_items(all_new, results):
    """
    Gộp các tin có cùng "duplicate_group" (do Claude xác định là cùng 1 sự
    kiện/tin tức, dù khác nguồn) thành 1 nhóm duy nhất -> sau này chỉ gửi
    1 tin nhắn Telegram cho cả nhóm, tránh đăng lặp lại cùng 1 câu chuyện.

    Trả về list các dict: {"names": [...], "links": [...], "title_vi": ...,
    "summary_vi": ..., "items": [item gốc, ...]} — theo đúng thứ tự nhóm nào
    xuất hiện trước trong all_new thì đứng trước.
    """
    groups = {}
    order = []
    for item, result in zip(all_new, results):
        if not result.get("relevant", True):
            continue
        group_key = result.get("duplicate_group")
        if group_key is None:
            # Claude không trả về duplicate_group (lỗi/fallback) -> coi mỗi tin
            # là 1 nhóm riêng, dùng link làm khoá để chắc chắn không trùng.
            group_key = ("__no_group__", item["link"])
        if group_key not in groups:
            groups[group_key] = {
                "names": [],
                "links": [],
                "title_vi": None,
                "summary_vi": None,
                "items": [],
            }
            order.append(group_key)
        g = groups[group_key]
        g["items"].append(item)
        if item["name"] not in g["names"]:
            g["names"].append(item["name"])
        g["links"].append(item["link"])
        # Ưu tiên bản dịch dài nhất trong nhóm làm bản đại diện (thường đầy đủ nhất).
        candidate_title = result.get("title_vi") or item["title"]
        candidate_summary = result.get("summary_vi") or ""
        if g["summary_vi"] is None or len(candidate_summary) > len(g["summary_vi"]):
            g["title_vi"] = candidate_title
            g["summary_vi"] = candidate_summary

    return [groups[key] for key in order]


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
        print(f"Không có tin mới trong {LOOKBACK_HOURS} giờ qua.")
        state["seen"] = list(seen_set)
        save_state(state)
        return

    print(f"Có {len(all_new)} tin mới trong {LOOKBACK_HOURS} giờ qua, đang lọc + dịch + gộp tin trùng bằng Claude...")
    try:
        results = classify_and_translate(all_new, client)
    except Exception as exc:
        # Nếu Claude lỗi (mạng, hết quota, JSON sai...) -> không để mất tin:
        # coi như mọi tin đều relevant, không gộp nhóm được (mỗi tin 1 nhóm),
        # gửi tạm bản gốc tiếng Anh, đánh dấu đã gửi.
        print(f"   [CẢNH BÁO] Lọc/dịch/gộp bằng Claude thất bại ({exc}). Gửi tạm bản gốc, không gộp trùng.")
        results = [{"relevant": True, "duplicate_group": None, "title_vi": None, "summary_vi": None} for _ in all_new]

    # Các tin không relevant: vẫn đánh dấu đã xem để không hỏi lại, nhưng KHÔNG gửi.
    for item, result in zip(all_new, results):
        if not result.get("relevant", True):
            seen_set.add(item["link"])
            print(f"   Bỏ qua (không liên quan/quảng cáo): {item['title'][:70]}")

    groups = group_relevant_items(all_new, results)

    sent_count = 0
    for g in groups:
        header = g["names"][0] if len(g["names"]) == 1 else f'{g["names"][0]} (+ {", ".join(g["names"][1:])})'
        text = f"🗞 <b>{header}</b>\n{g['title_vi']}"
        if g["summary_vi"]:
            text += f"\n{g['summary_vi']}"
        # Nếu gộp nhiều nguồn thì liệt kê đủ link để người đọc đối chiếu, tin lẻ chỉ có 1 link.
        text += "\n" + "\n".join(g["links"])

        ok = send_telegram_message(text)
        if ok:
            for link in g["links"]:
                seen_set.add(link)
            sent_count += 1
            n_sources = len(g["names"])
            tag = f" (gộp {n_sources} nguồn)" if n_sources > 1 else ""
            print(f"   Đã gửi{tag}: {g['title_vi'][:70]}")
        time.sleep(1)  # tránh gửi dồn dập bị Telegram giới hạn tốc độ

    print(f"Hoàn tất: gửi {sent_count}/{len(groups)} tin (từ {len(all_new)} tin gốc trước khi gộp).")
    state["seen"] = list(seen_set)
    save_state(state)


if __name__ == "__main__":
    main()

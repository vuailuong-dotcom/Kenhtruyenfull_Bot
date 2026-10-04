import asyncio
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urljoin, urldefrag
import cloudscraper
from bs4 import BeautifulSoup
from ebooklib import epub
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# WEB SERVER CHO RENDER (Giữ bot luôn hoạt động 24/7)
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Bot Kênh Truyện Full đang hoạt động!".encode('utf-8'))

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ============================================================
# CẤU HÌNH BOT & CLOUDSCRAPER
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_TRUYENFULL") or os.getenv("BOT_TOKEN")

def get_scraper():
    return cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )

def get_content(url):
    scraper = get_scraper()
    try:
        res = scraper.get(url, timeout=20)
        if res.status_code == 200:
            return BeautifulSoup(res.text, "lxml")
    except Exception as e:
        print(f"Lỗi tải {url}: {e}")
    return None

def extract_chapter_number(name):
    name_lower = name.lower()
    if "cuối" in name_lower or "ngoại" in name_lower or "ngoai" in name_lower or "extra" in name_lower:
        return 999999
        
    numbers = re.findall(r'\d+', name)
    if numbers:
        return int(numbers[0])
    return 0

def download_chap(url):
    soup = get_content(url)
    if not soup: return "", None
    
    container = (
        soup.select_one(".chapter-content") or
        soup.select_one("#chapter-content") or
        soup.select_one(".chapter-c") or
        soup.select_one("#chapter-c") or
        soup.select_one(".chapter-text") or
        soup.select_one("#content") or
        soup.select_one(".box-content") or
        soup.body
    )
    
    if not container:
        container = soup

    for tag in container.find_all(["img", "svg", "iframe", "picture", "hr", "script", "style", "ins"]):
        tag.decompose()
        
    for box in container.find_all(True):
        classes = " ".join(box.get("class", [])) if box.get("class") else ""
        if re.search(r"ads|banner|ebook|download|promo|nav|menu|box-h|truyen-hot|ads-chapter", classes, re.I):
            box.decompose()

    real_title = ""
    for h in container.find_all(["h1", "h2", "h3"]):
        text = h.get_text().strip()
        if re.match(r"^(chương|chuong|hồi|hoi)\s*\d+", text, re.I):
            real_title = text
            h.decompose()
            break
            
    if not real_title and soup.title:
        page_title = soup.title.get_text().strip()
        if "-" in page_title:
            parts = page_title.split("-")
            for p in parts:
                if re.search(r"chương|chuong", p, re.I):
                    real_title = p.strip()
                    break

    ignore_keywords = [
        "bỏ qua nội dung", "trang chủ", "lượt xem:", "cập nhật:", "chia sẻ", 
        "thích", "đang tải", "có liên quan", "báo lỗi", "khám phá thêm", 
        "đăng nhập", "bình luận", "viết:", "lúc", "danh sách",
        "phím mũi tên", "sang chương", "truyện hot mới", "tải ebook",
        "chương trước", "chương sau", "« chương", "chương tiếp »", "quảng cáo"
    ]

    for element in list(container.find_all(True)):
        if element.parent is None:
            continue
        text = element.get_text().strip().lower()
        if any(kw in text for kw in ignore_keywords) and len(text) < 200:
            if element not in [container, soup.body] and element.name not in ["p", "br"]:
                element.decompose()

    paragraphs = container.find_all(["p", "div"])
    valid_p = []
    seen_texts = set()
    
    for p in paragraphs:
        if p.name == "div" and p.find("div"):
            continue
            
        text = p.get_text().strip()
        if not text or len(text) < 2:
            continue
        lower_text = text.lower()
        
        if any(kw in lower_text for kw in ignore_keywords):
            continue
            
        if text in seen_texts:
            continue
        seen_texts.add(text)
            
        if p.name == "p":
            valid_p.append(str(p))
        else:
            valid_p.append(f"<p>{text}</p>")
        
    content_html = "".join(valid_p) if valid_p else str(container)
    return real_title, content_html

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang phân tích link và kết nối Kênh Truyện Full...")
    raw_url = url_match[0].strip()
    
    # Tự động dịch ngược link chương lẻ về trang chủ truyện
    if "/doc-truyen/" in raw_url:
        parts = raw_url.split("/doc-truyen/")
        base_site = parts[0].rstrip("/")
        slug = parts[1].split("/")[0]
        story_url = f"{base_site}/truyen/{slug}"
    else:
        story_url = raw_url
    
    main_soup = get_content(story_url)
    if not main_soup:
        story_url = raw_url
        main_soup = get_content(story_url)
        if not main_soup:
            await status.edit_text("❌ Không thể kết nối tới trang truyện.")
            return
        
    og_title = main_soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"]
    else:
        title_el = main_soup.select_one("h1") or main_soup.title
        title = title_el.get_text().strip() if title_el else "Truyện"
        
    if "|" in title:
        title = title.split('|')[0].strip()
        
    cover_url = None
    og_img = main_soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover_url = og_img["content"]
    
    if not cover_url:
        img_el = main_soup.select_one(".book img, .info-image img, .story-image img, img.cover, article img")
        if img_el:
            cover_url = img_el.get("data-src") or img_el.get("data-original") or img_el.get("src")

    if cover_url:
        cover_url = urljoin(story_url, cover_url)

    links = []
    all_tags = main_soup.find_all("a", href=True)
    for a in all_tags:
        href = a.get('href', '')
        text = a.get_text().strip()
        
        is_chap = re.match(r"^(chương|chuong|hồi|hoi|quyển|quyen|c\s*\d+|\d+|phần|phan|pn\s*\d+|nt\s*\d+|ngoại truyện)", text, flags=re.IGNORECASE)
        
        if is_chap and len(text) < 100:
            full_url = urldefrag(urljoin(story_url, href))[0]
            if not any(l['url'] == full_url for l in links):
                links.append({"name": text, "url": full_url})

    links.sort(key=lambda x: extract_chapter_number(x['name']))

    if not links:
        await status.edit_text("❌ Không tìm thấy chương nào hoặc link không hợp lệ.")
        return
        
    await status.edit_text(f"📚 {title}\n⚡ Đã quét xong {len(links)} chương. Đang tải song song...")

    results = {}
    
    def task(idx, chap_info):
        r_title, content = download_chap(chap_info["url"])
        final_title = r_title if r_title else chap_info["name"]
        return idx, {"name": final_title, "content": content}

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(task, i, c): i for i, c in enumerate(links)}
        completed = 0
        last_update_time = 0
        
        for future in as_completed(futures):
            idx, res = future.result()
            results[idx] = res
            completed += 1
            
            current_time = time.time()
            if completed == len(links) or (current_time - last_update_time > 2.5):
                last_update_time = current_time
                try:
                    pct = int((completed / len(links)) * 100)
                    await status.edit_text(f"📚 {title}\n⚡ Đang tải: {pct}%\n(Đã xong {completed}/{len(links)} chương)")
                except Exception:
                    pass

    book = epub.EpubBook()
    book.set_identifier('truyen_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')
    
    if cover_url:
        try:
            scraper = get_scraper()
            img_res = scraper.get(cover_url, timeout=15)
            if img_res.status_code == 200:
                book.set_cover("cover.jpg", img_res.content)
        except Exception as e:
            print(f"Lỗi tải ảnh bìa: {e}")

    chapters_list = []
    success_count = 0
    for i in range(len(links)):
        chap_data = results.get(i)
        if chap_data and chap_data["content"]:
            chap_title = chap_data["name"]
            chap = epub.EpubHtml(title=chap_title, file_name=f"chap_{i+1}.xhtml")
            chap.content = f"<h2>{chap_title}</h2>{chap_data['content']}"
            book.add_item(chap)
            chapters_list.append(chap)
            success_count += 1

    if success_count == 0:
        await status.edit_text("❌ Tải thất bại do trang web chặn toàn bộ nội dung.")
        return

    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
    file_name = f"{safe_title}.epub"
    epub.write_epub(file_name, book)
    
    await status.edit_text("⬆️ Đang gửi file EPUB qua Telegram...")
    with open(file_name, "rb") as f:
        await update.message.reply_document(
            document=f, 
            caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {success_count}/{len(links)} chương!"
        )

    await status.delete()
    if os.path.exists(file_name):
        os.remove(file_name)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    if not BOT_TOKEN:
        print("❌ Lỗi: Thiếu BOT_TOKEN!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

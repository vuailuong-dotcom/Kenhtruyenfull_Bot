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

def download_chap(url):
    soup = get_content(url)
    if not soup: return "", None, ""
    
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
    
    # Tìm link chương tiếp theo từ nút "Chương sau"
    next_url = ""
    for a in soup.find_all("a", href=True):
        t = a.get_text().strip().lower()
        if "chương sau" in t or "sau »" in t or "tiếp »" in t or "»" in t:
            href = a.get("href")
            if href and "javascript" not in href:
                next_url = urldefrag(urljoin(url, href))[0]
                break
                
    return real_title, content_html, next_url

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang bắt đầu quét truyện từ link chương...")
    start_url = url_match[0].strip()
    
    # Tải chương đầu tiên để lấy thông tin tên truyện và dò các chương tiếp theo
    first_soup = get_content(start_url)
    if not first_soup:
        await status.edit_text("❌ Không thể kết nối tới link chương.")
        return
        
    # Trích xuất tên truyện từ tiêu đề trang
    page_title = first_soup.title.get_text().strip() if first_soup.title else "Truyện"
    title = page_title.split("-")[0].strip() if "-" in page_title else page_title
    
    links = []
    current_url = start_url
    
    # Lần lượt cào các chương thông qua nút "Chương sau"
    await status.edit_text(f"📚 {title}\n⚡ Đang dò danh sách các chương...")
    
    max_safety = 2000 # Giới hạn tối đa tránh lặp vô tận
    while current_url and len(links) < max_safety:
        r_title, content, next_url = download_chap(current_url)
        if not content:
            break
            
        chap_name = r_title or f"Chương {len(links) + 1}"
        links.append({"url": current_url, "name": chap_name, "content": content})
        
        if not next_url or next_url == current_url:
            break
            
        current_url = next_url
        if len(links) % 10 == 0:
            await status.edit_text(f"📚 {title}\n⚡ Đã dò thấy {len(links)} chương...")

    if not links:
        await status.edit_text("❌ Không tìm thấy nội dung chương nào.")
        return
        
    await status.edit_text(f"📚 {title}\n⚡ Đã thu thập xong {len(links)} chương. Đang đóng gói EPUB...")

    book = epub.EpubBook()
    book.set_identifier('truyen_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')

    chapters_list = []
    for i, chap in enumerate(links):
        epub_chap = epub.EpubHtml(title=chap["name"], file_name=f"chap_{i+1}.xhtml")
        epub_chap.content = f"<h2>{chap['name']}</h2>{chap['content']}"
        book.add_item(epub_chap)
        chapters_list.append(epub_chap)

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
            caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {len(links)} chương!"
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

import asyncio
import os
import re
import time
import threading
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
    if not soup: return ""
    
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
    return content_html

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang phân tích link truyện...")
    input_url = url_match[0].strip()
    
    soup = get_content(input_url)
    if not soup:
        await status.edit_text("❌ Không thể kết nối tới đường dẫn này.")
        return

    # Tự động trích xuất slug để lấy trang thông tin chính (nơi chứa tên chuẩn của truyện)
    parsed_url = urlparse(input_url)
    path_parts = [p for p in parsed_url.path.split('/') if p]
    story_slug = ""
    if len(path_parts) >= 2:
        if path_parts[0] in ["truyen", "doc-truyen"]:
            story_slug = path_parts[1]

    home_url = input_url
    if story_slug:
        home_url = f"{parsed_url.scheme}://{parsed_url.netloc}/truyen/{story_slug}"

    home_soup = get_content(home_url) or soup

    # Lấy tên truyện chuẩn từ thẻ h1 hoặc tiêu đề trang chính
    title = "Truyen"
    h1_tag = home_soup.select_one("h1")
    if h1_tag:
        title = h1_tag.get_text().strip()
    else:
        page_title = home_soup.title.get_text().strip() if home_soup.title else "Truyện"
        title = page_title.split("-")[0].strip() if "-" in page_title else page_title

    # Tải ảnh bìa truyện
    cover_image_data = None
    cover_image_ext = "jpg"
    try:
        img_tag = home_soup.select_one(".book img") or home_soup.select_one(".info img") or home_soup.select_one("div.book img")
        if img_tag and img_tag.get("src"):
            img_url = urljoin(home_url, img_tag.get("src"))
            scraper = get_scraper()
            img_res = scraper.get(img_url, timeout=10)
            if img_res.status_code == 200:
                cover_image_data = img_res.content
                if "png" in img_url.lower():
                    cover_image_ext = "png"
    except Exception as e:
        print(f"Không lấy được ảnh bìa: {e}")

    # Thu thập toàn bộ danh sách chương từ mục lục
    await status.edit_text(f"📚 {title}\n⚡ Đang quét danh sách chương...")
    chapter_links = []
    pages_to_check = [home_url]
    pagination = home_soup.select(".pagination a, .list-page a")
    for a in pagination:
        href = a.get("href")
        if href:
            full_p_url = urldefrag(urljoin(home_url, href))[0]
            if full_p_url not in pages_to_check:
                pages_to_check.append(full_p_url)
                
    pages_to_check = pages_to_check[:15]

    for p_url in pages_to_check:
        p_soup = get_content(p_url) if p_url != home_url else home_soup
        if not p_soup: continue
        
        for a in p_soup.find_all("a", href=True):
            href = a.get("href", "")
            text = a.get_text().strip()
            if "/doc-truyen/" in href and ("chương" in text.lower() or "chuong-" in href.lower()):
                full_chap_url = urldefrag(urljoin(home_url, href))[0]
                if full_chap_url not in [x["url"] for x in chapter_links]:
                    chapter_links.append({"url": full_chap_url, "name": text if text else f"Chương {len(chapter_links)+1}"})

    def extract_chap_number(name_or_url):
        match = re.search(r"chương\s*(\d+)", name_or_url, re.I)
        if match:
            return int(match.group(1))
        match2 = re.search(r"chuong-(\d+)", name_or_url, re.I)
        if match2:
            return int(match2.group(1))
        return 999999

    chapter_links.sort(key=lambda x: extract_chap_number(x["name"] + " " + x["url"]))

    if not chapter_links:
        await status.edit_text("❌ Không tìm thấy danh sách chương từ trang này.")
        return

    total_chapters = len(chapter_links)
    await status.edit_text(f"📚 {title}\n⚡ Tìm thấy {total_chapters} chương. Đang tải nội dung...")

    links = []
    for i, item in enumerate(chapter_links):
        content = download_chap(item["url"])
        if content:
            links.append({"name": f"Chương {i+1}", "content": content})
            
        # Tính phần trăm hoàn thành (%)
        percent = int(((i + 1) / total_chapters) * 100)
        if (i + 1) % 5 == 0 or (i + 1) == total_chapters:
            await status.edit_text(f"📚 {title}\n⚡ Đã tải: {i+1}/{total_chapters} chương ({percent}%)")

    if not links:
        await status.edit_text("❌ Không thể tải nội dung các chương.")
        return

    await status.edit_text(f"📚 {title}\n⚡ Đã tải xong. Đang đóng gói EPUB...")

    book = epub.EpubBook()
    book.set_identifier('truyen_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')

    if cover_image_data:
        cover_filename = f"cover.{cover_image_ext}"
        book.set_cover(cover_filename, cover_image_data)

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
    
    await status.edit_text("⬆ Đang gửi file EPUB qua Telegram...")
    with open(file_name, "rb") as f:
        await update.message.reply_document(
            document=f, 
            caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {len(links)} chương (Có ảnh bìa)!"
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

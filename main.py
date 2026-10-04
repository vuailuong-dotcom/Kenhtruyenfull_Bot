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

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

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
    if not soup: return None, ""
    
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
    
    # Tìm chính xác nút "Tiếp >"
    next_url = ""
    next_btn = None
    
    for a in soup.find_all("a", href=True):
        t = a.get_text().strip().lower()
        href = a.get("href", "")
        
        is_next = any(k in t for k in ["tiếp", "sau", "»", "next"])
        is_prev = any(k in t for k in ["trước", "«", "prev"])
        
        if is_next and not is_prev and "/doc-truyen/" in href:
            if href and "javascript" not in href and "#" not in href:
                next_btn = a
                break
                
    if next_btn:
        next_url = urldefrag(urljoin(url, next_btn.get("href")))[0]
                    
    return content_html, next_url

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang kết nối tới truyện...")
    input_url = url_match[0].strip()
    
    soup = get_content(input_url)
    if not soup:
        await status.edit_text("❌ Không thể kết nối tới đường dẫn này.")
        return
        
    parsed_url = urlparse(input_url)
    path_parts = [p for p in parsed_url.path.split('/') if p]
    
    # Lấy slug từ link để truy cập trang thông tin (Home URL của truyện) lấy tên chính xác có dấu và ảnh bìa
    slug = ""
    if len(path_parts) >= 2:
        slug = path_parts[1] if path_parts[0] == "doc-truyen" else path_parts[0]
        
    home_url = f"{parsed_url.scheme}://{parsed_url.netloc}/truyen/{slug}" if slug else input_url
    home_soup = get_content(home_url) if slug else soup
    
    # 1. LẤY TÊN TRUYỆN CHUẨN CÓ DẤU (Ưu tiên từ thẻ h1 hoặc og:title ở trang thông tin)
    title = "Truyen"
    target_name_soup = home_soup if home_soup else soup
    if target_name_soup:
        og_title = target_name_soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].split("|")[0].strip()
        else:
            h1_tag = target_name_soup.select_one("h1") or target_name_soup.select_one(".title")
            if h1_tag:
                title = h1_tag.get_text().strip()
            elif target_name_soup.title:
                page_title = target_name_soup.title.get_text().strip()
                title = page_title.split("-")[0].strip() if "-" in page_title else page_title

    # Lọc bỏ chữ "Chương" nếu lỡ dính vào tên
    title = re.sub(r'\s*-\s*Chương\s*\d+.*$', '', title, flags=re.IGNORECASE).strip()

    start_url = input_url
    
    # Nếu gửi link trang giới thiệu (/truyen/), tự động tìm đến Chương 1
    if "/truyen/" in input_url:
        first_chap_link = None
        target_soup = home_soup if home_soup else soup
        for a in target_soup.find_all("a", href=True):
            href = a.get("href", "")
            if "chuong-1" in href and "/doc-truyen/" in href:
                first_chap_link = a
                break
        
        if first_chap_link and first_chap_link.get("href"):
            start_url = urldefrag(urljoin(input_url, first_chap_link.get("href")))[0]
        else:
            await status.edit_text("❌ Không tìm thấy Chương 1. Vui lòng gửi trực tiếp link Chương 1.")
            return

    # 2. LẤY ẢNH BÌA TRUYỆN ĐẦY ĐỦ VÀ CHÍNH XÁC
    cover_image_data = None
    cover_image_ext = "jpg"
    try:
        target_img_soup = home_soup if home_soup else soup
        if target_img_soup:
            # Quét tìm thẻ chứa ảnh bìa theo nhiều cấu trúc phổ biến của trang truyện
            img_tag = (
                target_img_soup.select_one(".book img") or 
                target_img_soup.select_one(".info img") or 
                target_img_soup.select_one("div.book img") or
                target_img_soup.select_one(".truyen-info img") or
                target_img_soup.find("meta", property="og:image")
            )
            img_url = ""
            if img_tag:
                if img_tag.name == "meta":
                    img_url = img_tag.get("content", "")
                else:
                    img_url = img_tag.get("data-lazy-src") or img_tag.get("data-src") or img_tag.get("src", "")
            
            if img_url:
                full_img_url = urljoin(home_url, img_url)
                scraper = get_scraper()
                img_res = scraper.get(full_img_url, timeout=15)
                if img_res.status_code == 200 and len(img_res.content) > 1000:
                    cover_image_data = img_res.content
                    if "png" in full_img_url.lower():
                        cover_image_ext = "png"
    except Exception as e:
        print(f"Không lấy được ảnh bìa: {e}")

    links = []
    current_url = start_url
    visited_urls = set()
    
    await status.edit_text(f"📚 {title}\n⚡ Đang tự động cào lần lượt từng chương...")
    
    # Vòng lặp cào tuần tự qua từng nút "Tiếp >" cho đến hết
    while current_url and current_url not in visited_urls:
        visited_urls.add(current_url)
        chapter_index = len(links) + 1
        chap_name = f"Chương {chapter_index}"
        
        content, next_url = download_chap(current_url)
        if not content:
            break
            
        links.append({"url": current_url, "name": chap_name, "content": content})
        
        if len(links) % 3 == 0:
            await status.edit_text(f"📚 {title}\n⚡ Đã thu thập được {len(links)} chương...")
        
        if not next_url or next_url in visited_urls:
            break
            
        current_url = next_url

    if not links:
        await status.edit_text("❌ Không tìm thấy nội dung chương nào.")
        return
        
    total_chapters = len(links)
    await status.edit_text(f"📚 {title}\n⚡ Đã xong tổng cộng {total_chapters} chương. Đang đóng gói EPUB...")

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
            caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {total_chapters} chương (Có ảnh bìa)!"
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

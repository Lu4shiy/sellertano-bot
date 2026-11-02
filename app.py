import os
import logging
import threading
import re
import json
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from curl_cffi import requests as c_requests
import cloudscraper
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
import requests
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Flask ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Sellertano Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False)

# --- Google Sheets (без изменений) ---
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
CREDS_FILE = '/etc/secrets/credentials.json'
HEADERS = ["Дата", "Ссылка", "Маркетплейс", "Артикул", "Бренд", "Название", "Текущая цена", "Базовая/Средняя цена", "Выгода (%)", "Продавец", "Рейтинг", "Отзывы", "Риск подделки", "Статус"]

def get_sheet():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open('Sellertano DB').sheet1

def ensure_headers(sheet):
    try:
        first_row = sheet.row_values(1)
        if first_row[:len(HEADERS)] != HEADERS:
            sheet.update('A1', [HEADERS])
    except Exception as e:
        logging.warning(f"Заголовки: {e}")

def save_to_sheet(data: Dict[str, Any]) -> bool:
    try:
        sheet = get_sheet()
        ensure_headers(sheet)
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            date_str, data.get("link", "—"), data.get("marketplace", "—"), str(data.get("sku", "—")),
            data.get("brand", "—"), data.get("name", "—"), data.get("price", "—"), data.get("old_price", "—"),
            f"{data.get('discount_percent', 0)}%", data.get("seller", "—"), data.get("rating", "—"),
            data.get("reviews_count", "—"), data.get("risk_level", "—"), "Проверено"
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        logging.error(f"Sheets: {e}")
        return False

# --- Новые функции обхода ---

def fetch_with_fallback(url: str, headers: dict) -> Optional[str]:
    """
    Гибридный подход: curl_cffi -> cloudscraper -> None.
    Возвращает HTML или None.
    """
    # Уровень 1: curl_cffi (быстрый, идеальный TLS)
    try:
        resp = c_requests.get(url, headers=headers, impersonate="chrome124", timeout=20)
        if resp.status_code == 200:
            logging.info(f"curl_cffi успех: {url}")
            return resp.text
        logging.warning(f"curl_cffi статус {resp.status_code} для {url}")
    except Exception as e:
        logging.warning(f"curl_cffi ошибка: {e}")

    # Уровень 2: cloudscraper (JS-челленджи)
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        resp = scraper.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            logging.info(f"cloudscraper успех: {url}")
            return resp.text
        logging.warning(f"cloudscraper статус {resp.status_code} для {url}")
    except Exception as e:
        logging.warning(f"cloudscraper ошибка: {e}")

    return None

def fetch_with_playwright(url: str) -> Optional[str]:
    """
    Тяжёлый fallback: полноценный браузер Chromium со скрытием следов.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                ]
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                locale='ru-RU',
                timezone_id='Europe/Moscow',
            )
            page = context.new_page()
            stealth_sync(page)
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)  # Даём JS время выполниться
            html = page.content()
            browser.close()
            logging.info(f"playwright-stealth успех: {url}")
            return html
    except Exception as e:
        logging.error(f"playwright ошибка: {e}")
        return None

def fetch_page_smart(url: str, headers: dict = None) -> Optional[str]:
    """
    Умный выбор: для Ozon сразу пробуем гибрид, если не вышло — браузер.
    Для WB достаточно curl_cffi.
    """
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    html = fetch_with_fallback(url, headers)
    if html:
        return html

    # Если это Ozon и гибрид не справился — запускаем браузер
    if "ozon.ru" in url:
        logging.info("Гибрид не справился, запускаем Playwright...")
        return fetch_with_playwright(url)

    return None

# --- Парсер Wildberries (исправленный v4, с fallback) ---
def analyze_wb_product(sku: str, url: str) -> Optional[Dict[str, Any]]:
    try:
        api_url = f"https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={sku}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        # Для WB обычно достаточно curl_cffi
        resp = c_requests.get(api_url, headers=headers, impersonate="chrome124", timeout=15)
        
        if resp.status_code != 200:
            logging.warning(f"WB API статус: {resp.status_code}")
            return None

        data = resp.json()
        products = data.get("data", {}).get("products", [])
        if not products:
            return None

        p = products[0]
        name = p.get("name") or "Без названия"
        brand = p.get("brand") or "Не указан"
        seller = p.get("supplier") or "Сторонний продавец"
        rating = p.get("reviewRating") or 0.0
        reviews_count = p.get("feedbacks") or 0

        price_kopecks = 0
        old_price_kopecks = 0
        sizes = p.get("sizes") or []
        if sizes:
            price_info = sizes[0].get("price") or {}
            price_kopecks = price_info.get("product") or price_info.get("total") or 0
            old_price_kopecks = price_info.get("basic") or 0

        if price_kopecks == 0:
            price_kopecks = p.get("salePriceU") or p.get("priceU") or 0
            old_price_kopecks = p.get("priceU") or 0

        price = round(price_kopecks / 100) if price_kopecks else 0
        old_price = round(old_price_kopecks / 100) if old_price_kopecks > price_kopecks else price

        if price == 0:
            return None

        discount_percent = 0
        if old_price > price:
            discount_percent = round(((old_price - price) / old_price) * 100)

        risk_level, risk_reason = calculate_replica_risk(
            brand=brand, seller=seller, rating=float(rating), reviews_count=int(reviews_count), discount_percent=discount_percent
        )

        return {
            "link": url, "marketplace": "Wildberries", "sku": sku,
            "name": name, "brand": brand, "seller": seller,
            "price": price, "old_price": old_price,
            "discount_percent": discount_percent,
            "rating": rating, "reviews_count": reviews_count,
            "risk_level": risk_level, "risk_reason": risk_reason,
            "delivery_time": "1-3 дня"
        }
    except Exception as e:
        logging.error(f"Ошибка WB: {e}")
        return None

# --- Парсер Ozon (с умным fallback) ---
def analyze_ozon_product(sku: str, url: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        clean_url = f"https://www.ozon.ru/product/{sku}/"
        logging.info(f"Загрузка Ozon: {clean_url}")

        html = fetch_page_smart(clean_url)
        if not html:
            logging.error("Ozon: не удалось загрузить страницу ни одним методом.")
            return None

        soup = BeautifulSoup(html, 'html.parser')
        name = None
        price = 0
        old_price = 0
        rating = 0.0
        reviews_count = 0
        brand = "Не указан"
        seller = "Ozon"

        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict) or item.get("@type") != "Product":
                        continue
                    if item.get("name"):
                        name = item["name"]
                    b = item.get("brand")
                    if isinstance(b, dict):
                        brand = b.get("name") or brand
                    elif isinstance(b, str):
                        brand = b

                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    if isinstance(offers, dict):
                        p_val = offers.get("price") or offers.get("lowPrice")
                        if p_val:
                            price = int(float(p_val))
                        ps = offers.get("priceSpecification")
                        specs = ps if isinstance(ps, list) else [ps] if isinstance(ps, dict) else []
                        for spec in specs:
                            if isinstance(spec, dict) and spec.get("priceType") in ("ListPrice", "StrikethroughPrice"):
                                try:
                                    old_price = int(float(spec.get("price", 0)))
                                except (TypeError, ValueError):
                                    pass

                    agg = item.get("aggregateRating", {})
                    if isinstance(agg, dict):
                        try:
                            rating = float(agg.get("ratingValue") or 0)
                        except (TypeError, ValueError):
                            rating = 0.0
                        try:
                            reviews_count = int(agg.get("reviewCount") or 0)
                        except (TypeError, ValueError):
                            reviews_count = 0
            except Exception:
                continue

        if not name:
            og = soup.find('meta', property='og:title')
            if og and og.get('content'):
                name = og['content']

        if price == 0:
            m = re.search(r'"price"\s*:\s*"?(\d+)"?', html)
            if m:
                price = int(m.group(1))

        if old_price == 0:
            m = re.search(r'"(?:originalPrice|oldPrice)"\s*:\s*"?(\d+)"?', html)
            if m:
                old_price = int(m.group(1))

        if old_price < price:
            old_price = price

        if not name or price == 0:
            logging.warning("Ozon: не удалось извлечь название или цену.")
            return None

        discount_percent = 0
        if old_price > price:
            discount_percent = round(((old_price - price) / old_price) * 100)

        risk_level, risk_reason = calculate_replica_risk(
            brand=brand, seller=seller, rating=rating, reviews_count=reviews_count, discount_percent=discount_percent
        )

        return {
            "link": url, "marketplace": "Ozon", "sku": sku,
            "name": name, "brand": brand, "seller": seller,
            "price": price, "old_price": old_price,
            "discount_percent": discount_percent,
            "rating": rating, "reviews_count": reviews_count,
            "risk_level": risk_level, "risk_reason": risk_reason,
            "delivery_time": "По данным площадки"
        }
    except Exception as e:
        logging.error(f"Ошибка Ozon: {e}")
        return None

def calculate_replica_risk(brand: str, seller: str, rating: float, reviews_count: int, discount_percent: int) -> tuple[str, str]:
    score = 0
    reasons = []
    if brand and seller and (brand.lower() in seller.lower() or seller.lower() in ["wildberries", "ozon"]):
        score -= 2
        reasons.append("Официальный продавец/маркетплейс")
    if rating < 4.0 and reviews_count > 10:
        score += 3
        reasons.append("Низкий рейтинг (< 4.0)")
    elif rating >= 4.7:
        score -= 1
    if reviews_count < 5:
        score += 2
        reasons.append("Мало отзывов")
    if discount_percent > 70:
        score += 2
        reasons.append("Скидка > 70%")

    if score >= 3:
        return "🔴 Высокий (риск пали!)", " | ".join(reasons) or "Совокупность факторов"
    elif score >= 1:
        return "🟡 Средний", " | ".join(reasons) or "Недостаточно проверен"
    else:
        return "🟢 Низкий", "Продавец и отзывы внушают доверие"

# --- Telegram (без изменений) ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Привет! Я Sellertano — Анализатор Рынка.**\n\n📥 **Отправь ссылку на товар Wildberries или Ozon!**", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("http"):
        await update.message.reply_text("❌ Нужна прямая ссылка (http:// или https://).")
        return

    sku, mp, path = extract_product_info_from_url(text)
    if not sku:
        await update.message.reply_text("⚠️ Не удалось распознать ссылку.")
        return

    await update.message.reply_text(f"🔎 Анализирую {mp}...")

    if mp == "Wildberries":
        data = await asyncio.to_thread(analyze_wb_product, sku, text)
    else:
        data = await asyncio.to_thread(analyze_ozon_product, sku, text, path)

    if not data:
        await update.message.reply_text("❌ Данные не получены. Площадка блокирует запросы.")
        return

    await asyncio.to_thread(save_to_sheet, data)

    report = (
        f"📊 **ОТЧЕТ SELLERTANO**\n━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **{data['name']}**\n🏷 Бренд: {data['brand']} | 🏪 {data['seller']}\n\n"
        f"💰 Цена: **{data['price']} ₽**\n🏷 Базовая: {data['old_price']} ₽\n"
        f"🔥 Выгода: **{data['discount_percent']}%** (экономия {max(0, data['old_price'] - data['price'])} ₽)\n\n"
        f"⭐ Рейтинг: {data['rating']} / 5.0 ({data['reviews_count']} отзывов)\n"
        f"🛡 Риск подделки: **{data['risk_level']}**\n💬 _{data['risk_reason']}_\n"
        f"━━━━━━━━━━━━━━━━━━━\n✅ Сохранено в базу."
    )
    await update.message.reply_text(report, parse_mode="Markdown")

async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    TOKEN = os.environ.get('BOT_TOKEN')
    if not TOKEN:
        print("Ошибка: BOT_TOKEN не найден!")
        return

    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Sellertano Analyzer с обходом запущен!")
    application.run_polling()

if __name__ == '__main__':
    main()
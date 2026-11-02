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
from bs4 import BeautifulSoup
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

# --- Google Sheets ---
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
CREDS_FILE = '/etc/secrets/credentials.json'

HEADERS = [
    "Дата", "Ссылка", "Маркетплейс", "Артикул", "Бренд",
    "Название", "Текущая цена", "Базовая/Средняя цена",
    "Выгода (%)", "Продавец", "Рейтинг", "Отзывы", "Риск подделки", "Статус"
]

def get_sheet():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open('Sellertano DB').sheet1

def ensure_headers(sheet):
    try:
        first_row = sheet.row_values(1)
        if first_row[:len(HEADERS)] != HEADERS:
            sheet.update('A1', [HEADERS])
            logging.info("Заголовки таблицы обновлены.")
    except Exception as e:
        logging.warning(f"Не удалось проверить/обновить заголовки: {e}")

def save_to_sheet(analysis_data: Dict[str, Any]) -> bool:
    try:
        sheet = get_sheet()
        ensure_headers(sheet)
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            date_str,
            analysis_data.get("link", "—"),
            analysis_data.get("marketplace", "—"),
            str(analysis_data.get("sku", "—")),
            analysis_data.get("brand", "—"),
            analysis_data.get("name", "—"),
            analysis_data.get("price", "—"),
            analysis_data.get("old_price", "—"),
            f"{analysis_data.get('discount_percent', 0)}%",
            analysis_data.get("seller", "—"),
            analysis_data.get("rating", "—"),
            analysis_data.get("reviews_count", "—"),
            analysis_data.get("risk_level", "—"),
            "Проверено"
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        logging.error(f"Ошибка Google Sheets: {e}")
        return False

# --- Разрешение коротких ссылок ---

def resolve_short_url(url: str) -> str:
    if any(domain in url for domain in ["ozon.ru/t/", "wb.ru/", "clck.ru/", "ozon.onelink.me"]):
        try:
            resp = c_requests.get(url, impersonate="chrome120", allow_redirects=True, timeout=15)
            final = resp.url
            if final and final != url:
                return final
        except Exception as e:
            logging.error(f"Ошибка редиректа короткой ссылки: {e}")
    return url

def extract_product_info_from_url(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    resolved_url = resolve_short_url(url)
    logging.info(f"Финальный URL после редиректа: {resolved_url}")

    parsed = urlparse(resolved_url)
    path = parsed.path

    if "wildberries.ru" in resolved_url or "wb.ru" in resolved_url:
        match = re.search(r'/catalog/(\d+)/detail', resolved_url)
        if match:
            return match.group(1), "Wildberries", None
    elif "ozon.ru" in resolved_url:
        match = re.search(r'-(\d+)(?:[/?]|$)', path)
        if not match:
            match = re.search(r'/product/.*?(\d{6,})', resolved_url)
        sku = match.group(1) if match else None
        return sku, "Ozon", path

    return None, None, None

# --- Wildberries (исправлено под v4) ---

def analyze_wb_product(sku: str, url: str) -> Optional[Dict[str, Any]]:
    try:
        api_url = f"https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={sku}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(api_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            logging.warning(f"WB API статус: {resp.status_code}")
            return None

        data = resp.json()
        products = data.get("data", {}).get("products", [])
        if not products:
            logging.warning(f"WB API: пустой список товаров для SKU {sku}")
            return None

        p = products[0]
        name = p.get("name") or "Без названия"
        brand = p.get("brand") or "Не указан"
        seller = p.get("supplier") or "Сторонний продавец"
        rating = p.get("reviewRating") or 0.0
        reviews_count = p.get("feedbacks") or 0

        # v4: цены в sizes[0].price
        price_kopecks = 0
        old_price_kopecks = 0
        sizes = p.get("sizes") or []
        if sizes:
            price_info = sizes[0].get("price") or {}
            price_kopecks = price_info.get("product") or price_info.get("total") or 0
            old_price_kopecks = price_info.get("basic") or 0

        # Fallback на старый формат
        if price_kopecks == 0:
            price_kopecks = p.get("salePriceU") or p.get("priceU") or 0
            old_price_kopecks = p.get("priceU") or 0

        price = round(price_kopecks / 100) if price_kopecks else 0
        old_price = round(old_price_kopecks / 100) if old_price_kopecks > price_kopecks else price

        if price == 0:
            logging.warning(f"WB: не удалось определить цену для SKU {sku}")
            return None

        discount_percent = 0
        if old_price > price:
            discount_percent = round(((old_price - price) / old_price) * 100)

        risk_level, risk_reason = calculate_replica_risk(
            brand=brand, seller=seller, rating=float(rating),
            reviews_count=int(reviews_count), discount_percent=discount_percent
        )

        return {
            "link": url,
            "marketplace": "Wildberries",
            "sku": sku,
            "name": name,
            "brand": brand,
            "seller": seller,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "rating": rating,
            "reviews_count": reviews_count,
            "risk_level": risk_level,
            "risk_reason": risk_reason,
            "delivery_time": "1-3 дня"
        }
    except Exception as e:
        logging.error(f"Ошибка парсинга WB: {e}")
        return None

# --- Ozon (исправлено: реальная старая цена) ---

def analyze_ozon_product(sku: str, url: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        clean_url = f"https://www.ozon.ru/product/{sku}/"
        logging.info(f"Загрузка страницы Ozon: {clean_url}")

        resp = c_requests.get(clean_url, impersonate="chrome120", timeout=20)
        logging.info(f"Ozon ответ: статус {resp.status_code}")

        if resp.status_code != 200:
            logging.warning(f"Ozon заблокировал. Статус: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

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
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") != "Product":
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
                            if not isinstance(spec, dict):
                                continue
                            if spec.get("priceType") in ("ListPrice", "StrikethroughPrice"):
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
            except Exception as e:
                logging.debug(f"JSON-LD parse: {e}")

        if not name:
            og = soup.find('meta', property='og:title')
            if og and og.get('content'):
                name = og['content']

        if price == 0:
            m = re.search(r'"price"\s*:\s*"?(\d+)"?', resp.text)
            if m:
                price = int(m.group(1))

        if old_price == 0:
            m = re.search(r'"(?:originalPrice|oldPrice)"\s*:\s*"?(\d+)"?', resp.text)
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
            brand=brand, seller=seller, rating=rating,
            reviews_count=reviews_count, discount_percent=discount_percent
        )

        return {
            "link": url,
            "marketplace": "Ozon",
            "sku": sku,
            "name": name,
            "brand": brand,
            "seller": seller,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "rating": rating,
            "reviews_count": reviews_count,
            "risk_level": risk_level,
            "risk_reason": risk_reason,
            "delivery_time": "По данным площадки"
        }
    except Exception as e:
        logging.error(f"Ошибка парсинга Ozon: {e}")
        return None

def calculate_replica_risk(brand: str, seller: str, rating: float, reviews_count: int, discount_percent: int) -> tuple[str, str]:
    score = 0
    reasons = []

    if brand and seller and (brand.lower() in seller.lower() or seller.lower() in ["wildberries", "ozon"]):
        score -= 2
        reasons.append("Официальный продавец/маркетплейс")

    if rating < 4.0 and reviews_count > 10:
        score += 3
        reasons.append("Низкий рейтинг товара (< 4.0)")
    elif rating >= 4.7:
        score -= 1

    if reviews_count < 5:
        score += 2
        reasons.append("Мало отзывов")

    if discount_percent > 70:
        score += 2
        reasons.append(f"Аномально большая скидка ({discount_percent}%)")

    if score >= 3:
        return "🔴 Высокий (Есть риск пали!)", " | ".join(reasons) or "Совокупность негативных факторов"
    elif score >= 1:
        return "🟡 Средний (Требуется внимание)", " | ".join(reasons) or "Недостаточно проверен"
    else:
        return "🟢 Низкий (Надёжное предложение)", "Продавец и отзывы внушают доверие"

# --- Telegram ---

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Привет! Я Sellertano — твой умный Анализатор Рынка.**\n\n"
        "🔍 **Что я умею:**\n"
        "• Анализировать товары на Wildberries и Ozon.\n"
        "• Считать выгоду и реальную скидку.\n"
        "• Оценивать **риск подделки (пали)**.\n"
        "• Сохранять данные в Google Таблицу.\n\n"
        "📥 **Отправь ссылку на любой товар!**",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("http"):
        await update.message.reply_text("Пожалуйста, отправь прямую ссылку на товар (http:// или https://).")
        return

    sku, mp, path = extract_product_info_from_url(text)

    if not sku:
        await update.message.reply_text("⚠️ Не удалось распознать ссылку. Поддерживаются Wildberries и Ozon.")
        return

    await update.message.reply_text(f"🔎 Анализирую товар на {mp}...")

    # Выносим блокирующие парсеры в отдельный поток
    if mp == "Wildberries":
        data = await asyncio.to_thread(analyze_wb_product, sku, text)
    else:
        data = await asyncio.to_thread(analyze_ozon_product, sku, text, path)

    if not data:
        await update.message.reply_text("❌ Не удалось получить данные. Возможно, товар недоступен или площадка блокирует запросы.")
        return

    # Сохранение в таблицу тоже блокирующее — тоже в поток
    await asyncio.to_thread(save_to_sheet, data)

    report = (
        f"📊 **ОТЧЕТ SELLERTANO**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Товар:** {data['name']}\n"
        f"🏷 **Бренд:** {data['brand']}\n"
        f"🏪 **Продавец:** {data['seller']}\n\n"
        f"💰 **Текущая цена:** {data['price']} ₽\n"
        f"🏷 **Базовая цена:** {data['old_price']} ₽\n"
        f"🔥 **Выгода:** {data['discount_percent']}%"
        f" (Экономия: {max(0, data['old_price'] - data['price'])} ₽)\n\n"
        f"⭐ **Рейтинг:** {data['rating']} / 5.0 ({data['reviews_count']} отзывов)\n"
        f"🚚 **Доставка:** ~{data['delivery_time']}\n"
        f"🛡 **Риск подделки:** {data['risk_level']}\n"
        f"💬 _Детали:_ {data['risk_reason']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ _Данные сохранены в Sellertano DB._"
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

    print("Sellertano Market Analyzer запущен!")
    application.run_polling()

if __name__ == '__main__':
    main()
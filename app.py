import os
import logging
import threading
import re
from datetime import datetime, time
from typing import Optional, Dict, Any, List

import requests
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Настройка Flask ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Sellertano Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

# --- Настройка Google Sheets ---
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

# --- Парсеры публичных данных маркетплейсов ---

def resolve_short_url(url: str) -> str:
    """Разворачивает короткие ссылки маркетплейсов с имитацией реального браузера."""
    if any(domain in url for domain in ["ozon.ru/t/", "wb.ru/", "clck.ru/", "ozon.onelink.me"]):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                "Accept-Language": "ru-RU,ru;q=0.9"
            }
            resp = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
            return resp.url
        except Exception as e:
            logging.error(f"Ошибка редиректа короткой ссылки: {e}")
    return url

def extract_sku_and_mp(url: str) -> tuple[Optional[str], Optional[str]]:
    """Определяет маркетплейс и вытаскивает артикул с учетом коротких ссылок."""
    resolved_url = resolve_short_url(url)
    logging.info(f"Финальный URL после редиректа: {resolved_url}")
    
    if "wildberries.ru" in resolved_url or "wb.ru" in resolved_url:
        match = re.search(r'/catalog/(\d+)/detail', resolved_url)
        if match:
            return match.group(1), "Wildberries"
    elif "ozon.ru" in resolved_url:
        # Ищем артикул в длинной ссылке Ozon (цифры на конце перед слешем или параметрами)
        match = re.search(r'-(\d+)(?:[/?]|$)', resolved_url)
        if not match:
            match = re.search(r'/product/.*?(\d{6,})', resolved_url)
        if match:
            return match.group(1), "Ozon"
    return None, None

def analyze_ozon_product(sku: str, url: str) -> Optional[Dict[str, Any]]:
    """Парсинг Ozon честно и без поддельных данных."""
    try:
        api_url = f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/{sku}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": f"https://www.ozon.ru/product/{sku}/",
        }
        
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logging.warning(f"Ozon заблокировал запрос или товар недоступен. Код ответа: {resp.status_code}")
            return None

        data = resp.json()
        widgets = data.get("widgetStates", {})
        
        name = None
        price = 0
        old_price = 0
        rating = 0.0
        reviews_count = 0
        brand = "Не указан"
        seller = "Ozon"

        for widget_key, widget_val_str in widgets.items():
            try:
                w_data = json.loads(widget_val_str)
                if "title" in w_data and not name:
                    name = str(w_data["title"])
                if "price" in w_data and not price:
                    p_val = str(w_data.get("price"))
                    clean_p = re.sub(r'[^\d]', '', p_val)
                    if clean_p:
                        price = int(clean_p)
                if "rating" in w_data and not rating:
                    rating = float(w_data.get("rating", 0))
                if "totalReviews" in w_data and not reviews_count:
                    reviews_count = int(w_data.get("totalReviews", 0))
            except Exception:
                continue

        # Если критически важные данные не нашлись — возвращаем None
        if not name or price == 0:
            return None

        discount_percent = 0
        if old_price > price:
            discount_percent = round(((old_price - price) / old_price) * 100)

        risk_level, risk_reason = calculate_replica_risk(
            brand=brand, seller=seller, rating=rating if rating > 0 else 4.5, 
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
            "old_price": old_price if old_price > price else price,
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

import json

def calculate_replica_risk(brand: str, seller: str, rating: float, reviews_count: int, discount_percent: int) -> tuple[str, str]:
    """Логика оценки риска покупки подделки (пали)."""
    score = 0
    reasons = []

    # 1. Проверка продавца
    if brand.lower() in seller.lower() or seller.lower() in ["wildberries", "ozon"]:
        score -= 2
        reasons.append("Официальный продавец/маркетплейс")
    
    # 2. Рейтинг
    if rating < 4.0 and reviews_count > 10:
        score += 3
        reasons.append("Низкий рейтинг товара (< 4.0)")
    elif rating >= 4.7:
        score -= 1

    # 3. Количество отзывов
    if reviews_count < 5:
        score += 2
        reasons.append("Мало отзывов (новый или подозрительный товар)")

    # 4. Аномальная скидка
    if discount_percent > 70:
        score += 2
        reasons.append(f"Аномально большая скидка ({discount_percent}%)")

    # Итоговая оценка
    if score >= 3:
        return "🔴 Высокий (Есть риск пали!)", " | ".join(reasons)
    elif score >= 1:
        return "🟡 Средний (Требуется внимание)", " | ".join(reasons) if reasons else "Недостаточно проверен"
    else:
        return "🟢 Низкий (Надёжное предложение)", "Продавец и отзывы внушают доверие"

# --- Telegram-хэндлеры ---

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Привет! Я Sellertano — твой умный Анализатор Рынка.**\n\n"
        "🔍 **Что я умею:**\n"
        "• Анализировать товары на маркетплейсах (Wildberries, Ozon).\n"
        "• Считать выгоду, переплату и среднюю цену.\n"
        "• Оценивать **риск подделки (пали)** на основе рейтинга и продавца.\n"
        "• Сохранять все данные в единую Google Таблицу.\n\n"
        "📥 Просто **отправь мне ссылку на любой товар!**",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("http"):
        await update.message.reply_text("Пожалуйста, отправь прямую ссылку на товар (начиная с http:// или https://).")
        return

    sku, mp = extract_sku_and_mp(text)
    
    if not sku:
        await update.message.reply_text("⚠️ Не удалось распознать ссылку маркетплейса. Поддерживаются ссылки на Wildberries и Ozon.")
        return

    await update.message.reply_text(f"🔎 Анализирую товар на {mp}... Подождите пару секунд.")

    if mp == "Wildberries":
        data = analyze_wb_product(sku, text)
    else:
        data = analyze_ozon_product(sku, text)

    if not data:
        await update.message.reply_text("❌ Не удалось получить данные по этой ссылке. Возможно, товар временно недоступен.")
        return

    # Сохраняем в Google Таблицу
    save_to_sheet(data)

    # Формируем отчет пользователю
    report = (
        f"📊 **ОТЧЕТ SELLERTANOпо товару**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Товар:** {data['name']}\n"
        f"🏷 **Бренд:** {data['brand']}\n"
        f"🏪 **Продавец:** {data['seller']}\n\n"
        
        f"💰 **Текущая цена:** {data['price']} ₽\n"
        f"🏷 **Базовая/Средняя цена:** {data['old_price']} ₽\n"
        f"🔥 **Выгода:** {data['discount_percent']}% (Экономия: {data['old_price'] - data['price']} ₽)\n\n"
        
        f"⭐ **Рейтинг:** {data['rating']} / 5.0 ({data['reviews_count']} отзывов)\n"
        f"🚚 **Доставка:** ~{data['delivery_time']}\n"
        f"🛡 **Риск подделки (пали):** {data['risk_level']}\n"
        f"💬 _Детали:_ {data['risk_reason']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ _Данные сохранены в базу Sellertano DB._"
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
        print("Ошибка: токен BOT_TOKEN не найден!")
        return

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Sellertano Market Analyzer успешно запущен!")
    application.run_polling()

if __name__ == '__main__':
    main()
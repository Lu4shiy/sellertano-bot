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

HEADERS = ["Дата", "Ссылка", "Маркетплейс", "Артикул", "Название", "Цена", "Остаток", "Статус"]

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

def save_to_sheet(link: str, sku: Optional[str] = None, product_info: Optional[Dict[str, Any]] = None) -> bool:
    try:
        sheet = get_sheet()
        ensure_headers(sheet)
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        marketplace = "Ozon" if sku else "—"
        sku_val = sku or "—"
        if product_info:
            name = product_info.get("name", "—")
            price = product_info.get("price", "—")
            stock = product_info.get("stock", "—")
        else:
            name = price = stock = "—"
        row = [date_str, link, marketplace, sku_val, name, price, stock, "Ожидает проверки"]
        sheet.append_row(row)
        return True
    except Exception as e:
        logging.error(f"Ошибка Google Sheets: {e}")
        return False

# --- Ozon API: вспомогательные функции ---

def extract_ozon_sku(link: str) -> Optional[str]:
    pattern = re.compile(r'/product/(?:[^/]+-)?(\d+)')
    match = pattern.search(link)
    return match.group(1) if match else None

def _get_ozon_headers() -> Optional[Dict[str, str]]:
    client_id = os.environ.get("OZON_CLIENT_ID")
    api_key = os.environ.get("OZON_API_KEY")
    if not client_id or not api_key:
        logging.warning("Ozon API: переменные OZON_CLIENT_ID и/или OZON_API_KEY не заданы.")
        return None
    return {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json"
    }

def fetch_ozon_product_info(sku: str) -> Optional[Dict[str, Any]]:
    """Информация о конкретном товаре (название, цена, остаток)."""
    headers = _get_ozon_headers()
    if not headers:
        return None
    try:
        product_id_int = int(sku)
    except ValueError:
        return None
    url = "https://api-seller.ozon.ru/v3/product/info/list"
    payload = {"product_id": [product_id_int]}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Ozon API (info): ошибка запроса — {e}")
        return None
    items = data.get("result", [])
    if not items:
        return None
    item = items[0]
    stock_list = item.get("stock", [])
    total_available = sum(s.get("available", 0) for s in stock_list)
    return {
        "name": item.get("name", "Без названия"),
        "price": item.get("price", "—"),
        "stock": total_available
    }

# --- НОВОЕ: Получение всех товаров и остатков для мониторинга ---

def fetch_all_products_with_stocks() -> Optional[List[Dict[str, Any]]]:
    """
    Получает список всех товаров продавца и их остатки.
    Использует актуальный эндпоинт /v4/product/info/stocks.
    """
    headers = _get_ozon_headers()
    if not headers:
        return None

    # Сначала получаем список product_id всех товаров
    url_list = "https://api-seller.ozon.ru/v3/product/list"
    payload_list = {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000}
    try:
        resp = requests.post(url_list, headers=headers, json=payload_list, timeout=15)
        resp.raise_for_status()
        data_list = resp.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Ozon API (list): ошибка запроса — {e}")
        return None

    items = data_list.get("result", {}).get("items", [])
    if not items:
        logging.info("Ozon API: список товаров пуст.")
        return []

    product_ids = [item["product_id"] for item in items if "product_id" in item]

    # Теперь запрашиваем остатки пачками (если товаров много)
    url_stocks = "https://api-seller.ozon.ru/v4/product/info/stocks"
    all_products = []

    for i in range(0, len(product_ids), 100):
        chunk = product_ids[i:i+100]
        payload_stocks = {"filter": {"product_id": chunk}, "limit": 100}
        try:
            resp = requests.post(url_stocks, headers=headers, json=payload_stocks, timeout=15)
            resp.raise_for_status()
            data_stocks = resp.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Ozon API (stocks): ошибка запроса — {e}")
            continue

        for item in data_stocks.get("result", {}).get("items", []):
            sku = item.get("product_id")
            if sku is None:
                continue
            # Суммируем доступные остатки по всем складам
            stock_entries = item.get("stocks", [])
            total_available = sum(s.get("present", 0) for s in stock_entries)
            all_products.append({
                "sku": sku,
                "name": item.get("name", f"Товар {sku}"),
                "stock": total_available
            })

    return all_products

# --- НОВОЕ: Расчёт скорости продаж и прогноза ---

def fetch_sales_velocity(product_id: int, days: int = 14) -> Optional[float]:
    """
    Возвращает среднюю скорость продаж (шт/день) за последние `days` дней.
    Использует метод аналитики /v1/analytics/data.
    """
    headers = _get_ozon_headers()
    if not headers:
        return None

    url = "https://api-seller.ozon.ru/v1/analytics/data"
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")

    payload = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": ["ordered_units"],
        "dimension": ["sku"],
        "filters": [{"key": "sku", "value": str(product_id)}],
        "limit": 1
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Ozon API (analytics): ошибка запроса — {e}")
        return None

    # Извлекаем количество заказанных единиц
    result = data.get("result", {})
    data_rows = result.get("data", [])
    if not data_rows:
        return 0.0

    ordered_units = 0
    for row in data_rows:
        # Структура ответа может варьироваться; ищем метрику ordered_units
        metrics = row.get("metrics", [])
        if metrics:
            ordered_units = metrics[0]  # первая метрика в списке

    return ordered_units / days if days > 0 else 0.0

# --- НОВОЕ: Задача для планировщика (ежедневная проверка) ---

async def check_stock_and_notify(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежедневная задача:
    1. Получает все товары и остатки.
    2. Для каждого товара считает скорость продаж и прогноз.
    3. Отправляет уведомление в Telegram, если осталось мало дней.
    """
    chat_id = context.job.chat_id if context.job else None
    if not chat_id:
        # Если chat_id не задан, используем переменную окружения
        chat_id = os.environ.get("SELLER_CHAT_ID")
    if not chat_id:
        logging.warning("Не задан chat_id для уведомлений. Установи SELLER_CHAT_ID в Render.")
        return

    products = fetch_all_products_with_stocks()
    if not products:
        logging.info("Нет товаров для проверки или ошибка API.")
        return

    THRESHOLD_DAYS = 7  # Порог: уведомлять, если осталось меньше 7 дней
    alerts = []

    for product in products:
        sku = product["sku"]
        stock = product["stock"]
        name = product["name"]

        if stock <= 0:
            alerts.append(f"🔴 **{name}** (SKU: {sku}) — остаток 0. Продажи остановлены.")
            continue

        velocity = fetch_sales_velocity(sku, days=14)
        if velocity is None or velocity <= 0:
            # Если данных о продажах нет или товар не продаётся, пропускаем прогноз
            continue

        days_left = stock / velocity
        if days_left < THRESHOLD_DAYS:
            alerts.append(
                f"🟡 **{name}** (SKU: {sku})\n"
                f"   Остаток: {stock} шт. | Скорость: ~{velocity:.1f} шт/день\n"
                f"   📉 Прогноз: закончится через **{days_left:.1f} дней**"
            )

    if alerts:
        message = "⚠️ **Внимание! Товары с низким остатком:**\n\n" + "\n\n".join(alerts)
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
            logging.info(f"Отправлено уведомление по {len(alerts)} товарам.")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление: {e}")
    else:
        logging.info("Все товары в норме, уведомлений нет.")

# --- Telegram-хэндлеры ---

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Привет! Я Sellertano.\n'
        'Отправь ссылку на товар Ozon, и я запишу его в базу.\n'
        'А раз в день я буду проверять остатки и присылать прогноз, если товар заканчивается.'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "http" not in text:
        await update.message.reply_text('Пожалуйста, отправь ссылку (начиная с http).')
        return

    sku = extract_ozon_sku(text)
    product_info = fetch_ozon_product_info(sku) if sku else None

    success = save_to_sheet(text, sku=sku, product_info=product_info)

    if success:
        if product_info:
            reply_text = (
                f"✅ Принял ссылку: {text}\n"
                f"📦 Товар: {product_info['name']}\n"
                f"💰 Цена: {product_info['price']} ₽\n"
                f"📊 Остаток: {product_info['stock']} шт.\n"
                f"Данные записаны в базу."
            )
        elif sku:
            reply_text = (
                f"✅ Принял ссылку: {text}\n"
                f"🔍 Артикул Ozon: {sku}\n"
                f"⚠️ API Ozon не вернул данные (товар не принадлежит подключённому продавцу или ключи не настроены).\n"
                f"Ссылка сохранена в базу для ручной проверки."
            )
        else:
            reply_text = f"✅ Принял ссылку: {text}\nУспешно записал в базу (артикул не распознан)."
        await update.message.reply_text(reply_text)
    else:
        await update.message.reply_text('Ой, не смог записать в базу. Проверь логи.')

async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    TOKEN = os.environ.get('BOT_TOKEN')
    if not TOKEN:
        print("Ошибка: токен не найден!")
        return

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- НОВОЕ: Запуск ежедневной проверки остатков ---
    # Время запуска: 09:00 утра. Можно изменить.
    application.job_queue.run_daily(
        check_stock_and_notify,
        time=time(hour=20, minute=9, second=0),
        name="daily_stock_check"
    )
    print("Планировщик ежедневных уведомлений об остатках запущен (09:00).")

    print("Бот запускается с Ozon API и умными уведомлениями...")
    application.run_polling()

if __name__ == '__main__':
    main()
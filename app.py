import os
import logging
import threading
from datetime import datetime
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
# Файл, который мы загрузим на Render
CREDS_FILE = '/etc/secrets/credentials.json'

def save_to_sheet(link):
    try:
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        # Открываем таблицу по названию
        sheet = client.open('Sellertano DB').sheet1
        # Записываем строку: [Дата, Ссылка, Статус]
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sheet.append_row([date_str, link, "Ожидает проверки"])
        return True
    except Exception as e:
        logging.error(f"Ошибка Google Sheets: {e}")
        return False

# --- Настройка Telegram ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Отправь мне ссылку на товар, и я запишу её в базу.')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "http" not in text:
        await update.message.reply_text('Пожалуйста, отправь ссылку (начиная с http).')
        return

    # Сохраняем в Google Sheets
    success = save_to_sheet(text)
    
    if success:
        await update.message.reply_text(f'Принял ссылку: {text}\nУспешно записал в базу!')
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
    
    print("Бот запускается с базой данных...")
    application.run_polling()

if __name__ == '__main__':
    main()
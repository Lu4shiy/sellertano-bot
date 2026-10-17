import os
import logging
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Настройка Flask (веб-сервер для Render) ---
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

# --- Настройка Telegram-бота ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Отправь мне ссылку на товар, и я сохраню её.')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    with open('links.txt', 'a', encoding='utf-8') as f:
        f.write(text + '\n')
    await update.message.reply_text(f'Принял ссылку: {text}\nСохранил в базу.')

def main():
    # 1. Запускаем Flask в фоновом потоке (чтобы Render видел, что мы живы)
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # 2. Запускаем Telegram-бота в ГЛАВНОМ потоке
    TOKEN = os.environ.get('BOT_TOKEN')
    if not TOKEN:
        print("Ошибка: токен не найден!")
        return
        
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Бот запускается...")
    application.run_polling()

if __name__ == '__main__':
    main()
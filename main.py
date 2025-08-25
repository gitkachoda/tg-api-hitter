import os
import sys
import json
import tempfile
import asyncio
import aiohttp
import random
import threading
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------- Load Environment -------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")   # e.g. https://your-app.onrender.com/webhook
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment")

# ------------------- Logging Setup -------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tg-api-hitter")

def _mask(s: str, keep=6):
    if not s:
        return s
    return s[:keep] + "..." if len(s) > keep else "***"

logger.info("Starting service")
logger.info("Env summary: PORT=%s, LOG_LEVEL=%s, BOT_TOKEN=%s", PORT, LOG_LEVEL, _mask(BOT_TOKEN))

# ------------------- Globals -------------------
BASE_URL = None
FIRST_TIME_USERS = set()
AWAITING_BASEURL = set()
PTB_LOOP = None

# ------------------- Flask App & PTB -------------------
app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)
application = ApplicationBuilder().token(BOT_TOKEN).build()

# ------------------- Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is Active ✅")

application.add_handler(CommandHandler("start", start))

# ------------------- Flask Routes -------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=False)
        update = Update.de_json(payload, bot)
        application.update_queue.put_nowait(update)
        return "ok", 200
    except Exception as e:
        logger.exception("Webhook handler error: %s", e)
        return "error", 500

@app.route("/")
def home():
    return "Bot is running!", 200

@app.route("/healthz")
def health():
    return "ok", 200

# ------------------- PTB Background Start -------------------
async def _ptb_start():
    global PTB_LOOP
    PTB_LOOP = asyncio.get_running_loop()
    await application.initialize()
    await application.start()
    logger.info("PTB Application started (webhook mode). Ready to process updates.")

    # Webhook reset & set once at startup
    try:
        await bot.delete_webhook()
        res = await bot.set_webhook(url=WEBHOOK_URL)
        logger.info("Webhook set response: %s", res)
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)

def run_ptb_bg():
    asyncio.run(_ptb_start())

threading.Thread(target=run_ptb_bg, daemon=True).start()

# ------------------- Main -------------------
if __name__ == "__main__":
    logger.info("Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)

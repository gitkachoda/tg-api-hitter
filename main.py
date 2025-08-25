import os
import sys
import json
import time
import tempfile
import asyncio
import aiohttp
import random
import threading
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =================== Env & Constants ===================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://tg-api-hitter.onrender.com/webhook
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # optional: your Telegram user ID (as string)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment")

WEBHOOK_PATH = "/webhook"   # fixed path
PORT = int(os.getenv("PORT", "8000"))

# =================== Logging ===========================
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
logger.info("Env summary: PORT=%s, LOG_LEVEL=%s, BOT_TOKEN=%s, WEBHOOK_URL=%s",
            PORT, LOG_LEVEL, _mask(BOT_TOKEN), WEBHOOK_URL)

# =================== Globals ===========================
BASE_URL = None
FIRST_TIME_USERS = set()
AWAITING_BASEURL = set()
PTB_LOOP = None  # event loop used by PTB for thread-safe calls

# =================== Flask & PTB =======================
app = Flask(__name__)
# PTB application
application: Application = Application.builder().token(BOT_TOKEN).build()
bot = Bot(token=BOT_TOKEN)

# =================== Utility ==========================
def get_greeting():
    # Use IST explicitly so greeting suits your timezone
    hour = datetime.utcnow().hour + 5.5  # crude IST conversion for greeting
    hour = int(hour % 24)
    if 5 <= hour < 12:
        return "Good Morning 🌞"
    elif 12 <= hour < 16:
        return "Good Afternoon ☀️"
    elif 16 <= hour < 20:
        return "Good Evening 🌇"
    else:
        return "Good Night 🌙"

def schedule_delete(chat_id: int, message_id: int, bot_instance):
    """Delete message after 24h; runs coroutine on PTB loop."""
    def _delete_msg():
        try:
            if PTB_LOOP and not PTB_LOOP.is_closed():
                fut = asyncio.run_coroutine_threadsafe(
                    bot_instance.delete_message(chat_id=chat_id, message_id=message_id),
                    PTB_LOOP
                )
                fut.result(timeout=10)
                logger.info("Scheduled delete executed: chat_id=%s msg_id=%s", chat_id, message_id)
            else:
                logger.warning("PTB_LOOP not available for scheduled delete")
        except Exception as e:
            logger.exception("Scheduled delete failed: %s", e)

    from threading import Timer
    Timer(24 * 60 * 60, _delete_msg).start()
    logger.info("Scheduled delete set for chat_id=%s msg_id=%s in 24h", chat_id, message_id)

def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

# =================== Download Logic ====================
async def download_video_with_progress(msg_processing, url, local_file: str):
    logger.info("Download start: url=%s -> %s", url, local_file)
    retries = 3
    for attempt in range(1, retries + 1):
        downloaded = 0
        last_percent_edit = -1
        last_percent_log = -10  # log every 10%
        try:
            timeout = aiohttp.ClientTimeout(total=3600)
            connector = aiohttp.TCPConnector(limit=0, ssl=False)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(url) as r:
                    r.raise_for_status()
                    total_size = int(r.headers.get("Content-Length", 0))
                    logger.info("HTTP 200 OK. Content-Length=%s", total_size or "unknown")

                    if total_size and total_size > 1_990_000_000:
                        await msg_processing.edit_text("❌ File too large for Telegram (> ~2GB).")
                        raise RuntimeError("File too large for Telegram")

                    loop = asyncio.get_event_loop()
                    next_edit_at = loop.time() + random.uniform(30, 35)
                    with open(local_file, "wb") as f:
                        async for chunk in r.content.iter_chunked(8192):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            percent = int(downloaded / total_size * 100) if total_size > 0 else None
                            now = loop.time()

                            # Throttled message edit
                            if now >= next_edit_at:
                                try:
                                    if total_size > 0 and percent != last_percent_edit:
                                        await msg_processing.edit_text(f"⏳ Downloading... {percent}%")
                                        last_percent_edit = percent
                                    elif total_size == 0:
                                        await msg_processing.edit_text(
                                            f"⏳ Downloading... {human_size(downloaded)}"
                                        )
                                except Exception as e:
                                    logger.debug("Progress edit skip: %s", e)
                                next_edit_at = now + random.uniform(30, 35)

                            # Throttled logs (every 10%)
                            if total_size > 0 and percent is not None and percent >= last_percent_log + 10:
                                logger.info("Download progress: %s%% (%s/%s)",
                                            percent, human_size(downloaded), human_size(total_size))
                                last_percent_log = percent

            await msg_processing.edit_text("⏳ Download complete! 100%")
            logger.info("Download complete: %s", local_file)
            return
        except Exception as e:
            logger.warning("Download attempt %s failed: %s", attempt, e)
            if attempt < retries:
                await asyncio.sleep(3)
                continue
            else:
                await msg_processing.edit_text(f"❌ Video download error: {e}")
                logger.exception("Download failed after retries")
                raise

# =================== Handlers ==========================
async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id if update.effective_user else None
        uname = update.effective_user.username if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        text = update.effective_message.text if update.effective_message else None
        logger.info("Incoming update: id=%s user=%s(@%s) chat=%s text=%r",
                    update.update_id, uid, uname, chat_id, text)
    except Exception:
        logger.exception("Failed to log update")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info("/start by user_id=%s", user_id)
    greeting = get_greeting()
    if user_id not in FIRST_TIME_USERS:
        FIRST_TIME_USERS.add(user_id)
        desc = (f"{greeting}, welcome! 👋\n\nThis bot fetches **Terabox videos**.\n\n"
                "1. Set base URL using /baseurl\n2. Send a Terabox link\n"
                "3. Videos auto-delete after 1 day\nUse /status or /stop")
        await update.message.reply_text(desc)
    else:
        await update.message.reply_text(f"{greeting}, welcome back! Send a Terabox link.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("/status by user_id=%s", update.effective_user.id if update.effective_user else None)
    await update.message.reply_text("Bot is Active ✅")

async def baseurl_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    AWAITING_BASEURL.add(uid)
    logger.info("/baseurl by user_id=%s (awaiting input)", uid)
    await update.message.reply_text("Please enter your base URL:")

async def set_baseurl_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    user_id = update.effective_user.id if update.effective_user else None
    text = (update.message.text or "").strip()
    if user_id in AWAITING_BASEURL:
        BASE_URL = text.rstrip("/")
        AWAITING_BASEURL.remove(user_id)
        logger.info("BASE_URL set by user_id=%s -> %s", user_id, BASE_URL)
        await update.message.reply_text(f"✅ Base URL set: {BASE_URL}")
        return
    logger.info("Message treated as link by user_id=%s: %s", user_id, text)
    await handle_message(update, context)

async def stop_baseurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    BASE_URL = None
    logger.info("/stop by user_id=%s -> BASE_URL cleared", update.effective_user.id if update.effective_user else None)
    await update.message.reply_text("Base URL cleared.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    if not update.message or not update.message.text:
        logger.info("Ignoring non-text message")
        return

    link = update.message.text.strip()
    uid = update.effective_user.id if update.effective_user else None
    logger.info("handle_message: user_id=%s link=%s", uid, link)

    if not BASE_URL:
        logger.warning("BASE_URL not set; prompting user")
        await update.message.reply_text("❌ Base URL not set. Use /baseurl first.")
        return

    api_url = f"{BASE_URL}/api?link={link}"
    logger.info("API request -> %s", api_url)
    msg_processing = await update.message.reply_text("⏳ Processing video...")

    try:
        timeout = aiohttp.ClientTimeout(total=600)
        connector = aiohttp.TCPConnector(limit=0, ssl=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(api_url) as resp:
                logger.info("API response status: %s", resp.status)
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                logger.debug("API response JSON: %s", json.dumps(data)[:500])
    except Exception as e:
        logger.exception("API request error")
        await msg_processing.edit_text(f"❌ API request error: {e}")
        return

    if not data.get("success") or "dlink" not in data or not data["dlink"]:
        logger.warning("API failed to provide dlink: %s", data)
        await msg_processing.edit_text("❌ Failed to fetch video from API.")
        return

    d = data["dlink"]
    dlink = d.get("dlink")
    name = d.get("name") or "video"
    size_str = d.get("size") or "unknown size"

    if not dlink:
        logger.warning("API did not return download link in dlink")
        await msg_processing.edit_text("❌ API did not return a download link.")
        return

    await msg_processing.edit_text("⏳ Downloading video...")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
        local_file = tmp_file.name

    try:
        await download_video_with_progress(msg_processing, dlink, local_file)
    except Exception:
        if os.path.exists(local_file):
            os.remove(local_file)
        return

    await msg_processing.edit_text("⏳ Uploading video...")
    try:
        file_size = os.path.getsize(local_file)
        logger.info("Uploading to Telegram: %s (%s)", name, human_size(file_size))
        with open(local_file, "rb") as f:
            sent = await update.message.reply_video(video=f, caption=f"{name} ({size_str})")
        logger.info("Upload success: chat_id=%s msg_id=%s", sent.chat_id, sent.message_id)
        schedule_delete(update.effective_chat.id, sent.message_id, context.bot)
        await msg_processing.delete()
    except Exception as e:
        logger.exception("Upload failed")
        await msg_processing.edit_text(f"❌ Upload failed: {e}")
    finally:
        try:
            if os.path.exists(local_file):
                os.remove(local_file)
                logger.info("Temp file removed: %s", local_file)
        except Exception:
            logger.debug("Temp file removal failed: %s", local_file)

# =================== Register Handlers =================
# Log first
application.add_handler(MessageHandler(filters.ALL, log_update), group=-1)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("baseurl", baseurl_prompt))
application.add_handler(CommandHandler("stop", stop_baseurl))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_baseurl_input))

# Error handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error in handler", exc_info=context.error)
application.add_error_handler(on_error)

# =================== Webhook Helpers ===================
def _telegram_api(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok") is True
        if not ok:
            logger.warning("%s failed: status=%s body=%s", method, r.status_code, r.text[:500])
        else:
            logger.info("%s ok: %s", method, r.json())
        return ok, r.json() if r.headers.get("content-type","").startswith("application/json") else r.text
    except Exception as e:
        logger.exception("%s exception: %s", method, e)
        return False, None

def ensure_webhook():
    """Delete existing webhook and set a new one using plain HTTP (robust on Render)."""
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL missing, cannot set webhook")
        return

    logger.info("Ensuring webhook -> %s", WEBHOOK_URL)
    _telegram_api("deleteWebhook", {"drop_pending_updates": True})
    ok, _ = _telegram_api("setWebhook", {
        "url": WEBHOOK_URL,
        "drop_pending_updates": True,
        # You can add secret_token for extra security:
        # "secret_token": os.getenv("WEBHOOK_SECRET", "")
    })
    if ok and ADMIN_CHAT_ID:
        # Notify admin bot is live
        _telegram_api("sendMessage", {
            "chat_id": ADMIN_CHAT_ID,
            "text": "✅ Bot is live and webhook set.\n" + (WEBHOOK_URL or "")
        })

# =================== Flask Routes ======================
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=False)
        logger.info("Webhook hit: headers=%s", dict(request.headers))
        update = Update.de_json(payload, bot)
        # push update to PTB queue
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

# =================== Start PTB in background ===========
async def _ptb_start():
    global PTB_LOOP
    PTB_LOOP = asyncio.get_running_loop()
    await application.initialize()
    await application.start()
    logger.info("PTB Application started (webhook mode). Ready to process updates.")

def run_ptb_bg():
    asyncio.run(_ptb_start())

# Start PTB background thread immediately at import time
threading.Thread(target=run_ptb_bg, daemon=True).start()

# Ensure webhook once at import time (works with gunicorn on Render)
ensure_webhook()

# =================== Main (local dev) ===================
if __name__ == "__main__":
    logger.info("Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)

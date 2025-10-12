import os
import sys
import logging
import aiohttp
import tempfile
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
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
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment")

# =================== Logging ===========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tg-terabox")

# =================== Globals ===========================
BASE_URL = None
FIRST_TIME_USERS = set()
AWAITING_BASEURL = set()

# =================== Utils ============================
def get_greeting():
    hour = datetime.utcnow().hour + 5.5  # crude IST conversion
    hour = int(hour % 24)
    if 5 <= hour < 12:
        return "Good Morning 🌞"
    elif 12 <= hour < 16:
        return "Good Afternoon ☀️"
    elif 16 <= hour < 20:
        return "Good Evening 🌇"
    else:
        return "Good Night 🌙"

def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

# =================== Handlers ==========================
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
    await update.message.reply_text("Bot is Active ✅")

async def baseurl_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    AWAITING_BASEURL.add(uid)
    await update.message.reply_text("Please enter your base URL:")

async def set_baseurl_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    user_id = update.effective_user.id if update.effective_user else None
    text = (update.message.text or "").strip()
    if user_id in AWAITING_BASEURL:
        BASE_URL = text.rstrip("/")
        AWAITING_BASEURL.remove(user_id)
        await update.message.reply_text(f"✅ Base URL set: {BASE_URL}")
        return
    await handle_message(update, context)

async def stop_baseurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    BASE_URL = None
    await update.message.reply_text("Base URL cleared.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    if not update.message or not update.message.text:
        return

    link = update.message.text.strip()
    if not BASE_URL:
        await update.message.reply_text("❌ Base URL not set. Use /baseurl first.")
        return

    api_url = f"{BASE_URL}/api?link={link}"
    msg_processing = await update.message.reply_text("⏳ Processing video...")

    try:
        timeout = aiohttp.ClientTimeout(total=600)
        connector = aiohttp.TCPConnector(limit=0, ssl=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(api_url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"API request error: {e}")
        await msg_processing.edit_text(f"❌ API request error: {e}")
        return

    if not data.get("success") or "dlink" not in data or not data["dlink"]:
        await msg_processing.edit_text("❌ Failed to fetch video from API.")
        return

    d = data["dlink"]
    dlink = d.get("dlink")
    name = d.get("name") or "video"
    size_str = d.get("size") or "unknown size"

    if not dlink:
        await msg_processing.edit_text("❌ API did not return a download link.")
        return

    # ✅ First try: Direct send (fastest)
    try:
        logger.info(f"Trying direct send: {dlink}")
        await msg_processing.edit_text("⏳ Sending video directly from link...")
        await update.message.reply_video(video=dlink, caption=f"{name} ({size_str})")
        await msg_processing.delete()
        logger.info("Direct send successful ✅")
        return
    except Exception as e:
        logger.warning(f"Direct send failed: {e}")
        await msg_processing.edit_text("⚠️ Direct send failed, downloading file...")

    # Fallback: Download + Upload with progress
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
        local_file = tmp_file.name

    try:
        await msg_processing.edit_text("⏳ Downloading video...")
        # Download with progress logs
        downloaded = 0
        async with aiohttp.ClientSession() as session:
            async with session.get(dlink) as resp:
                resp.raise_for_status()
                total_size = int(resp.headers.get("content-length", 0))
                with open(local_file, "wb") as f:
                    last_update = asyncio.get_event_loop().time()
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = asyncio.get_event_loop().time()
                        if now - last_update >= 15:  # 15s interval update
                            percent = int(downloaded * 100 / total_size) if total_size else 0
                            await msg_processing.edit_text(f"⏳ Downloading... {percent}%")
                            logger.info(f"Downloading... {percent}% ({human_size(downloaded)}/{human_size(total_size)})")
                            last_update = now

        await msg_processing.edit_text("⏳ Uploading video...")
        logger.info("Starting upload...")
        with open(local_file, "rb") as f:
            sent = await update.message.reply_video(video=f, caption=f"{name} ({size_str})")
        await msg_processing.edit_text("✅ Upload complete!")
        logger.info("Upload complete ✅")
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await msg_processing.edit_text(f"❌ Upload failed: {e}")
    finally:
        if os.path.exists(local_file):
            os.remove(local_file)

# =================== Main ==============================
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("baseurl", baseurl_prompt))
    application.add_handler(CommandHandler("stop", stop_baseurl))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_baseurl_input))
    logger.info("Bot started in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()

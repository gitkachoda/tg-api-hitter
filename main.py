import os
import json
import tempfile
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from threading import Timer

# ------------------- Load Environment -------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 5000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# ------------------- Globals -------------------
BASE_URL = None
FIRST_TIME_USERS = set()
AWAITING_BASEURL = set()

app = Flask(__name__)
bot = Bot(BOT_TOKEN)

# ------------------- Utility Functions -------------------
def get_greeting():
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Good Morning 🌞"
    elif 12 <= hour < 16:
        return "Good Afternoon ☀️"
    elif 16 <= hour < 20:
        return "Good Evening 🌇"
    else:
        return "Good Night 🌙"

def schedule_delete(chat_id, message_id):
    def delete_msg():
        try:
            bot.delete_message(chat_id=chat_id, message_id=message_id)
        except:
            pass
    Timer(24*60*60, delete_msg).start()

# ------------------- Video Download with Progress -------------------
async def download_video_with_progress(update, msg_processing, url, local_file):
    downloaded = 0
    last_update = -1
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3600) as r:
                r.raise_for_status()
                total_size = int(r.headers.get("Content-Length", 0))
                with open(local_file, "wb") as f:
                    async for chunk in r.content.iter_chunked(8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress = int(downloaded / total_size * 100)
                            if progress != last_update and (progress % 2 == 0 or progress == 100):
                                await msg_processing.edit_text(f"⏳ Downloading video... {progress}%")
                                last_update = progress
        if total_size > 0:
            await msg_processing.edit_text("⏳ Download complete! 100%")
    except asyncio.TimeoutError:
        await msg_processing.edit_text("❌ Video download timed out. Try again.")
        raise
    except Exception as e:
        await msg_processing.edit_text(f"❌ Video download error: {e}")
        raise

# ------------------- Telegram Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    greeting = get_greeting()
    if user_id not in FIRST_TIME_USERS:
        FIRST_TIME_USERS.add(user_id)
        desc = f"""{greeting}, welcome! 👋

This bot fetches **Terabox videos**.

1. Set base URL using /baseurl
2. Send a Terabox link
3. Videos auto-delete after 1 day
Use /status or /stop"""
        await update.message.reply_text(desc)
    else:
        await update.message.reply_text(f"{greeting}, welcome back! Send a Terabox link.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is Active ✅")

async def baseurl_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    AWAITING_BASEURL.add(user_id)
    await update.message.reply_text("Please enter your base URL:")

async def set_baseurl_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if user_id in AWAITING_BASEURL:
        BASE_URL = text.rstrip("/")
        AWAITING_BASEURL.remove(user_id)
        await update.message.reply_text(f"✅ Base URL set: {BASE_URL}")
        print(f"[LOG] BASE_URL set by {user_id}: {BASE_URL}")
    else:
        await handle_message(update, context)

async def stop_baseurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    BASE_URL = None
    await update.message.reply_text("Base URL cleared.")
    print(f"[LOG] BASE_URL cleared by {update.effective_user.id}")

# ------------------- Video Handling -------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    user_id = update.effective_user.id
    link = update.message.text.strip()

    if not BASE_URL:
        await update.message.reply_text("❌ Base URL not set. Use /baseurl first.")
        return

    api_url = f"{BASE_URL}/api?link={link}"
    print(f"[LOG] Requesting API: {api_url}")
    msg_processing = await update.message.reply_text("⏳ Processing video...")

    # ----- API Request -----
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=600) as resp:
                data = await resp.json()
        print(f"[LOG] API Response: {json.dumps(data, indent=2)}")
    except asyncio.TimeoutError:
        await msg_processing.edit_text("❌ API request timed out. Try again.")
        return
    except Exception as e:
        await msg_processing.edit_text(f"❌ API request error: {e}")
        print(f"[ERROR] API request failed for {user_id}: {e}")
        return

    if not data.get("success") or "dlink" not in data:
        await msg_processing.edit_text("❌ Failed to fetch video from API.")
        print(f"[WARN] API failed for {user_id}")
        return

    dlink = data["dlink"]["dlink"]
    name = data["dlink"]["name"]
    size_str = data["dlink"]["size"]

    # ----- Video Download -----
    await msg_processing.edit_text("⏳ Downloading video...")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
        local_file = tmp_file.name

    try:
        await download_video_with_progress(update, msg_processing, dlink, local_file)
    except Exception:
        return

    # ----- Upload Video (directly, no FFmpeg) -----
    await msg_processing.edit_text("⏳ Uploading video...")
    try:
        with open(local_file, "rb") as f:
            msg = await update.message.reply_video(video=f, caption=f"{name} ({size_str})")
        schedule_delete(update.effective_chat.id, msg.message_id)
        await msg_processing.delete()
        os.remove(local_file)
        print(f"[LOG] Video sent and scheduled for deletion for {user_id}")
    except Exception as e:
        await msg_processing.edit_text(f"❌ Upload failed: {e}")
        os.remove(local_file)
        print(f"[ERROR] Upload failed for {user_id}: {e}")

# ------------------- Build Bot -------------------
app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
app_bot.add_handler(CommandHandler("start", start))
app_bot.add_handler(CommandHandler("status", status))
app_bot.add_handler(CommandHandler("baseurl", baseurl_prompt))
app_bot.add_handler(CommandHandler("stop", stop_baseurl))
app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_baseurl_input))

# ------------------- Flask Webhook -------------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    asyncio.run(app_bot.process_update(update))
    return "ok"

@app.route("/")
def index():
    return "Bot is running ✅"

# ------------------- Main -------------------
if __name__ == "__main__":
    if WEBHOOK_URL:
        async def set_webhook():
            await bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}")
            print(f"[LOG] Webhook set: {WEBHOOK_URL}/{BOT_TOKEN}")
        asyncio.run(set_webhook())
        app.run(host="0.0.0.0", port=PORT)
    else:
        print("[LOG] Running locally using polling...")
        app_bot.run_polling()

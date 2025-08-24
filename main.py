import os
import json
import tempfile
import asyncio
import aiohttp
import random
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from threading import Timer

# ------------------- Load Environment -------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 5000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-domain.com
URL_PATH = os.getenv("URL_PATH", BOT_TOKEN)  # webhook path

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment")

# ------------------- Globals -------------------
BASE_URL = None
FIRST_TIME_USERS = set()
AWAITING_BASEURL = set()
application = None  # will set in main()


# ------------------- Utility -------------------
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


def schedule_delete(chat_id: int, message_id: int):
    def delete_msg():
        try:
            if application is not None:
                asyncio.run(application.bot.delete_message(chat_id=chat_id, message_id=message_id))
        except Exception:
            pass

    Timer(24 * 60 * 60, delete_msg).start()


def human_size(n: int) -> str:
    # Friendly bytes -> KB/MB/GB
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


# ------------------- Download with retries (30–35s progress updates) -------------------
async def download_video_with_progress(msg_processing, url, local_file: str):
    retries = 3

    for attempt in range(1, retries + 1):
        downloaded = 0
        last_percent = -1

        try:
            timeout = aiohttp.ClientTimeout(total=3600)
            connector = aiohttp.TCPConnector(limit=0, ssl=False)  # disable SSL verify (CDN quirks)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(url) as r:
                    r.raise_for_status()
                    total_size = int(r.headers.get("Content-Length", 0))

                    # Telegram hard limit ~2GB (guard if known)
                    if total_size and total_size > 1_990_000_000:
                        await msg_processing.edit_text("❌ File too large for Telegram upload (> ~2GB).")
                        raise RuntimeError("File too large")

                    # time-based throttle: update every 30–35 sec
                    loop = asyncio.get_event_loop()
                    now = loop.time()
                    next_edit_at = now + random.uniform(30, 35)

                    with open(local_file, "wb") as f:
                        async for chunk in r.content.iter_chunked(8192):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)

                            # progress calc
                            percent = int(downloaded / total_size * 100) if total_size > 0 else None

                            # time to update?
                            now = loop.time()
                            if now >= next_edit_at:
                                try:
                                    if total_size > 0 and percent is not None:
                                        # only send if % changed to avoid "message is not modified"
                                        if percent != last_percent:
                                            await msg_processing.edit_text(f"⏳ Downloading video... {percent}%")
                                            last_percent = percent
                                    else:
                                        # size unknown – show downloaded bytes
                                        await msg_processing.edit_text(
                                            f"⏳ Downloading video... {human_size(downloaded)}"
                                        )
                                except Exception:
                                    pass
                                # schedule next update window
                                next_edit_at = now + random.uniform(30, 35)

            # Final completion message
            try:
                if total_size > 0:
                    await msg_processing.edit_text("⏳ Download complete! 100%")
                else:
                    await msg_processing.edit_text("⏳ Download complete!")
            except Exception:
                pass
            return  # success

        except Exception as e:
            if attempt < retries:
                try:
                    await msg_processing.edit_text(
                        f"⚠️ Download failed (try {attempt}/{retries}), retrying in 3s..."
                    )
                except Exception:
                    pass
                await asyncio.sleep(3)
                continue
            else:
                try:
                    await msg_processing.edit_text(f"❌ Video download error: {e}")
                except Exception:
                    pass
                raise


# ------------------- Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    greeting = get_greeting()
    if user_id not in FIRST_TIME_USERS:
        FIRST_TIME_USERS.add(user_id)
        desc = (
            f"{greeting}, welcome! 👋\n\n"
            "This bot fetches **Terabox videos**.\n\n"
            "1. Set base URL using /baseurl\n"
            "2. Send a Terabox link\n"
            "3. Videos auto-delete after 1 day\n"
            "Use /status or /stop"
        )
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
    text = (update.message.text or "").strip()

    if user_id in AWAITING_BASEURL:
        BASE_URL = text.rstrip("/")
        AWAITING_BASEURL.remove(user_id)
        await update.message.reply_text(f"✅ Base URL set: {BASE_URL}")
        print(f"[LOG] BASE_URL set by {user_id}: {BASE_URL}")
        return

    await handle_message(update, context)


async def stop_baseurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    BASE_URL = None
    await update.message.reply_text("Base URL cleared.")
    print(f"[LOG] BASE_URL cleared by {update.effective_user.id}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_URL
    if update.message is None or update.message.text is None:
        return

    user_id = update.effective_user.id
    link = update.message.text.strip()

    if not BASE_URL:
        await update.message.reply_text("❌ Base URL not set. Use /baseurl first.")
        return

    api_url = f"{BASE_URL}/api?link={link}"
    print(f"[LOG] Requesting API: {api_url}")
    msg_processing = await update.message.reply_text("⏳ Processing video...")

    try:
        timeout = aiohttp.ClientTimeout(total=600)
        connector = aiohttp.TCPConnector(limit=0, ssl=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(api_url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        print(f"[LOG] API Response: {json.dumps(data, indent=2)}")
    except Exception as e:
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
        with open(local_file, "rb") as f:
            sent = await update.message.reply_video(video=f, caption=f"{name} ({size_str})")
        schedule_delete(update.effective_chat.id, sent.message_id)
        await msg_processing.delete()
    except Exception as e:
        await msg_processing.edit_text(f"❌ Upload failed: {e}")
    finally:
        if os.path.exists(local_file):
            os.remove(local_file)


# ------------------- Build & Run -------------------
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("baseurl", baseurl_prompt))
    app.add_handler(CommandHandler("stop", stop_baseurl))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_baseurl_input))
    return app


if __name__ == "__main__":
    application = build_app()

    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=URL_PATH,
            webhook_url=f"{WEBHOOK_URL}/{URL_PATH}",
            drop_pending_updates=True,
        )
    else:
        application.run_polling(drop_pending_updates=True)

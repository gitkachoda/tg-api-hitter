import asyncio
import os
from telegram import Bot
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = "https://tg-api-hitter.onrender.com/webhook"

async def main():
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook()
    res = await bot.set_webhook(url=WEBHOOK_URL)
    print(res)

if __name__ == "__main__":
    asyncio.run(main())

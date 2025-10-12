import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
res = requests.get(url)
print(res.json())

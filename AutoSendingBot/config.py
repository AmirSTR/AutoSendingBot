import os

TG_TOKEN = os.getenv("TG_TOKEN")
VK_TOKEN = os.getenv("VK_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.up.railway.app
PORT = int(os.getenv("PORT", "8443"))

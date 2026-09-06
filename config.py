import os
from dotenv import load_dotenv

# Բեռնում ենք փոփոխականները .env ֆայլից
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Խնդիր: BOT_TOKEN-ը գտնված չէ .env ֆայլում:")

# Կարդում ենք ADMIN_IDS-ը, ստորակետերով բաժանում և վերածում int-երի set-ի
raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(admin_id.strip())
    for admin_id in raw_admin_ids.split(",")
    if admin_id.strip().isdigit()
}
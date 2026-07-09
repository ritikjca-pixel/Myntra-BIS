import os

# --------------------------------------------------------------------------
# CHECK INTERVAL / MISC
# --------------------------------------------------------------------------
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "1"))
CSV_OUTPUT_PATH = os.getenv("CSV_OUTPUT_PATH", "stock_snapshot.csv")

# --------------------------------------------------------------------------
# TELEGRAM (used only by scraper.py's alert_terminal-style fallback if you
# ever run this standalone via CLI; app.py has its own Telegram handling)
# --------------------------------------------------------------------------
TELEGRAM_ENABLED = bool(os.getenv("8676436769:AAGAc99V8-aepu40ATzNMJrVZwAo7SviIUo")) and bool(os.getenv("5871058233"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8676436769:AAGAc99V8-aepu40ATzNMJrVZwAo7SviIUo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5871058233")

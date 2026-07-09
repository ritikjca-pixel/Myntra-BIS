import os

# --------------------------------------------------------------------------
# CONTROL BOT - the one you talk to: /menu, add/remove links, status, etc.
# --------------------------------------------------------------------------
CONTROL_BOT_TOKEN = os.environ["8676436769:AAGAc99V8-aepu40ATzNMJrVZwAo7SviIUo"]
CONTROL_CHAT_ID = os.environ["5871058233"]

# --------------------------------------------------------------------------
# ALERT BOT - purely sends "back in stock" pings, no commands processed here
# --------------------------------------------------------------------------
ALERT_BOT_TOKEN = os.environ["8676436769:AAGAc99V8-aepu40ATzNMJrVZwAo7SviIUo"]
ALERT_CHAT_ID = os.environ["5871058233"]

# --------------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------------
DATABASE_URL = os.environ["DATABASE_URL"]

# --------------------------------------------------------------------------
# CYCLE TIMING
# --------------------------------------------------------------------------
# Gap between the end of one full pass over all links and the start of the
# next full pass. NOT a per-link delay - that's still handled inside
# scraper.py's own DELAY_RANGE to avoid hammering Myntra link-to-link.
CYCLE_GAP_SECONDS = float(os.getenv("CYCLE_GAP_SECONDS", "2"))

CSV_OUTPUT_PATH = os.getenv("CSV_OUTPUT_PATH", "stock_snapshot.csv")

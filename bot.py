#!/usr/bin/env python3
"""
Myntra Back-in-Stock Monitor - Telegram-only (no web dashboard)
=================================================================
Two Telegram bots:
  - CONTROL bot: you talk to this one. /menu gives an inline-keyboard
    control panel to manage links, check status, force a check, etc.
  - ALERT bot: sends ONLY "back in stock" pings. No commands handled here.

Loop behaviour: scrape every monitored link back-to-back, then wait
config.CYCLE_GAP_SECONDS (default 2s), then do it again. Forever.
"""

import os
import threading
import time
import logging
import urllib.request
import json as _json
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

import config
import scraper

# --------------------------------------------------------------------------
# SETUP
# --------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def get_conn():
    return psycopg2.connect(config.DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS links (
                    id       SERIAL PRIMARY KEY,
                    title    TEXT NOT NULL DEFAULT '',
                    url      TEXT NOT NULL,
                    added_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_status (
                    product_id        TEXT PRIMARY KEY,
                    brand             TEXT,
                    product_name      TEXT,
                    product_link      TEXT,
                    source_title      TEXT,
                    in_stock          BOOLEAN DEFAULT TRUE,
                    last_seen         TIMESTAMPTZ,
                    last_out_of_stock TIMESTAMPTZ,
                    updated_at        TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        conn.commit()
    log.info("[db] Tables ready.")


# --------------------------------------------------------------------------
# LINKS HELPERS
# --------------------------------------------------------------------------

def read_links():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title, url FROM links ORDER BY id;")
            rows = cur.fetchall()
    return [(r["title"], r["url"]) for r in rows]


def read_links_with_ids():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, url FROM links ORDER BY id;")
            rows = cur.fetchall()
    return [(r["id"], r["title"], r["url"]) for r in rows]


def append_link(title: str, url: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO links (title, url) VALUES (%s, %s);",
                (title.strip(), url.strip())
            )
        conn.commit()


def remove_link_by_id(link_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM links WHERE id = %s RETURNING id;", (link_id,))
            deleted = cur.fetchone()
        conn.commit()
    return deleted is not None


def remove_link_by_position(position: int) -> bool:
    rows = read_links_with_ids()
    if position < 1 or position > len(rows):
        return False
    link_id = rows[position - 1][0]
    return remove_link_by_id(link_id)


# --------------------------------------------------------------------------
# STOCK STATUS HELPERS (DB backed)
# --------------------------------------------------------------------------

def load_known_from_db():
    known = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_id, brand, product_name, product_link,
                       source_title, in_stock, last_seen, last_out_of_stock
                FROM stock_status;
            """)
            for r in cur.fetchall():
                known[r["product_id"]] = {
                    "brand": r["brand"],
                    "product_name": r["product_name"],
                    "product_link": r["product_link"],
                    "source_title": r["source_title"],
                    "in_stock": bool(r["in_stock"]),
                    "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                    "last_out_of_stock": r["last_out_of_stock"].isoformat() if r["last_out_of_stock"] else None,
                }
    return known


def save_known_to_db(known):
    if not known:
        return
    rows = []
    for pid, entry in known.items():
        rows.append((
            str(pid),
            entry.get("brand"),
            entry.get("product_name"),
            entry.get("product_link"),
            entry.get("source_title") or "",
            bool(entry.get("in_stock", True)),
            entry.get("last_seen"),
            entry.get("last_out_of_stock"),
        ))

    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO stock_status
                    (product_id, brand, product_name, product_link,
                     source_title, in_stock, last_seen, last_out_of_stock, updated_at)
                VALUES %s
                ON CONFLICT (product_id) DO UPDATE SET
                    brand             = EXCLUDED.brand,
                    product_name      = EXCLUDED.product_name,
                    product_link      = EXCLUDED.product_link,
                    source_title      = EXCLUDED.source_title,
                    in_stock          = EXCLUDED.in_stock,
                    last_seen         = COALESCE(EXCLUDED.last_seen, stock_status.last_seen),
                    last_out_of_stock = COALESCE(EXCLUDED.last_out_of_stock, stock_status.last_out_of_stock),
                    updated_at        = NOW();
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s, NOW())",
                page_size=500,
            )
        conn.commit()
    log.info(f"[db] Upserted {len(rows)} stock records.")


def count_products():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM stock_status;")
            return cur.fetchone()["c"]


def count_out_of_stock():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM stock_status WHERE in_stock = FALSE;")
            return cur.fetchone()["c"]


# --------------------------------------------------------------------------
# TELEGRAM - LOW LEVEL (works for either bot, pass token explicitly)
# --------------------------------------------------------------------------

def _tg_call(token, method, payload):
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url, data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        log.warning(f"[telegram:{method}] failed: {e}")
        return None


def tg_send(token, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _tg_call(token, "sendMessage", payload)


def tg_edit(token, chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _tg_call(token, "editMessageText", payload)


def tg_answer_callback(token, callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _tg_call(token, "answerCallbackQuery", payload)


def tg_get_updates(token, offset):
    if not token:
        return []
    url = (
        f"https://api.telegram.org/bot{token}/getUpdates"
        f"?offset={offset}&timeout=20&allowed_updates=message,callback_query"
    )
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = _json.loads(resp.read())
            return data.get("result", [])
    except Exception as e:
        log.warning(f"[telegram] poll failed: {e}")
        return []


# --------------------------------------------------------------------------
# ALERT BOT - fires restock pings only
# --------------------------------------------------------------------------

def send_restock_alerts(back_in_stock_items):
    for item in back_in_stock_items:
        text = scraper.format_restock_message(item)
        tg_send(config.ALERT_BOT_TOKEN, config.ALERT_CHAT_ID, text)
        time.sleep(0.5)


def send_test_alert():
    """
    Fires ONE fake 'back in stock' message through the real alert bot /
    alert chat, using dummy product data. Lets you confirm ALERT_BOT_TOKEN
    and ALERT_CHAT_ID are correctly wired end-to-end, without waiting for
    a real restock to happen.
    """
    dummy_item = {
        "title": "TEST",
        "brand": "Test Brand",
        "product_name": "This is a manual test alert - ignore",
        "product_link": "https://www.myntra.com/",
    }
    send_restock_alerts([dummy_item])


# --------------------------------------------------------------------------
# SHARED BOT STATE
# --------------------------------------------------------------------------

bot_state = {
    "is_running": True,
    "status_message": "Initializing...",
    "current_action": "Waiting to start...",
    "last_checked": "Never",
    "last_cycle_count": 0,
}

check_now_flag = {"requested": False}
_convo_state = {}


def is_authorised(chat_id) -> bool:
    if not config.CONTROL_CHAT_ID:
        return True
    return str(chat_id) == str(config.CONTROL_CHAT_ID)


# --------------------------------------------------------------------------
# MENU / TEXT BUILDERS (control bot)
# --------------------------------------------------------------------------

HELP_TEXT = """<b>Myntra Back-in-Stock Monitor</b>

Use the buttons below the menu, or these commands directly:

/menu - open the control panel
/status - scraper status &amp; stats
/list - show all monitored links
/add [Title |] URL - add a new link
/remove N - remove link number N (see /list)
/checknow - force an immediate cycle
/testalert - send a dummy restock alert via the ALERT bot, to confirm delivery works
/help - show this message"""


def main_menu_keyboard():
    run_label = "Stop Scraper" if bot_state["is_running"] else "Start Scraper"
    run_data = "stop" if bot_state["is_running"] else "start"
    return [
        [{"text": "Status", "callback_data": "status"},
         {"text": "Check Now", "callback_data": "checknow"}],
        [{"text": run_label, "callback_data": run_data}],
        [{"text": "List Links", "callback_data": "list"},
         {"text": "Add Link", "callback_data": "add"}],
        [{"text": "Remove Link", "callback_data": "remove"},
         {"text": "Out of Stock", "callback_data": "oos"}],
        [{"text": "Test Alert", "callback_data": "testalert"}],
        [{"text": "Help", "callback_data": "help"}],
    ]


def back_to_menu_keyboard():
    return [[{"text": "Back to Menu", "callback_data": "menu"}]]


def links_keyboard_for_removal():
    rows = read_links_with_ids()
    kb = []
    for i, (lid, title, url) in enumerate(rows, 1):
        label = f"{i}. {title or url[:30]}"
        kb.append([{"text": label, "callback_data": f"rm:{lid}"}])
    kb.append([{"text": "Cancel", "callback_data": "menu"}])
    return kb, rows


def main_menu_text():
    state = "Running" if bot_state["is_running"] else "Stopped"
    return (
        f"<b>Myntra Back-in-Stock Monitor</b>\n"
        f"Status: <b>{state}</b>\n"
        f"Last checked: {bot_state['last_checked']}\n"
        f"Last cycle found: {bot_state['last_cycle_count']} restock(s)\n\n"
        f"Choose an action:"
    )


def status_text():
    links = read_links()
    try:
        prod_count = count_products()
        oos_count = count_out_of_stock()
    except Exception:
        prod_count, oos_count = "?", "?"
    state = "Running" if bot_state["is_running"] else "Stopped"
    return (
        f"<b>Status:</b> {state}\n"
        f"<b>Action:</b> {bot_state['current_action']}\n"
        f"<b>Last checked:</b> {bot_state['last_checked']}\n"
        f"<b>Links monitored:</b> {len(links)}\n"
        f"<b>Products tracked:</b> {prod_count}\n"
        f"<b>Currently marked OOS:</b> {oos_count}"
    )


def list_text():
    rows = read_links_with_ids()
    if not rows:
        return "No links yet. Tap 'Add Link' to add one."
    lines = []
    for i, (lid, title, url) in enumerate(rows, 1):
        label = f"<b>{title}</b> - " if title else ""
        short_url = url[:55] + "..." if len(url) > 55 else url
        lines.append(f"{i}. {label}<code>{short_url}</code>")
    return "\n".join(lines)


def oos_text():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_name, product_link, brand, last_out_of_stock
                FROM stock_status
                WHERE in_stock = FALSE
                ORDER BY last_out_of_stock DESC NULLS LAST
                LIMIT 25;
            """)
            rows = cur.fetchall()
    if not rows:
        return "Nothing currently marked out of stock."
    lines = []
    for r in rows:
        brand_line = f"{r['brand']} - " if r["brand"] else ""
        lines.append(f"{brand_line}{r['product_name']}\n{r['product_link']}")
    header = f"<b>{len(rows)} item(s) marked out of stock</b> (most recent first, capped at 25):\n\n"
    return header + "\n\n".join(lines)


def add_usage_text():
    return (
        "Send me the Myntra link now (optionally with a title).\n\n"
        "Formats accepted:\n"
        "<code>https://www.myntra.com/shoes?f=...</code>\n"
        "or\n"
        "<code>Men Shoes | https://www.myntra.com/shoes?f=...</code>"
    )


def render_menu(chat_id, message_id=None):
    text = main_menu_text()
    kb = main_menu_keyboard()
    if message_id:
        tg_edit(config.CONTROL_BOT_TOKEN, chat_id, message_id, text, kb)
    else:
        tg_send(config.CONTROL_BOT_TOKEN, chat_id, text, kb)


# --------------------------------------------------------------------------
# CONTROL BOT - CALLBACK + TEXT HANDLERS
# --------------------------------------------------------------------------

def handle_callback(update):
    cq = update["callback_query"]
    callback_id = cq["id"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    token = config.CONTROL_BOT_TOKEN

    if not is_authorised(chat_id):
        tg_answer_callback(token, callback_id, "Unauthorised.")
        return

    tg_answer_callback(token, callback_id)

    if data == "menu":
        _convo_state.pop(str(chat_id), None)
        render_menu(chat_id, message_id)

    elif data == "status":
        tg_edit(token, chat_id, message_id, status_text(), back_to_menu_keyboard())

    elif data == "checknow":
        check_now_flag["requested"] = True
        tg_edit(token, chat_id, message_id, "Check requested - next cycle starts shortly.", back_to_menu_keyboard())

    elif data == "start":
        bot_state["is_running"] = True
        bot_state["status_message"] = "Resuming..."
        render_menu(chat_id, message_id)

    elif data == "stop":
        bot_state["is_running"] = False
        bot_state["status_message"] = "Stopped via Telegram"
        bot_state["current_action"] = "Idle."
        render_menu(chat_id, message_id)

    elif data == "list":
        tg_edit(token, chat_id, message_id, list_text(), back_to_menu_keyboard())

    elif data == "oos":
        tg_edit(token, chat_id, message_id, oos_text(), back_to_menu_keyboard())

    elif data == "help":
        tg_edit(token, chat_id, message_id, HELP_TEXT, back_to_menu_keyboard())

    elif data == "testalert":
        try:
            send_test_alert()
            tg_edit(token, chat_id, message_id,
                    "Test alert sent via the ALERT bot. Check that chat now.\n\n"
                    "If nothing arrived, ALERT_BOT_TOKEN or ALERT_CHAT_ID is wrong.",
                    back_to_menu_keyboard())
        except Exception as e:
            tg_edit(token, chat_id, message_id, f"Test alert failed to send: {e}", back_to_menu_keyboard())

    elif data == "add":
        _convo_state[str(chat_id)] = {"action": "awaiting_add"}
        tg_edit(token, chat_id, message_id, add_usage_text(), back_to_menu_keyboard())

    elif data == "remove":
        kb, rows = links_keyboard_for_removal()
        if not rows:
            tg_edit(token, chat_id, message_id, "No links to remove.", back_to_menu_keyboard())
        else:
            tg_edit(token, chat_id, message_id, "Tap a link to remove it:", kb)

    elif data.startswith("rm:"):
        link_id = data.split(":", 1)[1]
        if link_id.isdigit() and remove_link_by_id(int(link_id)):
            tg_edit(token, chat_id, message_id, "Link removed.", back_to_menu_keyboard())
        else:
            tg_edit(token, chat_id, message_id, "Couldn't remove that link (already gone?).", back_to_menu_keyboard())


def handle_text_message(chat_id, text: str):
    text = text.strip()
    token = config.CONTROL_BOT_TOKEN

    if not is_authorised(chat_id):
        tg_send(token, chat_id, "Unauthorised.")
        return

    state = _convo_state.get(str(chat_id))

    if state and state.get("action") == "awaiting_add":
        rest = text
        if "|" in rest:
            title, url = rest.split("|", 1)
            title, url = title.strip(), url.strip()
        else:
            title, url = "", rest.strip()

        if "myntra.com" not in url:
            tg_send(token, chat_id, "That doesn't look like a Myntra URL. Send it again, or tap Back to Menu.",
                    back_to_menu_keyboard())
            return

        append_link(title, url)
        _convo_state.pop(str(chat_id), None)
        label = f"<b>{title}</b> - " if title else ""
        tg_send(token, chat_id, f"Added {label}<code>{url[:60]}</code>", back_to_menu_keyboard())
        return

    parts = text.split(None, 1)
    cmd = parts[0].lower().split("@")[0] if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help",):
        tg_send(token, chat_id, HELP_TEXT, back_to_menu_keyboard())
    elif cmd in ("/start", "/menu"):
        render_menu(chat_id)
    elif cmd == "/status":
        tg_send(token, chat_id, status_text(), back_to_menu_keyboard())
    elif cmd == "/list":
        tg_send(token, chat_id, list_text(), back_to_menu_keyboard())
    elif cmd == "/checknow":
        check_now_flag["requested"] = True
        tg_send(token, chat_id, "Check requested - next cycle starts shortly.", back_to_menu_keyboard())
    elif cmd == "/testalert":
        try:
            send_test_alert()
            tg_send(token, chat_id,
                    "Test alert sent via the ALERT bot. Check that chat now.\n\n"
                    "If nothing arrived, ALERT_BOT_TOKEN or ALERT_CHAT_ID is wrong.",
                    back_to_menu_keyboard())
        except Exception as e:
            tg_send(token, chat_id, f"Test alert failed to send: {e}", back_to_menu_keyboard())
    elif cmd == "/add":
        if not rest:
            tg_send(token, chat_id, add_usage_text())
            return
        if "|" in rest:
            title, url = rest.split("|", 1)
            title, url = title.strip(), url.strip()
        else:
            title, url = "", rest.strip()
        if "myntra.com" not in url:
            tg_send(token, chat_id, "That doesn't look like a Myntra URL.")
            return
        append_link(title, url)
        label = f"<b>{title}</b> - " if title else ""
        tg_send(token, chat_id, f"Added {label}<code>{url[:60]}</code>", back_to_menu_keyboard())
    elif cmd == "/remove":
        if not rest or not rest.strip().isdigit():
            tg_send(token, chat_id, "Usage: /remove N  (use /list to see numbers)")
            return
        idx = int(rest.strip())
        if remove_link_by_position(idx):
            tg_send(token, chat_id, f"Link #{idx} removed.", back_to_menu_keyboard())
        else:
            tg_send(token, chat_id, f"No link at position {idx}. Use /list to check.")
    else:
        render_menu(chat_id)


def control_bot_polling_worker():
    offset = 0
    if not config.CONTROL_BOT_TOKEN:
        log.warning("[control-bot] token not set - control bot disabled.")
        return
    log.info("[control-bot] Polling started.")
    while True:
        updates = tg_get_updates(config.CONTROL_BOT_TOKEN, offset)
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    handle_callback(update)
                else:
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id")
                    if chat_id and text:
                        handle_text_message(chat_id, text)
            except Exception as e:
                log.error(f"[control-bot] handler error: {e}")
        time.sleep(1)


# --------------------------------------------------------------------------
# SCRAPE LOOP - runs continuously, CYCLE_GAP_SECONDS between full passes
# --------------------------------------------------------------------------

def scrape_loop_worker():
    while True:
        if bot_state["is_running"]:
            try:
                urls = read_links()
            except Exception as e:
                bot_state["current_action"] = f"DB read error: {e}"
                time.sleep(5)
                continue

            if urls:
                try:
                    bot_state["status_message"] = "Scraping in progress..."

                    def live_update(msg):
                        bot_state["current_action"] = msg

                    _, back_in_stock, _, _ = scraper.run_once(
                        urls,
                        progress_callback=live_update,
                        load_known=load_known_from_db,
                        save_known=save_known_to_db,
                        alert_callback=send_restock_alerts,
                    )

                    bot_state["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    bot_state["last_cycle_count"] = len(back_in_stock)
                    bot_state["status_message"] = "Active (waiting...)"
                    bot_state["current_action"] = "Cycle complete, resting before next pass."
                except Exception as e:
                    bot_state["status_message"] = "Error"
                    bot_state["current_action"] = f"Failed: {str(e)}"
                    log.exception("Scrape run failed")
            else:
                bot_state["status_message"] = "Active"
                bot_state["current_action"] = "No URLs to scrape. Add via Telegram /add."

            check_now_flag["requested"] = False
            time.sleep(config.CYCLE_GAP_SECONDS)
        else:
            bot_state["status_message"] = "Stopped"
            bot_state["current_action"] = "Idle."
            time.sleep(2)


# --------------------------------------------------------------------------
# ENTRY POINT
# --------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()

    t1 = threading.Thread(target=scrape_loop_worker, daemon=True)
    t2 = threading.Thread(target=control_bot_polling_worker, daemon=True)
    t1.start()
    t2.start()

    log.info("Bot running. Waiting on threads...")
    t1.join()
    t2.join()

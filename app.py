import os
import threading
import time
import logging
import urllib.request
import json as _json
from datetime import datetime
from flask import Flask, request, render_template_string, redirect
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

import scraper

# --------------------------------------------------------------------------
# SETUP
# --------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


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
    """Return {product_id: {product_name, product_link, brand, in_stock, ...}}"""
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
    """Upsert the whole known dict into stock_status."""
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
# TELEGRAM CONFIG
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

_tg_offset = 0


def tg_send(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = _json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"[telegram] send failed: {e}")


def tg_get_updates(offset):
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        f"?offset={offset}&timeout=20&allowed_updates=message"
    )
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = _json.loads(resp.read())
            return data.get("result", [])
    except Exception as e:
        log.warning(f"[telegram] poll failed: {e}")
        return []


def is_authorised(chat_id) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


HELP_TEXT = """<b>Myntra Back-in-Stock Monitor - Commands</b>

/status - scraper status &amp; stats
/start_scraper - resume scraping
/stop - pause scraping
/list - show all monitored links
/add [Title |] URL - add a new link
  e.g. <code>/add Men Shoes | https://www.myntra.com/shoes?f=...</code>
  or plain: <code>/add https://www.myntra.com/shoes</code>
/remove N - remove link number N (see /list)
/help - show this message"""


def handle_telegram_command(chat_id, text: str):
    text = text.strip()
    parts = text.split(None, 1)
    cmd = parts[0].lower().split("@")[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not is_authorised(chat_id):
        tg_send(chat_id, "Unauthorised.")
        return

    if cmd in ("/help", "/start"):
        tg_send(chat_id, HELP_TEXT)

    elif cmd == "/status":
        links = read_links()
        try:
            prod_count = count_products()
            oos_count = count_out_of_stock()
        except Exception:
            prod_count, oos_count = "?", "?"
        state = "Running" if bot_state["is_running"] else "Stopped"
        msg = (
            f"<b>Status:</b> {state}\n"
            f"<b>Action:</b> {bot_state['current_action']}\n"
            f"<b>Last checked:</b> {bot_state['last_checked']}\n"
            f"<b>Links monitored:</b> {len(links)}\n"
            f"<b>Products tracked:</b> {prod_count}\n"
            f"<b>Currently marked OOS:</b> {oos_count}"
        )
        tg_send(chat_id, msg)

    elif cmd == "/stop":
        bot_state["is_running"] = False
        bot_state["status_message"] = "Stopped via Telegram"
        bot_state["current_action"] = "Idle."
        tg_send(chat_id, "Scraper stopped.")

    elif cmd == "/start_scraper":
        bot_state["is_running"] = True
        bot_state["status_message"] = "Resuming..."
        tg_send(chat_id, "Scraper started.")

    elif cmd == "/list":
        rows = read_links_with_ids()
        if not rows:
            tg_send(chat_id, "No links yet. Use /add to add one.")
            return
        lines = []
        for i, (lid, title, url) in enumerate(rows, 1):
            label = f"<b>{title}</b> - " if title else ""
            short_url = url[:55] + "..." if len(url) > 55 else url
            lines.append(f"{i}. {label}<code>{short_url}</code>")
        tg_send(chat_id, "\n".join(lines))

    elif cmd == "/add":
        if not rest:
            tg_send(chat_id, "Usage:\n/add Title | https://www.myntra.com/...\nor\n/add https://www.myntra.com/...")
            return
        if "|" in rest:
            title, url = rest.split("|", 1)
            title = title.strip()
            url = url.strip()
        else:
            title = ""
            url = rest.strip()
        if "myntra.com" not in url:
            tg_send(chat_id, "That doesn't look like a Myntra URL.")
            return
        append_link(title, url)
        label = f"<b>{title}</b> - " if title else ""
        tg_send(chat_id, f"Added {label}<code>{url[:60]}</code>")

    elif cmd == "/remove":
        if not rest or not rest.strip().isdigit():
            tg_send(chat_id, "Usage: /remove N  (use /list to see numbers)")
            return
        idx = int(rest.strip())
        success = remove_link_by_position(idx)
        if success:
            tg_send(chat_id, f"Link #{idx} removed.")
        else:
            tg_send(chat_id, f"No link at position {idx}. Use /list to check.")

    else:
        tg_send(chat_id, f"Unknown command <code>{cmd}</code>. Send /help for the list.")


def telegram_polling_worker():
    global _tg_offset
    if not TELEGRAM_BOT_TOKEN:
        log.warning("[telegram] TELEGRAM_BOT_TOKEN not set - bot disabled.")
        return
    log.info("[telegram] Polling started.")
    while True:
        updates = tg_get_updates(_tg_offset)
        for update in updates:
            _tg_offset = update["update_id"] + 1
            msg  = update.get("message", {})
            text = msg.get("text", "")
            chat_id = msg.get("chat", {}).get("id")
            if chat_id and text.startswith("/"):
                try:
                    handle_telegram_command(chat_id, text)
                except Exception as e:
                    log.error(f"[telegram] handler error: {e}")
        time.sleep(1)


# --------------------------------------------------------------------------
# SHARED BOT STATE
# --------------------------------------------------------------------------

bot_state = {
    "is_running": True,
    "status_message": "Initializing...",
    "current_action": "Waiting to start...",
    "last_checked": "Never",
}

# --------------------------------------------------------------------------
# FLASK WEB UI
# --------------------------------------------------------------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Myntra Back-in-Stock Monitor</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 820px; margin: 40px auto; padding: 20px; }
        .dashboard { background: #f4f4f9; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .status { font-size: 1.2em; font-weight: bold; }
        .running { color: #28a745; }
        .stopped { color: #dc3545; }
        .action-log { background: #222; color: #0f0; padding: 10px; border-radius: 4px;
                      font-family: monospace; word-wrap: break-word; margin-top: 10px; }
        input[type="text"] { padding: 9px; border: 1px solid #ccc; border-radius: 4px; }
        button { padding: 10px 20px; color: white; border: none; border-radius: 4px;
                 cursor: pointer; font-weight: bold; }
        .btn-add   { background: #007bff; }
        .btn-stop  { background: #dc3545; }
        .btn-start { background: #28a745; }
        .btn-rm    { background: #dc3545; padding: 4px 9px; font-size: 0.8em; }
        table { width: 100%; border-collapse: collapse; background: #fff;
                border: 1px solid #ddd; border-radius: 5px; }
        th { background: #f0f0f0; padding: 10px; text-align: left; }
        td { padding: 9px 10px; border-top: 1px solid #eee; word-break: break-all; }
        .title-cell { color: #007bff; font-weight: bold; white-space: nowrap; }
    </style>
    <meta http-equiv="refresh" content="5">
</head>
<body>
    <h2>Myntra Back-in-Stock Monitor</h2>

    <div class="dashboard">
        <p><strong>Status:</strong>
            <span class="status {% if state.is_running %}running{% else %}stopped{% endif %}">
                {{ state.status_message }}
            </span>
        </p>
        <p><strong>Last Checked:</strong> {{ state.last_checked }}</p>
        <p><strong>Products tracked:</strong> {{ product_count }} &nbsp;|&nbsp; <strong>Marked OOS:</strong> {{ oos_count }}</p>
        <div class="action-log">&gt; {{ state.current_action }}</div>
        <form method="POST" action="/toggle" style="margin-top:15px;">
            {% if state.is_running %}
                <button type="submit" class="btn-stop">Stop Scraper</button>
            {% else %}
                <button type="submit" class="btn-start">Start Scraper</button>
            {% endif %}
        </form>
    </div>

    <h3>Add New Myntra URL</h3>
    <form method="POST" action="/add" style="margin-bottom:20px; display:flex; gap:8px; flex-wrap:wrap;">
        <input type="text" name="title" placeholder="Title (optional)" style="width:22%;">
        <input type="text" name="url" placeholder="https://www.myntra.com/..." required style="width:55%;">
        <button type="submit" class="btn-add">Add</button>
    </form>

    <h3>Monitored Links ({{ links|length }})</h3>
    {% if links %}
    <table>
        <tr><th>#</th><th>Title</th><th>URL</th><th></th></tr>
        {% for i, lid, title, url in links %}
        <tr>
            <td>{{ i }}</td>
            <td class="title-cell">{{ title or '' }}</td>
            <td><a href="{{ url }}" target="_blank">{{ url[:70] }}</a></td>
            <td>
                <form method="POST" action="/remove" style="margin:0;">
                    <input type="hidden" name="id" value="{{ lid }}">
                    <button type="submit" class="btn-rm">X</button>
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
        <p style="color:#888;">No links yet. Add one above or via Telegram /add.</p>
    {% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    raw = read_links_with_ids()
    links = [(i, lid, title, url) for i, (lid, title, url) in enumerate(raw, 1)]
    try:
        pc = count_products()
        oc = count_out_of_stock()
    except Exception:
        pc, oc = "?", "?"
    return render_template_string(HTML_TEMPLATE, links=links, state=bot_state, product_count=pc, oos_count=oc)


@app.route("/add", methods=["POST"])
def add_url():
    new_url   = request.form.get("url", "").strip()
    new_title = request.form.get("title", "").strip()
    if new_url and "myntra.com" in new_url:
        append_link(new_title, new_url)
    return redirect("/")


@app.route("/remove", methods=["POST"])
def remove_url():
    link_id = request.form.get("id", "")
    if link_id.isdigit():
        remove_link_by_id(int(link_id))
    return redirect("/")


@app.route("/toggle", methods=["POST"])
def toggle_scraper():
    bot_state["is_running"] = not bot_state["is_running"]
    return redirect("/")


# --------------------------------------------------------------------------
# BACKGROUND SCRAPER WORKER
# --------------------------------------------------------------------------

def run_background_worker():
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

                    scraper.run_once(
                        urls,
                        progress_callback=live_update,
                        load_known=load_known_from_db,
                        save_known=save_known_to_db,
                    )

                    bot_state["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    bot_state["status_message"] = "Active (waiting...)"
                    bot_state["current_action"] = "Resting before next cycle."
                except Exception as e:
                    bot_state["status_message"] = "Error"
                    bot_state["current_action"] = f"Failed: {str(e)}"
                    log.exception("Scrape run failed")
            else:
                bot_state["status_message"] = "Active"
                bot_state["current_action"] = "No URLs to scrape. Add via Telegram /add or web UI."
            time.sleep(10)
        else:
            bot_state["status_message"] = "Stopped"
            bot_state["current_action"] = "Idle."
            time.sleep(2)


# --------------------------------------------------------------------------
# ENTRY POINT
# --------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()

    threading.Thread(target=run_background_worker, daemon=True).start()
    threading.Thread(target=telegram_polling_worker, daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

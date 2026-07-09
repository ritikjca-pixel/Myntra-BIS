#!/usr/bin/env python3
"""
Myntra Back-in-Stock Monitor (API method, no Selenium/browser)
================================================================
Same fetching/pagination/parsing engine as the price monitor's scraper.py.
The only thing that's different is what we do with the results:

  Myntra's search/listing API only returns products that are currently
  sellable. So "back in stock" == a product_id that we'd previously marked
  out_of_stock (because it vanished from the listing) showing up in the
  listing again.

Persistence (who's in stock, who isn't) lives in Postgres - see app.py.
"""

import argparse
import csv
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote

from curl_cffi import requests
from curl_cffi.requests.errors import RequestsError

import config

# --------------------------------------------------------------------------
# SCRAPER CONFIG
# --------------------------------------------------------------------------

API_BASE = "https://www.myntra.com/gateway/v2/search"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.myntra.com/",
    "x-requested-with": "browser"
}

PRODUCTS_PER_PAGE = 50
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
DELAY_RANGE = (1.5, 3.5)
MAX_PAGES_SAFETY = 200

DEBUG_RAW = os.getenv("DEBUG_RAW", "0") == "1"


# --------------------------------------------------------------------------
# URL -> API PARAMETER TRANSLATION
# --------------------------------------------------------------------------

def parse_myntra_url(url: str):
    parsed = urlparse(url)
    if "myntra.com" not in parsed.netloc:
        raise ValueError(f"Not a myntra.com URL: {url}")

    category_path = parsed.path.strip("/")
    if not category_path:
        raise ValueError(f"Could not find a category path in URL: {url}")

    qs = parse_qs(parsed.query, keep_blank_values=True)
    params = {k: unquote(v[-1]) for k, v in qs.items()}

    return category_path, params


def build_api_url(category_path: str):
    return f"{API_BASE}/{category_path}"


# --------------------------------------------------------------------------
# AUTO-SPLITTING FOR FILTERS THAT EXCEED MYNTRA'S PAGINATION CEILING
# --------------------------------------------------------------------------

PAGINATION_CEILING = 480


def _split_rf_segments(rf_value):
    return rf_value.split("::") if rf_value else []


def _get_price_range(rf_value):
    for seg in _split_rf_segments(rf_value):
        if seg.startswith("Price:"):
            body = seg.split(":", 1)[1]
            nums = body.replace(" TO ", "_").split("_")
            try:
                return float(nums[0]), float(nums[1])
            except (ValueError, IndexError):
                return None
    return None


def _set_price_range(rf_value, low, high):
    segs = _split_rf_segments(rf_value)
    new_seg = f"Price:{low}_{high}_{low} TO {high}"
    out, found = [], False
    for seg in segs:
        if seg.startswith("Price:"):
            out.append(new_seg)
            found = True
        else:
            out.append(seg)
    if not found:
        out.append(new_seg)
    return "::".join(out)


# --------------------------------------------------------------------------
# FETCHING & ROUTING
# --------------------------------------------------------------------------

_WARMED_UP = False

def warm_up_session(session, progress_callback=None):
    global _WARMED_UP
    if _WARMED_UP:
        return
    try:
        if progress_callback:
            progress_callback("Routing Step 1/2: Simulating visit to myntra.com homepage...")
        session.get("https://www.myntra.com/", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        time.sleep(random.uniform(2.0, 4.0))

        if progress_callback:
            progress_callback("Routing Step 2/2: Simulating visit to myntra.com/clothing...")
        session.get("https://www.myntra.com/clothing", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        time.sleep(random.uniform(2.0, 4.0))

        _WARMED_UP = True
        if progress_callback:
            progress_callback("Routing complete. Session cookies established. Proceeding to API...")
    except RequestsError as e:
        msg = f"[warning] Routing sequence failed: {e}"
        print(msg, file=sys.stderr)
        if progress_callback:
            progress_callback(msg)


def fetch_page(session, category_path, base_params, page_number, progress_callback=None):
    warm_up_session(session, progress_callback)

    api_url = build_api_url(category_path)

    params = dict(base_params)
    rows_per_page = int(params.get("rows", PRODUCTS_PER_PAGE))
    params.setdefault("rows", rows_per_page)
    params["p"] = page_number
    params["o"] = (page_number - 1) * rows_per_page

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(api_url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    last_err = "Response was not valid JSON (got HTML? possibly blocked)"
            elif resp.status_code == 400:
                print(f"  Page {page_number} returned HTTP 400 (Bad Request) - stopping pagination here.")
                return "PAGINATION_LIMIT"
            elif resp.status_code == 403:
                last_err = "403 Forbidden - Myntra is likely blocking this request pattern"
            elif resp.status_code == 401:
                last_err = "401 Unauthorized - session/cookie issue"
            elif resp.status_code == 429:
                last_err = "429 Too Many Requests - back off / slow down"
                time.sleep(5 * attempt)
            else:
                last_err = f"HTTP {resp.status_code}"
        except RequestsError as e:
            last_err = str(e)

        print(f"  [retry {attempt}/{MAX_RETRIES}] {last_err}", file=sys.stderr)
        time.sleep(2 * attempt)

    print(f"  [FAILED] page {page_number}: {last_err}", file=sys.stderr)
    return "FETCH_FAILED:" + (last_err or "unknown error")


def extract_products(api_response):
    if not api_response:
        return [], 0

    if "products" in api_response:
        return api_response["products"], api_response.get("totalCount", 0)

    results = api_response.get("results")
    if isinstance(results, dict) and "products" in results:
        return results["products"], results.get("totalCount") or api_response.get("totalCount") or 0

    def find_list_of_dicts(node):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return node
        if isinstance(node, dict):
            for v in node.values():
                found = find_list_of_dicts(v)
                if found is not None:
                    return found
        return None

    return find_list_of_dicts(api_response) or [], 0


# --------------------------------------------------------------------------
# ESSENTIAL FIELD EXTRACTION
# --------------------------------------------------------------------------

def first_present(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def extract_essentials(product: dict):
    product_id = first_present(product, ["productId", "id", "styleId"])

    if not product_id:
        landing = first_present(product, ["landingPageUrl"], default="")
        if landing:
            parts = str(landing).rstrip("/").split("/")
            for part in reversed(parts):
                if part.isdigit():
                    product_id = part
                    break
            if not product_id:
                product_id = landing

    name = first_present(product, ["productName", "product", "name"], default="")

    link = first_present(product, ["landingPageUrl", "url", "link"], default="")
    if link and not link.startswith("http"):
        link = f"https://www.myntra.com/{link.lstrip('/')}"

    brand = first_present(product, ["brand"], default="")
    if isinstance(brand, dict):
        brand = brand.get("name", "")

    return {
        "product_id": str(product_id) if product_id is not None else None,
        "brand": brand,
        "product_name": name,
        "product_link": link,
    }


# --------------------------------------------------------------------------
# SCRAPE LOGIC (pagination + auto-split, unchanged from price monitor)
# --------------------------------------------------------------------------

def _paginate_one_range(session, category_path, params, url, title, progress_callback=None):
    all_products = []
    page = 1
    blocked = False
    dumped_raw = False
    prev_page_ids = None
    reported_total = None

    while page <= MAX_PAGES_SAFETY:
        msg = f"Fetching page {page}... (Found {len(all_products)} items so far) | URL: {url[:40]}..."
        print(f"  {msg}")
        if progress_callback:
            progress_callback(msg)

        data = fetch_page(session, category_path, params, page, progress_callback)

        if data == "PAGINATION_LIMIT":
            print(f"  Stopping at {len(all_products)} products (Myntra's pagination ceiling reached).")
            break
        if isinstance(data, str) and data.startswith("FETCH_FAILED:"):
            err = data.split(":", 1)[1]
            warn = f"  STOPPED EARLY due to errors after page {page-1}: {err}"
            print(warn, file=sys.stderr)
            if progress_callback:
                progress_callback(f"Stopped early (blocked?) at {len(all_products)} items: {err}")
            blocked = True
            break
        if data is None:
            break

        products, total_count = extract_products(data)
        if reported_total is None and total_count:
            reported_total = total_count
        if not products:
            print("  No more products found, stopping pagination.")
            break

        if DEBUG_RAW and not dumped_raw:
            print("[DEBUG raw product]:")
            print(json.dumps(products[0], indent=2, ensure_ascii=False)[:3000])
            dumped_raw = True

        current_page_ids = {
            first_present(p, ["productId", "id", "styleId"]) for p in products
        }
        if prev_page_ids is not None and current_page_ids == prev_page_ids:
            warn = f"  Page {page} returned identical products to page {page-1} - pagination stuck, stopping."
            print(warn, file=sys.stderr)
            if progress_callback:
                progress_callback(warn)
            break
        prev_page_ids = current_page_ids

        for p in products:
            essentials = extract_essentials(p)
            essentials["_source_url"] = url
            essentials["_source_title"] = title
            all_products.append(essentials)

        has_next = data.get("hasNextPage")
        if has_next is False:
            print("  API reports hasNextPage=False, stopping.")
            break
        if total_count and len(all_products) >= total_count:
            print(f"  Reached reported total of {total_count} products.")
            break
        if has_next is None and len(products) < PRODUCTS_PER_PAGE:
            break

        page += 1
        time.sleep(random.uniform(*DELAY_RANGE))

    return all_products, blocked, reported_total


def _scrape_with_auto_split(session, category_path, params, url, title, progress_callback=None, depth=0):
    products, blocked, reported_total = _paginate_one_range(
        session, category_path, params, url, title, progress_callback
    )

    price_range = _get_price_range(params.get("rf", ""))
    if (
        reported_total
        and len(products) < reported_total
        and not blocked
        and price_range
        and depth < 8
    ):
        low, high = price_range
        mid = round((low + high) / 2, 1)
        if low < mid < high:
            msg = (f"  Myntra reports {reported_total} total products for this "
                   f"query but only {len(products)} were reachable (ceiling hit). "
                   f"Splitting Price {low}-{high} into {low}-{mid} and {mid}-{high}...")
            print(msg)
            if progress_callback:
                progress_callback(msg)

            left_params = dict(params)
            left_params["rf"] = _set_price_range(params.get("rf", ""), low, mid)
            right_params = dict(params)
            right_params["rf"] = _set_price_range(params.get("rf", ""), mid, high)

            left_products, left_blocked, _ = _scrape_with_auto_split(
                session, category_path, left_params, url, title, progress_callback, depth + 1
            )
            right_products, right_blocked, _ = _scrape_with_auto_split(
                session, category_path, right_params, url, title, progress_callback, depth + 1
            )

            merged, seen = [], set()
            for p in left_products + right_products:
                pid = p.get("product_id")
                if pid in seen:
                    continue
                seen.add(pid)
                merged.append(p)
            return merged, (blocked or left_blocked or right_blocked), reported_total

    return products, blocked, reported_total


def scrape_url(session, url, title="", progress_callback=None):
    print(f"\n=== Scraping: {url} ===")
    if progress_callback:
        progress_callback(f"Connecting to: {url[:60]}...")

    try:
        category_path, params = parse_myntra_url(url)
    except ValueError as e:
        msg = f"[SKIP] Invalid URL: {e}"
        print(msg, file=sys.stderr)
        if progress_callback:
            progress_callback(msg)
        return [], False

    all_products, blocked, _ = _scrape_with_auto_split(
        session, category_path, params, url, title, progress_callback
    )

    finish_msg = f"Finished link. Total items extracted: {len(all_products)}"
    if blocked:
        finish_msg += "  (stopped early - looked blocked/rate-limited, NOT a complete result)"
    print(finish_msg)
    if progress_callback:
        progress_callback(finish_msg)

    return all_products, blocked


def scrape_all(urls, progress_callback=None):
    global _WARMED_UP
    _WARMED_UP = False

    session = requests.Session(impersonate="chrome110")

    all_rows = []
    blocked_urls = []
    total_urls = len(urls)

    for idx, entry in enumerate(urls, 1):
        if isinstance(entry, (list, tuple)):
            title, url = entry[0], entry[1]
        else:
            title, url = "", entry

        if progress_callback:
            progress_callback(f"Starting Link {idx} of {total_urls}...")

        rows, blocked = scrape_url(session, url.strip(), title, progress_callback)
        all_rows.extend(rows)
        if blocked:
            blocked_urls.append(url.strip())
        time.sleep(random.uniform(*DELAY_RANGE))

    return all_rows, blocked_urls


# --------------------------------------------------------------------------
# CSV OUTPUT (backup snapshot)
# --------------------------------------------------------------------------

CSV_FIELDS = ["product_id", "brand", "product_name", "product_link", "_source_title", "_source_url"]

def write_csv(rows, path):
    if not rows:
        print("No products scraped. CSV not written.", file=sys.stderr)
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"CSV written: {path} ({len(rows)} products)")


# --------------------------------------------------------------------------
# STOCK CHANGE DETECTION
# --------------------------------------------------------------------------
# `known` is a dict: { product_id: {"product_name":.., "product_link":.., .., "in_stock": bool} }
# loaded from / saved to Postgres in app.py.
#
# Rule (per your spec): a product counts as "back in stock" any time it
# reappears in the listing/search results at all - i.e. it was previously
# marked in_stock=False (because a prior run didn't see it) and now shows
# up again. Brand-new products we've never seen before are just recorded,
# not alerted, since there's nothing to "come back" from.

def detect_stock_changes(rows, known):
    now = datetime.now().isoformat(timespec="seconds")

    current_by_id = {}
    for row in rows:
        pid = row.get("product_id")
        if pid is None:
            continue
        current_by_id[str(pid)] = row

    back_in_stock = []
    newly_out_of_stock = []

    # 1) Anything present in this run
    for pid, row in current_by_id.items():
        prev = known.get(pid)
        if prev is not None and prev.get("in_stock") is False:
            back_in_stock.append({
                "title": row.get("_source_title", ""),
                "product_name": row.get("product_name"),
                "product_link": row.get("product_link"),
                "brand": row.get("brand", ""),
            })
        known[pid] = {
            "product_name": row.get("product_name"),
            "product_link": row.get("product_link"),
            "brand": row.get("brand", ""),
            "source_title": row.get("_source_title", ""),
            "in_stock": True,
            "last_seen": now,
        }

    # 2) Anything that WAS in_stock but is missing from this run -> now OOS
    for pid, prev in known.items():
        if pid in current_by_id:
            continue
        if prev.get("in_stock") is True:
            newly_out_of_stock.append({
                "product_name": prev.get("product_name"),
                "product_link": prev.get("product_link"),
                "brand": prev.get("brand", ""),
                "title": prev.get("source_title", ""),
            })
            prev["in_stock"] = False
            prev["last_out_of_stock"] = now

    print(f"[diag] scraped_rows={len(rows)} | tracked_products={len(known)} "
          f"| back_in_stock={len(back_in_stock)} | newly_oos={len(newly_out_of_stock)}",
          file=sys.stderr)

    return back_in_stock, newly_out_of_stock, known


# --------------------------------------------------------------------------
# ALERTS
# --------------------------------------------------------------------------

def format_restock_message(item):
    title = item.get("title", "")
    brand = item.get("brand", "")
    title_line = f"[{title}]\n" if title else ""
    brand_line = f"{brand}\n" if brand else ""
    return (
        f"BACK IN STOCK\n"
        f"{title_line}"
        f"{brand_line}"
        f"{item['product_name']}\n"
        f"{item['product_link']}"
    )


def alert_terminal(back_in_stock):
    if not back_in_stock:
        print("\nNo restocks this run.")
        return
    print(f"\n{'='*60}\n {len(back_in_stock)} ITEM(S) BACK IN STOCK\n{'='*60}")
    for item in back_in_stock:
        print(format_restock_message(item))
        print("-" * 60)


def alert_telegram(back_in_stock):
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if "PASTE_YOUR" in token or "PASTE_YOUR" in chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for item in back_in_stock:
        text = format_restock_message(item)
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
            if resp.status_code != 200:
                print(f"  [telegram] failed: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        except RequestsError as e:
            print(f"  [telegram] error: {e}", file=sys.stderr)
        time.sleep(0.5)


def send_alerts(back_in_stock):
    alert_terminal(back_in_stock)
    alert_telegram(back_in_stock)


# --------------------------------------------------------------------------
# MAIN RUN
# --------------------------------------------------------------------------

def run_once(urls, progress_callback=None, load_known=None, save_known=None):
    """
    load_known(): returns dict {product_id: {...}}  (from DB)
    save_known(known): persists the dict            (to DB)
    """
    start_msg = f"\n{'#'*70}\n# Run started: {datetime.now().isoformat(timespec='seconds')}\n{'#'*70}"
    print(start_msg)
    if progress_callback:
        progress_callback("Initializing run sequence...")

    rows, blocked_urls = scrape_all(urls, progress_callback)

    if progress_callback:
        progress_callback("Writing data to CSV backup...")
    try:
        write_csv(rows, config.CSV_OUTPUT_PATH)
    except Exception as e:
        print(f"[warning] CSV write failed: {e}", file=sys.stderr)

    if progress_callback:
        progress_callback("Loading known stock status...")
    known = load_known() if load_known else {}

    if progress_callback:
        progress_callback("Detecting restocks against known status...")
    back_in_stock, newly_oos, known = detect_stock_changes(rows, known)

    if save_known:
        save_known(known)

    if progress_callback:
        progress_callback(f"Sending alerts: {len(back_in_stock)} restocked...")
    send_alerts(back_in_stock)

    return rows, back_in_stock, newly_oos, blocked_urls


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_urls_from_file(path):
    if not os.path.exists(path):
        return None
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                title, url = line.split("|", 1)
                title = title.strip()
                url = url.strip()
            else:
                url = line
                title = ""
            urls.append((title, url))
    return urls


def main():
    parser = argparse.ArgumentParser(description="Monitor Myntra listings for restocked items.")
    parser.add_argument("urls", nargs="*", help="Myntra listing URL(s); overrides links.txt if given")
    parser.add_argument("--links-file", default="links.txt", help="Path to file with one Myntra URL per line")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit (no loop)")
    args = parser.parse_args()

    def get_urls():
        if args.urls:
            return args.urls
        urls = load_urls_from_file(args.links_file)
        if urls is None:
            print(f"'{args.links_file}' not found.", file=sys.stderr)
            return None
        return urls

    urls = get_urls()
    if not urls:
        if urls is not None:
            print(f"'{args.links_file}' exists but has no URLs in it.", file=sys.stderr)
        sys.exit(1)

    if args.once:
        run_once(urls)
        return

    interval_seconds = config.CHECK_INTERVAL_HOURS * 3600
    print(f"Starting monitor loop. Checking every {config.CHECK_INTERVAL_HOURS} hour(s). Ctrl+C to stop.")

    while True:
        current_urls = get_urls()
        if not current_urls:
            print("  [warning] no URLs available this cycle, skipping run.", file=sys.stderr)
        else:
            try:
                run_once(current_urls)
            except Exception as e:
                print(f"\n[ERROR] Run failed: {e}", file=sys.stderr)
                traceback.print_exc()

        print(f"\nSleeping for {config.CHECK_INTERVAL_HOURS} hour(s)...")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()

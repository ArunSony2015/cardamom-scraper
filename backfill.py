"""
One-Time Backfill Script — Spices Board Cardamom Auction Archive
=================================================================
Fetches paginated historical data from Spices Board, filters to records
on/after START_DATE, and appends them to data/auction_data.csv.

Safe to run multiple times — uses the same dedup logic as the daily scraper.

Run via GitHub Actions:
  Repo → Actions → "One-time Backfill" → Run workflow → choose start date

After successful run, you can delete this script and the backfill workflow.
The daily scraper continues normally — backfill is a one-shot tool.
"""

import os
import re
import sys
import csv
import time
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ---------- Configuration ----------
DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "auction_data.csv"

# Default start date — override by setting BACKFILL_START env var
START_DATE = os.environ.get("BACKFILL_START", "2026-01-01").strip()

# Maximum number of pages to scan before giving up
# Each page typically contains 12 records (~3 days). 30 pages = ~3 months coverage.
MAX_PAGES = int(os.environ.get("BACKFILL_MAX_PAGES", "30"))

# Polite delay between page fetches (seconds)
PAGE_DELAY = float(os.environ.get("BACKFILL_PAGE_DELAY", "1.5"))

BASE_URL = "https://www.indianspices.com/marketing/price/domestic/daily-price-small.html"

IST = timezone(timedelta(hours=5, minutes=30))

CSV_HEADERS = [
    "captured_at_ist",
    "auction_date",
    "auctioneer",
    "lots",
    "qty_arrived_kg",
    "qty_sold_kg",
    "max_price_inr",
    "avg_price_inr",
    "source",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("backfill")


# ---------- Fetching & parsing ----------
def fetch_page(page_num):
    """Fetch a single archive page. Page 1 is current, higher numbers go further back."""
    url = f"{BASE_URL}?page={page_num}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            log.warning(f"Page {page_num} attempt {attempt+1} failed: {e}")
            time.sleep(2)
    raise RuntimeError(f"Failed to fetch page {page_num}: {last_err}")


def parse_page(html):
    """Extract Small Cardamom records from page HTML. Returns list of dicts."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    pattern = re.compile(
        r"Spice:\s*Small Cardamom\s*,\s*"
        r"Date of Auction:\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4})\s*,\s*"
        r"Auctioneer:\s*(.*?)\s*,\s*"
        r"No\.of lots:\s*([0-9]+)\s*,\s*"
        r"Qty Arrived \(Kgs\):\s*([0-9.]+)\s*,\s*"
        r"Qty Sold \(Kgs\):\s*([0-9.]+)\s*,\s*"
        r"Max Price \(Rs\./Kg\):\s*([0-9.]+)\s*,\s*"
        r"Avg\.\s*Price \(Rs\./Kg\):\s*([0-9.]+)",
        re.IGNORECASE
    )

    records = []
    for m in pattern.finditer(text):
        date_str, auctioneer, lots, qty_arr, qty_sold, max_price, avg_price = m.groups()
        try:
            date_iso = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            log.warning(f"Could not parse date: {date_str} — skipping record")
            continue

        records.append({
            "auction_date": date_iso,
            "auctioneer": auctioneer.strip(),
            "lots": int(lots),
            "qty_arrived_kg": float(qty_arr),
            "qty_sold_kg": float(qty_sold),
            "max_price_inr": float(max_price),
            "avg_price_inr": float(avg_price),
            "source": "spicesboard_archive"
        })
    return records


# ---------- CSV operations ----------
def ensure_csv_exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        log.info(f"Created new CSV at {CSV_PATH}")


def existing_keys():
    keys = set()
    if not CSV_PATH.exists():
        return keys
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("auction_date", "").strip()
            a = row.get("auctioneer", "").strip().upper()
            if d and a:
                keys.add((d, a))
    return keys


def append_records(records):
    if not records:
        return 0
    seen = existing_keys()
    captured_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    new_count = 0

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        for r in records:
            key = (r["auction_date"], r["auctioneer"].upper())
            if key in seen:
                continue
            writer.writerow({
                "captured_at_ist": captured_at,
                "auction_date": r["auction_date"],
                "auctioneer": r["auctioneer"],
                "lots": r["lots"],
                "qty_arrived_kg": r["qty_arrived_kg"],
                "qty_sold_kg": r["qty_sold_kg"],
                "max_price_inr": r["max_price_inr"],
                "avg_price_inr": r["avg_price_inr"],
                "source": r["source"],
            })
            seen.add(key)
            new_count += 1

    return new_count


# ---------- Main backfill loop ----------
def main():
    log.info("=" * 60)
    log.info("CARDAMOM AUCTION BACKFILL — ONE-TIME RUN")
    log.info("=" * 60)
    log.info(f"Target start date: {START_DATE}")
    log.info(f"Max pages to scan: {MAX_PAGES}")

    # Validate start date
    try:
        start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
    except ValueError:
        log.error(f"Invalid BACKFILL_START format: {START_DATE} — must be YYYY-MM-DD")
        return 1

    ensure_csv_exists()

    pre_existing = len(existing_keys())
    log.info(f"Existing records in CSV before backfill: {pre_existing}")

    all_records = []
    pages_with_in_range = 0
    pages_with_zero_in_range_streak = 0

    for page_num in range(1, MAX_PAGES + 1):
        log.info(f"Fetching page {page_num}...")
        try:
            html = fetch_page(page_num)
        except Exception as e:
            log.error(f"Failed page {page_num}: {e}")
            break

        records = parse_page(html)
        if not records:
            log.warning(f"Page {page_num}: 0 records parsed (page might be empty/end-of-archive)")
            pages_with_zero_in_range_streak += 1
            if pages_with_zero_in_range_streak >= 2:
                log.info("Two consecutive empty pages — assuming end of archive. Stopping.")
                break
            continue

        # Filter to records on/after target start date
        in_range = [r for r in records if r["auction_date"] >= START_DATE]
        log.info(f"Page {page_num}: parsed {len(records)} records, {len(in_range)} in target range")

        all_records.extend(in_range)

        # Stop condition: if THIS page had 0 in range but DID have records,
        # we've gone past the start date — stop.
        if len(records) > 0 and len(in_range) == 0:
            log.info(f"Page {page_num} has records but all are before {START_DATE} — stopping.")
            break

        if len(in_range) > 0:
            pages_with_in_range += 1
            pages_with_zero_in_range_streak = 0

        time.sleep(PAGE_DELAY)

    log.info(f"Total records collected from archive: {len(all_records)}")

    # Append (with dedup)
    new_count = append_records(all_records)
    log.info(f"New records added to CSV: {new_count}")
    log.info(f"Skipped (already existed): {len(all_records) - new_count}")

    # Final summary
    if CSV_PATH.exists():
        with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            dates = sorted({row["auction_date"] for row in reader if row.get("auction_date")})

        if dates:
            log.info(f"CSV date range now: {dates[0]} → {dates[-1]}")
            log.info(f"Total unique auction dates: {len(dates)}")
            log.info(f"Total CSV records: {len(existing_keys())}")

    log.info("=" * 60)
    log.info("BACKFILL COMPLETE")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)

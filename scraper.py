"""
Cardamom Auction Daily Scraper (CSV-in-Repo edition)
====================================================
Fetches daily auction data from Spices Board, appends to a CSV file
committed in this same repo, and sends notifications via Email / Telegram / WhatsApp.

No Google Cloud. No service accounts. No IAM. Just a CSV + GitHub.
The CSV lives at: data/auction_data.csv

Designed to run on GitHub Actions (free cloud cron) on a daily schedule.

Sources:
  - Primary:  https://www.indianspices.com/marketing/price/domestic/daily-price-small.html
  - Backup:   https://www.cardamom.auction/cardamom-daily-auction-price.html
"""

import os
import re
import sys
import csv
import smtplib
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------- Configuration ----------
DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "auction_data.csv"

# Email
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO  = os.environ.get("EMAIL_TO", "")

# Telegram
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# WhatsApp via CallMeBot
CMB_PHONE  = os.environ.get("CALLMEBOT_PHONE", "")
CMB_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")

# Sources
PRIMARY_URL = "https://www.indianspices.com/marketing/price/domestic/daily-price-small.html"
BACKUP_URL  = "https://www.cardamom.auction/cardamom-daily-auction-price.html"

# Timezone
IST = timezone(timedelta(hours=5, minutes=30))

# CSV columns
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

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("cardamom")


# ---------- Scraping ----------
def fetch_html(url, timeout=30):
    """Fetch a URL with browser-like headers and reasonable retries."""
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
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            log.warning(f"Fetch attempt {attempt+1} for {url} failed: {e}")
    raise RuntimeError(f"All fetch attempts failed for {url}: {last_err}")


def parse_spices_board(html):
    """Parse Spices Board ticker text. Returns list of dicts (Small Cardamom only)."""
    records = []

    # Strip HTML tags to a flat string
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

    for m in pattern.finditer(text):
        date_str, auctioneer, lots, qty_arr, qty_sold, max_price, avg_price = m.groups()
        try:
            date_iso = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            date_iso = date_str
        records.append({
            "auction_date": date_iso,
            "auctioneer": auctioneer.strip(),
            "lots": int(lots),
            "qty_arrived_kg": float(qty_arr),
            "qty_sold_kg": float(qty_sold),
            "max_price_inr": float(max_price),
            "avg_price_inr": float(avg_price),
            "source": "spicesboard"
        })

    log.info(f"Parsed {len(records)} Small Cardamom records from Spices Board")
    return records


def fetch_auction_data():
    """Try primary source, fall back to secondary."""
    try:
        html = fetch_html(PRIMARY_URL)
        records = parse_spices_board(html)
        if records:
            return records
        log.warning("Primary source returned 0 records — trying backup")
    except Exception as e:
        log.error(f"Primary source failed: {e}")

    try:
        html = fetch_html(BACKUP_URL)
        log.info(f"Backup HTML fetched ({len(html)} chars). Parser not implemented.")
    except Exception as e:
        log.error(f"Backup source also failed: {e}")

    return []


# ---------- CSV operations ----------
def ensure_csv_exists():
    """Create the CSV file with headers if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        log.info(f"Created new CSV at {CSV_PATH}")


def existing_keys():
    """Build set of (auction_date, auctioneer_uppercase) tuples already in CSV."""
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


def append_new_rows(records):
    """Append only records not already in CSV. Returns list of newly-added records."""
    if not records:
        return []

    seen = existing_keys()
    captured_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    new_records = []

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
            new_records.append(r)

    if new_records:
        log.info(f"Appended {len(new_records)} new rows to {CSV_PATH}")
    else:
        log.info("No new records — CSV already up to date")

    return new_records


# ---------- Smart layer: trends ----------
def compute_trend_signals():
    """Compare today's averages with last-5-day average to flag spikes/drops."""
    if not CSV_PATH.exists():
        return None

    from collections import defaultdict
    by_date = defaultdict(list)

    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                d = row["auction_date"].strip()
                v = float(row["avg_price_inr"])
                if d:
                    by_date[d].append(v)
            except (ValueError, KeyError):
                continue

    if len(by_date) < 2:
        return None

    daily_avg = {d: sum(v)/len(v) for d, v in by_date.items()}
    sorted_dates = sorted(daily_avg.keys(), reverse=True)
    if len(sorted_dates) < 2:
        return None

    today_avg = daily_avg[sorted_dates[0]]
    prior = sorted_dates[1:6]
    if not prior:
        return None
    base_avg = sum(daily_avg[d] for d in prior) / len(prior)
    if base_avg == 0:
        return None
    change_pct = (today_avg - base_avg) / base_avg * 100

    signal = "neutral"
    if change_pct >= 3:
        signal = "spike_up"
    elif change_pct <= -3:
        signal = "drop"

    return {
        "today_avg": today_avg,
        "base_5d_avg": base_avg,
        "change_pct": change_pct,
        "signal": signal,
        "today_date": sorted_dates[0],
    }


def build_message(new_records, signals):
    """Compose a concise notification message."""
    if not new_records:
        return None

    today = new_records[0]["auction_date"]
    lines = [f"🌿 *Cardamom Auction Update — {today}*", ""]

    for r in new_records:
        lines.append(
            f"• {r['auctioneer'][:50]}\n"
            f"  Avg ₹{r['avg_price_inr']:.0f}/kg · Max ₹{r['max_price_inr']:.0f}/kg · "
            f"{r['lots']} lots · {r['qty_sold_kg']:,.0f} kg sold"
        )

    if signals:
        lines.append("")
        if signals["signal"] == "spike_up":
            lines.append(
                f"🔺 *PRICE SPIKE*: today avg ₹{signals['today_avg']:.0f} is "
                f"+{signals['change_pct']:.1f}% vs 5-day avg ₹{signals['base_5d_avg']:.0f}"
            )
            lines.append("→ Action: close pending quotes today before exporters re-quote.")
        elif signals["signal"] == "drop":
            lines.append(
                f"🔻 *PRICE DROP*: today avg ₹{signals['today_avg']:.0f} is "
                f"{signals['change_pct']:.1f}% vs 5-day avg ₹{signals['base_5d_avg']:.0f}"
            )
            lines.append("→ Action: buying window — consider locking inventory.")
        else:
            lines.append(
                f"~ Steady: today avg ₹{signals['today_avg']:.0f} vs 5-day avg "
                f"₹{signals['base_5d_avg']:.0f} ({signals['change_pct']:+.1f}%)"
            )

    return "\n".join(lines)


# ---------- Notifications ----------
def send_email(subject, body):
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        log.info("Email not configured — skipping")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [a.strip() for a in EMAIL_TO.split(",")], msg.as_string())
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


def send_telegram(text):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log.info("Telegram not configured — skipping")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=15)
        r.raise_for_status()
        log.info("Telegram sent")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


def send_whatsapp(text):
    if not (CMB_PHONE and CMB_APIKEY):
        log.info("WhatsApp (CallMeBot) not configured — skipping")
        return
    try:
        clean = text.replace("*", "")
        url = "https://api.callmebot.com/whatsapp.php"
        r = requests.get(url, params={
            "phone": CMB_PHONE,
            "text": clean,
            "apikey": CMB_APIKEY
        }, timeout=20)
        r.raise_for_status()
        log.info("WhatsApp sent")
    except Exception as e:
        log.error(f"WhatsApp failed: {e}")


# ---------- Main ----------
def main():
    log.info("=== Cardamom Auction Scraper started ===")

    ensure_csv_exists()

    records = fetch_auction_data()
    if not records:
        log.warning("No auction records found. Possibly no auction today.")
        return 0

    new_records = append_new_rows(records)

    if not new_records:
        log.info("Nothing new — exiting silently.")
        return 0

    signals = compute_trend_signals()
    message = build_message(new_records, signals)

    if message:
        log.info("Message:\n" + message)
        send_email(f"Cardamom Auction — {new_records[0]['auction_date']}", message)
        send_telegram(message)
        send_whatsapp(message)

    log.info("=== Done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)

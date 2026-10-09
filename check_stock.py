#!/usr/bin/env python3
"""
check_stock.py — runs on a GitHub Actions schedule (see .github/workflows/check_stock.yml).

Watches one OR MORE Amazon.ae product pages (comma-separated ASINS), decides
for each whether it's in stock, and emails an alert (once per restock event,
per product) with a direct add-to-cart link.

Optional seller filter (ONLY_AMAZON_SELLER, default "true"): only alert when
the buy box says the item is sold by Amazon. If the seller can't be read from
the page, it alerts anyway and says "seller unverified" in the email — a
parsing miss should never silently swallow a real restock.

This does NOT log into Amazon, does NOT add anything to a cart, and does NOT
place any order. It only reads public product pages.

Runs as a LOOP: GitHub's cron can't reliably go below ~5 minutes, so this
checks every POLL_INTERVAL_SECONDS (default 60s) for up to MAX_RUNTIME_MINUTES,
then exits cleanly so the next scheduled trigger can take over.
"""

import json
import os
import random
import re
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def _env(name, default=""):
    """os.environ.get that also treats an empty string as 'not set' (GitHub
    passes unset repo variables as empty strings, not as missing)."""
    return os.environ.get(name) or default


# Accept ASINS="B0AAA,B0BBB" (preferred) or the old single ASIN="B0AAA".
ASINS = [a.strip().upper() for a in _env("ASINS", _env("ASIN")).split(",") if a.strip()]
if not ASINS:
    sys.exit("No products configured: set the ASINS repo variable (comma-separated).")

DOMAIN = _env("AMAZON_DOMAIN", "amazon.ae")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_EMAIL_TO = os.environ["ALERT_EMAIL_TO"]

POLL_INTERVAL_SECONDS = int(_env("POLL_INTERVAL_SECONDS", "60"))
MAX_RUNTIME_MINUTES = int(_env("MAX_RUNTIME_MINUTES", "230"))  # ~3h50m
ONLY_AMAZON_SELLER = _env("ONLY_AMAZON_SELLER", "true").lower() in ("1", "true", "yes")
COMMIT_EVERY_MINUTES = 15  # how often to push state.json just for freshness
TEST_EMAIL = _env("TEST_EMAIL").lower() in ("1", "true", "yes")  # send one test email, then exit

STATE_FILE = Path(__file__).parent / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

UNAVAILABLE_PHRASES = ["currently unavailable", "out of stock", "we don't know when"]
AVAILABLE_HINTS = ["in stock", "add to cart"]


def product_url(asin):
    return f"https://www.{DOMAIN}/dp/{asin}"


def add_to_cart_url(asin):
    return f"https://www.{DOMAIN}/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"


# ---------------------------------------------------------------- state ----
def load_state():
    """State is {"products": {ASIN: {notified, last_status, last_checked}}}.
    An old single-product state file (top-level "notified") is dropped — at
    worst that costs one duplicate email, never a missed one."""
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    if "products" not in state:
        state = {"products": {}}
    for asin in ASINS:
        state["products"].setdefault(
            asin, {"notified": False, "last_status": "unknown", "last_checked": None}
        )
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def commit_state(reason):
    """Best-effort commit + push of state.json. Never fatal: a failed push just
    means the next one picks it up; crashing would stop monitoring for hours."""
    try:
        subprocess.run(["git", "add", "state.json"], check=True, cwd=STATE_FILE.parent)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=STATE_FILE.parent).returncode == 0:
            return  # nothing changed
        subprocess.run(
            ["git", "commit", "-m", f"Update stock state [skip ci]: {reason}"],
            check=True, cwd=STATE_FILE.parent,
        )
        subprocess.run(["git", "push"], check=True, cwd=STATE_FILE.parent)
        print(f"Committed state.json ({reason}).")
    except subprocess.CalledProcessError as e:
        print(f"git commit/push failed (non-fatal): {e}", file=sys.stderr)


# ------------------------------------------------------------- page logic --
def determine_stock_status(soup):
    """True = in stock, False = out of stock, None = couldn't tell (likely a
    CAPTCHA/block page) — inconclusive, so no action is taken on it."""
    if soup.find("form", {"action": lambda v: v and "validateCaptcha" in v}):
        return None

    availability = soup.find(id="availability")
    availability_text = availability.get_text(" ", strip=True).lower() if availability else ""
    add_to_cart_btn = soup.find(id="add-to-cart-button")
    page_text = soup.get_text(" ", strip=True).lower()

    if any(p in availability_text for p in UNAVAILABLE_PHRASES):
        return False
    if add_to_cart_btn is not None:
        return True
    if availability_text and any(h in availability_text for h in AVAILABLE_HINTS):
        return True
    if "add to cart" in page_text and "currently unavailable" not in page_text:
        return True
    return None


def get_title(soup, asin):
    t = soup.find(id="productTitle")
    return t.get_text(" ", strip=True) if t else asin


def get_seller(soup):
    """Returns the seller name as lowercase text, or None if it can't be read.
    Looks at the buy box 'Sold by' line; strips trailing 'fulfilled by' /
    'ships from' text so 'Sold by SomeStore and Fulfilled by Amazon' doesn't
    read as Amazon."""
    for element_id in ("merchant-info", "tabular-buybox", "sellerProfileTriggerId"):
        el = soup.find(id=element_id)
        if not el:
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        m = re.search(r"sold by\s+(.+)", text, flags=re.IGNORECASE)
        if m:
            seller = re.split(r"\s+and\s+fulfilled|fulfilled by|ships from|\||\.\s", m.group(1), flags=re.IGNORECASE)[0]
            return seller.strip().lower()
        if element_id == "sellerProfileTriggerId":
            return el.get_text(" ", strip=True).lower()
    return None


def seller_is_amazon(seller):
    return seller is not None and "amazon" in seller


# ---------------------------------------------------------------- email ----
def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_EMAIL_TO

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls(context=context)
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [ALERT_EMAIL_TO], msg.as_string())


# ----------------------------------------------------------- check logic ---
def check_product(asin, pstate):
    """One check of one product. Mutates pstate. Returns True if state changed
    in a way worth committing right away."""
    url = product_url(asin)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        print(f"[{asin}] request failed: {e}", file=sys.stderr)
        return False
    if resp.status_code != 200:
        print(f"[{asin}] non-200 response: {resp.status_code}", file=sys.stderr)
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    status = determine_stock_status(soup)
    pstate["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if status is None:
        print(f"[{asin}] inconclusive (possible CAPTCHA/block) — flag unchanged.")
        pstate["last_status"] = "unknown"
        return False

    if not status:
        pstate["last_status"] = "out_of_stock"
        if pstate.get("notified"):
            print(f"[{asin}] back out of stock — resetting notification flag.")
            pstate["notified"] = False
            return True
        return False

    # In stock from here on.
    seller = get_seller(soup)
    if ONLY_AMAZON_SELLER and seller is not None and not seller_is_amazon(seller):
        # Third-party stock only: don't alert, and don't set notified, so we
        # still alert later if Amazon itself takes the buy box.
        pstate["last_status"] = "in_stock_third_party"
        print(f"[{asin}] in stock but sold by '{seller}', not Amazon — skipping alert.")
        return False

    pstate["last_status"] = "in_stock"
    if pstate.get("notified"):
        print(f"[{asin}] still in stock, already notified — no email.")
        return False

    seller_line = (
        f"Seller: {seller}" if seller is not None
        else "Seller: could not be verified from the page — check 'Sold by' before buying"
    )
    title = get_title(soup, asin)
    body = (
        f"{title}\n"
        f"is now showing as in stock on {DOMAIN}.\n\n"
        f"{seller_line}\n\n"
        f"Product page: {url}\n"
        f"Quick add-to-cart link: {add_to_cart_url(asin)}\n\n"
        f"(The quick add-to-cart link is Amazon's own query-string feature — it "
        f"should drop the item into your cart when you're logged in, but verify "
        f"it lands correctly the first time.)\n"
    )
    try:
        send_email(f"Back in stock: {title[:80]}", body)
    except Exception as e:  # SMTP/auth failure: keep flag False so we retry next cycle
        print(f"[{asin}] EMAIL FAILED ({e}) — will retry next check.", file=sys.stderr)
        return False

    pstate["notified"] = True
    print(f"[{asin}] sent restock alert email.")
    return True


def main():
    if TEST_EMAIL:
        # One-off check that the Gmail credentials work. A failure raises, so the
        # GitHub run shows red with the SMTP error (e.g. 535 = bad app password).
        send_email(
            "Stock alert bot: test email",
            "If you can read this, the bot can send you alerts.\n\n"
            f"Watching: {', '.join(ASINS)} on {DOMAIN}",
        )
        print(f"Test email sent to {ALERT_EMAIL_TO}.")
        return

    state = load_state()
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=MAX_RUNTIME_MINUTES)
    last_commit = start

    print(
        f"Monitoring {len(ASINS)} product(s) on {DOMAIN}: {', '.join(ASINS)} | "
        f"every {POLL_INTERVAL_SECONDS}s | Amazon-only seller filter: {ONLY_AMAZON_SELLER} | "
        f"until {end.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )

    while datetime.now(timezone.utc) < end:
        important = False
        for asin in ASINS:
            try:
                if check_product(asin, state["products"][asin]):
                    important = True
            except Exception as e:  # one bad product must never stop the others
                print(f"[{asin}] unexpected error: {e!r}", file=sys.stderr)
            time.sleep(random.uniform(1, 3))  # don't hit all pages in the same instant
        save_state(state)

        now = datetime.now(timezone.utc)
        if important:
            commit_state("alert sent / flag reset")
            last_commit = now
        elif (now - last_commit).total_seconds() >= COMMIT_EVERY_MINUTES * 60:
            commit_state("periodic freshness update")
            last_commit = now

        # Jitter avoids a perfectly predictable request cadence.
        time.sleep(max(5, POLL_INTERVAL_SECONDS + random.uniform(-5, 5)))

    save_state(state)
    commit_state("loop ending, handing off to next scheduled run")
    print("Loop budget reached — exiting so the next scheduled run can take over.")


if __name__ == "__main__":
    main()
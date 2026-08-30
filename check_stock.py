#!/usr/bin/env python3
"""
check_stock.py — runs on a GitHub Actions schedule (see .github/workflows/check_stock.yml).
Fetches an Amazon.ae product page, decides if it's in stock, and emails an alert
(once per restock event) with a direct add-to-cart link.

This does NOT log into Amazon, does NOT add anything to a cart, and does NOT
place any order. It only reads a public product page.

Runs as a LOOP: GitHub's cron can't reliably go below ~5 minutes, so instead
this checks every POLL_INTERVAL_SECONDS (default 60s) in a loop for up to
MAX_RUNTIME_MINUTES, then exits cleanly so the next scheduled trigger can
take over. See the workflow file for the schedule that keeps these loops
chained together through the day.
"""

import json
import os
import random
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

ASIN = os.environ["ASIN"]
DOMAIN = os.environ.get("AMAZON_DOMAIN", "amazon.ae")
PRODUCT_URL = os.environ.get("PRODUCT_URL", f"https://www.{DOMAIN}/dp/{ASIN}")
ADD_TO_CART_URL = f"https://www.{DOMAIN}/gp/aws/cart/add.html?ASIN.1={ASIN}&Quantity.1=1"

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_EMAIL_TO = os.environ["ALERT_EMAIL_TO"]

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS") or "60")
MAX_RUNTIME_MINUTES = int(os.environ.get("MAX_RUNTIME_MINUTES") or "230")  # ~3h50m
COMMIT_EVERY_MINUTES = 15  # how often to push state.json just for freshness

STATE_FILE = Path(__file__).parent / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

UNAVAILABLE_PHRASES = [
    "currently unavailable",
    "out of stock",
    "we don't know when",
]
AVAILABLE_HINTS = [
    "in stock",
    "add to cart",
]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified": False, "last_status": "unknown", "last_checked": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_page():
    resp = requests.get(PRODUCT_URL, headers=HEADERS, timeout=20)
    return resp


def determine_stock_status(html):
    """
    Returns True (in stock), False (out of stock), or None (couldn't tell —
    likely a CAPTCHA/block page, treat as inconclusive and don't act on it).
    """
    soup = BeautifulSoup(html, "html.parser")

    if soup.find("form", {"action": lambda v: v and "validateCaptcha" in v}):
        return None  # blocked / CAPTCHA page

    availability = soup.find(id="availability")
    availability_text = availability.get_text(" ", strip=True).lower() if availability else ""

    add_to_cart_btn = soup.find(id="add-to-cart-button")
    buybox_text = soup.get_text(" ", strip=True).lower()

    if any(phrase in availability_text for phrase in UNAVAILABLE_PHRASES):
        return False

    if add_to_cart_btn is not None:
        return True

    if availability_text and any(hint in availability_text for hint in AVAILABLE_HINTS):
        return True

    # Ambiguous — page loaded but neither a clear "unavailable" nor a clear
    # add-to-cart button was found. Treat as inconclusive rather than guess.
    if "add to cart" in buybox_text and "currently unavailable" not in buybox_text:
        return True

    return None


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_EMAIL_TO

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [ALERT_EMAIL_TO], msg.as_string())


def commit_state(reason):
    """Commit + push state.json. Best-effort — a failed push just means the
    next commit (or the next scheduled run) picks it up; it never crashes
    the loop, since missing one commit is harmless but crashing would stop
    monitoring for hours."""
    try:
        subprocess.run(["git", "add", "state.json"], check=True, cwd=STATE_FILE.parent)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=STATE_FILE.parent
        )
        if result.returncode == 0:
            return  # nothing changed, skip an empty commit
        subprocess.run(
            ["git", "commit", "-m", f"Update stock state [skip ci]: {reason}"],
            check=True,
            cwd=STATE_FILE.parent,
        )
        subprocess.run(["git", "push"], check=True, cwd=STATE_FILE.parent)
        print(f"Committed state.json ({reason}).")
    except subprocess.CalledProcessError as e:
        print(f"git commit/push failed (non-fatal): {e}", file=sys.stderr)


def check_once(state):
    """Runs a single check. Mutates and returns state. Returns None on a
    transient failure (network error / non-200 / CAPTCHA) without touching
    the notified flag, so a bad check never causes a false reset or a
    missed/duplicate email."""
    try:
        resp = fetch_page()
    except requests.RequestException as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return state

    if resp.status_code != 200:
        print(f"Non-200 response: {resp.status_code}", file=sys.stderr)
        return state

    status = determine_stock_status(resp.text)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if status is None:
        print("Stock status inconclusive (possible CAPTCHA/block) — leaving notified flag unchanged.")
        state["last_checked"] = now_str
        state["last_status"] = "unknown"
        return state

    state["last_checked"] = now_str
    state["last_status"] = "in_stock" if status else "out_of_stock"

    if status and not state.get("notified"):
        subject = "Back in stock: your Amazon.ae item"
        body = (
            f"The product you're tracking is now showing as in stock.\n\n"
            f"Product page: {PRODUCT_URL}\n"
            f"Quick add-to-cart link: {ADD_TO_CART_URL}\n\n"
            f"(The quick add-to-cart link is Amazon's own query-string feature — "
            f"it should drop the item straight into your cart when you're logged "
            f"in, but double check it lands correctly the first time.)\n"
        )
        send_email(subject, body)
        state["notified"] = True
        print("Sent restock alert email.")
        commit_state("restock alert sent")
    elif not status:
        if state.get("notified"):
            print("Item went back out of stock — resetting notification flag.")
            state["notified"] = False
            commit_state("back out of stock, flag reset")
    else:
        print("Still in stock, already notified — no email sent.")

    return state


def main():
    state = load_state()
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=MAX_RUNTIME_MINUTES)
    last_commit = start

    print(
        f"Starting monitor loop: every {POLL_INTERVAL_SECONDS}s, "
        f"until {end.strftime('%Y-%m-%d %H:%M:%S UTC')}."
    )

    while datetime.now(timezone.utc) < end:
        state = check_once(state)
        save_state(state)

        now = datetime.now(timezone.utc)
        if (now - last_commit).total_seconds() >= COMMIT_EVERY_MINUTES * 60:
            commit_state("periodic freshness update")
            last_commit = now

        # Jitter avoids hitting Amazon at an exact, perfectly predictable cadence.
        time.sleep(max(5, POLL_INTERVAL_SECONDS + random.uniform(-5, 5)))

    save_state(state)
    commit_state("loop ending, handing off to next scheduled run")
    print("Loop budget reached — exiting so the next scheduled run can take over.")


if __name__ == "__main__":
    main()
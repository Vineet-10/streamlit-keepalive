"""
Streamlit Keepalive Script
==========================
Visits one or more Streamlit Community Cloud apps and wakes them
if they have entered hibernation mode.

KNOWN FAILURE MODES HANDLED:
  1. App takes >30s to load         → generous timeouts + wait_for_load_state
  2. Sleep button text changes       → multiple selector fallbacks
  3. Chromium crashes in CI          → --no-sandbox + --disable-dev-shm-usage flags
  4. Flaky network / transient 5xx   → 3-attempt retry with exponential backoff
  5. Page is still "loading" (spinner) vs "sleeping" (button) → separate detection
  6. No URLs provided                → fail fast with clear error
  7. One URL fails, others shouldn't → per-URL isolation + summary at end
  8. Silent failures in CI           → screenshots uploaded as artifacts every run

IMPORTANT (as of March 2025):
  - Streamlit Community Cloud now sleeps apps after ONLY 12 hours of inactivity
    (reduced from 24h). Run this workflow at least every 8 hours to be safe.
  - Commits no longer wake apps (changed April 2025). You must visit the URL.
"""

import os
import sys
import time
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# Comma-separated list of Streamlit URLs (set as a GitHub Secret)
RAW_URLS = os.environ.get("STREAMLIT_URLS", "")

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [0, 15, 30]   # Wait before attempt 1, 2, 3
PAGE_TIMEOUT_MS = 90_000              # 90s — Streamlit apps can be slow to boot
NAV_TIMEOUT_MS = 90_000

SCREENSHOTS_DIR = "screenshots"

# ── Selector bank ─────────────────────────────────────────────────────────────
# Streamlit Community Cloud sleep page buttons, in order of reliability.
# Using multiple because Streamlit has updated their UI several times.
WAKE_SELECTORS = [
    # Primary: exact visible text (most reliable)
    "text=Yes, get this app back up!",
    # Secondary: button containing that text (handles wrapping)
    "button:has-text('Yes, get this app back up!')",
    # Tertiary: partial match in case Streamlit shortens the label
    "button:has-text('get this app back up')",
    # Quaternary: any button on the sleep/hibernate interstitial page
    "button:has-text('Wake up')",
    "button:has-text('Rerun')",
]

# Text that indicates the app is in Streamlit's hibernation interstitial
SLEEP_INDICATORS = [
    "This app has gone to sleep",
    "gone to sleep",
    "App is not running",
    "Zzzz",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def take_screenshot(page, label: str) -> None:
    """Save a PNG screenshot for CI artifact upload (silent on failure)."""
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = datetime.utcnow().strftime("%H%M%S")
        path = f"{SCREENSHOTS_DIR}/{label}_{ts}.png"
        page.screenshot(path=path, full_page=True)
        logger.info(f"📸 Screenshot → {path}")
    except Exception as exc:
        logger.warning(f"Could not save screenshot: {exc}")


def detect_sleep_state(page) -> bool:
    """
    Return True if the page looks like Streamlit's hibernation interstitial.

    Strategy:
      1. Check for known button selectors (fastest)
      2. Scan page text for sleep-indicator strings (fallback)
    """
    # Fast path: is the wake button visible?
    for sel in WAKE_SELECTORS:
        try:
            if page.is_visible(sel, timeout=2_000):
                logger.info(f"  ↳ Sleep detected via selector: '{sel}'")
                return True
        except Exception:
            pass

    # Slow path: read page text
    try:
        content = page.content()
        for phrase in SLEEP_INDICATORS:
            if phrase.lower() in content.lower():
                logger.info(f"  ↳ Sleep detected via page text: '{phrase}'")
                return True
    except Exception:
        pass

    return False


def click_wake_button(page) -> bool:
    """
    Try each selector in order and click the first visible one.
    Returns True if a button was successfully clicked.
    """
    for sel in WAKE_SELECTORS:
        try:
            if page.is_visible(sel, timeout=2_000):
                page.click(sel, timeout=10_000)
                logger.info(f"  ↳ Clicked wake button: '{sel}'")
                return True
        except Exception as exc:
            logger.debug(f"  Selector failed ({sel}): {exc}")

    logger.warning("  ↳ No wake button found/clickable despite sleep detection.")
    return False


def visit_once(browser, url: str, attempt: int) -> bool:
    """
    Open a new browser context, visit the URL, optionally wake it.
    Returns True on success, False on any failure.
    Isolated context means cookies/state from previous attempts don't interfere.
    """
    context = browser.new_context(
        # Realistic UA reduces chance of bot-detection or caching quirks
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=False,
    )
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

    slug = url.split("//")[-1][:40]   # short label for logs/screenshots

    try:
        logger.info(f"  [attempt {attempt}] → {url}")

        response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

        if response is None:
            logger.error("  ↳ Navigation returned no response object.")
            take_screenshot(page, f"{slug}_no_response_a{attempt}")
            return False

        http_status = response.status
        logger.info(f"  ↳ HTTP {http_status}")

        if http_status >= 500:
            logger.error(f"  ↳ Server error ({http_status}), will retry.")
            take_screenshot(page, f"{slug}_server_error_a{attempt}")
            return False

        # Give JS a moment to render the React/Streamlit shell
        page.wait_for_timeout(4_000)
        take_screenshot(page, f"{slug}_loaded_a{attempt}")

        if detect_sleep_state(page):
            logger.info("  ↳ App is sleeping — attempting wake…")
            woke = click_wake_button(page)
            if not woke:
                take_screenshot(page, f"{slug}_wake_failed_a{attempt}")
                return False

            # Wait for Streamlit to reboot (can take 20-40s on cold start)
            logger.info("  ↳ Waiting for app to reboot (up to 60s)…")
            try:
                page.wait_for_function(
                    # True once the sleep indicators are gone from the DOM
                    """() => !document.body.innerText.includes('gone to sleep')
                          && !document.body.innerText.includes('App is not running')""",
                    timeout=60_000,
                )
            except PlaywrightTimeoutError:
                logger.warning("  ↳ Timed out waiting for reboot; checking screenshot…")

            page.wait_for_timeout(3_000)
            take_screenshot(page, f"{slug}_after_wake_a{attempt}")
            logger.info(f"  ✅ Woke up: {url}")
        else:
            logger.info(f"  ✅ Already awake: {url}")

        return True

    except PlaywrightTimeoutError as exc:
        logger.error(f"  ↳ Timeout: {exc}")
        take_screenshot(page, f"{slug}_timeout_a{attempt}")
        return False

    except Exception as exc:
        logger.error(f"  ↳ Unexpected error: {exc}")
        try:
            take_screenshot(page, f"{slug}_error_a{attempt}")
        except Exception:
            pass
        return False

    finally:
        context.close()


def process_url(browser, url: str) -> bool:
    """Wrap visit_once with retry-and-backoff logic."""
    url = url.strip()
    if not url:
        return True   # skip blanks silently

    logger.info(f"\n{'─'*60}")
    logger.info(f"Processing: {url}")
    logger.info(f"{'─'*60}")

    for attempt in range(1, MAX_RETRIES + 1):
        wait = RETRY_BACKOFF_SECONDS[attempt - 1]
        if wait:
            logger.info(f"  Waiting {wait}s before retry…")
            time.sleep(wait)

        if visit_once(browser, url, attempt):
            return True

        logger.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed.")

    logger.error(f"❌ Giving up on: {url}")
    return False


def main() -> None:
    urls = [u.strip() for u in RAW_URLS.split(",") if u.strip()]

    if not urls:
        logger.error(
            "STREAMLIT_URLS environment variable is empty or not set.\n"
            "Set it as a GitHub Secret containing one or more comma-separated URLs.\n"
            "Example:  https://myapp.streamlit.app,https://otherapp.streamlit.app"
        )
        sys.exit(1)

    logger.info(f"{'='*60}")
    logger.info(f"Streamlit Keepalive — {datetime.utcnow().isoformat()}Z")
    logger.info(f"Apps to check: {len(urls)}")
    logger.info(f"{'='*60}")

    results: dict[str, bool] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",               # Required in GitHub Actions (no root ns)
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",    # /dev/shm is tiny in many CI envs
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",               # Avoids zombie child processes
                "--disable-gpu",
                "--disable-extensions",
                "--single-process",          # Safer inside Docker/CI containers
            ],
        )

        for url in urls:
            results[url] = process_url(browser, url)

        browser.close()

    # ── Final summary ─────────────────────────────────────────────────────────
    logger.info(f"\n{'='*60}")
    logger.info("FINAL SUMMARY")
    logger.info(f"{'='*60}")

    all_ok = True
    for url, ok in results.items():
        icon = "✅" if ok else "❌"
        logger.info(f"  {icon}  {url}")
        if not ok:
            all_ok = False

    if not all_ok:
        logger.error("One or more apps could not be woken. Check screenshots artifact.")
        sys.exit(1)   # Makes the GitHub Actions step red

    logger.info("All apps OK 🎉")


if __name__ == "__main__":
    main()

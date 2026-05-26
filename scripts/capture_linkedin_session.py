#!/usr/bin/env python3
"""One-time: log into LinkedIn in a real browser and save session for Phase 3 scraper.

After saving, run scrapes with:
  python scripts/scrape_linkedin_companies.py --companies "Aircover" "Salesforce"

Session file default: data/linkedin_session.json (auto-detected by scraper).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
DEFAULT_OUTPUT = _PROJECT_ROOT / "data" / "linkedin_session.json"


def _has_li_at(storage_path: Path) -> bool:
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        for c in data.get("cookies", []):
            if c.get("name") == "li_at" and c.get("value"):
                return True
    except Exception:
        pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write Playwright storage state (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is required. Install:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium",
            file=sys.stderr,
        )
        return 1

    out_path: Path = args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("Opening Chromium — log into LinkedIn in the window that appears.")
    print("Use email/password OR 'Continue with Microsoft/Google' in that window.")
    print("Complete 2FA if prompted.")
    print("When you see LinkedIn home/feed (NOT the login form), press ENTER here.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        try:
            input("Press ENTER after you are logged in... ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled — session not saved.")
            browser.close()
            return 1

        # Save current browser cookies immediately (do not require /feed navigation)
        context.storage_state(path=str(out_path))

        # Optional check — never fail the save if this errors (SSO redirects are common)
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            if "/login" in page.url or "/uas/login" in page.url:
                print(
                    "Warning: browser still shows login URL. "
                    "If scrape fails, log in again and re-run this script.",
                    file=sys.stderr,
                )
            else:
                print("Feed loaded — refreshing saved session.")
                context.storage_state(path=str(out_path))
        except Exception as exc:
            print(f"Note: feed check skipped ({type(exc).__name__}). Session was still saved.")

        browser.close()

    if not out_path.exists():
        print("Error: session file was not written.", file=sys.stderr)
        return 1

    if not _has_li_at(out_path):
        print(
            "Warning: saved file has no li_at cookie. "
            "You may not be logged in — run again and finish login before ENTER.",
            file=sys.stderr,
        )
        return 1

    print(f"\nSaved session to:\n  {out_path.resolve()}")
    print("\nNext — extract posts with LinkedIn Phase 3:")
    print("  python main.py posts --full --linkedin")
    print("\nSubset example:")
    print('  python main.py posts --companies "Gong" "Salesforce" --linkedin')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Extract Gartner CSO & Sales Leader Conference (2026) LinkedIn posts for all
exhibitors in linkedin_urls.csv.

Includes:
  - Official company-page posts
  - Relevant employee posts from the same organization

Excludes:
  - Posts authored by rival exhibitor company pages
  - Unrelated Gartner mentions (Magic Quadrant, other summits, generic research)
  - Third-party posts with no link to the target company

Every company appears in the output. Companies with no qualifying posts get one row
with status "No posts found".

Usage:
  python scripts/extract_gartner_cso_posts.py --full
  python scripts/extract_gartner_cso_posts.py --companies "Varicent" "AuctusIQ"
  python scripts/extract_gartner_cso_posts.py --full --linkedin   # also scrape /posts/
  python scripts/extract_gartner_cso_posts.py --full --mine-only  # reuse saved CSVs only

Requirements:
  pip install ddgs requests beautifulsoup4
  Optional: playwright + data/linkedin_session.json for --linkedin
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        DDGS = None  # type: ignore

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from linkedin_extractor.config import (  # noqa: E402
    CONFERENCE_YEAR,
    LINKEDIN_URLS_CSV,
    POSTS_OUTPUT_CSV,
    POSTS_SUMMARY_TXT,
    SESSION_FILE,
)

INPUT_CSV = LINKEDIN_URLS_CSV
OUTPUT_CSV = POSTS_OUTPUT_CSV
LOG_FILE = _ROOT / "data" / "gartner_cso_posts_extract_log.txt"
SUMMARY_FILE = POSTS_SUMMARY_TXT

SCRAPE_DATE = date.today()
YEAR_FILTER = CONFERENCE_YEAR

OUTPUT_COLS = [
    "company_name",
    "linkedin_url",
    "post_date",
    "post_text",
    "post_link",
    "posted_by",
    "status",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_LI_POST_RE = re.compile(
    r"linkedin\.com/posts/[a-zA-Z0-9_%-]+_[a-zA-Z0-9_%-]+-activity-\d+",
    re.I,
)

GARTNER_EVENT_KW = (
    "gartner cso",
    "gartner® cso",
    "gartner sales leader",
    "cso & sales leader",
    "cso and sales leader",
    "sales leader conference",
    "chief sales officer",
    "#gartnercso",
    "#gartnersales",
    f"#gartnercso{CONFERENCE_YEAR}",
    "gartnercso",
    "gartnersales",
    "gartner cso & sales",
)

UNRELATED_GARTNER_KW = (
    "magic quadrant",
    "security & risk",
    "security and risk",
    "leadership vision for cso",
    "gartner for sales",
    "gartner research shows",
    "according to gartner",
    "gartner named a leader",
    "gartner report",
    "gartner magic",
)

THIRD_PARTY_AUTHORS = frozenset({
    "gartner",
    "gartner-for-sales",
    "gartner-inc",
    "linkedin-news",
})

# Optional Phase 1 inputs: add paths here or pass --mine-csv (see main / argparse).
MINE_SOURCES: list[Path] = []

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Company:
    company_name: str
    linkedin_company_url: str
    booth: str = ""
    slug: str = ""

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = linkedin_slug(self.linkedin_company_url)


@dataclass
class PostHit:
    company_name: str
    linkedin_url: str
    post_link: str
    post_text: str
    post_date: str
    posted_by: str
    source: str = "search"


# ---------------------------------------------------------------------------
# URL / slug helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_url(url: str) -> str:
    u = (url or "").split("?")[0].split("#")[0].rstrip("/")
    m = re.search(
        r"(https://www\.linkedin\.com/posts/[a-zA-Z0-9_%-]+_[a-zA-Z0-9_%-]+-activity-\d+)",
        u,
        re.I,
    )
    return m.group(1) if m else u


def linkedin_slug(url: str) -> str:
    m = re.search(r"linkedin\.com/company/([^/?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def post_author_slug(post_url: str) -> str:
    m = re.search(r"linkedin\.com/posts/([a-zA-Z0-9_-]+?)_", post_url or "", re.I)
    return m.group(1).lower() if m else ""


def is_li_post(url: str) -> bool:
    return bool(url and _LI_POST_RE.search(url))


def normalize_company_name(name: str) -> str:
    return name.strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# Company index (rival detection + matching)
# ---------------------------------------------------------------------------

def load_companies(path: Path) -> list[Company]:
    rows: list[Company] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("linkedin_company_url") or "").strip()
            if not url or "/company/" not in url.lower():
                continue
            rows.append(
                Company(
                    company_name=normalize_company_name(row["company_name"]),
                    linkedin_company_url=url,
                    booth=(row.get("booth") or "").strip(),
                )
            )
    return rows


def build_slug_to_company(companies: list[Company]) -> dict[str, str]:
    """LinkedIn vanity slug -> exhibitor company name."""
    out: dict[str, str] = {}
    for c in companies:
        if c.slug:
            out[c.slug] = c.company_name
    return out


def resolve_author_to_exhibitor(author: str, slug_map: dict[str, str]) -> str | None:
    """If post author is an exhibitor company page, return that company name."""
    if not author:
        return None
    a = author.lower()
    if a in slug_map:
        return slug_map[a]
    for slug, name in sorted(slug_map.items(), key=lambda x: -len(x[0])):
        if slug.isdigit():
            continue
        if len(slug) >= 3 and a.startswith(slug + "-"):
            return name
    return None


def company_name_variants(company: Company) -> list[str]:
    name = company.company_name.lower()
    short = re.sub(r"\s*(inc\.?|llc|corp\.?|ltd\.?)$", "", name, flags=re.I).strip()
    variants = {name, short, name.replace(",", "")}
    compact = re.sub(r"[^a-z0-9]", "", name)
    if len(compact) >= 3:
        variants.add(compact)
    slug_compact = company.slug.replace("-", "")
    if len(slug_compact) >= 3:
        variants.add(slug_compact)
    slug_base = company.slug.split("-")[0]
    if len(slug_base) >= 4:
        variants.add(slug_base)
    return [v for v in variants if v]


def post_belongs_to_company(
    post_url: str,
    text: str,
    company: Company,
) -> bool:
    """True if post is from company page or plausibly an employee of that company."""
    author = post_author_slug(post_url)
    slug = company.slug

    if slug and author == slug:
        return True
    if slug and author.startswith(slug + "-"):
        return True

    text_lower = text.lower()
    url_lower = post_url.lower()

    if slug and slug in url_lower:
        return True

    for variant in company_name_variants(company):
        if len(variant) < 3:
            continue
        if variant in text_lower:
            return True
        if variant in re.sub(r"[^a-z0-9]", "", text_lower):
            return True

    if company.booth:
        for booth_part in re.split(r"[,/]", company.booth):
            booth = booth_part.strip()
            if booth and re.search(rf"\bbooth\s*#?\s*{re.escape(booth)}\b", text_lower):
                return True

    return False


def is_rival_page_post(post_url: str, target: Company, slug_map: dict[str, str]) -> bool:
    """Post authored by another exhibitor's company page."""
    author = post_author_slug(post_url)
    if not author or author in THIRD_PARTY_AUTHORS:
        return author in THIRD_PARTY_AUTHORS
    resolved = resolve_author_to_exhibitor(author, slug_map)
    if resolved is None:
        return False
    return resolved.lower() != target.company_name.lower()


# ---------------------------------------------------------------------------
# Gartner CSO event + 2026 filters
# ---------------------------------------------------------------------------

def is_gartner_cso_event(text: str) -> bool:
    low = (text or "").lower()
    if "gartner" not in low:
        return False

    has_event = any(k in low for k in GARTNER_EVENT_KW)
    has_cso_context = (
        "cso" in low
        and any(x in low for x in ("sales leader", "conference", "booth", "vegas", "las vegas"))
    )
    if not has_event and not has_cso_context:
        return False

    if any(k in low for k in UNRELATED_GARTNER_KW):
        if not has_event and not re.search(r"gartner\s+cso|#gartnercso", low):
            return False

    prev = str(YEAR_FILTER - 1)
    if prev in low and str(YEAR_FILTER) not in low:
        if not re.search(r"\b(?:apr|may)\s+(?:19|20)", low):
            return False

    return True


def date_from_activity_id(post_url: str) -> date | None:
    m = re.search(r"activity-(\d+)", post_url or "")
    if not m:
        return None
    try:
        aid = int(m.group(1))
    except ValueError:
        return None

    # LinkedIn activity IDs are 19 digits; use the leading prefix as a year proxy.
    prefix = aid // 10_000_000_000_000_000
    if prefix >= 746:
        return date(YEAR_FILTER, 5, 20)
    if prefix >= 745:
        return date(YEAR_FILTER, 5, 10)
    if prefix >= 744:
        return date(YEAR_FILTER, 4, 20)
    if prefix >= 733:
        return date(2025, 5, 20)
    if prefix >= 719:
        return date(2025, 5, 15)
    if prefix >= 706:
        return date(2023, 5, 15)
    if prefix >= 645:
        return date(2024, 4, 15)
    return None


def activity_id_year(post_url: str) -> int | None:
    d = date_from_activity_id(post_url)
    return d.year if d else None


def parse_post_date(text: str, raw: str = "", post_url: str = "") -> date | None:
    combined = f"{raw} {text}"

    # Activity IDs in the 744e18+ range are reliable for this scrape window.
    aid_year = activity_id_year(post_url)
    if aid_year is not None:
        aid_date = date_from_activity_id(post_url)
        if aid_date and aid_date.year != YEAR_FILTER:
            return aid_date

    m = re.search(r"(\d+)\s+days?\s+ago", combined, re.I)
    if m:
        return SCRAPE_DATE - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s+weeks?\s+ago", combined, re.I)
    if m:
        return SCRAPE_DATE - timedelta(weeks=int(m.group(1)))
    if re.search(r"\byesterday\b", combined, re.I):
        return SCRAPE_DATE - timedelta(days=1)

    m = re.search(
        r"\b(\d{1,2})\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{4})\b",
        combined,
        re.I,
    )
    if m:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        except ValueError:
            pass

    m = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{1,2}),?\s+(\d{4})\b",
        combined,
        re.I,
    )
    if m:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        except ValueError:
            pass

    if re.search(r"\bgartner\b", combined, re.I):
        m = re.search(
            r"\b(apr(?:il)?|may)\s+(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\b",
            combined,
            re.I,
        )
        if m:
            return date(YEAR_FILTER, _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        if re.search(
            r"\bthis week\b|\bon the floor\b|\bgreat week\b|\bthat'?s a wrap\b|\bwrap\b",
            combined,
            re.I,
        ):
            return date(YEAR_FILTER, 5, 22)

    return date_from_activity_id(post_url)


def is_2026_post(text: str, raw: str, post_url: str) -> bool:
    aid_year = activity_id_year(post_url)
    if aid_year is not None and aid_year != YEAR_FILTER:
        return False

    d = parse_post_date(text, raw, post_url)
    if d is not None:
        return d.year == YEAR_FILTER

    combined = f"{raw} {text}".lower()
    prev = str(YEAR_FILTER - 1)
    year_s = str(YEAR_FILTER)
    if prev in combined and year_s not in combined:
        return False
    if year_s in combined:
        return True

    return aid_year == YEAR_FILTER


def format_display_date(text: str, raw: str, post_url: str) -> str:
    d = parse_post_date(text, raw, post_url)
    if d:
        labels = (
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        )
        return f"{d.day} {labels[d.month - 1]} {d.year}"
    return ""


def clean_post_text(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(
        r"^[^·]{0,140}?(?:Post\s*-\s*LinkedIn|on LinkedIn:?)\s+\d{1,2}\s+\w+\s+\d{4}\s*·\s*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"^\d{1,2}\s+\w+\s+\d{4}\s*·\s*", "", t)
    t = re.sub(r"\s*Missing:.*$", "", t, flags=re.I)
    t = re.sub(r"\s*Show results with:.*$", "", t, flags=re.I)
    return t.strip()[:3000]


def infer_posted_by(post_url: str, text: str, company: Company) -> str:
    author = post_author_slug(post_url)
    if author == company.slug or author.startswith(company.slug + "-"):
        return "Company Page"

    title = text[:200]
    m = re.search(r"^([A-Z][^·|]{2,60}?)(?:'s Post|' on LinkedIn|\s+-\s+LinkedIn|\s+\|)", title)
    if m:
        name = m.group(1).strip()
        if name.lower() not in ("linkedin", company.company_name.lower()):
            return name

    m = re.search(
        rf"^([A-Z][a-z]+(?:\s+[A-Z][a-z.-]+)+)(?:'s|\s+on\s+LinkedIn)",
        title,
    )
    if m:
        return m.group(1).strip()

    if author and author not in THIRD_PARTY_AUTHORS:
        readable = author.replace("-", " ").title()
        return f"Employee ({readable})"

    return "Employee"


# ---------------------------------------------------------------------------
# Fetch + search
# ---------------------------------------------------------------------------

def fetch_post_text(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.find("meta", property="og:description")
        if og and og.get("content", "").strip():
            return og["content"].strip()
        og_title = soup.find("meta", property="og:title")
        title = (og_title.get("content") or "").strip() if og_title else ""
        for sel in ["article", "[class*='break-words']", "p"]:
            el = soup.select_one(sel)
            if el:
                body = el.get_text(" ", strip=True)
                if len(body) > 40:
                    return f"{title} {body}".strip()[:3000]
    except Exception:
        pass
    return ""


def ddg_search(query: str, max_results: int = 15) -> list[dict]:
    if DDGS is None:
        return []
    results: list[dict] = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=max_results):
                results.append({
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                    "body": r.get("body", ""),
                })
        time.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass
    return results


def build_queries(company: Company) -> list[str]:
    name = company.company_name
    slug = company.slug
    booth = company.booth
    y = YEAR_FILTER
    queries = [
        f'site:linkedin.com/posts "{slug}" "gartner cso" {y}',
        f'site:linkedin.com/posts/{slug}_ gartner cso {y}',
        f'"{name}" "gartner cso" OR "gartner sales leader" {y} site:linkedin.com/posts',
        f'"{name}" gartnercso OR gartnersales linkedin {y}',
        f'"{name}" gartner cso conference vegas linkedin {y}',
        f'"{name}" employee gartner cso {y} site:linkedin.com/posts',
        f'"{name}" "great week" OR "that\'s a wrap" gartner cso linkedin {y}',
        f'linkedin.com/posts {slug} gartner sales leader conference {y}',
    ]
    if booth:
        first_booth = re.split(r"[,/]", booth)[0].strip()
        if first_booth:
            queries.append(f'booth {first_booth} "{name}" gartner cso {y} site:linkedin.com')
    return queries


def evaluate_candidate(
    post_url: str,
    snippet: str,
    company: Company,
    slug_map: dict[str, str],
    *,
    fetch_if_needed: bool = True,
) -> PostHit | None:
    post_url = norm_url(post_url)
    if not is_li_post(post_url):
        return None
    if is_rival_page_post(post_url, company, slug_map):
        return None

    author = post_author_slug(post_url)
    if author in THIRD_PARTY_AUTHORS:
        return None

    text = clean_post_text(snippet)
    if len(text) < 50 and fetch_if_needed:
        fetched = fetch_post_text(post_url)
        if fetched:
            text = clean_post_text(fetched)

    if not text or not is_gartner_cso_event(text):
        return None
    if not post_belongs_to_company(post_url, text, company):
        return None
    if not is_2026_post(text, snippet, post_url):
        return None

    resolved = resolve_author_to_exhibitor(author, slug_map)
    if resolved and resolved.lower() != company.company_name.lower():
        return None

    return PostHit(
        company_name=company.company_name,
        linkedin_url=company.linkedin_company_url,
        post_link=post_url,
        post_text=text,
        post_date=format_display_date(text, snippet, post_url),
        posted_by=infer_posted_by(post_url, text, company),
    )


def search_company_ddg(
    company: Company,
    slug_map: dict[str, str],
    seen_urls: set[str],
    log: list[str],
) -> list[PostHit]:
    found: list[PostHit] = []
    local_seen: set[str] = set()

    for query in build_queries(company):
        log.append(f"  DDG: {query!r}")
        for r in ddg_search(query, max_results=15):
            post_url = norm_url(r["url"])
            if not post_url or post_url in seen_urls or post_url in local_seen:
                continue
            snippet = f"{r.get('title', '')} {r.get('body', '')}".strip()
            hit = evaluate_candidate(post_url, snippet, company, slug_map)
            if hit is None:
                continue
            local_seen.add(post_url)
            seen_urls.add(post_url)
            hit.source = "duckduckgo"
            found.append(hit)
            log.append(f"    + {post_url[:75]}")

        if len(found) >= 8:
            break

    log.append(f"  DDG total: {len(found)}")
    return found


# ---------------------------------------------------------------------------
# Mine existing CSVs
# ---------------------------------------------------------------------------

def mine_existing_csvs(
    companies: list[Company],
    slug_map: dict[str, str],
    seen_urls: set[str],
    log: list[str],
    mine_sources: list[Path] | None = None,
) -> list[PostHit]:
    by_name = {c.company_name.lower(): c for c in companies}
    found: list[PostHit] = []

    for src in mine_sources or MINE_SOURCES:
        if not src.exists():
            continue
        n = 0
        with open(src, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                post_url = norm_url(row.get("post_url", "") or row.get("post_link", ""))
                if not post_url or post_url in seen_urls:
                    continue

                name = normalize_company_name(row.get("company_name", ""))
                company = by_name.get(name.lower())
                if not company:
                    continue

                text = row.get("post_text", "") or row.get("post_summary", "")
                raw_date = row.get("post_date", "")
                hit = evaluate_candidate(
                    post_url,
                    f"{raw_date} {text}",
                    company,
                    slug_map,
                    fetch_if_needed=False,
                )
                if hit is None:
                    continue
                seen_urls.add(post_url)
                hit.source = f"mine:{src.name}"
                found.append(hit)
                n += 1
        log.append(f"  {src.name}: +{n}")

    log.append(f"  Mined total: {len(found)}")
    return found


# ---------------------------------------------------------------------------
# Optional LinkedIn scrape (company /posts/)
# ---------------------------------------------------------------------------

def scrape_company_linkedin(
    company: Company,
    session_path: Path,
    slug_map: dict[str, str],
    seen_urls: set[str],
    log: list[str],
) -> list[PostHit]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.append("  Playwright not installed — skip LinkedIn scrape")
        return []

    slug = company.slug
    found: list[PostHit] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=str(session_path),
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        target = f"https://www.linkedin.com/company/{slug}/posts/?feedView=all&sortBy=recency"
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            for _ in range(8):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
            html = page.content()
        except Exception as exc:
            log.append(f"  LinkedIn nav error: {exc}")
            html = ""
        browser.close()

    if not html or len(html) < 10_000:
        log.append("  LinkedIn: no usable HTML")
        return found

    esc = re.escape(slug)
    urls: set[str] = set()
    for m in re.finditer(
        rf"https://www\.linkedin\.com/posts/{esc}_[a-zA-Z0-9_-]+-activity-\d+",
        html,
        re.I,
    ):
        urls.add(norm_url(m.group(0)))

    log.append(f"  LinkedIn URLs in page: {len(urls)}")
    for post_url in sorted(urls):
        if post_url in seen_urls:
            continue
        fetched = fetch_post_text(post_url)
        hit = evaluate_candidate(post_url, fetched, company, slug_map, fetch_if_needed=False)
        if hit is None:
            continue
        seen_urls.add(post_url)
        hit.source = "linkedin_scrape"
        found.append(hit)

    return found


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def hits_to_rows(hits: list[PostHit]) -> list[dict]:
    rows = []
    for h in hits:
        rows.append({
            "company_name": h.company_name,
            "linkedin_url": h.linkedin_url,
            "post_date": h.post_date or "not_found",
            "post_text": h.post_text,
            "post_link": h.post_link,
            "posted_by": h.posted_by,
            "status": "Post Found",
        })
    return rows


def build_final_output(companies: list[Company], hits: list[PostHit]) -> list[dict]:
    by_company: dict[str, list[PostHit]] = {}
    for h in hits:
        by_company.setdefault(h.company_name, []).append(h)

    output: list[dict] = []
    for company in sorted(companies, key=lambda c: c.company_name.lower()):
        company_hits = by_company.get(company.company_name, [])
        if company_hits:
            for h in sorted(company_hits, key=lambda x: (x.post_date, x.post_link)):
                output.append({
                    "company_name": company.company_name,
                    "linkedin_url": company.linkedin_company_url,
                    "post_date": h.post_date or "not_found",
                    "post_text": h.post_text,
                    "post_link": h.post_link,
                    "posted_by": h.posted_by,
                    "status": "Post Found",
                })
        else:
            output.append({
                "company_name": company.company_name,
                "linkedin_url": company.linkedin_company_url,
                "post_date": "",
                "post_text": "No posts found",
                "post_link": "",
                "posted_by": "",
                "status": "No posts found",
            })
    return output


def write_summary(companies: list[Company], output: list[dict], path: Path) -> None:
    with_posts = {r["company_name"] for r in output if r["status"] == "Post Found"}
    post_rows = [r for r in output if r["status"] == "Post Found"]
    company_page = sum(1 for r in post_rows if r["posted_by"] == "Company Page")
    employee = len(post_rows) - company_page
    missing = [c.company_name for c in companies if c.company_name not in with_posts]

    lines = [
        f"Gartner CSO {YEAR_FILTER} post extraction — {date.today().isoformat()}",
        f"Companies in input:     {len(companies)}",
        f"Companies with posts:   {len(with_posts)}",
        f"Companies without posts:{len(missing)}",
        f"Total post rows:        {len(post_rows)}",
        f"  Company page posts:   {company_page}",
        f"  Employee posts:       {employee}",
        "",
    ]
    if missing:
        lines.append("Companies with no posts found:")
        lines.extend(f"  - {n}" for n in sorted(missing, key=str.lower))
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", type=Path, default=INPUT_CSV)
    parser.add_argument("--output", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--session", type=Path, default=SESSION_FILE)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--full", action="store_true", help="Process all companies")
    grp.add_argument("--companies", nargs="+", metavar="NAME", help="Subset of companies")
    parser.add_argument(
        "--linkedin",
        action="store_true",
        help="Also scrape company /posts/ with saved LinkedIn session",
    )
    parser.add_argument(
        "--mine-only",
        action="store_true",
        help="Only reuse posts from existing project CSVs (no web search)",
    )
    parser.add_argument(
        "--skip-mine",
        action="store_true",
        help="Skip mining existing CSVs",
    )
    parser.add_argument(
        "--mine-csv",
        type=Path,
        nargs="*",
        default=[],
        metavar="CSV",
        help="Extra CSV file(s) for Phase 1 mining (columns: company_name, post_link/post_url, post_text)",
    )
    args = parser.parse_args()

    mine_sources = list(MINE_SOURCES) + list(args.mine_csv)

    if not args.mine_only and DDGS is None:
        print("Install ddgs for web search: pip install ddgs", file=sys.stderr)
        if not args.skip_mine:
            print("Continuing with --mine-only behaviour...", file=sys.stderr)
            args.mine_only = True

    companies = load_companies(args.input)
    if args.companies:
        ql = [q.lower() for q in args.companies]
        companies = [c for c in companies if any(q in c.company_name.lower() for q in ql)]
    if not companies:
        print(f"No companies found in {args.input}", file=sys.stderr)
        return 1

    slug_map = build_slug_to_company(companies)
    seen_urls: set[str] = set()
    all_hits: list[PostHit] = []
    log: list[str] = [f"extract_gartner_cso_posts — {now_iso()}", ""]

    print(f"Companies: {len(companies)}")
    print(f"Output:    {args.output}\n")

    if not args.skip_mine:
        print("Phase 1: mining existing CSVs ...")
        log.append("=== Phase 1: mine existing CSVs ===")
        mined = mine_existing_csvs(companies, slug_map, seen_urls, log, mine_sources)
        all_hits.extend(mined)
        print(f"  -> {len(mined)} posts\n")

    if not args.mine_only:
        print("Phase 2: DuckDuckGo search per company ...")
        for i, company in enumerate(companies, 1):
            print(f"[{i:>2}/{len(companies)}] {company.company_name} ...", end=" ", flush=True)
            clog: list[str] = [f"\n{company.company_name} ({company.slug})"]
            hits = search_company_ddg(company, slug_map, seen_urls, clog)
            all_hits.extend(hits)
            log.extend(clog)
            print(f"{len(hits)} new")
            if i < len(companies):
                time.sleep(random.uniform(1.0, 2.0))
        print()

    if args.linkedin and args.session.exists():
        print("Phase 3: LinkedIn company page scrape ...")
        log.append("\n=== Phase 3: LinkedIn scrape ===")
        for i, company in enumerate(companies, 1):
            print(f"[{i:>2}/{len(companies)}] {company.company_name} ...", end=" ", flush=True)
            clog: list[str] = [f"\n{company.company_name} LI scrape"]
            hits = scrape_company_linkedin(company, args.session, slug_map, seen_urls, clog)
            all_hits.extend(hits)
            log.extend(clog)
            print(f"+{len(hits)}")
            if i < len(companies):
                time.sleep(random.uniform(2.0, 3.5))
        print()
    elif args.linkedin:
        print(f"Warning: session not found at {args.session} — skipping LinkedIn scrape")

    # Deduplicate by post URL (keep first)
    deduped: list[PostHit] = []
    seen: set[str] = set()
    for h in all_hits:
        if h.post_link in seen:
            continue
        seen.add(h.post_link)
        deduped.append(h)

    output = build_final_output(companies, deduped)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
        w.writeheader()
        w.writerows(output)

    write_summary(companies, output, SUMMARY_FILE)
    LOG_FILE.write_text("\n".join(log), encoding="utf-8")

    post_count = sum(1 for r in output if r["status"] == "Post Found")
    company_count = len({r["company_name"] for r in output if r["status"] == "Post Found"})

    print("=" * 55)
    print(f"Post rows written:      {post_count}")
    print(f"Companies with posts:   {company_count} / {len(companies)}")
    print(f"Output CSV:             {args.output}")
    print(f"Summary:                {SUMMARY_FILE}")
    print(f"Log:                    {LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

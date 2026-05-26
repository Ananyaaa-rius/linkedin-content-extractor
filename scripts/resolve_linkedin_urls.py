#!/usr/bin/env python3
"""Resolve LinkedIn company URLs from exhibitor websites and public search."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus, urljoin, urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import pandas as pd
import requests
from bs4 import BeautifulSoup

from url_utils import normalize_website, sanitize_website_column

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
MIN_DELAY_SEC = 1.0
MAX_DELAY_SEC = 2.0

LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9\-_%]+)/?",
    re.IGNORECASE,
)
LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/company/([a-zA-Z0-9\-_%]+)", re.IGNORECASE)
LINKEDIN_JOBS_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/[^\s\"'<>]+",
    re.IGNORECASE,
)

# Explicit manual-review cases from task brief
HARD_CASES: dict[str, str] = {
    "Equilar, Inc.": "Website points to ExecAtlas brand; may not match Equilar LinkedIn slug",
    "Oracle, Inc.": "Exhibitor website is Oracle Sales product page, not corporate homepage",
    "LinkedIn": "Exhibitor is LinkedIn itself; confirm correct /company/ page for scraping scope",
    "1mind": "Short or ambiguous company name",
    "Gong": "Short or ambiguous company name",
    "Pigment": "Short or ambiguous company name",
}

SHORT_NAME_MAX_LEN = 4  # flag names with 4 or fewer characters (Gong, Clay, etc.)
# Common LinkedIn slug suffixes after domain brand (1mind -> 1mindai, nooks -> nooksapp)
BRAND_SLUG_SUFFIXES = frozenset({"ai", "hq", "io", "app", "labs", "tech", "co"})

MAX_SITE_PAGES = 20
MAX_JS_RENDER_PAGES = 5
_USE_JS_RENDER = True
_playwright_available: bool | None = None
# Path segments commonly used for footer / social / careers links (not company-specific)
SITE_PATH_KEYWORDS = (
    "contact",
    "about",
    "team",
    "company",
    "careers",
    "jobs",
    "job",
    "hiring",
    "join",
    "culture",
    "people",
    "legal",
    "privacy",
)
FOOTER_LINK_KEYWORDS = SITE_PATH_KEYWORDS + (
    "solution",
    "footer",
    "meet",
    "support",
    "connect",
    "social",
)


def build_common_crawl_paths() -> tuple[str, ...]:
    """Generic paths derived from keywords — no per-company paths."""
    paths: list[str] = ["/"]
    for kw in SITE_PATH_KEYWORDS:
        paths.append(f"/{kw}")
        if "-" not in kw:
            paths.append(f"/{kw}-us")
    return tuple(dict.fromkeys(paths))


COMMON_CRAWL_PATHS = build_common_crawl_paths()


@dataclass
class ResolutionResult:
    company_name: str
    linkedin_company_url: str = ""
    status: str = "not_found"
    method: str = "none"
    notes: str = ""
    candidates: list[str] = field(default_factory=list)


def jitter_sleep() -> None:
    time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))


def js_rendering_available() -> bool:
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        _playwright_available = True
    except ImportError:
        _playwright_available = False
    return _playwright_available


def _dismiss_cookie_banner(page) -> None:
    """Best-effort cookie/consent dismiss (generic selectors, not site-specific)."""
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('Agree')",
    ):
        try:
            page.locator(selector).first.click(timeout=2500)
            page.wait_for_timeout(800)
            return
        except Exception:
            continue


def fetch_page_rendered(url: str) -> tuple[str, str]:
    """Load page in headless browser so JS-injected footer social links appear."""
    if not js_rendering_available():
        return "", ""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT, locale="en-US")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                _dismiss_cookie_banner(page)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2500)
                html = page.content()
                if "linkedin.com/company" not in html.lower():
                    page.wait_for_timeout(2500)
                    html = page.content()
                return html, page.url
            finally:
                browser.close()
    except Exception:
        return "", ""


def website_domain_brand(website: str) -> str:
    """Primary brand token from exhibitor website hostname (e.g. arist.com -> arist)."""
    host = urlparse(normalize_website(website)).netloc.lower().removeprefix("www.")
    if not host:
        return ""
    label = host.split(".")[0]
    return re.sub(r"[^a-z0-9]", "", label.lower())


def normalize_linkedin_url(url_or_slug: str) -> str | None:
    """Return canonical https://www.linkedin.com/company/{slug}/ or None."""
    if not url_or_slug:
        return None
    text = url_or_slug.strip()
    match = LINKEDIN_COMPANY_RE.search(text) or LINKEDIN_SLUG_RE.search(text)
    if not match:
        # bare slug
        if re.fullmatch(r"[a-zA-Z0-9\-_%]+", text):
            slug = text.strip("/").lower()
            return f"https://www.linkedin.com/company/{slug}/"
        return None
    slug = match.group(1).strip("/").lower()
    slug = requests.utils.unquote(slug).replace("%20", "-")
    if slug in ("in", "jobs", "school", "showcase"):
        return None
    return f"https://www.linkedin.com/company/{slug}/"


def extract_linkedin_urls_from_html(html: str, base_url: str = "") -> list[str]:
    """Collect unique normalized company URLs from page HTML and embedded JSON."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        norm = normalize_linkedin_url(raw)
        if norm and norm not in seen:
            seen.add(norm)
            found.append(norm)

    for match in LINKEDIN_COMPANY_RE.finditer(html):
        add(match.group(0))
    for match in LINKEDIN_SLUG_RE.finditer(html):
        add(match.group(0))
    for match in re.finditer(
        r"linkedin\\.com\\u002Fcompany\\u002F([a-zA-Z0-9\-_%]+)", html, re.IGNORECASE
    ):
        add(f"https://www.linkedin.com/company/{match.group(1)}/")
    for match in re.finditer(
        r"linkedin\.com%2Fcompany%2F([a-zA-Z0-9\-_%]+)", html, re.IGNORECASE
    ):
        add(f"https://www.linkedin.com/company/{match.group(1)}/")

    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(href=True):
            href = tag.get("href", "")
            if "linkedin.com/company" in href.lower():
                add(urljoin(base_url, href) if base_url else href)
        for tag in soup.find_all(True):
            for attr in ("data-href", "data-url", "data-link", "content"):
                val = tag.get(attr)
                if val and "linkedin.com/company" in str(val).lower():
                    add(urljoin(base_url, str(val)) if base_url else str(val))
        for tag in soup.find_all(src=True):
            src = tag.get("src", "")
            if "linkedin.com/company" in src.lower():
                add(src)
        for script in soup.find_all("script"):
            body = script.string or ""
            if not body:
                continue
            for match in LINKEDIN_SLUG_RE.finditer(body):
                add(match.group(0))
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                same_as = item.get("sameAs", [])
                if isinstance(same_as, str):
                    same_as = [same_as]
                for url in same_as:
                    if isinstance(url, str) and "linkedin.com/company" in url:
                        add(url)
    except Exception:
        pass

    return found


def extract_linkedin_jobs_urls_from_html(html: str, base_url: str = "") -> list[str]:
    """Collect LinkedIn job listing URLs (often linked from Careers pages)."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        raw = raw.strip()
        if "linkedin.com/jobs" not in raw.lower():
            return
        absolute = urljoin(base_url, raw) if base_url else raw
        norm = absolute.split("#")[0]
        if norm not in seen:
            seen.add(norm)
            found.append(norm)

    for match in LINKEDIN_JOBS_RE.finditer(html):
        add(match.group(0))

    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(href=True):
            href = tag.get("href", "")
            if "linkedin.com/jobs" in href.lower():
                add(urljoin(base_url, href) if base_url else href)
    except Exception:
        pass

    return found


def company_tokens(name: str) -> set[str]:
    """Tokens for fuzzy match scoring."""
    raw = re.sub(r"[^\w\s]", " ", name.lower())
    stop = {"inc", "llc", "ltd", "corp", "corporation", "co", "the", "and", "ai"}
    tokens = {t for t in raw.split() if t and t not in stop}
    return tokens


def slug_tokens(slug: str) -> set[str]:
    slug = slug.lower().replace("-", " ").replace("_", " ")
    return {t for t in slug.split() if t}


def score_slug_match(company_name: str, url: str, *, website: str = "") -> float:
    """Higher is better. 0 = no relation."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1].lower()
    ct = company_tokens(company_name)
    st = slug_tokens(slug)
    brand = website_domain_brand(website)
    if brand:
        ct = ct | {brand}
    if not ct or not st:
        return 0.0
    overlap = len(ct & st) / max(len(ct), 1)
    if slug.replace("-", "") in re.sub(r"\W", "", company_name.lower()):
        overlap = max(overlap, 0.85)
    compact_name = re.sub(r"\W", "", company_name.lower())
    slug_compact = slug.replace("-", "")
    if compact_name and compact_name in slug_compact:
        overlap = max(overlap, 0.5)
    if slug_compact in compact_name:
        overlap = max(overlap, 0.5)
    if brand and brand in slug_compact:
        overlap = max(overlap, 0.8)
    # Prefer brand+suffix slugs (nooks.ai -> nooksapp) over bare slug (nooks)
    if brand and slug_compact.startswith(brand):
        if len(slug_compact) > len(brand):
            overlap = max(overlap, 0.92)
            suffix = slug_compact[len(brand) :]
            if suffix in BRAND_SLUG_SUFFIXES:
                overlap = max(overlap, 0.94)
        elif slug_compact == brand:
            overlap = min(overlap, 0.72)
    # Domain go{Name} often maps to LinkedIn get{Name} (e.g. goconsensus.com -> getconsensus)
    if brand.startswith("go") and len(brand) > 3:
        get_variant = "get" + brand[2:]
        if slug_compact == get_variant:
            overlap = max(overlap, 0.93)
    name_tokens = company_tokens(company_name)
    if name_tokens and all(t in slug_compact for t in name_tokens):
        overlap = max(overlap, 0.88)
    # Penalize unrelated hyphen-separated tokens (e.g. arist-automation-bhopal for Arist)
    if "-" in slug and len(st) > 1:
        extra = st - ct
        if extra:
            overlap -= min(0.6, 0.22 * len(extra))
    return max(0.0, overlap)


def pick_best_candidate(
    company_name: str,
    candidates: list[str],
    *,
    from_website: bool = False,
    website: str = "",
) -> tuple[str, float]:
    if not candidates:
        return "", 0.0
    scored = [
        (url, score_slug_match(company_name, url, website=website))
        for url in candidates
        if is_plausible_match(company_name, url, from_website=from_website)
    ]
    if not scored:
        return "", 0.0
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0]


def is_plausible_match(company_name: str, url: str, *, from_website: bool = False) -> bool:
    """Reject obvious junk (/in/ paths, numeric internal IDs on marketing sites)."""
    if "/in/" in url.lower():
        return False
    slug = urlparse(url).path.rstrip("/").split("/")[-1].lower()
    name_key = company_name.strip().lower()

    # LinkedIn's business site embeds internal numeric company IDs (e.g. /company/1337)
    if name_key == "linkedin" and "linkedin" not in slug:
        return False

    if slug.isdigit() and not from_website:
        return False
    # Numeric slug on own site is only trusted when name aligns (e.g. Anaplan IDs)
    if slug.isdigit() and from_website and score_slug_match(company_name, url) < 0.2:
        return False
    return True


def search_queries(company_name: str, website: str) -> list[str]:
    """Build search queries from company name and exhibitor website domain."""
    queries = [f"{company_name} linkedin company"]
    brand = website_domain_brand(website)
    if brand and brand not in company_name.lower():
        queries.append(f"{brand} linkedin company")
        queries.append(f"site:linkedin.com/company {brand}")
        for suffix in ("ai", "app", "hq"):
            queries.append(f"site:linkedin.com/company {brand}{suffix}")
    if website:
        host = urlparse(normalize_website(website)).netloc.lower().removeprefix("www.")
        if host:
            queries.append(f'"{host}" linkedin company')
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def fetch_page(url: str, session: requests.Session) -> tuple[str, str]:
    """Returns (html, final_url) or ("", "") on failure."""
    try:
        resp = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        resp.raise_for_status()
        return resp.text, resp.url
    except requests.RequestException:
        return "", ""


def _normalize_url_for_compare(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}"


def _is_crawlable_link(href: str, base_url: str) -> bool:
    """Same registrable domain or trusted LinkedIn corporate subsite from footer."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    parsed = urlparse(urljoin(base_url, href))
    if not parsed.netloc:
        return False
    if parsed.netloc.endswith("linkedin.com") and parsed.netloc != "www.linkedin.com":
        # Footer on business.linkedin.com links to about.linkedin.com, etc.
        return parsed.netloc in (
            "about.linkedin.com",
            "business.linkedin.com",
            "news.linkedin.com",
        )
    start_netloc = urlparse(base_url).netloc.lower().removeprefix("www.")
    link_base = parsed.netloc.lower().removeprefix("www.")
    return link_base == start_netloc or link_base.endswith("." + start_netloc)


def _priority_link_score(path: str) -> int:
    path_l = path.lower()
    score = sum(1 for kw in FOOTER_LINK_KEYWORDS if kw in path_l)
    if "linkedin.com/jobs" in path_l:
        score += 5
    return score


def discover_crawl_urls(start_url: str, html: str, final_url: str) -> list[str]:
    """Build ordered list of same-site pages; real in-page links (e.g. /sales/contact/) come first."""
    parsed = urlparse(final_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    ordered: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(url: str, priority: int = 0) -> None:
        norm = _normalize_url_for_compare(url)
        if norm in seen:
            return
        seen.add(norm)
        ordered.append((priority, url))

    add(start_url, 0)

    try:
        soup = BeautifulSoup(html, "html.parser")
        footer_regions = []
        if soup.find("footer"):
            footer_regions.append(soup.find("footer"))
        footer_regions.extend(
            soup.select('[class*="footer" i], [id*="footer" i], [role="contentinfo"]')
        )
        footer_tags: list = []
        for region in footer_regions:
            if region:
                footer_tags.extend(region.find_all("a", href=True))
        all_tags = soup.find_all("a", href=True)
        link_tags: list = []
        seen_tag_ids: set[int] = set()
        for tag in footer_tags + all_tags:
            tid = id(tag)
            if tid in seen_tag_ids:
                continue
            seen_tag_ids.add(tid)
            link_tags.append(tag)

        scored_links: list[tuple[int, str]] = []
        for tag in link_tags:
            href = tag.get("href", "").strip()
            absolute = urljoin(final_url, href)
            if "linkedin.com/jobs" in absolute.lower():
                scored_links.append((_priority_link_score(absolute) + 10, absolute))
                continue
            if not _is_crawlable_link(href, final_url):
                continue
            scored_links.append((_priority_link_score(urlparse(absolute).path), absolute))
        scored_links.sort(key=lambda x: (-x[0], x[1]))
        # Crawl real links from the page before generic guessed paths like /contact-us
        for score, link in scored_links:
            add(link, 5 - min(score, 4))
    except Exception:
        pass

    add(f"{base}/", 20)
    for path in COMMON_CRAWL_PATHS:
        add(urljoin(base, path), 40)

    ordered.sort(key=lambda x: (x[0], x[1]))
    return [url for _, url in ordered[:MAX_SITE_PAGES]]


def crawl_website_for_linkedin(
    company_name: str,
    start_url: str,
    session: requests.Session,
) -> tuple[list[str], list[str], dict[str, str]]:
    """
    Fetch exhibitor site pages (from CSV website URL), extract /company/ links and
    resolve /jobs/ links found on-site (e.g. Careers footer).
    """
    jitter_sleep()
    html, final_url = fetch_page(start_url, session)
    if not html:
        return [], [], {}

    candidates: list[str] = []
    seen_urls: set[str] = set()
    pages_checked: list[str] = []
    url_sources: dict[str, str] = {}
    jobs_to_resolve: list[tuple[str, str]] = []
    seen_jobs: set[str] = set()

    crawl_queue = discover_crawl_urls(start_url, html, final_url)

    for i, page_url in enumerate(crawl_queue):
        if i == 0:
            page_html, page_final = html, final_url
        else:
            jitter_sleep()
            page_html, page_final = fetch_page(page_url, session)
        if not page_html:
            continue
        pages_checked.append(page_final)
        for link in extract_linkedin_urls_from_html(page_html, page_final):
            if link not in seen_urls:
                seen_urls.add(link)
                candidates.append(link)
                url_sources[link] = page_final
        for jobs_url in extract_linkedin_jobs_urls_from_html(page_html, page_final):
            norm = jobs_url.split("#")[0]
            if norm not in seen_jobs:
                seen_jobs.add(norm)
                jobs_to_resolve.append((norm, page_final))

    if jobs_to_resolve:
        source_by_jobs = {u: src for u, src in jobs_to_resolve}
        for jobs_url, source_page in jobs_to_resolve:
            jitter_sleep()
            jobs_html, jobs_final = fetch_page(jobs_url, session)
            if jobs_final:
                pages_checked.append(jobs_final)
            if not jobs_html:
                continue
            for link in extract_linkedin_urls_from_html(jobs_html, jobs_final):
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                candidates.append(link)
                url_sources[link] = (
                    f"{source_page} (via LinkedIn Jobs {jobs_url})"
                )

    if not candidates and _USE_JS_RENDER and js_rendering_available():
        render_queue = sorted(
            pages_checked or [final_url],
            key=lambda u: -_priority_link_score(urlparse(u).path),
        )
        for page_url in render_queue[:MAX_JS_RENDER_PAGES]:
            rendered_html, rendered_final = fetch_page_rendered(page_url)
            if not rendered_html:
                continue
            pages_checked.append(rendered_final)
            for link in extract_linkedin_urls_from_html(rendered_html, rendered_final):
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                candidates.append(link)
                url_sources[link] = f"{rendered_final} (JS-rendered footer)"
            for jobs_url in extract_linkedin_jobs_urls_from_html(
                rendered_html, rendered_final
            ):
                norm = jobs_url.split("#")[0]
                if norm in seen_jobs:
                    continue
                seen_jobs.add(norm)
                jobs_html, jobs_final = fetch_page_rendered(norm)
                if jobs_final:
                    pages_checked.append(jobs_final)
                for link in extract_linkedin_urls_from_html(jobs_html, jobs_final):
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)
                    candidates.append(link)
                    url_sources[link] = (
                        f"{rendered_final} (JS-rendered, via LinkedIn Jobs)"
                    )

    return candidates, pages_checked, url_sources


def resolve_from_website(
    company_name: str, website: str, session: requests.Session
) -> ResolutionResult:
    result = ResolutionResult(company_name=company_name)
    site = normalize_website(website)
    if not site:
        result.notes = "No website in CSV"
        return result

    urls, pages_checked, url_sources = crawl_website_for_linkedin(
        company_name, site, session
    )
    result.candidates.extend(urls)

    if not urls:
        result.notes = (
            f"No LinkedIn company link on crawled pages ({len(pages_checked)} checked)"
        )
        if pages_checked:
            result.notes += ": " + ", ".join(pages_checked[:5])
            if len(pages_checked) > 5:
                result.notes += "..."
        if _USE_JS_RENDER and not js_rendering_available():
            result.notes += (
                "; install playwright in this venv for JS footer links: "
                "pip install playwright && python -m playwright install chromium"
            )
        return result

    best_url, score = pick_best_candidate(
        company_name, urls, from_website=True, website=website
    )
    result.linkedin_company_url = best_url
    result.method = "website"
    found_on = url_sources.get(best_url, pages_checked[0] if pages_checked else site)
    result.notes = (
        f"Found via site crawl ({len(pages_checked)} pages); "
        f"match score {score:.2f}; found on {found_on}"
    )
    if len(urls) > 1:
        result.notes += f"; {len(urls)} candidates: {', '.join(urls)}"
    return result


def _extract_from_search_html(company_name: str, html: str, seen: set[str]) -> list[str]:
    found: list[str] = []
    for match in LINKEDIN_COMPANY_RE.finditer(html):
        norm = normalize_linkedin_url(match.group(0))
        if norm and norm not in seen and is_plausible_match(company_name, norm):
            seen.add(norm)
            found.append(norm)
    return found


def search_brave(
    company_name: str,
    website: str,
    session: requests.Session,
) -> list[str]:
    """Brave Search HTML for public LinkedIn company page discovery."""
    candidates: list[str] = []
    seen: set[str] = set()

    for query in search_queries(company_name, website):
        url = f"https://search.brave.com/search?q={quote_plus(query)}"
        jitter_sleep()
        html, _ = fetch_page(url, session)
        if not html:
            continue
        candidates.extend(_extract_from_search_html(company_name, html, seen))
        if candidates:
            break

    return candidates


def search_startpage(
    company_name: str,
    website: str,
    session: requests.Session,
) -> list[str]:
    """Startpage search fallback when Brave is empty or rate-limited."""
    candidates: list[str] = []
    seen: set[str] = set()

    for query in search_queries(company_name, website):
        url = f"https://www.startpage.com/sp/search?query={quote_plus(query)}"
        jitter_sleep()
        html, _ = fetch_page(url, session)
        if not html:
            continue
        candidates.extend(_extract_from_search_html(company_name, html, seen))
        if candidates:
            break

    return candidates


def search_public(
    company_name: str,
    website: str,
    session: requests.Session,
) -> tuple[list[str], str]:
    """Try Brave, then Startpage. Returns (candidates, engine_name)."""
    hits = search_brave(company_name, website, session)
    if hits:
        return hits, "brave"
    hits = search_startpage(company_name, website, session)
    if hits:
        return hits, "startpage"
    return [], "none"


def apply_status(
    result: ResolutionResult,
    *,
    force_review: bool = False,
    review_reason: str = "",
) -> ResolutionResult:
    name = result.company_name
    hard_note = HARD_CASES.get(name, "")

    if not result.linkedin_company_url:
        result.status = "not_found"
        if hard_note:
            result.notes = f"{result.notes}; {hard_note}".strip("; ")
        return result

    score = score_slug_match(name, result.linkedin_company_url)
    short_name = len(name.strip()) <= SHORT_NAME_MAX_LEN

    needs_review = (
        force_review
        or bool(hard_note)
        or short_name
        or result.method == "search"
        and score < 0.5
        or score < 0.35
    )

    if needs_review:
        result.status = "needs_review"
        reasons = []
        if hard_note:
            reasons.append(hard_note)
        if review_reason:
            reasons.append(review_reason)
        if short_name and name not in HARD_CASES:
            reasons.append("Very short company name")
        if score < 0.5 and result.method == "search":
            reasons.append(f"Low search match score ({score:.2f})")
        if reasons:
            result.notes = "; ".join(filter(None, [result.notes, *reasons]))
    else:
        result.status = "verified"
    return result


def resolve_company(
    company_name: str,
    website: str,
    session: requests.Session,
) -> ResolutionResult:
    result = resolve_from_website(company_name, website, session)

    website_score = 0.0
    if result.linkedin_company_url:
        website_score = score_slug_match(
            company_name, result.linkedin_company_url, website=website
        )
        brand = website_domain_brand(website)
        slug = urlparse(result.linkedin_company_url).path.rstrip("/").split("/")[-1]
        brand_in_slug = bool(brand and brand in slug.replace("-", "").lower())
        if is_plausible_match(
            company_name, result.linkedin_company_url, from_website=True
        ) and (website_score >= 0.35 or brand_in_slug or "(via LinkedIn Jobs" in result.notes):
            return apply_status(result)
        result.notes += "; weak website match, trying search"
        result.linkedin_company_url = ""
        result.method = "none"

    search_hits, engine = search_public(company_name, website, session)
    if search_hits:
        best_url, score = pick_best_candidate(
            company_name, search_hits, website=website
        )
        brand = website_domain_brand(website)
        slug = urlparse(best_url).path.rstrip("/").split("/")[-1] if best_url else ""
        brand_in_slug = bool(brand and brand in slug.replace("-", "").lower())
        min_search_score = 0.55 if len(company_name.strip()) <= SHORT_NAME_MAX_LEN else 0.45
        if best_url and (score >= min_search_score or brand_in_slug):
            result.linkedin_company_url = best_url
            result.method = "search"
            result.candidates = list(dict.fromkeys(result.candidates + search_hits))
            result.notes = f"{engine} search; match score {score:.2f}"
            if len(search_hits) > 1:
                result.notes += f"; {len(search_hits)} candidates"
        elif best_url:
            result.candidates = list(dict.fromkeys(result.candidates + search_hits))
            result.notes = (
                f"{engine} search rejected weak match ({score:.2f}); "
                f"best guess was {best_url}"
            )

    return apply_status(result)


def load_dataframe(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    if "website" in df.columns:
        df["website"] = sanitize_website_column(df["website"])
    return df


def fix_csv_website_urls(exhibitors_path: Path, linkedin_path: Path) -> int:
    """Rewrite CSV files with sanitized website column. Returns rows updated."""
    changed = 0
    for path in (exhibitors_path, linkedin_path):
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str).fillna("")
        if "website" not in df.columns:
            continue
        before = df["website"].astype(str).tolist()
        df["website"] = sanitize_website_column(df["website"])
        after = df["website"].astype(str).tolist()
        changed += sum(1 for a, b in zip(before, after) if a != b)
        df.to_csv(path, index=False)
    return changed


def merge_exhibitor_websites(
    df: pd.DataFrame, exhibitors_path: Path
) -> pd.DataFrame:
    """Always resolve using website URL from exhibitors_clean.csv when available."""
    if not exhibitors_path.exists():
        return df
    ex = load_dataframe(exhibitors_path)
    if "company_name" not in ex.columns or "website" not in ex.columns:
        return df
    site_by_name = {
        str(r["company_name"]).strip(): str(r["website"]).strip()
        for _, r in ex.iterrows()
        if str(r.get("website", "")).strip()
    }
    out = df.copy()
    if "website" not in out.columns:
        out["website"] = ""
    for idx, row in out.iterrows():
        name = str(row["company_name"]).strip()
        if name in site_by_name:
            out.at[idx, "website"] = site_by_name[name]
    return out


def write_log(entries: list[ResolutionResult], path: Path, *, append: bool = False) -> None:
    blocks = []
    for e in entries:
        blocks.append(f"Company: {e.company_name}")
        blocks.append(f"  Method:  {e.method}")
        blocks.append(f"  URL:     {e.linkedin_company_url or '(none)'}")
        blocks.append(f"  Status:  {e.status}")
        if e.candidates:
            blocks.append(f"  Candidates: {', '.join(e.candidates)}")
        if e.notes:
            blocks.append(f"  Notes:   {e.notes}")
        blocks.append("")

    if append and path.exists():
        path.write_text(path.read_text(encoding="utf-8") + "\n".join(blocks), encoding="utf-8")
    else:
        header = ["LinkedIn URL Resolution Log", "=" * 60, ""]
        path.write_text("\n".join(header + blocks), encoding="utf-8")


def write_summary_from_csv(df: pd.DataFrame, path: Path) -> None:
    counts = df["status"].value_counts().to_dict()
    review_rows = df[df["status"] == "needs_review"]
    lines = [
        "LinkedIn URL Resolution Summary",
        "=" * 40,
        f"Total companies in CSV: {len(df)}",
        f"  verified:      {counts.get('verified', 0)}",
        f"  needs_review:  {counts.get('needs_review', 0)}",
        f"  not_found:     {counts.get('not_found', 0)}",
        f"  pending:       {counts.get('pending', 0)}",
        "",
        "Manual confirmation recommended before Phase 3:",
    ]
    if len(review_rows):
        for _, row in review_rows.iterrows():
            lines.append(
                f"  - {row['company_name']}: {row.get('linkedin_company_url', '') or 'no URL'}"
            )
    else:
        lines.append("  (none)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_csv(df: pd.DataFrame, entries: list[ResolutionResult], path: Path) -> None:
    by_name = {e.company_name: e for e in entries}
    for idx, row in df.iterrows():
        name = str(row["company_name"]).strip()
        if name not in by_name:
            continue
        res = by_name[name]
        df.at[idx, "linkedin_company_url"] = res.linkedin_company_url
        df.at[idx, "status"] = res.status
    df.to_csv(path, index=False)


def run_resolution(
    df: pd.DataFrame,
    *,
    companies: list[str] | None = None,
) -> list[ResolutionResult]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    targets = df
    if companies:
        norm = {c.lower(): c for c in df["company_name"].astype(str)}
        selected = []
        for want in companies:
            key = want.lower().strip()
            if key in norm:
                selected.append(norm[key])
            else:
                # partial match
                for k, v in norm.items():
                    if key in k or k in key:
                        selected.append(v)
                        break
        targets = df[df["company_name"].isin(selected)]

    results: list[ResolutionResult] = []
    for _, row in targets.iterrows():
        name = str(row["company_name"]).strip()
        website = str(row.get("website", "")).strip()
        print(f"Resolving: {name} ...", flush=True)
        res = resolve_company(name, website, session)
        results.append(res)
        print(f"  -> {res.status}: {res.linkedin_company_url or '—'} ({res.method})", flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "linkedin_urls.csv",
    )
    parser.add_argument(
        "--exhibitors",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "exhibitors_clean.csv",
        help="Source of truth for exhibitor website URLs (used for crawling)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="Resolve pilot set: Aircover, Oracle, Equilar, Gong, LinkedIn",
    )
    parser.add_argument(
        "--companies",
        nargs="*",
        help="Resolve only these company names",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Resolve all non-verified companies",
    )
    parser.add_argument(
        "--retry-not-found",
        action="store_true",
        help="Re-resolve rows with status not_found only",
    )
    parser.add_argument(
        "--no-js",
        action="store_true",
        help="Skip headless-browser footer rendering (faster; misses JS-only social links)",
    )
    parser.add_argument(
        "--fix-csv-urls",
        action="store_true",
        help="Sanitize website URLs in exhibitors_clean.csv and linkedin_urls.csv, then exit",
    )
    args = parser.parse_args()

    data_dir = args.data_dir
    exhibitors_path = args.exhibitors
    linkedin_path = args.input

    if args.fix_csv_urls:
        n = fix_csv_website_urls(exhibitors_path, linkedin_path)
        print(f"Sanitized website URLs ({n} row(s) changed).")
        print(f"  {exhibitors_path}")
        print(f"  {linkedin_path}")
        return 0

    global _USE_JS_RENDER
    _USE_JS_RENDER = not args.no_js
    if _USE_JS_RENDER and not js_rendering_available():
        print(
            "Note: playwright not installed — footer JS rendering disabled; "
            "some sites (e.g. Salesforce) may resolve via search only. "
            "Install: pip install playwright && python -m playwright install chromium",
            file=sys.stderr,
        )

    df = merge_exhibitor_websites(load_dataframe(args.input), args.exhibitors)

    log_path = data_dir / "linkedin_urls_resolution_log.txt"
    summary_path = data_dir / "linkedin_urls_summary.txt"

    if args.pilot:
        companies = [
            "Aircover",
            "Oracle, Inc.",
            "Equilar, Inc.",
            "Gong",
            "LinkedIn",
        ]
    elif args.companies:
        companies = args.companies
    elif args.retry_not_found:
        df_targets = df[df["status"].fillna("") == "not_found"]
        if len(df_targets) == 0:
            print("No not_found rows to retry.", file=sys.stderr)
            return 0
        companies = df_targets["company_name"].tolist()
        print(f"Retry not_found: {len(companies)} companies...", flush=True)
    elif args.full:
        # Re-resolve anything not yet verified (pending, not_found, needs_review)
        df_targets = df[df["status"].fillna("") != "verified"]
        if len(df_targets) == 0:
            print("All rows already verified.", file=sys.stderr)
            return 0
        companies = df_targets["company_name"].tolist()
        print(f"Full run: resolving {len(companies)} companies...", flush=True)
    else:
        print("Specify --pilot, --full, --retry-not-found, or --companies", file=sys.stderr)
        return 1

    results = run_resolution(df, companies=companies)

    append_log = (args.full or args.retry_not_found) and log_path.exists()
    write_log(results, log_path, append=append_log)
    update_csv(df, results, args.input)
    write_summary_from_csv(load_dataframe(args.input), summary_path)

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("\nPilot/full run complete:")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    print(f"Log: {log_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared website URL normalization (strip accidental city/country suffixes)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Stops at comma so "https://www.nooks.ai/,San Francisco" -> https://www.nooks.ai/
URL_IN_TEXT_RE = re.compile(r"https?://[^\s,<>'\"]+", re.IGNORECASE)


def normalize_website(url: str) -> str:
    """
    Return a single fetchable website URL.

    Handles values where city/country were concatenated, e.g.
    ``https://www.nooks.ai/,San Francisco`` or ``https://www.richardson.com/,Philadelphia,United``.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    match = URL_IN_TEXT_RE.search(raw)
    if match:
        raw = match.group(0).rstrip(".,;)")
    elif "," in raw:
        raw = raw.split(",", 1)[0].strip()

    if not raw:
        return ""

    if not raw.startswith(("http://", "https://")):
        head = raw.split(",")[0].strip().lstrip("/")
        if "." in head and " " not in head:
            raw = "https://" + head
        else:
            return ""

    parsed = urlparse(raw)
    if not parsed.netloc:
        return ""

    path = parsed.path or "/"
    if "," in path or path.startswith("/,"):
        path = "/"

    result = f"{parsed.scheme}://{parsed.netloc}{path}"
    if parsed.query:
        result += f"?{parsed.query}"
    return result


def sanitize_website_column(series):
    """Apply normalize_website to a pandas Series."""
    return series.fillna("").astype(str).map(normalize_website)

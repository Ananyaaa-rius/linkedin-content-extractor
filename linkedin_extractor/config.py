"""Shared paths and tunable defaults (override via CLI flags or environment)."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"

# Conference scope — override for a future event year without editing search logic
CONFERENCE_YEAR = int(os.environ.get("GARTNER_CONFERENCE_YEAR", "2026"))

RAW_EXHIBITORS_CSV = RAW_DIR / "gartner_exhibitors.csv"
EXHIBITORS_CLEAN_CSV = DATA_DIR / "exhibitors_clean.csv"
LINKEDIN_URLS_CSV = DATA_DIR / "linkedin_urls.csv"
POSTS_OUTPUT_CSV = DATA_DIR / "gartner_cso_posts_extracted.csv"
POSTS_SUMMARY_TXT = DATA_DIR / "gartner_cso_posts_extract_summary.txt"
SESSION_FILE = DATA_DIR / "linkedin_session.json"

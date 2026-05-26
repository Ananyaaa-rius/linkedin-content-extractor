#!/usr/bin/env python3
"""Entry point: python main.py <command> [options]

Commands:
  exhibitors   Step 1 — clean raw Gartner exhibitor CSV
  urls         Step 2 — resolve LinkedIn company page URLs
  session      One-time LinkedIn login (for Phase 3)
  posts        Step 3 — three-phase post extraction
  all          Run steps 1–3 in sequence
"""

from linkedin_extractor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

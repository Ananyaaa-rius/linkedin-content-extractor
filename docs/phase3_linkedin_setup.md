# Phase 3 — LinkedIn session setup

Phase 3 scrapes each company's `/posts/` feed using Playwright and a saved browser session.

---

## Setup

```bash
pip install playwright
python -m playwright install chromium
python main.py session
```

1. A Chromium window opens — log in to LinkedIn.
2. Press **Enter** in the terminal when finished.
3. Session is saved to `data/linkedin_session.json` (gitignored).

---

## Run Phase 3

```bash
python main.py posts --full --linkedin
```

Subset:

```bash
python main.py posts --companies "Gong" "Salesforce" --linkedin
```

---

## Security

- `data/linkedin_session.json` contains session cookies — **never commit it**
- No passwords or `li_at` values are stored in source code
- Optional: set `LINKEDIN_STORAGE_STATE` if you store the file outside `data/`

---

## Troubleshooting

| Symptom | Action |
|---------|--------|
| Phase 3 adds 0 posts | Re-run `python main.py session`; LinkedIn may have expired the session |
| `login_required` in logs | Session missing or invalid |
| Slow run | Expected — delays between companies reduce blocking |

Without a session, Phases 1–2 (mine + DuckDuckGo) still run; only Phase 3 is skipped.

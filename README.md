# Gartner CSO LinkedIn Content Extractor

Collect LinkedIn posts about the **Gartner CSO & Sales Leader Conference** for companies listed as Gartner exhibitors.

The tool runs in three steps: clean exhibitor data, resolve LinkedIn company URLs, then extract and filter relevant posts using web search and optional authenticated LinkedIn scraping.

---

## Features

- Parse a raw Gartner exhibitor export into a deduplicated company list
- Resolve `linkedin.com/company/...` URLs from websites, sitemaps, and public search
- Find conference-related posts per exhibitor via DuckDuckGo (no LinkedIn API key)
- Optional Phase 3 scrape of company `/posts/` pages with a saved browser session
- Filter out rival exhibitor pages, unrelated Gartner content, and wrong years
- One row per company even when no posts are found

---

## Requirements

- Python 3.10+
- Windows, macOS, or Linux
- Network access for search and scraping

---

## Installation

```bash
git clone <your-repo-url>
cd linkedin-content-extractor

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

---

## Quick start

### 1. Add the raw exhibitor file

Export the exhibitor list from Gartner and save it as:

```
data/raw/gartner_exhibitors.csv
```

Or pass any path with `--input`.

### 2. Run the pipeline

```bash
# Step 1 — clean exhibitors
python main.py exhibitors

# Step 2 — resolve LinkedIn URLs (all pending / non-verified rows)
python main.py urls --full

# Optional — save LinkedIn login for Phase 3 (opens a browser once)
python main.py session

# Step 3 — extract posts (Phases 1–3)
python main.py posts --full --linkedin
```

Run all steps in one command:

```bash
python main.py all --linkedin
```

---

## Pipeline overview

```
gartner_exhibitors.csv
        │
        ▼  main.py exhibitors
exhibitors_clean.csv ──► linkedin_urls.csv (skeleton)
        │
        ▼  main.py urls --full
linkedin_urls.csv (verified URLs)
        │
        ▼  main.py posts --full [--linkedin]
gartner_cso_posts_extracted.csv
gartner_cso_posts_extract_summary.txt
```

### Post extraction phases (`posts` command)

| Phase | Description | Skip flag |
|-------|-------------|-----------|
| **1 — Mine** | Reuse posts from extra CSVs you pass with `--mine-csv` | `--skip-mine` |
| **2 — Search** | DuckDuckGo queries per company (`gartnercso`, conference terms, year) | `--mine-only` |
| **3 — LinkedIn** | Scrape company `/posts/` with `data/linkedin_session.json` | omit `--linkedin` |

Example (search + LinkedIn only, no mining):

```bash
python main.py posts --full --linkedin --skip-mine
```

Subset of companies:

```bash
python main.py posts --companies "Gong" "Varicent" --linkedin
```

---

## Output

### Final CSV — `data/gartner_cso_posts_extracted.csv`

| Column | Description |
|--------|-------------|
| `company_name` | Exhibitor name |
| `linkedin_url` | Resolved company page |
| `post_date` | Display date when known |
| `post_text` | Snippet or post body |
| `post_link` | LinkedIn activity URL |
| `posted_by` | `Company Page` or employee author |
| `status` | `Post Found` or `No posts found` |

### Other generated files

| File | Step |
|------|------|
| `data/exhibitors_clean.csv` | 1 |
| `data/linkedin_urls.csv` | 1 (skeleton) / 2 (filled) |
| `data/exhibitors_summary.txt` | 1 |
| `data/linkedin_urls_summary.txt` | 2 |
| `data/gartner_cso_posts_extract_summary.txt` | 3 |

---

## Configuration

Paths and the conference year are centralized in `linkedin_extractor/config.py`.  
All file paths are **relative to the project root** (no machine-specific absolute paths).

| Setting | Default | Override |
|---------|---------|----------|
| Conference year | `2026` | Environment variable `GARTNER_CONFERENCE_YEAR` |
| Raw exhibitor CSV | `data/raw/gartner_exhibitors.csv` | `python main.py exhibitors --input <path>` |
| LinkedIn URLs | `data/linkedin_urls.csv` | `--input` on `posts` / `urls` |
| Output posts CSV | `data/gartner_cso_posts_extracted.csv` | `--output` on `posts` |
| Session file | `data/linkedin_session.json` | `--session` or `main.py session --output` |

### What is *not* hardcoded

- No absolute paths (e.g. `C:\Users\...` or `Downloads\`)
- No API keys or LinkedIn passwords in source code
- Company list comes from your CSV inputs, not a fixed list in code (except the optional `--pilot` test set on `urls`)

### Domain-specific rules (intentional)

These are **business logic** for this conference, not environment-specific hacks:

- Search keywords and hashtags (`gartnercso`, CSO conference terms) in `scripts/extract_gartner_cso_posts.py`
- `HARD_CASES` in `scripts/resolve_linkedin_urls.py` — flags ambiguous exhibitor names (e.g. Gong, 1mind) for review
- LinkedIn activity-ID date heuristics tuned for recent conference windows
- `--pilot` on `urls` resolves five sample companies for smoke testing

To target another year, set `GARTNER_CONFERENCE_YEAR=2027` before running `posts`.

---

## LinkedIn session (Phase 3)

LinkedIn limits anonymous access. For Phase 3:

1. `python main.py session` — log in once in the opened browser
2. `python main.py posts --full --linkedin`

Details: [docs/phase3_linkedin_setup.md](docs/phase3_linkedin_setup.md)

**Never commit** `data/linkedin_session.json` (listed in `.gitignore`).

---

## Project structure

```
.
├── main.py                         # CLI entry point
├── linkedin_extractor/
│   ├── cli.py                      # Command routing
│   ├── config.py                   # Paths + GARTNER_CONFERENCE_YEAR
│   └── paths.py
├── scripts/
│   ├── parse_exhibitors.py         # Step 1
│   ├── resolve_linkedin_urls.py    # Step 2
│   ├── extract_gartner_cso_posts.py# Step 3
│   ├── capture_linkedin_session.py
│   └── url_utils.py
├── data/
│   ├── raw/                        # Place gartner_exhibitors.csv here
│   ├── exhibitors_clean.csv
│   ├── linkedin_urls.csv
│   └── gartner_cso_posts_extracted.csv
├── docs/
│   └── phase3_linkedin_setup.md
├── requirements.txt
└── .gitignore
```

---

## CLI reference

```bash
python main.py --help
python main.py exhibitors --help
python main.py urls --help
python main.py posts --help
```

| Command | Common flags |
|---------|----------------|
| `exhibitors` | `--input`, `--data-dir` |
| `urls` | `--full`, `--pilot`, `--companies NAME ...`, `--retry-not-found`, `--no-js` |
| `session` | `--output` |
| `posts` | `--full`, `--companies`, `--linkedin`, `--skip-mine`, `--mine-csv FILE ...` |
| `all` | `--linkedin`, `--skip-mine`, `--no-js`, `--input` |

Direct script invocation (equivalent):

```bash
python scripts/parse_exhibitors.py --input data/raw/gartner_exhibitors.csv
python scripts/resolve_linkedin_urls.py --full
python scripts/extract_gartner_cso_posts.py --full --linkedin
```

---

## Security and ethics

- Respect LinkedIn’s terms of service; use a personal session only for research you are authorized to perform
- Rate limiting and delays are built into HTTP and search calls
- Do not share or commit session cookies

---

## License

Add your license here (e.g. MIT) before publishing.

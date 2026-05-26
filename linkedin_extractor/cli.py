"""Unified CLI for the three-step Gartner CSO LinkedIn extraction pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from linkedin_extractor import DATA_DIR, PROJECT_ROOT, RAW_DIR
from linkedin_extractor.paths import (
    EXHIBITORS_CLEAN_CSV,
    LINKEDIN_URLS_CSV,
    POSTS_OUTPUT_CSV,
    RAW_EXHIBITORS_CSV,
    SESSION_FILE,
)

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _run_exhibitors(args: argparse.Namespace) -> int:
    from parse_exhibitors import main as run

    sys.argv = [
        "parse_exhibitors",
        "--input",
        str(args.input),
        "--data-dir",
        str(args.data_dir),
    ]
    return run()


def _run_urls(args: argparse.Namespace) -> int:
    from resolve_linkedin_urls import main as run

    argv = ["resolve_linkedin_urls", "--data-dir", str(args.data_dir)]
    if args.fix_csv_urls:
        argv.append("--fix-csv-urls")
    elif args.pilot:
        argv.append("--pilot")
    elif args.full:
        argv.append("--full")
    elif args.retry_not_found:
        argv.append("--retry-not-found")
    elif args.companies:
        argv.extend(["--companies", *args.companies])
    else:
        print(
            "Specify --pilot, --full, --retry-not-found, --companies, or --fix-csv-urls",
            file=sys.stderr,
        )
        return 1
    if args.no_js:
        argv.append("--no-js")
    sys.argv = argv
    return run()


def _run_posts(args: argparse.Namespace) -> int:
    from extract_gartner_cso_posts import main as run

    argv = [
        "extract_gartner_cso_posts",
        "--input",
        str(args.input),
        "--output",
        str(args.output),
        "--session",
        str(args.session),
    ]
    if args.full:
        argv.append("--full")
    elif args.companies:
        argv.extend(["--companies", *args.companies])
    else:
        print("Specify --full or --companies", file=sys.stderr)
        return 1
    if args.linkedin:
        argv.append("--linkedin")
    if args.mine_only:
        argv.append("--mine-only")
    if args.skip_mine:
        argv.append("--skip-mine")
    for path in getattr(args, "mine_csv", None) or []:
        argv.extend(["--mine-csv", str(path)])
    sys.argv = argv
    return run()


def _run_session(args: argparse.Namespace) -> int:
    from capture_linkedin_session import main as run

    sys.argv = ["capture_linkedin_session", "--output", str(args.output)]
    return run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gartner CSO & Sales Leader Conference — LinkedIn content extractor.\n\n"
            "Pipeline: (1) clean exhibitors -> (2) resolve LinkedIn URLs -> "
            "(3) extract posts in three phases."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py exhibitors --input data/raw/gartner_exhibitors.csv\n"
            "  python main.py urls --full\n"
            "  python main.py session\n"
            "  python main.py posts --full --linkedin\n"
            "  python main.py all --linkedin\n"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help=f"Data directory (default: {DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Step 1
    p1 = sub.add_parser(
        "exhibitors",
        help="Step 1: parse raw Gartner exhibitor CSV -> exhibitors_clean + linkedin_urls skeleton",
    )
    p1.add_argument(
        "--input",
        type=Path,
        default=RAW_EXHIBITORS_CSV,
        help=f"Raw exhibitor export CSV (default: {RAW_EXHIBITORS_CSV})",
    )
    p1.set_defaults(func=_run_exhibitors)

    # Step 2
    p2 = sub.add_parser(
        "urls",
        help="Step 2: resolve linkedin.com/company/ URLs from exhibitor websites",
    )
    p2.add_argument("--pilot", action="store_true", help="Resolve 5-company pilot set")
    p2.add_argument("--full", action="store_true", help="Resolve all non-verified companies")
    p2.add_argument(
        "--retry-not-found",
        action="store_true",
        help="Re-resolve rows with status not_found",
    )
    p2.add_argument("--companies", nargs="*", metavar="NAME")
    p2.add_argument(
        "--fix-csv-urls",
        action="store_true",
        help="Sanitize website URLs in CSVs and exit",
    )
    p2.add_argument(
        "--no-js",
        action="store_true",
        help="Skip Playwright footer rendering (faster)",
    )
    p2.set_defaults(func=_run_urls)

    # LinkedIn session (optional, for Phase 3)
    ps = sub.add_parser(
        "session",
        help="One-time: save LinkedIn login for Phase 3 scraping",
    )
    ps.add_argument("--output", type=Path, default=SESSION_FILE)
    ps.set_defaults(func=_run_session)

    # Step 3
    p3 = sub.add_parser(
        "posts",
        help="Step 3: extract Gartner CSO posts (Phase 1 mine, Phase 2 search, Phase 3 LinkedIn)",
    )
    p3.add_argument("--input", type=Path, default=LINKEDIN_URLS_CSV)
    p3.add_argument("--output", type=Path, default=POSTS_OUTPUT_CSV)
    p3.add_argument("--session", type=Path, default=SESSION_FILE)
    p3.add_argument("--full", action="store_true", help="All companies in linkedin_urls.csv")
    p3.add_argument("--companies", nargs="+", metavar="NAME")
    p3.add_argument(
        "--linkedin",
        action="store_true",
        help="Run Phase 3: scrape company /posts/ (needs data/linkedin_session.json)",
    )
    p3.add_argument(
        "--mine-only",
        action="store_true",
        help="Phase 1 only — reuse existing project CSVs, no web search",
    )
    p3.add_argument("--skip-mine", action="store_true", help="Skip Phase 1")
    p3.add_argument(
        "--mine-csv",
        type=Path,
        nargs="*",
        default=[],
        metavar="CSV",
        help="Extra CSV file(s) for Phase 1 mining",
    )
    p3.set_defaults(func=_run_posts)

    # Full pipeline
    pall = sub.add_parser(
        "all",
        help="Run exhibitors, urls --full, posts --full (pass --linkedin for Phase 3)",
    )
    pall.add_argument(
        "--input",
        type=Path,
        default=RAW_EXHIBITORS_CSV,
        help="Raw exhibitor CSV for step 1",
    )
    pall.add_argument("--linkedin", action="store_true")
    pall.add_argument("--skip-mine", action="store_true")
    pall.add_argument("--no-js", action="store_true")
    pall.set_defaults(func=_run_all)

    return parser


def _run_all(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not args.input.exists():
        print(
            f"Raw exhibitor file not found: {args.input}\n"
            f"Place your Gartner export at: {RAW_EXHIBITORS_CSV}",
            file=sys.stderr,
        )
        return 1

    exhibitors_args = argparse.Namespace(
        input=args.input,
        data_dir=args.data_dir,
    )
    urls_args = argparse.Namespace(
        data_dir=args.data_dir,
        pilot=False,
        full=True,
        retry_not_found=False,
        companies=None,
        fix_csv_urls=False,
        no_js=args.no_js,
    )
    posts_args = argparse.Namespace(
        input=LINKEDIN_URLS_CSV,
        output=POSTS_OUTPUT_CSV,
        session=SESSION_FILE,
        data_dir=args.data_dir,
        full=True,
        companies=None,
        linkedin=args.linkedin,
        mine_only=False,
        skip_mine=args.skip_mine,
        mine_csv=getattr(args, "mine_csv", []) or [],
    )

    steps = [
        ("Step 1/3 — Clean exhibitors", _run_exhibitors, exhibitors_args),
        ("Step 2/3 — Resolve LinkedIn URLs", _run_urls, urls_args),
        ("Step 3/3 — Extract posts", _run_posts, posts_args),
    ]
    for label, fn, step_args in steps:
        print(f"\n{'=' * 55}\n{label}\n{'=' * 55}\n")
        code = fn(step_args)
        if code != 0:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

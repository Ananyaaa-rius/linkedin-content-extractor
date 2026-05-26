#!/usr/bin/env python3
"""Parse Gartner exhibitor CSV into a clean, deduplicated company list."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import pandas as pd

from url_utils import sanitize_website_column

# Duplicate "Description" column from export becomes Description.1 in pandas
DESCRIPTION_PRODUCT_COL = "Description.1"

ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def load_exhibitor_csv(path: Path) -> tuple[pd.DataFrame, str | None]:
    """Load CSV, trying common encodings. Returns (dataframe, error_message)."""
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=encoding)
            return df, None
        except UnicodeDecodeError as exc:
            last_error = exc
    return pd.DataFrame(), f"Could not decode file with {ENCODINGS}: {last_error}"


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename duplicate Description column and standardize header names."""
    rename_map: dict[str, str] = {}
    if DESCRIPTION_PRODUCT_COL in df.columns:
        rename_map[DESCRIPTION_PRODUCT_COL] = "product_description"
    # pandas may also mangle as Description.1 — handle both
    for col in df.columns:
        if col.startswith("Description") and col != "Description":
            rename_map[col] = "product_description"

    df = df.rename(columns=rename_map)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse_exhibitors(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Extract unique exhibitor companies (Name non-empty).
    Ignores product continuation rows where Name is empty.
    """
    errors: list[str] = []

    if "Name" not in df.columns:
        return pd.DataFrame(), ["Missing required column: Name"]

    total_rows = len(df)

    # Company rows only
    name_series = df["Name"].fillna("").astype(str).str.strip()
    company_mask = name_series != ""
    company_df = df.loc[company_mask].copy()
    company_df["company_name"] = name_series[company_mask]

    continuation_rows = total_rows - len(company_df)
    if continuation_rows:
        errors.append(
            f"Ignored {continuation_rows} product continuation row(s) (empty Name)."
        )

    # Map source columns
    def col_or_blank(frame: pd.DataFrame, col: str) -> pd.Series:
        if col in frame.columns:
            return frame[col].fillna("").astype(str).str.strip()
        errors.append(f"Optional column missing: {col}")
        return pd.Series([""] * len(frame), index=frame.index)

    clean = pd.DataFrame(
        {
            "company_name": company_df["company_name"],
            "booth": col_or_blank(company_df, "Booths"),
            "website": col_or_blank(company_df, "Website"),
            "city": col_or_blank(company_df, "City"),
            "country": col_or_blank(company_df, "Country"),
            "exhibitor_type": col_or_blank(company_df, "ExhibitorType"),
        }
    )

    clean["website"] = sanitize_website_column(clean["website"])

    # Deduplicate by normalized company name (keep first occurrence)
    before_dedup = len(clean)
    clean = clean.drop_duplicates(subset=["company_name"], keep="first")
    dupes_removed = before_dedup - len(clean)
    if dupes_removed:
        errors.append(f"Removed {dupes_removed} duplicate company name(s).")

    if len(clean) != before_dedup and before_dedup != len(company_df):
        pass  # covered above

    # Attach parse metadata for summary
    clean.attrs["total_rows_parsed"] = total_rows
    clean.attrs["company_rows"] = len(company_df)
    clean.attrs["continuation_rows"] = continuation_rows

    return clean, errors


def write_outputs(
    clean: pd.DataFrame,
    errors: list[str],
    data_dir: Path,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)

    clean_path = data_dir / "exhibitors_clean.csv"
    clean.to_csv(clean_path, index=False)

    linkedin_path = data_dir / "linkedin_urls.csv"
    linkedin_df = pd.DataFrame(
        {
            "company_name": clean["company_name"],
            "website": clean["website"],
            "booth": clean["booth"],
            "linkedin_company_url": "",
            "status": "pending",
        }
    )
    linkedin_df.to_csv(linkedin_path, index=False)

    summary_path = data_dir / "exhibitors_summary.txt"
    total = clean.attrs.get("total_rows_parsed", "?")
    company_rows = clean.attrs.get("company_rows", "?")
    continuation = clean.attrs.get("continuation_rows", "?")

    lines = [
        "Gartner Exhibitors CSV — Parse Summary",
        "=" * 40,
        f"Total rows parsed:        {total}",
        f"Company rows (Name set):  {company_rows}",
        f"Continuation rows skipped: {continuation}",
        f"Unique companies output:  {len(clean)}",
        "",
        "Output files:",
        f"  - {clean_path}",
        f"  - {linkedin_path}",
        "",
    ]
    if errors:
        lines.append("Notes / parse messages:")
        for msg in errors:
            lines.append(f"  - {msg}")
    else:
        lines.append("Parse errors: none")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--input",
        type=Path,
        default=_root / "data" / "raw" / "gartner_exhibitors.csv",
        help="Path to raw Gartner exhibitor CSV (export from Gartner)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="Directory for output CSV files",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 1

    df, load_error = load_exhibitor_csv(args.input)
    if load_error:
        print(load_error, file=sys.stderr)
        return 1

    df = normalize_columns(df)
    clean, errors = parse_exhibitors(df)

    if clean.empty:
        print("No exhibitor companies extracted.", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    write_outputs(clean, errors, args.data_dir)
    print(f"Wrote {len(clean)} companies to {args.data_dir}")
    for err in errors:
        print(f"  note: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

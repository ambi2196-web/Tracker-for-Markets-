#!/usr/bin/env python3
"""
Regime data — NIFTY 50 index levels, closing the Phase 7 gap flagged repeatedly in
render_summary.py: §9 requires signal performance to never be reported "without its beta
context." Sourced via yfinance (§4.2/§6: allowed, "backfill and gap repair only"),
unaffected by NSE's own anti-bot blocking since it hits Yahoo's infrastructure.

Not point-in-time archival in the same sense as the NSE collectors — yfinance can be
re-queried for any historical date at any time, so there's no --replay mode; --live just
fetches whatever date range the prices table currently covers and (re)ingests it.

Usage:
    python src/build_index.py --live
    python src/build_index.py --live --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from collect import index_prices as index_collect  # noqa: E402
from parse import index_prices as index_parse  # noqa: E402
from build_db import create_schema  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DB_PATH = DATA_ROOT / "tracker.db"
PIPELINE_VERSION = "0.1.0"


def run_live(*, dry_run: bool) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone()
    if not row or not row[0]:
        print("no prices archived yet — nothing to fetch regime data for")
        conn.close()
        return 1

    start = date.fromisoformat(row[0])
    end = date.fromisoformat(row[1]) + timedelta(days=1)  # yfinance's `end` is exclusive

    if dry_run:
        print(f"[dry-run] would fetch {index_collect.TICKER} from {start} to {end}. Nothing written.")
        conn.close()
        return 0

    path = index_collect.fetch_and_archive(start, end, DATA_ROOT)
    if path is None:
        print("yfinance returned no data")
        conn.close()
        return 1

    rows = index_parse.parse_file(path)
    now_iso = datetime.now(timezone.utc).isoformat()
    source_file = path.relative_to(DATA_ROOT).as_posix()

    create_schema(conn)
    conn.executemany(
        """
        INSERT INTO index_prices (index_name, date, close, source, source_file, ingested_at, pipeline_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(index_name, date) DO UPDATE SET
            close=excluded.close, source_file=excluded.source_file,
            ingested_at=excluded.ingested_at, pipeline_version=excluded.pipeline_version
        """,
        [
            (index_collect.INDEX_NAME, r.date, r.close, "yfinance", source_file, now_iso, PIPELINE_VERSION)
            for r in rows
        ],
    )
    conn.commit()
    print(f"ingested {len(rows)} {index_collect.INDEX_NAME} rows from {path.name} -> {DB_PATH}")
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run_live(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Phase 8 — sector map ingest and dispersion monitor (work item 3).

Fetches NSE's index constituent file (which carries NSE's own Industry taxonomy) into
`sector_map`, then runs the dispersion monitor over the trailing window and records any
firing sector as a row in `research_prompts`.

A research prompt is not a signal. It never reaches `ledger`, carries no entry price or
stop, and is excluded from every performance statistic. It means "go read about this".

Modes mirror the other build scripts:
    --live       Fetch constituents, ingest, run dispersion, write prompts.
    --dry-run    Compute and print, write nothing.
    --no-fetch   Skip the network fetch and reuse the newest archived snapshot.

Usage:
    python src/build_sectors.py --live
    python src/build_sectors.py --live --dry-run
    python src/build_sectors.py --live --no-fetch
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import dispersion  # noqa: E402
from build_db import create_schema  # noqa: E402
from collect import sector_map as sector_collect  # noqa: E402
from parse import sector_map as sector_parse  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DB_PATH = DATA_ROOT / "tracker.db"
PIPELINE_VERSION = "0.1.0"


def _newest_snapshot(data_root: Path) -> Path | None:
    files = sorted((data_root / "raw" / "sector_map").glob("*.csv"))
    return files[-1] if files else None


def ingest_sector_map(conn: sqlite3.Connection, path: Path) -> int:
    rows = sector_parse.parse_file(path)
    now = datetime.now(timezone.utc).isoformat()
    source_file = path.relative_to(DATA_ROOT).as_posix()
    conn.executemany(
        """
        INSERT INTO sector_map (symbol, sector, company_name, source, source_file,
                                 ingested_at, pipeline_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            sector=excluded.sector, company_name=excluded.company_name,
            source=excluded.source, source_file=excluded.source_file,
            ingested_at=excluded.ingested_at, pipeline_version=excluded.pipeline_version
        """,
        [(r.symbol, r.sector, r.company_name, sector_collect.SOURCE_NAME, source_file,
          now, PIPELINE_VERSION) for r in rows],
    )
    return len(rows)


def run(*, dry_run: bool, no_fetch: bool) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)

        if no_fetch:
            path = _newest_snapshot(DATA_ROOT)
            if path is None:
                print("no archived sector-map snapshot and --no-fetch given; nothing to do")
                return 1
        elif dry_run:
            path = _newest_snapshot(DATA_ROOT)
            if path is None:
                print("[dry-run] would fetch NSE constituent file; no archived snapshot to compute from yet")
                return 0
            print(f"[dry-run] would fetch NSE constituent file; computing from {path.name}")
        else:
            path = sector_collect.fetch_and_archive(DATA_ROOT)
            print(f"archived {path.relative_to(DATA_ROOT).as_posix()}")

        if not dry_run:
            n = ingest_sector_map(conn, path)
            print(f"ingested {n} sector-map rows from {path.name}")
        else:
            n = len(sector_parse.parse_file(path))
            print(f"[dry-run] would ingest {n} sector-map rows from {path.name}")

        dispersions = dispersion.compute(conn)
        if not dispersions:
            print("no dispersion output — needs both sector_map and enough price history")
            if not dry_run:
                conn.commit()
            return 0

        firing = [d for d in dispersions if d.fires]
        print(f"\n{len(dispersions)} sectors evaluated over {dispersion.LOOKBACK_TRADING_DAYS} trading days "
              f"(trigger: spread <= {dispersion.SECTOR_SPREAD_TRIGGER}pp, min {dispersion.MIN_CONSTITUENTS} constituents)")
        for d in sorted(dispersions, key=lambda x: (x.spread if x.spread is not None else 0)):
            flag = "  <-- FIRES" if d.fires else ""
            spread = f"{d.spread:+.1f}pp" if d.spread is not None else "n/a"
            print(f"  {d.sector:<38} n={d.n_constituents:<4} median={d.median_return:+7.2f}% "
                  f"spread={spread:>9} breadth={d.breadth:.0%}{flag}")

        if dry_run:
            print(f"\n[dry-run] would write {len(firing)} research prompt(s). Nothing written.")
            return 0

        written = dispersion.write_research_prompts(
            conn, dispersions, detected_date=date.today().isoformat()
        )
        conn.commit()
        print(f"\nwrote {written} new research prompt(s) -> {DB_PATH}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-fetch", action="store_true",
                         help="reuse the newest archived snapshot instead of fetching")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, no_fetch=args.no_fetch)


if __name__ == "__main__":
    raise SystemExit(main())

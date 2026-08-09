#!/usr/bin/env python3
"""
One-time-ish backfill utility: fetches historical raw archive using the same collector
modules as run_collect.py (so archived files are indistinguishable from ones collected
live), but reuses a single NSEClient session across the whole date range instead of paying
warm-up cost per day. Does NOT touch data/health.json — health is a record of the live
pipeline's real attempts, not backfill research, so this writes its own log only.

Usage:
    python -m collect.backfill --from 2026-06-01 --to 2026-08-04
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

from . import asm, bhavcopy, calendar, fo_ban
from .nse_client import NSEClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
COLLECTORS = [bhavcopy, fo_ban, asm]


def backfill(from_date: date, to_date: date) -> None:
    client = NSEClient()
    d = from_date
    summary = {mod.SOURCE: {"success": 0, "already": 0, "failed": 0} for mod in COLLECTORS}

    while d <= to_date:
        if not calendar.is_trading_day(d):
            d += timedelta(days=1)
            continue
        for mod in COLLECTORS:
            outcome = mod.collect(d, DATA_ROOT, client)
            if outcome.status == "success":
                summary[mod.SOURCE]["success"] += 1
            elif outcome.status == "already_archived":
                summary[mod.SOURCE]["already"] += 1
            else:
                summary[mod.SOURCE]["failed"] += 1
                print(f"{d} [{mod.SOURCE}] {outcome.status}: {outcome.detail}")
        d += timedelta(days=1)

    print("\n--- backfill summary ---")
    for source, counts in summary.items():
        print(f"{source}: {counts['success']} fetched, {counts['already']} already archived, "
              f"{counts['failed']} failed (likely holidays or genuine gaps)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", required=True, type=str)
    parser.add_argument("--to", dest="to_date", required=True, type=str)
    args = parser.parse_args()
    backfill(date.fromisoformat(args.from_date), date.fromisoformat(args.to_date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

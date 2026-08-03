#!/usr/bin/env python3
"""
Phase 1 raw collector — daily entry point.

Usage:
    python run_collect.py                     # collect for today
    python run_collect.py --date 2026-08-03    # collect for a specific date (backfill/manual run)
    python run_collect.py --dry-run            # print what would be fetched, write nothing

Scope (per PHASED_IMPLEMENTATION_PLAN.md Phase 1): fetch the three daily files, write them
unmodified into data/raw/, log the attempt, update the health record. No parsing, no
database, no signals — that's Phase 2 onward.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from collect import asm, bhavcopy, calendar, fo_ban, health  # noqa: E402
from collect.nse_client import NSEClient  # noqa: E402

COLLECTORS = [bhavcopy, fo_ban, asm]
DATA_ROOT = REPO_ROOT / "data"
LOGS_ROOT = REPO_ROOT / "logs"


def setup_logging(run_date: date) -> logging.Logger:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_ROOT / f"collect_{run_date.strftime('%Y%m%d')}.log"
    logger = logging.getLogger("collect")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def run(run_date: date, *, dry_run: bool) -> int:
    logger = setup_logging(run_date)
    now_iso = datetime.now(timezone.utc).isoformat()

    if not calendar.is_trading_day(run_date):
        logger.info(
            "%s is a weekend/holiday (per data/seed/nse_holidays.csv) — no-op run, "
            "health timestamps touched only",
            run_date.isoformat(),
        )
        for mod in COLLECTORS:
            health.record_no_op(mod.SOURCE, now_iso, reason="weekend_or_holiday")
        return 0

    client = NSEClient()
    exit_code = 0
    for mod in COLLECTORS:
        health.record_attempt(mod.SOURCE, now_iso)
        outcome = mod.collect(run_date, DATA_ROOT, client, dry_run=dry_run)

        if outcome.status in ("success", "already_archived"):
            logger.info(
                "[%s] %s url=%s dest=%s bytes=%s elapsed=%.2fs",
                outcome.source,
                outcome.status,
                outcome.url,
                outcome.dest_path,
                outcome.bytes_written,
                outcome.elapsed_seconds or 0.0,
            )
            if outcome.status == "success" and not dry_run:
                health.record_success(mod.SOURCE, now_iso)
        else:
            logger.error("[%s] FAILED url=%s detail=%s", outcome.source, outcome.url, outcome.detail)
            if not dry_run:
                health.record_failure(mod.SOURCE, now_iso, outcome.detail)
            exit_code = 1

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, default: today")
    parser.add_argument("--dry-run", action="store_true", help="print what would be fetched, write nothing")
    args = parser.parse_args()

    run_date = date.fromisoformat(args.date) if args.date else date.today()
    return run(run_date, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

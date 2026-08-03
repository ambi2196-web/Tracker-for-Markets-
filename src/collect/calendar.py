"""
Trading-day check: weekends always; declared holidays from data/seed/nse_holidays.csv.

The holiday file ships with header-only. Populate it from NSE's official holiday
circular (see SIGNAL_TRACKER_REQUIREMENTS.md §4.4 — manual seed data, refreshed as
events occur). Until populated, holidays are indistinguishable from "not published
yet"/failure in the log; that is a known gap, not silently masked (see
NEXT_STEPS.md).
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

HOLIDAYS_PATH = Path(__file__).resolve().parents[2] / "data" / "seed" / "nse_holidays.csv"


def _load_holidays() -> set[date]:
    if not HOLIDAYS_PATH.exists():
        return set()
    holidays = set()
    with HOLIDAYS_PATH.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            raw = row.get("date", "").strip()
            if raw:
                holidays.add(date.fromisoformat(raw))
    return holidays


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:  # Saturday, Sunday
        return False
    return d not in _load_holidays()

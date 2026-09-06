"""
Regime comparison (SIGNAL_TRACKER_REQUIREMENTS.md §9: "signal performance is never
presented without its beta context"). Shared by render_summary.py, render_dashboard.py,
render_digest.py and verdict.py so no two surfaces can disagree.

Two different figures, deliberately kept apart:

- `compute_regime()` — NIFTY return over one global span covering the whole closed
  ledger. This is a *portfolio-level* context number and nothing else.
- `matched_window_return()` — NIFTY return over a single trade's own entry→exit window.

Phase 8 correctness note: cohort verdicts must use matched windows. Comparing a cohort
of 5-trading-day trades against a 12-month global span is not a comparison — the global
figure carries a year of drift the trades were never exposed to. The global number stays
for portfolio-level reporting, labelled as such.
"""
from __future__ import annotations

import sqlite3
from statistics import mean


def compute_regime(conn: sqlite3.Connection, track: str = "mechanism") -> dict | None:
    """Portfolio-level: NIFTY return over the full span one track's closed ledger covers.
    Track-scoped like every other aggregate — a span mixing a 5-day mechanism trade with a
    9-month discretionary hold describes neither. Returns {start_date, end_date,
    nifty_pct} or None if there's no closed sample on that track."""
    span = conn.execute(
        "SELECT MIN(entry_date), MAX(exit_date) FROM ledger WHERE exit_date IS NOT NULL AND track = ?",
        (track,),
    ).fetchone()
    if not span or not span[0]:
        return None
    start_date, end_date = span

    try:
        start_row = conn.execute(
            "SELECT close FROM index_prices WHERE date >= ? ORDER BY date ASC LIMIT 1", (start_date,)
        ).fetchone()
        end_row = conn.execute(
            "SELECT close FROM index_prices WHERE date <= ? ORDER BY date DESC LIMIT 1", (end_date,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None

    if not start_row or not end_row:
        return {"start_date": start_date, "end_date": end_date, "nifty_pct": None}

    nifty_pct = (end_row[0] / start_row[0] - 1.0) * 100.0
    return {"start_date": start_date, "end_date": end_date, "nifty_pct": nifty_pct}


def matched_window_return(conn: sqlite3.Connection, entry_date: str, exit_date: str) -> float | None:
    """NIFTY return over exactly one trade's holding window. Returns None when the index
    archive doesn't cover the window — the caller must drop that trade from the matched
    average rather than substituting a global figure."""
    if not entry_date or not exit_date:
        return None
    try:
        start_row = conn.execute(
            "SELECT date, close FROM index_prices WHERE date >= ? ORDER BY date ASC LIMIT 1", (entry_date,)
        ).fetchone()
        end_row = conn.execute(
            "SELECT date, close FROM index_prices WHERE date <= ? ORDER BY date DESC LIMIT 1", (exit_date,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None

    if not start_row or not end_row or not start_row[1]:
        return None
    # The resolved anchors must still bracket a real window; a trade whose entry resolves
    # past its own exit (archive gap at one end) is not comparable.
    if start_row[0] > end_row[0]:
        return None
    return (end_row[1] / start_row[1] - 1.0) * 100.0


def mean_matched_window_return(conn: sqlite3.Connection, windows: list[tuple[str, str]]) -> float | None:
    """Average of per-trade matched-window NIFTY returns across a cohort. None if no
    window in the cohort is computable."""
    values = [
        r for r in (matched_window_return(conn, entry, exit_) for entry, exit_ in windows)
        if r is not None
    ]
    return mean(values) if values else None

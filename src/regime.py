"""
Shared regime-comparison computation (SIGNAL_TRACKER_REQUIREMENTS.md §9: "signal
performance is never presented without its beta context"). Used by both
render_summary.py and render_dashboard.py so the two surfaces never disagree.
"""
from __future__ import annotations

import sqlite3


def compute_regime(conn: sqlite3.Connection) -> dict | None:
    """Returns {start_date, end_date, nifty_pct} over the span the closed ledger sample
    covers, or None if there's no closed sample yet or no index data for that span."""
    span = conn.execute(
        "SELECT MIN(entry_date), MAX(exit_date) FROM ledger WHERE exit_date IS NOT NULL"
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

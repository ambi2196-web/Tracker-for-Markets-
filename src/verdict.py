"""
Per-cohort verdict table for Track A (mechanism signals) — Phase 8 work item 2.

Shared by render_dashboard.py, render_summary.py and render_digest.py so every surface
renders the same verdict from the same computation.

Cohort = (signal_type, direction), over closed trades only, on the mechanism track only.
Track B is never included: averaging a 40-trade mechanical sample with a 4-trade
discretionary one destroys the ability to measure either, which is the whole reason
Phase 8 split the ledger.

The regime figure here is the *matched-window* one — mean NIFTY return over each trade's
own entry→exit window — not the global span. See regime.py for why.

Hard rule, enforced by these being the only labels this module can emit: no verdict may
ever assert a proven edge. "PROMISING — NOT PROVEN" is the strongest statement the system
is permitted to make, no matter how good the spread looks.
"""
from __future__ import annotations

import sqlite3
from statistics import median

import regime

MIN_SAMPLE = 20  # matches §9's "fewer than 20 closed observations" and §11's capital gate

LABEL_INSUFFICIENT = "INSUFFICIENT SAMPLE"
LABEL_NO_EDGE = "NO EDGE"
LABEL_PROMISING = "PROMISING — NOT PROVEN"
# Not in the Phase 8 table, but a cohort whose regime windows aren't covered by the index
# archive cannot be judged either way. Calling that "NO EDGE" would be a fabricated
# verdict, so it gets its own honest label.
LABEL_NO_REGIME_DATA = "NO REGIME DATA"

TRACK_MECHANISM = "mechanism"


def _label(n: int, spread: float | None) -> str:
    if n < MIN_SAMPLE:
        return LABEL_INSUFFICIENT
    if spread is None:
        return LABEL_NO_REGIME_DATA
    if spread <= 0:
        return LABEL_NO_EDGE
    return LABEL_PROMISING


def cohort_verdicts(conn: sqlite3.Connection, track: str = TRACK_MECHANISM) -> list[dict]:
    """One row per (signal_type, direction) cohort of closed mechanism trades."""
    try:
        rows = conn.execute(
            """
            SELECT s.signal_type, l.direction, l.mode, l.entry_date, l.exit_date,
                   l.net_return_pct, l.holding_days
            FROM ledger l JOIN signals s ON s.signal_id = l.signal_id
            WHERE l.exit_date IS NOT NULL
              AND l.net_return_pct IS NOT NULL
              AND l.track = ?
            ORDER BY s.signal_type, l.direction
            """,
            (track,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    cohorts: dict[tuple[str, str, str], list[tuple]] = {}
    for signal_type, direction, mode, entry_date, exit_date, net, hold in rows:
        cohorts.setdefault((signal_type, direction, mode), []).append((entry_date, exit_date, net, hold))

    out = []
    for (signal_type, direction, mode), trades in sorted(cohorts.items()):
        nets = [t[2] for t in trades]
        holds = [t[3] for t in trades if t[3] is not None]
        n = len(nets)
        wins = sum(1 for v in nets if v > 0)
        mean_net = sum(nets) / n
        median_net = median(nets)

        matched_nifty = regime.mean_matched_window_return(
            conn, [(t[0], t[1]) for t in trades]
        )
        spread = (mean_net - matched_nifty) if matched_nifty is not None else None

        out.append({
            "signal_type": signal_type,
            "direction": direction,
            "mode": mode,
            "track": track,
            "n": n,
            "hit_rate": wins / n * 100.0,
            "mean_net": mean_net,
            "median_net": median_net,
            # §9.6 also requires worst single outcome and mean holding days; kept here so
            # every surface reads one computation rather than re-deriving its own.
            "worst": min(nets),
            "mean_holding_days": (sum(holds) / len(holds)) if holds else None,
            "matched_nifty": matched_nifty,
            "spread": spread,
            "verdict": _label(n, spread),
        })
    return out

"""
Sector dispersion monitor — Phase 8 work item 3.

The Track B thesis is industry-level, not stock-level: the market prices a temporary
input shock as permanent impairment across a whole sector. The detectable signature is a
sector whose *median* constituent is deeply below the index over the same window. Median
rather than mean, deliberately — one collapsed constituent should not manufacture a
sector-wide signal.

What this produces is a RESEARCH PROMPT, not a signal. It says "go read about this". It
carries no entry price, no stop, no direction, and it must never reach `ledger` or any
performance statistic. That separation is enforced by construction (this module has no
ledger write path at all) and covered by a test.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

LOOKBACK_TRADING_DAYS = 90
SECTOR_SPREAD_TRIGGER = -20.0   # percentage points below the index
MIN_CONSTITUENTS = 5
BREADTH_DOWN_THRESHOLD = -20.0  # a constituent counts toward breadth if down more than this

STATUS_OPEN = "open"
PIPELINE_VERSION = "0.1.0"
_SOURCE = "dispersion_monitor"
_SOURCE_FILE = "computed"


@dataclass
class SectorDispersion:
    sector: str
    n_constituents: int
    median_return: float
    index_return: float | None
    spread: float | None
    breadth: float
    fires: bool


def _window_dates(conn: sqlite3.Connection, lookback: int) -> tuple[str, str] | None:
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT ?", (lookback,)
    )]
    if len(dates) < 2:
        return None
    return dates[-1], dates[0]  # (start, end) — query returned newest first


def _index_return(conn: sqlite3.Connection, start: str, end: str) -> float | None:
    try:
        s = conn.execute(
            "SELECT close FROM index_prices WHERE date >= ? ORDER BY date ASC LIMIT 1", (start,)
        ).fetchone()
        e = conn.execute(
            "SELECT close FROM index_prices WHERE date <= ? ORDER BY date DESC LIMIT 1", (end,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not s or not e or not s[0]:
        return None
    return (e[0] / s[0] - 1.0) * 100.0


def compute(conn: sqlite3.Connection, lookback: int = LOOKBACK_TRADING_DAYS) -> list[SectorDispersion]:
    """Per-sector dispersion over the trailing `lookback` trading days."""
    window = _window_dates(conn, lookback)
    if window is None:
        return []
    start, end = window
    index_return = _index_return(conn, start, end)

    try:
        rows = conn.execute(
            """
            SELECT m.sector, p_start.symbol,
                   p_start.close AS start_close, p_end.close AS end_close
            FROM sector_map m
            JOIN prices p_start ON p_start.symbol = m.symbol AND p_start.date = ?
            JOIN prices p_end   ON p_end.symbol   = m.symbol AND p_end.date   = ?
            WHERE p_start.close IS NOT NULL AND p_start.close > 0 AND p_end.close IS NOT NULL
            """,
            (start, end),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    by_sector: dict[str, list[float]] = {}
    for sector, _symbol, start_close, end_close in rows:
        by_sector.setdefault(sector, []).append((end_close / start_close - 1.0) * 100.0)

    out = []
    for sector, returns in sorted(by_sector.items()):
        n = len(returns)
        med = median(returns)
        spread = (med - index_return) if index_return is not None else None
        breadth = sum(1 for r in returns if r < BREADTH_DOWN_THRESHOLD) / n
        fires = (
            spread is not None
            and spread <= SECTOR_SPREAD_TRIGGER
            and n >= MIN_CONSTITUENTS
        )
        out.append(SectorDispersion(
            sector=sector, n_constituents=n, median_return=med,
            index_return=index_return, spread=spread, breadth=breadth, fires=fires,
        ))
    return out


def write_research_prompts(
    conn: sqlite3.Connection,
    dispersions: list[SectorDispersion],
    *,
    detected_date: str,
    lookback: int = LOOKBACK_TRADING_DAYS,
) -> int:
    """Persist firing sectors as research prompts. Never touches `ledger` — a prompt is
    an instruction to go read, not a position. Re-running for the same sector/date is
    idempotent."""
    written = 0
    now = datetime.now(timezone.utc).isoformat()
    for d in dispersions:
        if not d.fires:
            continue
        prompt_id = f"prompt:{d.sector.replace(' ', '_').lower()}:{detected_date}"
        existing = conn.execute(
            "SELECT 1 FROM research_prompts WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO research_prompts (prompt_id, sector, detected_date, window_days, spread,
                                           n_constituents, median_constituent_return, index_return,
                                           breadth, status, notes, source, source_file,
                                           ingested_at, pipeline_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (prompt_id, d.sector, detected_date, lookback, d.spread, d.n_constituents,
             d.median_return, d.index_return, d.breadth, STATUS_OPEN,
             _SOURCE, _SOURCE_FILE, now, PIPELINE_VERSION),
        )
        written += 1
    return written


def open_prompts(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT prompt_id, sector, detected_date, window_days, spread, n_constituents,
                   median_constituent_return, index_return, breadth, status, notes
            FROM research_prompts WHERE status = 'open'
            ORDER BY spread ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    keys = ["prompt_id", "sector", "detected_date", "window_days", "spread", "n_constituents",
            "median_constituent_return", "index_return", "breadth", "status", "notes"]
    return [dict(zip(keys, r)) for r in rows]

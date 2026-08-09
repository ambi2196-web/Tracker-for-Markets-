#!/usr/bin/env python3
"""
Phase 4 — signal state machine and paper ledger.

Builds fo_ban_exit signals (SIGNAL_TRACKER_REQUIREMENTS.md §7.1) from Phase 3's events,
and both-direction paper ledger rows (long + short — direction is explicitly undetermined,
see src/signals/fo_ban_exit.py). Every write happens inside a single transaction per run:
nothing commits until the whole batch succeeds, so a crash mid-run leaves the database
exactly as it was before the run started — never a half-transitioned signal.

The lookahead-safety assertion (no price row used to arm a signal may be dated after the
arming date) is not a separate opt-in check — it runs unconditionally inside
signals.fo_ban_exit.build_signals() on every invocation of any mode below.

Modes mirror build_db.py / build_events.py:
    --live       Full events/prices archive -> data/tracker.db (upsert; safe to rerun).
    --rebuild    Clear existing fo_ban_exit signal/ledger rows first, then --live.
    --replay FROM TO
                 Only events with effective_date in [FROM, TO] -> a scratch database.
    --dry-run    Compute and print a summary, write nothing.

Usage:
    python src/build_signals.py --live
    python src/build_signals.py --rebuild
    python src/build_signals.py --replay 2026-06-01 2026-08-07
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from signals import fo_ban_exit  # noqa: E402
from build_db import create_schema  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DEFAULT_DB_PATH = DATA_ROOT / "tracker.db"
DEFAULT_REPLAY_DB_PATH = DATA_ROOT / "scratch_replay_signals.db"
PIPELINE_VERSION = "0.1.0"


def _write(conn: sqlite3.Connection, signal_rows, ledger_rows, *, now_iso: str) -> None:
    conn.executemany(
        """
        INSERT INTO signals (signal_id, event_id, symbol, signal_type, state, armed_date,
                              window_start, window_end, entry_date, entry_ref_price,
                              stop_price, invalidation_reason, filters_passed, updated_at,
                              pipeline_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id) DO UPDATE SET
            state=excluded.state, window_end=excluded.window_end, entry_date=excluded.entry_date,
            entry_ref_price=excluded.entry_ref_price, stop_price=excluded.stop_price,
            invalidation_reason=excluded.invalidation_reason, filters_passed=excluded.filters_passed,
            updated_at=excluded.updated_at, pipeline_version=excluded.pipeline_version
        """,
        [
            (s.signal_id, s.event_id, s.symbol, fo_ban_exit.SIGNAL_TYPE, s.state, s.armed_date,
             s.window_start, s.window_end, s.entry_date, s.entry_ref_price, s.stop_price,
             s.invalidation_reason, json.dumps(s.filters_passed), now_iso, PIPELINE_VERSION)
            for s in signal_rows
        ],
    )
    conn.executemany(
        """
        INSERT INTO ledger (ledger_id, signal_id, mode, direction, entry_date, entry_price,
                             exit_date, exit_price, exit_reason, gross_return_pct, costs_pct,
                             net_return_pct, holding_days, notes, updated_at, pipeline_version)
        VALUES (?, ?, 'paper', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ledger_id) DO UPDATE SET
            entry_date=excluded.entry_date, entry_price=excluded.entry_price,
            exit_date=excluded.exit_date, exit_price=excluded.exit_price,
            exit_reason=excluded.exit_reason, gross_return_pct=excluded.gross_return_pct,
            costs_pct=excluded.costs_pct, net_return_pct=excluded.net_return_pct,
            holding_days=excluded.holding_days, notes=excluded.notes,
            updated_at=excluded.updated_at, pipeline_version=excluded.pipeline_version
        """,
        [
            (l.ledger_id, l.signal_id, l.direction, l.entry_date, l.entry_price, l.exit_date,
             l.exit_price, l.exit_reason, l.gross_return_pct, l.costs_pct, l.net_return_pct,
             l.holding_days, l.notes, now_iso, PIPELINE_VERSION)
            for l in ledger_rows
        ],
    )


def _summarize(signal_rows, ledger_rows) -> str:
    from collections import Counter
    state_counts = Counter(s.state for s in signal_rows)
    reason_counts = Counter(l.exit_reason for l in ledger_rows if l.exit_reason)
    return (
        f"{len(signal_rows)} signals ({dict(state_counts)}), "
        f"{len(ledger_rows)} ledger rows, exit reasons {dict(reason_counts)}"
    )


def run_live_or_rebuild(*, rebuild: bool, dry_run: bool) -> int:
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        create_schema(conn)
        signal_rows, ledger_rows = fo_ban_exit.build_signals(conn)

        if dry_run:
            print(f"[dry-run] {_summarize(signal_rows, ledger_rows)}. Nothing written.")
            return 0

        if rebuild:
            conn.execute("DELETE FROM ledger WHERE signal_id IN (SELECT signal_id FROM signals WHERE signal_type=?)", (fo_ban_exit.SIGNAL_TYPE,))
            conn.execute("DELETE FROM signals WHERE signal_type=?", (fo_ban_exit.SIGNAL_TYPE,))
            print("[rebuild] cleared existing fo_ban_exit signal/ledger rows")

        now_iso = datetime.now(timezone.utc).isoformat()
        _write(conn, signal_rows, ledger_rows, now_iso=now_iso)
        conn.commit()  # single transaction for the whole run — see module docstring
        print(f"done: {_summarize(signal_rows, ledger_rows)} -> {DEFAULT_DB_PATH}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


def run_replay(from_date: date, to_date: date, *, out_path: Path, dry_run: bool) -> int:
    # Replay reads the FULL prices/events archive for correct trading-day arithmetic and
    # ADV lookback, then filters to signals whose *arming* event falls in the window.
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        signal_rows, ledger_rows = fo_ban_exit.build_signals(conn)
    finally:
        conn.close()

    signal_rows = [s for s in signal_rows if from_date <= date.fromisoformat(s.armed_date) <= to_date]
    kept_ids = {s.signal_id for s in signal_rows}
    ledger_rows = [l for l in ledger_rows if l.signal_id in kept_ids]

    if dry_run:
        print(f"[dry-run][replay {from_date}..{to_date}] {_summarize(signal_rows, ledger_rows)}. Nothing written.")
        return 0

    if out_path.exists():
        out_path.unlink()

    out_conn = sqlite3.connect(out_path)
    try:
        create_schema(out_conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        _write(out_conn, signal_rows, ledger_rows, now_iso=now_iso)
        out_conn.commit()
        print(f"[replay {from_date}..{to_date}] {_summarize(signal_rows, ledger_rows)} "
              f"-> {out_path} (data/tracker.db untouched)")
    except Exception:
        out_conn.rollback()
        raise
    finally:
        out_conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--rebuild", action="store_true")
    mode.add_argument("--replay", nargs=2, metavar=("FROM", "TO"))
    parser.add_argument("--out", type=str, default=str(DEFAULT_REPLAY_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.replay:
        from_date, to_date = (date.fromisoformat(d) for d in args.replay)
        return run_replay(from_date, to_date, out_path=Path(args.out), dry_run=args.dry_run)
    return run_live_or_rebuild(rebuild=args.rebuild, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

"""
F&O ban-exit signal, implementing SIGNAL_TRACKER_REQUIREMENTS.md §7.1 on top of the
shared lifecycle engine (src/signals/engine.py — extracted in Phase 6 when a second
signal type needed the same armed/entry/exit/stop/cost machinery; see that module's
docstring for why).

This module owns only what's specific to F&O ban exits: which events trigger it, the
ADV-20 liquidity diagnostic (with the lookahead-safety assertion — the plan's flagged
highest-risk failure), and invalidation-by-re-entry. Everything generic (entry
resolution, per-direction stop/window_end resolution, cost model, ledger construction)
lives in the engine and is shared verbatim with ipo_lockin.py.

Direction is explicitly undetermined per §7.1/§12: every signal produces TWO ledger rows
(long and short) — see engine.py.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from signals import calendar, engine

SIGNAL_TYPE = "fo_ban_exit"

ADV_LOOKBACK_SESSIONS = 20
ACTION_WINDOW_TRADING_DAYS = 5
ENTRY_OFFSET_TRADING_DAYS = 1

# --- provisional, §12 open decisions — tune freely, everything here is replayable ---
LIQUIDITY_FLOOR_ADV_RUPEES = 5_000_000.0  # 50 lakh ADV(20), a modest floor for F&O-eligible names
STOP_LOSS_PCT = 0.08  # fixed 8% (§12 open decision #3 not yet resolved between fixed % vs ATR)
COST_PCT_ROUNDTRIP = 0.30  # STT + stamp + exchange + GST, round trip, conservative flat estimate
SLIPPAGE_PCT_ROUNDTRIP = 0.20  # flat slippage allowance
TOTAL_COSTS_PCT = COST_PCT_ROUNDTRIP + SLIPPAGE_PCT_ROUNDTRIP


def build_signals(conn: sqlite3.Connection) -> tuple[list[engine.SignalRow], list[engine.LedgerRow]]:
    trading_days = [date.fromisoformat(r[0]) for r in conn.execute("SELECT DISTINCT date FROM prices ORDER BY date")]

    exit_events = conn.execute(
        "SELECT event_id, symbol, effective_date FROM events WHERE event_type='fo_ban_exit' ORDER BY effective_date"
    ).fetchall()
    entries_by_symbol: dict[str, list[date]] = {}
    for symbol, eff in conn.execute("SELECT symbol, effective_date FROM events WHERE event_type='fo_ban_entry'"):
        entries_by_symbol.setdefault(symbol, []).append(date.fromisoformat(eff))
    for lst in entries_by_symbol.values():
        lst.sort()

    signals: list[engine.SignalRow] = []
    ledger_rows: list[engine.LedgerRow] = []

    for event_id, symbol, effective_date_str in exit_events:
        T = date.fromisoformat(effective_date_str)
        signal_id = f"signal:{SIGNAL_TYPE}:{symbol}:{T.isoformat()}"

        # window_end recomputed here (and again inside the engine) purely to bound the
        # re-entry invalidation lookup — cheap, deterministic, keeps this module decoupled.
        window_end = calendar.add_trading_days(trading_days, T, ACTION_WINDOW_TRADING_DAYS)
        invalidation_date = next(
            (d for d in entries_by_symbol.get(symbol, []) if d > T and (window_end is None or d <= window_end)),
            None,
        )

        # ADV-20 using only sessions through T — no lookahead, see module docstring.
        adv_sessions = calendar.sessions_up_to(trading_days, T, ADV_LOOKBACK_SESSIONS)
        adv_rows = []
        if adv_sessions:
            placeholders = ",".join("?" * len(adv_sessions))
            adv_rows = conn.execute(
                f"SELECT date, close, volume FROM prices WHERE symbol=? AND date IN ({placeholders})",
                [symbol, *[d.isoformat() for d in adv_sessions]],
            ).fetchall()
        for d_str, _, _ in adv_rows:
            assert date.fromisoformat(d_str) <= T, (
                f"LOOKAHEAD VIOLATION: {signal_id} used a price row dated {d_str} to arm on {T.isoformat()}"
            )
        adv_values = [c * v for _, c, v in adv_rows if c is not None and v is not None]
        adv_20_rupees = sum(adv_values) / len(adv_values) if adv_values else None
        liquidity_ok = adv_20_rupees is not None and adv_20_rupees >= LIQUIDITY_FLOOR_ADV_RUPEES
        filters_passed = {"adv_20_rupees": adv_20_rupees, "liquidity_ok": liquidity_ok}

        signal_row, rows = engine.build_signal_lifecycle(
            conn, trading_days,
            signal_id=signal_id, event_id=event_id, symbol=symbol, signal_type=SIGNAL_TYPE, T=T,
            window_end_offset_trading_days=ACTION_WINDOW_TRADING_DAYS,
            entry_offset_trading_days=ENTRY_OFFSET_TRADING_DAYS,
            stop_pct=STOP_LOSS_PCT, cost_pct=TOTAL_COSTS_PCT,
            invalidation_date=invalidation_date, filters_passed=filters_passed,
        )
        # Reason string specific to this signal type (engine's is generic "invalidated_on_...").
        if signal_row.invalidation_reason and invalidation_date is not None:
            suffix = "before_entry" if "before_entry" in signal_row.invalidation_reason else "after_entry"
            signal_row.invalidation_reason = f"re_entered_ban_on_{invalidation_date.isoformat()}_{suffix}"

        signals.append(signal_row)
        ledger_rows.extend(rows)

    return signals, ledger_rows

"""
IPO lock-in expiry signal, implementing SIGNAL_TRACKER_REQUIREMENTS.md §7.2 on the shared
lifecycle engine (src/signals/engine.py) — Phase 6's whole purpose is proving that engine
generalizes to a second signal type without changes, so compare this module to
fo_ban_exit.py: same shape, same generic calls, only the type-specific pieces differ.

Differences from fo_ban_exit that the engine had to be extended to support:
- The unlock date is known well in advance (from the manually-entered seed file), so
  signals sit in `watching` until within WATCHING_UNTIL_CALENDAR_DAYS_BEFORE of it,
  matching §7.2's "Trigger: Days-to-unlock reaches 10, 5, 2, 0" — fo_ban_exit's trigger
  is reactive and never uses this.
- The action window starts *before* the trigger date (unlock date âˆ’5 trading days), not
  at it — window_start_offset_trading_days is negative.

Not yet implemented: automatic invalidation ("lock-in extended or waived; block deal
clears the overhang before the date" per §7.2) — there is no data source that detects
any of those in this system yet. Every signal here can currently only reach
armed/watching/open/exit_due/closed, never invalidated. This is a known gap, not a
silent assumption that it can't happen.

Direction: §7.2 doesn't commit to long or short any more than §7.1 did, so this follows
the same "log both, resolve empirically" convention as fo_ban_exit for architectural
uniformity — see engine.py.

Stop-loss and cost model constants are shared with fo_ban_exit's (same open §12
decisions, not signal-type-specific) rather than duplicated.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from signals import calendar, engine, fo_ban_exit

SIGNAL_TYPE = "ipo_lockin"

WINDOW_START_OFFSET_TRADING_DAYS = -5
WINDOW_END_OFFSET_TRADING_DAYS = 10
ENTRY_OFFSET_TRADING_DAYS = 1
WATCHING_UNTIL_CALENDAR_DAYS_BEFORE = 10  # §7.2: "days-to-unlock reaches 10, 5, 2, 0"
ADV_LOOKBACK_SESSIONS = 20  # for the watch metric only, not an entry gate

STOP_LOSS_PCT = fo_ban_exit.STOP_LOSS_PCT
TOTAL_COSTS_PCT = fo_ban_exit.TOTAL_COSTS_PCT


def _unlock_day_volume_vs_adv(conn: sqlite3.Connection, trading_days: list[date], symbol: str, T: date) -> dict:
    """§7.2 watch metric: volume on unlock day vs 20-day ADV ending the prior session.
    A diagnostic only (filters_passed), never a gate — 'a muted response means the setup
    is dead' is something a reviewer reads, not something this code enforces."""
    adv_sessions = [d for d in calendar.sessions_up_to(trading_days, T, ADV_LOOKBACK_SESSIONS + 1) if d < T]
    adv_sessions = adv_sessions[-ADV_LOOKBACK_SESSIONS:]
    volumes = []
    if adv_sessions:
        placeholders = ",".join("?" * len(adv_sessions))
        volumes = [
            v for (v,) in conn.execute(
                f"SELECT volume FROM prices WHERE symbol=? AND date IN ({placeholders})",
                [symbol, *[d.isoformat() for d in adv_sessions]],
            ) if v is not None
        ]
    adv_20 = sum(volumes) / len(volumes) if volumes else None

    unlock_day_volume_row = conn.execute(
        "SELECT volume FROM prices WHERE symbol=? AND date=?", (symbol, T.isoformat())
    ).fetchone()
    unlock_day_volume = unlock_day_volume_row[0] if unlock_day_volume_row else None

    ratio = (unlock_day_volume / adv_20) if (unlock_day_volume is not None and adv_20) else None
    return {"unlock_day_volume": unlock_day_volume, "adv_20_volume": adv_20, "volume_vs_adv_ratio": ratio}


def build_signals(conn: sqlite3.Connection) -> tuple[list[engine.SignalRow], list[engine.LedgerRow]]:
    trading_days = [date.fromisoformat(r[0]) for r in conn.execute("SELECT DISTINCT date FROM prices ORDER BY date")]

    unlock_events = conn.execute(
        "SELECT event_id, symbol, effective_date, detail FROM events WHERE event_type='ipo_lockin_unlock' "
        "ORDER BY effective_date"
    ).fetchall()

    signals: list[engine.SignalRow] = []
    ledger_rows: list[engine.LedgerRow] = []

    for event_id, symbol, effective_date_str, detail_json in unlock_events:
        T = date.fromisoformat(effective_date_str)
        signal_id = f"signal:{event_id}"
        detail = json.loads(detail_json) if detail_json else {}

        filters_passed = {"tranche": detail.get("tranche"), "unlock_pct": detail.get("unlock_pct")}
        filters_passed.update(_unlock_day_volume_vs_adv(conn, trading_days, symbol, T))

        signal_row, rows = engine.build_signal_lifecycle(
            conn, trading_days,
            signal_id=signal_id, event_id=event_id, symbol=symbol, signal_type=SIGNAL_TYPE, T=T,
            window_start_offset_trading_days=WINDOW_START_OFFSET_TRADING_DAYS,
            window_end_offset_trading_days=WINDOW_END_OFFSET_TRADING_DAYS,
            entry_offset_trading_days=ENTRY_OFFSET_TRADING_DAYS,
            stop_pct=STOP_LOSS_PCT, cost_pct=TOTAL_COSTS_PCT,
            invalidation_date=None,  # not yet implemented — see module docstring
            filters_passed=filters_passed,
            watching_until_calendar_days_before=WATCHING_UNTIL_CALENDAR_DAYS_BEFORE,
        )
        signals.append(signal_row)
        ledger_rows.extend(rows)

    return signals, ledger_rows

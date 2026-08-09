"""
F&O ban-exit signal state machine and paper ledger (PHASED_IMPLEMENTATION_PLAN.md Phase 4),
implementing SIGNAL_TRACKER_REQUIREMENTS.md §7.1.

States: armed -> open -> (exit_due while waiting for the closing price) -> closed | invalidated.
"watching" is not used for this signal type — the fo_ban_exit *event* (Phase 3) already is
the trigger, so a signal starts life directly at "armed" the moment the event exists.

Direction is explicitly undetermined per §7.1/§12: every signal produces TWO ledger rows
(long and short), sharing the same entry/exit dates and reference prices but computing
opposite returns, so a later performance review can see empirically which side (if either)
has edge — see §9's reporting rule against pooling paper and live, extended here to not
conflate long and short either.

`signals.stop_price` (a single column in the fixed schema) stores the LONG-side stop price
by convention; the short-side stop is derived symmetrically in code as
entry * (1 + STOP_LOSS_PCT) and is not stored separately.

LOOKAHEAD SAFETY (the plan's flagged highest-risk failure): ADV-20 liquidity is computed
using only `prices` rows dated on or before the arming date T. This is enforced by
construction (calendar.sessions_up_to only returns dates <= T) and re-asserted explicitly
in build_signals.py before any signal is trusted — see verify_no_lookahead().

Provisional constants below are open decisions per SIGNAL_TRACKER_REQUIREMENTS.md §12
(liquidity floor, stop-loss basis, cost model). They are paper-only inputs — nothing here
risks capital — and are deliberately easy to change and replay.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from signals import calendar

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


@dataclass
class SignalRow:
    signal_id: str
    event_id: str
    symbol: str
    state: str
    armed_date: str
    window_start: str
    window_end: str | None
    entry_date: str | None
    entry_ref_price: float | None
    stop_price: float | None
    invalidation_reason: str | None
    filters_passed: dict = field(default_factory=dict)


@dataclass
class LedgerRow:
    ledger_id: str
    signal_id: str
    direction: str
    entry_date: str | None
    entry_price: float | None
    exit_date: str | None
    exit_price: float | None
    exit_reason: str | None
    gross_return_pct: float | None
    costs_pct: float | None
    net_return_pct: float | None
    holding_days: int | None
    notes: str | None = None


def _price_close(conn: sqlite3.Connection, symbol: str, d: date) -> float | None:
    row = conn.execute("SELECT close FROM prices WHERE symbol=? AND date=?", (symbol, d.isoformat())).fetchone()
    return row[0] if row and row[0] is not None else None


def _price_open(conn: sqlite3.Connection, symbol: str, d: date) -> float | None:
    row = conn.execute("SELECT open FROM prices WHERE symbol=? AND date=?", (symbol, d.isoformat())).fetchone()
    return row[0] if row and row[0] is not None else None


def _next_available_close(conn: sqlite3.Connection, symbol: str, trading_days: list[date], from_date: date) -> tuple[date, float] | None:
    """Fallback for trading halts / circuit limits producing no price on the exact target
    date: use the first available close on or after from_date instead of getting stuck."""
    for d in trading_days:
        if d < from_date:
            continue
        close = _price_close(conn, symbol, d)
        if close is not None:
            return d, close
    return None


def _gross_return_pct(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "long":
        return (exit_price / entry_price - 1.0) * 100.0
    return (entry_price / exit_price - 1.0) * 100.0


def _make_ledger_row(signal_id: str, direction: str, entry_date: date | None, entry_price: float | None,
                      exit_date: date | None, exit_price: float | None, exit_reason: str | None,
                      notes: str | None = None) -> LedgerRow:
    gross = net = None
    holding_days = None
    if entry_price is not None and exit_price is not None:
        gross = _gross_return_pct(direction, entry_price, exit_price)
        net = gross - TOTAL_COSTS_PCT
        holding_days = (exit_date - entry_date).days
    return LedgerRow(
        ledger_id=f"ledger:{signal_id}:{direction}",
        signal_id=signal_id,
        direction=direction,
        entry_date=entry_date.isoformat() if entry_date else None,
        entry_price=entry_price,
        exit_date=exit_date.isoformat() if exit_date else None,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return_pct=gross,
        costs_pct=TOTAL_COSTS_PCT if net is not None else None,
        net_return_pct=net,
        holding_days=holding_days,
        notes=notes,
    )


def _resolve_direction(conn: sqlite3.Connection, trading_days: list[date], symbol: str, direction: str,
                        entry_date: date, entry_price: float, window_end: date | None,
                        signal_id: str) -> tuple[LedgerRow, str]:
    stop_trigger = entry_price * (1 - STOP_LOSS_PCT) if direction == "long" else entry_price * (1 + STOP_LOSS_PCT)
    scan_days = [d for d in trading_days if d >= entry_date and (window_end is None or d <= window_end)]

    for d in scan_days:
        close = _price_close(conn, symbol, d)
        if close is None:
            continue  # trading halt / circuit limit that day — just skip, don't get stuck
        breached = (close <= stop_trigger) if direction == "long" else (close >= stop_trigger)
        if breached:
            return _make_ledger_row(signal_id, direction, entry_date, entry_price, d, close, "stop"), "closed"
        if window_end is not None and d == window_end:
            return _make_ledger_row(signal_id, direction, entry_date, entry_price, d, close, "window_end"), "closed"

    if window_end is not None:
        # Reached here without resolving inside the loop even though window_end is a known
        # date — either it wasn't in scan_days yet, or (the trading-halt case) it was, but
        # this symbol had no price on it and got skipped. Either way, fall back to the
        # first available close on/after window_end instead of stalling forever.
        fallback = _next_available_close(conn, symbol, trading_days, window_end)
        if fallback is not None:
            d, close = fallback
            notes = None if d == window_end else f"no price on window_end={window_end.isoformat()}; used next available close"
            return _make_ledger_row(
                signal_id, direction, entry_date, entry_price, d, close, "window_end", notes=notes,
            ), "closed"

    return _make_ledger_row(signal_id, direction, entry_date, entry_price, None, None, None), "pending"


def build_signals(conn: sqlite3.Connection) -> tuple[list[SignalRow], list[LedgerRow]]:
    trading_days = [date.fromisoformat(r[0]) for r in conn.execute("SELECT DISTINCT date FROM prices ORDER BY date")]

    exit_events = conn.execute(
        "SELECT event_id, symbol, effective_date FROM events WHERE event_type='fo_ban_exit' ORDER BY effective_date"
    ).fetchall()
    entries_by_symbol: dict[str, list[date]] = {}
    for symbol, eff in conn.execute("SELECT symbol, effective_date FROM events WHERE event_type='fo_ban_entry'"):
        entries_by_symbol.setdefault(symbol, []).append(date.fromisoformat(eff))
    for lst in entries_by_symbol.values():
        lst.sort()

    signals: list[SignalRow] = []
    ledger_rows: list[LedgerRow] = []

    for event_id, symbol, effective_date_str in exit_events:
        T = date.fromisoformat(effective_date_str)
        signal_id = f"signal:{SIGNAL_TYPE}:{symbol}:{T.isoformat()}"
        window_end = calendar.add_trading_days(trading_days, T, ACTION_WINDOW_TRADING_DAYS)

        reentry_date = next(
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

        entry_date = calendar.add_trading_days(trading_days, T, ENTRY_OFFSET_TRADING_DAYS)
        state = "armed"
        entry_ref_price = None
        invalidation_reason = None
        long_ledger = short_ledger = None

        if reentry_date is not None and (entry_date is None or reentry_date <= entry_date):
            state = "invalidated"
            invalidation_reason = f"re_entered_ban_on_{reentry_date.isoformat()}_before_entry"
        elif entry_date is not None:
            entry_ref_price = _price_open(conn, symbol, entry_date)
            if entry_ref_price is None:
                state = "invalidated"
                invalidation_reason = "no_price_data_at_entry"
            elif reentry_date is not None and reentry_date > entry_date:
                exit_close = _price_close(conn, symbol, reentry_date)
                if exit_close is not None:
                    long_ledger = _make_ledger_row(signal_id, "long", entry_date, entry_ref_price, reentry_date, exit_close, "invalidated")
                    short_ledger = _make_ledger_row(signal_id, "short", entry_date, entry_ref_price, reentry_date, exit_close, "invalidated")
                    state = "invalidated"
                    invalidation_reason = f"re_entered_ban_on_{reentry_date.isoformat()}_after_entry"
                else:
                    long_ledger = _make_ledger_row(signal_id, "long", entry_date, entry_ref_price, None, None, None)
                    short_ledger = _make_ledger_row(signal_id, "short", entry_date, entry_ref_price, None, None, None)
                    state = "open"
            else:
                long_ledger, long_state = _resolve_direction(conn, trading_days, symbol, "long", entry_date, entry_ref_price, window_end, signal_id)
                short_ledger, short_state = _resolve_direction(conn, trading_days, symbol, "short", entry_date, entry_ref_price, window_end, signal_id)
                if long_state == "pending" or short_state == "pending":
                    state = "exit_due" if window_end is not None else "open"
                else:
                    state = "closed"
        # else: entry_date is None -> not enough archived data yet, stays 'armed'

        stop_price = entry_ref_price * (1 - STOP_LOSS_PCT) if entry_ref_price is not None else None

        signals.append(SignalRow(
            signal_id=signal_id, event_id=event_id, symbol=symbol, state=state,
            armed_date=T.isoformat(), window_start=T.isoformat(),
            window_end=window_end.isoformat() if window_end else None,
            entry_date=entry_date.isoformat() if entry_date else None,
            entry_ref_price=entry_ref_price, stop_price=stop_price,
            invalidation_reason=invalidation_reason, filters_passed=filters_passed,
        ))
        if long_ledger:
            ledger_rows.append(long_ledger)
        if short_ledger:
            ledger_rows.append(short_ledger)

    return signals, ledger_rows

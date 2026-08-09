"""
Shared signal lifecycle engine (PHASED_IMPLEMENTATION_PLAN.md Phase 6).

Extracted from the Phase 4 fo_ban_exit implementation when Phase 6 added a second signal
type (ipo_lockin), per the plan's own acceptance test: "New signal type added without
modifying the state machine. If the state machine needs changes, fix the abstraction
before adding a third type." This module IS that abstraction — signal-type modules
(fo_ban_exit.py, ipo_lockin.py) supply their own event source, invalidation check, and
diagnostic filters, and call build_signal_lifecycle() for everything generic: entry
resolution, per-direction stop/window_end resolution, cost model, and ledger rows.

Both directions (long and short) are always logged — see fo_ban_exit.py's docstring for
why this was the Phase 4 default; Phase 6 keeps it uniform across types rather than
special-casing, since neither §7.1 nor §7.2 commits to a direction in advance.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from signals import calendar


@dataclass
class SignalRow:
    signal_id: str
    event_id: str
    symbol: str
    signal_type: str
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


def price_close(conn: sqlite3.Connection, symbol: str, d: date) -> float | None:
    row = conn.execute("SELECT close FROM prices WHERE symbol=? AND date=?", (symbol, d.isoformat())).fetchone()
    return row[0] if row and row[0] is not None else None


def price_open(conn: sqlite3.Connection, symbol: str, d: date) -> float | None:
    row = conn.execute("SELECT open FROM prices WHERE symbol=? AND date=?", (symbol, d.isoformat())).fetchone()
    return row[0] if row and row[0] is not None else None


def next_available_close(conn: sqlite3.Connection, symbol: str, trading_days: list[date], from_date: date) -> tuple[date, float] | None:
    """Fallback for trading halts / circuit limits producing no price on the exact target
    date: use the first available close on or after from_date instead of getting stuck."""
    for d in trading_days:
        if d < from_date:
            continue
        close = price_close(conn, symbol, d)
        if close is not None:
            return d, close
    return None


def _gross_return_pct(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "long":
        return (exit_price / entry_price - 1.0) * 100.0
    return (entry_price / exit_price - 1.0) * 100.0


def make_ledger_row(signal_id: str, direction: str, entry_date: date | None, entry_price: float | None,
                     exit_date: date | None, exit_price: float | None, exit_reason: str | None,
                     cost_pct: float, notes: str | None = None) -> LedgerRow:
    gross = net = None
    holding_days = None
    if entry_price is not None and exit_price is not None:
        gross = _gross_return_pct(direction, entry_price, exit_price)
        net = gross - cost_pct
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
        costs_pct=cost_pct if net is not None else None,
        net_return_pct=net,
        holding_days=holding_days,
        notes=notes,
    )


def resolve_direction(conn: sqlite3.Connection, trading_days: list[date], symbol: str, direction: str,
                       entry_date: date, entry_price: float, window_end: date | None,
                       signal_id: str, *, stop_pct: float, cost_pct: float) -> tuple[LedgerRow, str]:
    stop_trigger = entry_price * (1 - stop_pct) if direction == "long" else entry_price * (1 + stop_pct)
    scan_days = [d for d in trading_days if d >= entry_date and (window_end is None or d <= window_end)]

    for d in scan_days:
        close = price_close(conn, symbol, d)
        if close is None:
            continue  # trading halt / circuit limit that day — just skip, don't get stuck
        breached = (close <= stop_trigger) if direction == "long" else (close >= stop_trigger)
        if breached:
            return make_ledger_row(signal_id, direction, entry_date, entry_price, d, close, "stop", cost_pct), "closed"
        if window_end is not None and d == window_end:
            return make_ledger_row(signal_id, direction, entry_date, entry_price, d, close, "window_end", cost_pct), "closed"

    if window_end is not None:
        # Reached here without resolving inside the loop even though window_end is a known
        # date — either it wasn't in scan_days yet, or (the trading-halt case) it was, but
        # this symbol had no price on it and got skipped. Either way, fall back to the
        # first available close on/after window_end instead of stalling forever.
        fallback = next_available_close(conn, symbol, trading_days, window_end)
        if fallback is not None:
            d, close = fallback
            notes = None if d == window_end else f"no price on window_end={window_end.isoformat()}; used next available close"
            return make_ledger_row(
                signal_id, direction, entry_date, entry_price, d, close, "window_end", cost_pct, notes=notes,
            ), "closed"

    return make_ledger_row(signal_id, direction, entry_date, entry_price, None, None, None, cost_pct), "pending"


def build_signal_lifecycle(
    conn: sqlite3.Connection,
    trading_days: list[date],
    *,
    signal_id: str,
    event_id: str,
    symbol: str,
    signal_type: str,
    T: date,
    window_end_offset_trading_days: int,
    entry_offset_trading_days: int,
    stop_pct: float,
    cost_pct: float,
    invalidation_date: date | None,
    filters_passed: dict,
    window_start_offset_trading_days: int = 0,
    watching_until_calendar_days_before: int | None = None,
) -> tuple[SignalRow, list[LedgerRow]]:
    """The generic watching -> armed -> entry -> (both directions independently) ->
    closed|invalidated lifecycle shared by every signal type. `invalidation_date` is
    precomputed by the caller (each signal type has its own notion of what invalidates
    it) — None means "no invalidation detected for this signal type/instance."

    `watching_until_calendar_days_before`, if set, gates arming on a coarse calendar-day
    distance from the latest archived trading day to T (e.g. fo_ban_exit's trigger is
    already "now", so it never uses this; ipo_lockin's is known well in advance, so it
    sits in `watching` until within N calendar days of the unlock date — see §7.2's
    "Trigger: Days-to-unlock reaches 10, 5, 2, 0"). This check only ever looks at the
    boundary of already-known data, so it can't introduce lookahead.
    """
    if watching_until_calendar_days_before is not None:
        latest_known = trading_days[-1] if trading_days else None
        if latest_known is None or (T - latest_known).days > watching_until_calendar_days_before:
            return SignalRow(
                signal_id=signal_id, event_id=event_id, symbol=symbol, signal_type=signal_type,
                state="watching", armed_date=T.isoformat(), window_start=None, window_end=None,
                entry_date=None, entry_ref_price=None, stop_price=None,
                invalidation_reason=None, filters_passed=filters_passed,
            ), []

    window_start = (
        T if window_start_offset_trading_days == 0
        else calendar.add_trading_days(trading_days, T, window_start_offset_trading_days)
    )
    window_end = calendar.add_trading_days(trading_days, T, window_end_offset_trading_days)
    entry_date = calendar.add_trading_days(trading_days, T, entry_offset_trading_days)

    state = "armed"
    entry_ref_price = None
    invalidation_reason = None
    ledger_rows: list[LedgerRow] = []

    if invalidation_date is not None and (entry_date is None or invalidation_date <= entry_date):
        state = "invalidated"
        invalidation_reason = f"invalidated_on_{invalidation_date.isoformat()}_before_entry"
    elif entry_date is not None:
        entry_ref_price = price_open(conn, symbol, entry_date)
        if entry_ref_price is None:
            state = "invalidated"
            invalidation_reason = "no_price_data_at_entry"
        elif invalidation_date is not None and invalidation_date > entry_date:
            exit_close = price_close(conn, symbol, invalidation_date)
            if exit_close is not None:
                for direction in ("long", "short"):
                    ledger_rows.append(make_ledger_row(
                        signal_id, direction, entry_date, entry_ref_price, invalidation_date,
                        exit_close, "invalidated", cost_pct,
                    ))
                state = "invalidated"
                invalidation_reason = f"invalidated_on_{invalidation_date.isoformat()}_after_entry"
            else:
                for direction in ("long", "short"):
                    ledger_rows.append(make_ledger_row(
                        signal_id, direction, entry_date, entry_ref_price, None, None, None, cost_pct,
                    ))
                state = "open"
        else:
            resolved_states = []
            for direction in ("long", "short"):
                row, dir_state = resolve_direction(
                    conn, trading_days, symbol, direction, entry_date, entry_ref_price,
                    window_end, signal_id, stop_pct=stop_pct, cost_pct=cost_pct,
                )
                ledger_rows.append(row)
                resolved_states.append(dir_state)
            state = "closed" if all(s == "closed" for s in resolved_states) else (
                "exit_due" if window_end is not None else "open"
            )
    # else: entry_date is None -> not enough archived data yet, stays 'armed'

    stop_price = entry_ref_price * (1 - stop_pct) if entry_ref_price is not None else None

    signal_row = SignalRow(
        signal_id=signal_id, event_id=event_id, symbol=symbol, signal_type=signal_type, state=state,
        armed_date=T.isoformat(), window_start=window_start.isoformat() if window_start else None,
        window_end=window_end.isoformat() if window_end else None,
        entry_date=entry_date.isoformat() if entry_date else None,
        entry_ref_price=entry_ref_price, stop_price=stop_price,
        invalidation_reason=invalidation_reason, filters_passed=filters_passed,
    )
    return signal_row, ledger_rows

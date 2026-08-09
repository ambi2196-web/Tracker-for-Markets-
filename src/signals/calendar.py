"""
Trading-day arithmetic driven by our own archived `prices` dates — NSE's own realized
calendar, not a maintained holiday list. This means "N trading days after X" is only
answerable once we've actually archived that many real sessions past X; until then it
returns None and callers must wait rather than guess (this is what keeps signals in
`armed`/`open` instead of prematurely closing them against a projected date).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date


def add_trading_days(trading_days: list[date], from_date: date, n: int) -> date | None:
    """The date `n` trading days after from_date (n > 0), or `|n|` trading days before it
    (n < 0), per the given sorted trading-day list. n == 0 returns from_date itself if it
    is a known trading day, else None. Returns None whenever the archive doesn't extend
    far enough in the requested direction yet — callers must wait rather than guess."""
    if n == 0:
        return from_date if is_known_trading_day(trading_days, from_date) else None
    if n > 0:
        idx = bisect_right(trading_days, from_date)  # first index strictly after from_date
        target_idx = idx + n - 1
        if target_idx >= len(trading_days):
            return None
        return trading_days[target_idx]
    idx = bisect_left(trading_days, from_date)  # first index >= from_date
    target_idx = idx + n  # n is negative
    if target_idx < 0:
        return None
    return trading_days[target_idx]


def sessions_up_to(trading_days: list[date], as_of: date, lookback: int) -> list[date]:
    """The `lookback` most recent trading days on or before as_of. Shorter than lookback
    if the archive doesn't extend far enough back — callers must check the length."""
    idx = bisect_right(trading_days, as_of)  # exclusive upper bound
    start = max(0, idx - lookback)
    return trading_days[start:idx]


def is_known_trading_day(trading_days: list[date], d: date) -> bool:
    idx = bisect_left(trading_days, d)
    return idx < len(trading_days) and trading_days[idx] == d

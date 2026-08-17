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
    far enough in the requested direction yet — callers must wait rather than guess.

    Also returns None (rather than a silently wrong answer) when from_date itself falls
    outside the archive's covered range in the direction being asked: a real bug found
    during Phase 7 backfilling, where events dated ~9 months before the price archive's
    start resolved to nonsense entry dates in the archive's *first* few days, because
    bisect on a from_date far before trading_days[0] still returns a small, "valid"
    insertion index. We have zero visibility into whatever real trading days fell in the
    unarchived gap, so forward counting from before the archive (or backward counting
    from after it) cannot be trusted just because the arithmetic produces an in-range
    index — the safe answer is "we don't know yet", not a guess."""
    if not trading_days:
        return None
    if n == 0:
        return from_date if is_known_trading_day(trading_days, from_date) else None
    if n > 0:
        if from_date < trading_days[0]:
            return None  # archive doesn't cover the gap between from_date and its start
        idx = bisect_right(trading_days, from_date)  # first index strictly after from_date
        target_idx = idx + n - 1
        if target_idx >= len(trading_days):
            return None
        return trading_days[target_idx]
    if from_date > trading_days[-1]:
        return None  # archive doesn't cover the gap between its end and from_date
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

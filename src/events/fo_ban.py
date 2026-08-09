"""
F&O ban entry/exit event detection (PHASED_IMPLEMENTATION_PLAN.md Phase 3).

Deliberately archive-driven, not calendar-driven: transitions are detected between
consecutive *archived observations*, not consecutive calendar or trading days. This is
what keeps a market holiday (no file expected) and a genuine collection gap (file
expected but missing, e.g. the real 2026-06-26 gap found during backfill) from being
misread as an exit followed by a re-entry — there is simply no comparison across a date
that was never observed, regardless of why it's missing. `detail.gap_calendar_days`
records how large that unobserved stretch was so it stays visible, not hidden.

The very first snapshot in a series produces no events for whatever is already banned
on it — that state is left-censored (we don't know the true entry date), and inventing
an "entry" for it would be a fabricated signal.
"""
from __future__ import annotations

from dataclasses import dataclass

from parse.fo_ban import BanSnapshot

EVENT_TYPE_ENTRY = "fo_ban_entry"
EVENT_TYPE_EXIT = "fo_ban_exit"


@dataclass
class BanEvent:
    event_id: str
    symbol: str
    event_type: str
    effective_date: str  # ISO
    detail: dict
    source_file: str


def detect(snapshots_with_sources: list[tuple[BanSnapshot, str]]) -> list[BanEvent]:
    """snapshots_with_sources: [(BanSnapshot, source_file_relpath), ...] sorted by date ascending."""
    events: list[BanEvent] = []
    prev: BanSnapshot | None = None

    for snap, source_file in snapshots_with_sources:
        if prev is not None:
            gap_days = (snap.date - prev.date).days
            entered = sorted(snap.symbols - prev.symbols)
            exited = sorted(prev.symbols - snap.symbols)

            for symbol in entered:
                events.append(BanEvent(
                    event_id=f"{EVENT_TYPE_ENTRY}:{symbol}:{snap.date.isoformat()}",
                    symbol=symbol,
                    event_type=EVENT_TYPE_ENTRY,
                    effective_date=snap.date.isoformat(),
                    detail={"previous_observed_date": prev.date.isoformat(), "gap_calendar_days": gap_days},
                    source_file=source_file,
                ))
            for symbol in exited:
                events.append(BanEvent(
                    event_id=f"{EVENT_TYPE_EXIT}:{symbol}:{snap.date.isoformat()}",
                    symbol=symbol,
                    event_type=EVENT_TYPE_EXIT,
                    effective_date=snap.date.isoformat(),
                    detail={"previous_observed_date": prev.date.isoformat(), "gap_calendar_days": gap_days},
                    source_file=source_file,
                ))
        prev = snap

    return events

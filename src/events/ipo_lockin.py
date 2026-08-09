"""
IPO lock-in unlock event generation (SIGNAL_TRACKER_REQUIREMENTS.md §7.2).

Unlike fo_ban's events (detected by diffing consecutive daily archives), these are known
in advance and derived directly from the manually-maintained seed/ipo_listings.csv — each
populated unlock-date column on a row becomes one event. No "detection" step is possible
or needed; the seed file *is* the source of truth here, which is exactly why it's manual
(§4.4: forces someone to read the actual listing circular).
"""
from __future__ import annotations

from dataclasses import dataclass

from parse.ipo_listings import IpoListing

EVENT_TYPE = "ipo_lockin_unlock"

# (seed column, tranche label, unlock percentage)
TRANCHES = [
    ("anchor_lockin_30d", "anchor_30d", 50),
    ("anchor_lockin_90d", "anchor_90d", 50),
    ("preipo_lockin_180d", "preipo_180d", 100),
]


@dataclass
class IpoLockinEvent:
    event_id: str
    symbol: str
    event_type: str
    effective_date: str
    detail: dict
    source_file: str


def generate_events(listings: list[IpoListing], source_file: str) -> list[IpoLockinEvent]:
    events = []
    for listing in listings:
        for column, tranche, pct in TRANCHES:
            unlock_date = getattr(listing, column)
            if not unlock_date:
                continue
            events.append(IpoLockinEvent(
                event_id=f"{EVENT_TYPE}:{listing.symbol}:{tranche}:{unlock_date}",
                symbol=listing.symbol,
                event_type=EVENT_TYPE,
                effective_date=unlock_date,
                detail={"tranche": tranche, "unlock_pct": pct, "listing_date": listing.listing_date},
                source_file=source_file,
            ))
    return events

"""
Parser for data/seed/ipo_listings.csv — manually maintained, per
SIGNAL_TRACKER_REQUIREMENTS.md §4.4: entered by hand from NSE listing circulars, which
forces someone to actually read the announcement. Not a raw-archive auto-collected
source, so it doesn't get the same defensive "reject anything unexpected" treatment as
the NSE parsers — a typo here is the operator's own to notice and fix, not a sign the
exchange silently changed a format.

Columns: symbol, listing_date, anchor_lockin_30d, anchor_lockin_90d, preipo_lockin_180d
(all dates ISO YYYY-MM-DD). The three unlock-date columns are optional per row — leave
blank if not applicable (e.g. no anchor allocation in that IPO).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


class ParseError(Exception):
    pass


REQUIRED_COLUMNS = {"symbol", "listing_date", "anchor_lockin_30d", "anchor_lockin_90d", "preipo_lockin_180d"}


@dataclass(frozen=True)
class IpoListing:
    symbol: str
    listing_date: str
    anchor_lockin_30d: str | None
    anchor_lockin_90d: str | None
    preipo_lockin_180d: str | None


def parse_file(path: Path) -> list[IpoListing]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []
        header = {h.strip() for h in reader.fieldnames}
        missing = REQUIRED_COLUMNS - header
        if missing:
            raise ParseError(f"{path}: missing expected columns {sorted(missing)}")

        listings = []
        for row in reader:
            symbol = (row.get("symbol") or "").strip()
            if not symbol:
                continue
            listings.append(IpoListing(
                symbol=symbol,
                listing_date=(row.get("listing_date") or "").strip(),
                anchor_lockin_30d=(row.get("anchor_lockin_30d") or "").strip() or None,
                anchor_lockin_90d=(row.get("anchor_lockin_90d") or "").strip() or None,
                preipo_lockin_180d=(row.get("preipo_lockin_180d") or "").strip() or None,
            ))
        return listings

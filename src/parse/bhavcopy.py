"""
Parser for the raw "full bhavcopy" CSV (data/raw/bhavcopy/*), keyed off the file's own
header row rather than an assumed fixed schema — per PHASED_IMPLEMENTATION_PLAN.md Phase 2's
warning that NSE has changed column names/ordering mid-archive before.

v1 scope is cash equity only (SIGNAL_TRACKER_REQUIREMENTS.md §3), so only SERIES == "EQ" rows
become `prices` rows. Every other series present in the file (BE, SME's SM/ST, bonds' GS/GB,
etc.) is counted and reported, not silently dropped — see ParseResult.series_counts.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EQUITY_SERIES = {"EQ"}

REQUIRED_COLUMNS = {
    "SYMBOL", "SERIES", "DATE1", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE",
    "CLOSE_PRICE", "TTL_TRD_QNTY", "DELIV_PER",
}


class ParseError(Exception):
    pass


@dataclass
class PriceRow:
    symbol: str
    date: str  # ISO YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int
    delivery_pct: float | None
    source: str = "nse_bhavcopy_full"


@dataclass
class ParseResult:
    rows: list[PriceRow]
    total_rows: int
    series_counts: dict[str, int] = field(default_factory=dict)


def _num(value: str) -> float | None:
    value = value.strip()
    if value in ("", "-"):
        return None
    return float(value)


def parse_file(path: Path) -> ParseResult:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            raw_header = next(reader)
        except StopIteration:
            raise ParseError(f"{path}: empty file")
        header = [h.strip() for h in raw_header]

        missing = REQUIRED_COLUMNS - set(header)
        if missing:
            raise ParseError(
                f"{path}: missing expected columns {sorted(missing)} — "
                f"NSE may have changed the bhavcopy format, do not guess positionally"
            )
        idx = {name: i for i, name in enumerate(header)}

        rows: list[PriceRow] = []
        series_counts: dict[str, int] = {}
        total_rows = 0

        for raw_row in reader:
            if not raw_row or all(not c.strip() for c in raw_row):
                continue
            total_rows += 1
            cells = [c.strip() for c in raw_row]
            series = cells[idx["SERIES"]]
            series_counts[series] = series_counts.get(series, 0) + 1
            if series not in EQUITY_SERIES:
                continue

            date_iso = datetime.strptime(cells[idx["DATE1"]], "%d-%b-%Y").date().isoformat()
            volume_raw = _num(cells[idx["TTL_TRD_QNTY"]])
            rows.append(
                PriceRow(
                    symbol=cells[idx["SYMBOL"]],
                    date=date_iso,
                    open=_num(cells[idx["OPEN_PRICE"]]),
                    high=_num(cells[idx["HIGH_PRICE"]]),
                    low=_num(cells[idx["LOW_PRICE"]]),
                    close=_num(cells[idx["CLOSE_PRICE"]]),
                    volume=int(volume_raw) if volume_raw is not None else None,
                    delivery_pct=_num(cells[idx["DELIV_PER"]]),
                )
            )

    return ParseResult(rows=rows, total_rows=total_rows, series_counts=series_counts)

"""Parser for the raw NIFTY 50 snapshot CSVs (data/raw/index/*) — just Date,Close."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class IndexPriceRow:
    date: str
    close: float


def parse_file(path: Path) -> list[IndexPriceRow]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or {"Date", "Close"} - set(reader.fieldnames):
            raise ParseError(f"{path}: expected Date,Close columns")
        rows = []
        for row in reader:
            date_str = row["Date"].strip()
            close_str = row["Close"].strip()
            if not date_str or not close_str:
                continue
            rows.append(IndexPriceRow(date=date_str, close=float(close_str)))
        return rows

"""
Parser for NSE index constituent files (data/raw/sector_map/*).

Columns as published: Company Name, Industry, Symbol, Series, ISIN Code. Keyed off the
header rather than position, and rejects a file missing the columns we depend on — same
defensive stance as the bhavcopy parser, since this is an auto-collected NSE file whose
layout can change without notice.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

REQUIRED_COLUMNS = {"Symbol", "Industry"}


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class SectorRow:
    symbol: str
    sector: str
    company_name: str | None


def parse_file(path: Path) -> list[SectorRow]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ParseError(f"{path}: empty file")
        header = {h.strip() for h in reader.fieldnames}
        missing = REQUIRED_COLUMNS - header
        if missing:
            raise ParseError(
                f"{path}: missing expected columns {sorted(missing)} — "
                "NSE may have changed the constituent file layout, do not guess"
            )

        rows = []
        for raw in reader:
            symbol = (raw.get("Symbol") or "").strip()
            sector = (raw.get("Industry") or "").strip()
            if not symbol or not sector:
                continue
            rows.append(SectorRow(
                symbol=symbol,
                sector=sector,
                company_name=(raw.get("Company Name") or "").strip() or None,
            ))
        return rows

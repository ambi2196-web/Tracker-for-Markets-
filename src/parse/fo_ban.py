"""
Parser for the raw F&O securities-in-ban file (data/raw/fo_ban/*).

Real format, confirmed against live NSE data during Phase 3 (not guessed — see
phase0_spike/findings.md §2 for the original NIL-only observation, and the Phase 3
backfill for the multi-symbol confirmation):

    Empty day:    "Securities in Ban For Trade Date 03-AUG-2026: NIL"
    Non-empty:    "Securities in Ban For Trade Date 07-AUG-2026:\n1,BANDHANBNK\n2,LICI"

Each banned symbol is a "<serial>,<symbol>" token. Anything that doesn't match this
shape raises ParseError rather than being silently dropped or guessed at — per
PHASED_IMPLEMENTATION_PLAN.md Phase 3's warning that the file "occasionally publishes
... in a different layout on expiry days."
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

HEADER_RE = re.compile(r"^Securities in Ban For Trade Date (\d{2}-[A-Za-z]{3}-\d{4}):\s*(.*)$", re.DOTALL)
ENTRY_RE = re.compile(r"^(\d+),(\S+)$")


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class BanSnapshot:
    date: date
    symbols: frozenset[str]


def parse_file(path: Path) -> BanSnapshot:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    m = HEADER_RE.match(text)
    if not m:
        raise ParseError(f"{path}: header did not match expected 'Securities in Ban For Trade Date ...' shape")

    date_str, rest = m.groups()
    trade_date = datetime.strptime(date_str.title(), "%d-%b-%Y").date()
    rest = rest.strip()

    if rest.upper() == "NIL":
        return BanSnapshot(date=trade_date, symbols=frozenset())

    symbols = []
    for token in rest.split():
        entry_match = ENTRY_RE.match(token)
        if not entry_match:
            raise ParseError(
                f"{path}: unexpected ban-list entry {token!r} — layout may have changed, do not guess"
            )
        symbols.append(entry_match.group(2))
    return BanSnapshot(date=trade_date, symbols=frozenset(symbols))

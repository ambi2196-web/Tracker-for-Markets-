"""
NSE ASM/GSM surveillance indicator (REG_IND) collector.

Not a short "list of flagged symbols" — one row per listed symbol every trading day,
with per-indicator columns (GSM, Long/Short-Term ASM, ESM, pledge, etc.). See
phase0_spike/findings.md §3. Value semantics (what "100" vs "1" means per column) are not
yet confirmed against NSE's file-structure PDF; that confirmation is a Phase 3 blocker for
event detection, not a Phase 1 blocker for archiving the raw file unmodified.

URL discovered via NSE's internal `https://www.nseindia.com/api/merged-daily-reports?key=CM`
catalog (fileKey=CM-SURVEILLANCE-INDICATOR-CSV), not by guessing a filename pattern.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .nse_client import NSEClient, FetchError
from .outcome import CollectOutcome

SOURCE = "asm"


def _url_for(d: date) -> str:
    # 2-digit year, unlike bhavcopy/ban list which use 4-digit year.
    return f"https://nsearchives.nseindia.com/content/cm/REG_IND{d.strftime('%d%m%y')}.csv"


def _dest_for(d: date, data_root: Path) -> Path:
    filename = f"REG_IND{d.strftime('%d%m%y')}.csv"
    return data_root / "raw" / SOURCE / f"{d.year:04d}" / f"{d.month:02d}" / filename


def collect(d: date, data_root: Path, client: NSEClient, *, dry_run: bool = False) -> CollectOutcome:
    dest = _dest_for(d, data_root)
    if dest.exists():
        return CollectOutcome(SOURCE, "already_archived", f"{dest} already exists, skipping fetch")

    url = _url_for(d)
    if dry_run:
        return CollectOutcome(SOURCE, "success", f"[dry-run] would fetch {url} -> {dest}", url=url)

    try:
        result = client.fetch(url)
    except FetchError as exc:
        return CollectOutcome(SOURCE, "failed", str(exc), url=url)

    head = result.content[:60].decode("utf-8", errors="replace")
    if "scripcode" not in head.lower():
        return CollectOutcome(
            SOURCE, "failed", f"response did not look like the REG_IND file (head: {head!r})", url=url
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(result.content)
    return CollectOutcome(
        SOURCE,
        "success",
        f"wrote {len(result.content)} bytes",
        url=url,
        dest_path=str(dest),
        bytes_written=len(result.content),
        elapsed_seconds=result.elapsed_seconds,
    )

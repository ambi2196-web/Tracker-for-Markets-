"""
NSE F&O securities-in-ban list collector.

See phase0_spike/findings.md §2. On days with no banned securities the file is a single
line ("Securities in Ban For Trade Date DD-MON-YYYY: NIL"), not a data table — callers
downstream (Phase 3 event detection) must special-case that sentinel rather than assume a
header + rows.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .nse_client import NSEClient, FetchError
from .outcome import CollectOutcome

SOURCE = "fo_ban"


def _url_for(d: date) -> str:
    return f"https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{d.strftime('%d%m%Y')}.csv"


def _dest_for(d: date, data_root: Path) -> Path:
    filename = f"fo_secban_{d.strftime('%d%m%Y')}.csv"
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
    if "ban" not in head.lower():
        return CollectOutcome(
            SOURCE, "failed", f"response did not look like the ban-list file (head: {head!r})", url=url
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

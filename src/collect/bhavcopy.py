"""
NSE full bhavcopy + delivery data collector.

Uses the "full bhavcopy" endpoint (CM-BHAVDATA-FULL in NSE's own report catalog), not the
newer UDiFF zip, because this one carries DELIV_QTY/DELIV_PER natively and the `prices`
table (SIGNAL_TRACKER_REQUIREMENTS.md §8) needs delivery_pct. See phase0_spike/findings.md §1.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .nse_client import NSEClient, FetchError
from .outcome import CollectOutcome

SOURCE = "bhavcopy"


def _url_for(d: date) -> str:
    return f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"


def _dest_for(d: date, data_root: Path) -> Path:
    filename = f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
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

    # Sanity-check the header row before archiving, since a wrong-but-200 response
    # (e.g. a redirected landing page) would otherwise pass content-type validation.
    head = result.content[:80].decode("utf-8", errors="replace")
    if "SYMBOL" not in head.upper():
        return CollectOutcome(
            SOURCE, "failed", f"response did not look like bhavcopy CSV (head: {head!r})", url=url
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

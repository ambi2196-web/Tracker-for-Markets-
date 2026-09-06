"""
NSE sector-map collector — Phase 8 work item 3.

Source: NSE's published index constituent files, which carry an `Industry` column using
NSE's own sector taxonomy. We use the Total Market list rather than stitching together
~13 individual sectoral index files because it is one fetch instead of thirteen, covers
755 symbols instead of a few hundred, and every sector but one clears MIN_CONSTITUENTS —
which matters for a *breadth* measure, where thin coverage silently biases the median.

Note the `Industry` column is not the same as index membership: ASHOKLEY sits in Nifty
Auto but is classified "Capital Goods". The published Industry field is the more coherent
grouping for dispersion, so that is what we key on.

Like index_prices.py, one fetch returns a bulk snapshot rather than a per-day file, so
each archive file is a timestamped snapshot — immutable once written, never overwritten.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .nse_client import NSEClient

TOTAL_MARKET_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"
SOURCE_NAME = "nse_index_constituents"


def fetch_and_archive(data_root: Path, client: NSEClient | None = None, url: str = TOTAL_MARKET_URL) -> Path:
    client = client or NSEClient()
    result = client.fetch(url)

    head = result.content[:200].decode("utf-8", errors="replace")
    if "Symbol" not in head or "Industry" not in head:
        from .nse_client import FetchError
        raise FetchError(f"constituent file missing Symbol/Industry columns (head: {head[:120]!r})")

    dest_dir = data_root / "raw" / "sector_map"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"nifty_totalmarket_{stamp}.csv"
    dest.write_bytes(result.content)
    return dest

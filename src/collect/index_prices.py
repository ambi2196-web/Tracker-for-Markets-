"""
NIFTY 50 index-level collector — closes the Phase 7 regime-comparison gap flagged
repeatedly in render_summary.py: "signal performance is never presented without its beta
context" (SIGNAL_TRACKER_REQUIREMENTS.md §9) requires an index return series, and nothing
in the NSE bhavcopy archive we already use carries index *levels* (only per-security
rows). yfinance is explicitly allowed for this in §4.2/§6 ("Backfill and gap repair
only") and is unaffected by NSE's own anti-bot blocking since it hits Yahoo's
infrastructure, not nseindia.com.

Unlike the per-day NSE collectors, one fetch call returns a bulk date range, so each
archive file is a dated snapshot of "whatever yfinance returned for this range as of
today" rather than a single trading day's file — still immutable once written, just a
coarser unit. Re-running for a range already covered by an existing snapshot is fine;
each fetch is saved under its own timestamped filename rather than overwriting.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

TICKER = "^NSEI"
INDEX_NAME = "NIFTY50"


def fetch_and_archive(start_date: date, end_date: date, data_root: Path) -> Path | None:
    import yfinance as yf

    df = yf.Ticker(TICKER).history(start=start_date.isoformat(), end=(end_date).isoformat())
    if df.empty:
        return None

    dest_dir = data_root / "raw" / "index"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"nifty50_{start_date.isoformat()}_{end_date.isoformat()}_{stamp}.csv"

    out = df.reset_index()[["Date", "Close"]]
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out.to_csv(dest, index=False)
    return dest

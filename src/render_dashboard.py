#!/usr/bin/env python3
"""
Renders out/dashboard.html — a single self-contained static file, opened directly from
disk, per SIGNAL_TRACKER_REQUIREMENTS.md §10: inline CSS, no CDN, no JS frameworks, must
render correctly with the network disconnected.

Health strip reads data/health.json directly (updated by every `run_collect.py` run) so it
stays accurate even if the database hasn't been rebuilt yet. Everything else reads
data/tracker.db. Action-required / calendar / ledger / recent-transitions sections are
placeholders until Phase 3 (events) and Phase 4 (signal state machine + ledger) exist —
they say so plainly rather than rendering fabricated rows.

Usage:
    python src/render_dashboard.py
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "data"
DB_PATH = DATA_ROOT / "tracker.db"
HEALTH_JSON_PATH = DATA_ROOT / "health.json"
OUT_PATH = REPO_ROOT / "out" / "dashboard.html"

TRADING_DAYS_STALE_THRESHOLD = 2  # per SIGNAL_TRACKER_REQUIREMENTS.md §4.5


def _load_health() -> dict:
    if not HEALTH_JSON_PATH.exists():
        return {}
    with HEALTH_JSON_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _days_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    then = datetime.fromisoformat(iso_ts)
    now = datetime.now(timezone.utc) if then.tzinfo else datetime.now()
    return (now - then).total_seconds() / 86400.0


def _health_rows() -> list[dict]:
    health = _load_health()
    rows = []
    for source in sorted(health.keys()):
        row = health[source]
        age_days = _days_since(row.get("last_success"))
        if row.get("last_success") is None:
            status = "red"
        elif age_days is not None and age_days > TRADING_DAYS_STALE_THRESHOLD:
            status = "red"
        elif row.get("last_attempt") != row.get("last_success"):
            # Most recent attempt did not succeed (last_error is set). Not stale enough
            # to be red yet — e.g. today's file simply isn't published yet — but a green
            # dot next to a visible last_error would be misleading, so flag it amber.
            status = "amber"
        else:
            status = "green"
        rows.append(
            {
                "source": source,
                "status": status,
                "last_success": row.get("last_success") or "never",
                "last_attempt": row.get("last_attempt") or "never",
                "consecutive_failures": row.get("consecutive_failures", 0),
                "last_error": row.get("last_error"),
            }
        )
    return rows


def _price_snapshot(conn: sqlite3.Connection) -> tuple[str | None, list[dict], dict]:
    latest_date_row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    latest_date = latest_date_row[0] if latest_date_row else None
    if not latest_date:
        return None, [], {}

    rows = conn.execute(
        """
        SELECT p.symbol, p.close, p.volume, p.delivery_pct,
               (SELECT close FROM prices p2
                WHERE p2.symbol = p.symbol AND p2.date < p.date
                ORDER BY p2.date DESC LIMIT 1) AS prev_close
        FROM prices p
        WHERE p.date = ?
        ORDER BY p.symbol
        """,
        (latest_date,),
    ).fetchall()

    snapshot = []
    for symbol, close, volume, delivery_pct, prev_close in rows:
        pct_change = None
        if prev_close not in (None, 0) and close is not None:
            pct_change = (close - prev_close) / prev_close * 100.0
        snapshot.append(
            {
                "symbol": symbol,
                "close": close,
                "pct_change": pct_change,
                "volume": volume,
                "delivery_pct": delivery_pct,
            }
        )

    stats = {
        "symbol_count": len(snapshot),
        "min_date": conn.execute("SELECT MIN(date) FROM prices").fetchone()[0],
        "max_date": latest_date,
        "distinct_dates": conn.execute("SELECT COUNT(DISTINCT date) FROM prices").fetchone()[0],
    }
    return latest_date, snapshot, stats


def _fmt(v, spec="") -> str:
    if v is None:
        return "&mdash;"
    if spec:
        return format(v, spec)
    return html.escape(str(v))


def render() -> str:
    health_rows = _health_rows()

    conn = sqlite3.connect(DB_PATH) if DB_PATH.exists() else None
    if conn is not None:
        latest_date, snapshot, stats = _price_snapshot(conn)
        conn.close()
    else:
        latest_date, snapshot, stats = None, [], {}

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    health_html = "\n".join(
        f"""
        <tr>
          <td><span class="dot {r['status']}"></span>{html.escape(r['source'])}</td>
          <td>{html.escape(r['last_success'])}</td>
          <td>{html.escape(r['last_attempt'])}</td>
          <td>{r['consecutive_failures']}</td>
          <td class="err">{html.escape(r['last_error']) if r['last_error'] else '&mdash;'}</td>
        </tr>"""
        for r in health_rows
    ) or '<tr><td colspan="5">No health data yet — run run_collect.py at least once.</td></tr>'

    any_red = any(r["status"] == "red" for r in health_rows)
    banner_html = (
        '<div class="banner red">One or more sources are stale or have never succeeded. '
        "Numbers below may be provisional — check the health strip before trusting anything else on this page.</div>"
        if any_red
        else ""
    )

    price_rows_html = "\n".join(
        f"""
        <tr>
          <td>{html.escape(r['symbol'])}</td>
          <td class="num">{_fmt(r['close'], '.2f')}</td>
          <td class="num {'pos' if (r['pct_change'] or 0) > 0 else 'neg' if (r['pct_change'] or 0) < 0 else ''}">
            {_fmt(r['pct_change'], '+.2f') + '%' if r['pct_change'] is not None else '&mdash;'}
          </td>
          <td class="num">{_fmt(r['volume'], ',')}</td>
          <td class="num">{_fmt(r['delivery_pct'], '.1f')}</td>
        </tr>"""
        for r in snapshot
    ) or '<tr><td colspan="5">No price data yet — run build_db.py --live after the first collection.</td></tr>'

    coverage_note = (
        f"{stats.get('distinct_dates', 0)} trading day(s) archived "
        f"({stats.get('min_date', '—')} to {stats.get('max_date', '—')}), "
        f"{stats.get('symbol_count', 0)} EQ symbols on the latest date."
        if stats.get("distinct_dates")
        else "No data in tracker.db yet."
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Signal Tracker Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #0f1115; --panel: #171a21; --border: #2a2e37; --text: #e6e8eb; --muted: #9aa1ab;
    --green: #3ecf6e; --amber: #e3b341; --red: #f0554f; --accent: #5b9dff;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f5f6f8; --panel: #ffffff; --border: #dfe2e7; --text: #1a1d23; --muted: #5b626e;
      --green: #1e9e4e; --amber: #a5750c; --red: #c8362f; --accent: #1f6fdb;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 24px; max-width: 1100px; margin-inline: auto;
  }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .meta {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }}
  .panel {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 20px; margin-bottom: 20px; overflow-x: auto;
  }}
  .panel h2 {{ font-size: 1rem; margin: 0 0 12px 0; display: flex; align-items: center; gap: 8px; }}
  .placeholder {{ color: var(--muted); font-size: 0.9rem; line-height: 1.5; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 600; cursor: pointer; user-select: none; white-space: nowrap; }}
  th:hover {{ color: var(--accent); }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.err {{ color: var(--red); font-size: 0.8rem; }}
  .pos {{ color: var(--green); }}
  .neg {{ color: var(--red); }}
  .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; }}
  .dot.green {{ background: var(--green); }}
  .dot.amber {{ background: var(--amber); }}
  .dot.red {{ background: var(--red); }}
  .banner.red {{
    background: color-mix(in srgb, var(--red) 15%, transparent);
    border: 1px solid var(--red); color: var(--text);
    padding: 10px 14px; border-radius: 8px; margin-bottom: 20px; font-size: 0.9rem;
  }}
  input#search {{
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: 6px 10px; border-radius: 6px; margin-bottom: 10px; width: 240px; font-size: 0.85rem;
  }}
  .maxrows {{ color: var(--muted); font-size: 0.78rem; margin-top: 8px; }}
</style>
</head>
<body>
  <h1>Signal Tracker Dashboard</h1>
  <div class="meta">Generated {generated_at} &middot; {coverage_note}</div>

  {banner_html}

  <div class="panel">
    <h2>Health</h2>
    <table>
      <thead><tr><th>Source</th><th>Last success</th><th>Last attempt</th><th>Consecutive failures</th><th>Last error</th></tr></thead>
      <tbody>{health_html}</tbody>
    </table>
  </div>

  <div class="panel">
    <h2>Action required</h2>
    <div class="placeholder">Not available yet &mdash; requires Phase 4 (signal state machine + ledger). Nothing to show until signals exist.</div>
  </div>

  <div class="panel">
    <h2>Calendar (next 30 days)</h2>
    <div class="placeholder">Not available yet &mdash; requires Phase 3 (event detection). Nothing to show until events are ingested.</div>
  </div>

  <div class="panel">
    <h2>Latest price snapshot{f' ({latest_date})' if latest_date else ''}</h2>
    <input type="text" id="search" placeholder="Filter by symbol&hellip;" oninput="filterTable()">
    <table id="priceTable">
      <thead><tr>
        <th onclick="sortTable(0)">Symbol</th>
        <th onclick="sortTable(1)">Close</th>
        <th onclick="sortTable(2)">Chg %</th>
        <th onclick="sortTable(3)">Volume</th>
        <th onclick="sortTable(4)">Delivery %</th>
      </tr></thead>
      <tbody>{price_rows_html}</tbody>
    </table>
    <div class="maxrows">Chg % is blank until a symbol has 2+ archived trading days.</div>
  </div>

  <div class="panel">
    <h2>Ledger summary</h2>
    <div class="placeholder">Not available yet &mdash; requires Phase 4. No signal type has any closed paper observations.</div>
  </div>

  <div class="panel">
    <h2>Recent state transitions</h2>
    <div class="placeholder">Not available yet &mdash; requires Phase 4.</div>
  </div>

<script>
function filterTable() {{
  const q = document.getElementById('search').value.toLowerCase();
  const rows = document.querySelectorAll('#priceTable tbody tr');
  rows.forEach(row => {{
    const symbol = row.cells[0]?.textContent.toLowerCase() || '';
    row.style.display = symbol.includes(q) ? '' : 'none';
  }});
}}
let sortDirs = {{}};
function sortTable(colIndex) {{
  const table = document.getElementById('priceTable');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const dir = sortDirs[colIndex] = !sortDirs[colIndex];
  rows.sort((a, b) => {{
    let x = a.cells[colIndex]?.textContent.trim().replace(/[%,]/g, '') || '';
    let y = b.cells[colIndex]?.textContent.trim().replace(/[%,]/g, '') || '';
    const nx = parseFloat(x), ny = parseFloat(y);
    let cmp;
    if (!isNaN(nx) && !isNaN(ny)) cmp = nx - ny;
    else cmp = x.localeCompare(y);
    return dir ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</body>
</html>
"""


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

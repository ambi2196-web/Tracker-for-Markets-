#!/usr/bin/env python3
"""
Renders out/dashboard.html — a single self-contained static file, opened directly from
disk, per SIGNAL_TRACKER_REQUIREMENTS.md §10: inline CSS, no CDN, no JS frameworks, must
render correctly with the network disconnected.

Health strip reads data/health.json directly (updated by every `run_collect.py` run) so it
stays accurate even if the database hasn't been rebuilt yet. Everything else reads
data/tracker.db. Recent state transitions remains a placeholder — signals/ledger only
store current state, not a change history, so there's nothing real to show yet.

Every list-of-rows panel (Action required, Outlier moments, Recent events) is
click-to-expand: each row has a hidden detail row beneath it with the full underlying
record, toggled by plain JS (no framework) — a flat table plus a click target, not a
separate drill-down view.

Usage:
    python src/render_dashboard.py
"""
from __future__ import annotations

import html
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import dispersion  # noqa: E402
import ledger as ledger_api  # noqa: E402
import regime  # noqa: E402
import verdict  # noqa: E402

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


def _recent_events(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT event_type, symbol, effective_date, source_file, detail
            FROM events
            WHERE event_type IN ('fo_ban_entry', 'fo_ban_exit')
            ORDER BY effective_date DESC, symbol
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # events table doesn't exist yet (build_events.py not run)
    return [
        {"event_type": t, "symbol": s, "effective_date": d, "source_file": sf,
         "detail": json.loads(dj) if dj else {}}
        for t, s, d, sf, dj in rows
    ]


def _insights_bar(conn: sqlite3.Connection, action_items: list[dict], health_rows: list[dict]) -> dict:
    """Compact at-a-glance stats for the top of the page — counts only, no verdicts (the
    20-observation floor still applies further down), just enough to answer 'does
    anything need my attention right now' without scrolling."""
    try:
        state_counts = dict(conn.execute("SELECT state, COUNT(*) FROM signals GROUP BY state").fetchall())
    except sqlite3.OperationalError:
        state_counts = {}

    try:
        events_7d = conn.execute(
            "SELECT COUNT(*) FROM events WHERE effective_date >= date((SELECT MAX(date) FROM prices), '-7 days')"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        events_7d = 0

    urgent_window_end = next((a["window_end"] for a in action_items if a["window_end"]), None)
    overall_health = "red" if any(r["status"] == "red" for r in health_rows) else (
        "amber" if any(r["status"] == "amber" for r in health_rows) else "green"
    )

    return {
        "needs_attention": len(action_items),
        "urgent_window_end": urgent_window_end,
        "open_count": state_counts.get("open", 0) + state_counts.get("exit_due", 0),
        "closed_count": state_counts.get("closed", 0),
        "events_7d": events_7d,
        "overall_health": overall_health,
    }


MIN_OBSERVATIONS_FOR_VERDICT = 20  # SIGNAL_TRACKER_REQUIREMENTS.md §9 reporting rule


def _action_required(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT symbol, signal_type, state, window_end, armed_date, window_start,
                   entry_date, entry_ref_price, stop_price, filters_passed
            FROM signals
            WHERE state IN ('armed', 'open', 'exit_due')
            ORDER BY (window_end IS NULL), window_end
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for s, t, st, we, armed, ws, ed, erp, sp, fp in rows:
        out.append({
            "symbol": s, "signal_type": t, "state": st, "window_end": we,
            "armed_date": armed, "window_start": ws, "entry_date": ed,
            "entry_ref_price": erp, "stop_price": sp,
            "filters_passed": json.loads(fp) if fp else {},
        })
    return out


def _ledger_summary(conn: sqlite3.Connection) -> list[dict]:
    """Per §9: never pool paper/live, and (extended here) never pool long/short since
    direction is explicitly undetermined for this signal type. No verdict below the
    20-closed-observation floor — count only."""
    try:
        rows = conn.execute(
            """
            SELECT s.signal_type, l.direction, l.mode,
                   COUNT(*) AS n_closed,
                   AVG(l.net_return_pct) AS mean_net,
                   SUM(CASE WHEN l.net_return_pct > 0 THEN 1 ELSE 0 END) AS n_wins,
                   MIN(l.net_return_pct) AS worst,
                   AVG(l.holding_days) AS mean_holding_days
            FROM ledger l JOIN signals s ON s.signal_id = l.signal_id
            WHERE l.exit_date IS NOT NULL AND l.track = 'mechanism'
            GROUP BY s.signal_type, l.direction, l.mode
            ORDER BY s.signal_type, l.direction
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for signal_type, direction, mode, n, mean_net, n_wins, worst, mean_hold in rows:
        out.append({
            "signal_type": signal_type, "direction": direction, "mode": mode, "n_closed": n,
            "hit_rate": (n_wins / n * 100.0) if n else None,
            "mean_net": mean_net, "worst": worst, "mean_holding_days": mean_hold,
            "verdict_ready": n >= MIN_OBSERVATIONS_FOR_VERDICT,
        })
    return out


def _outlier_trades(conn: sqlite3.Connection, limit: int = 3) -> dict:
    """Best and worst closed paper trades by net return — the extremes, not a flat
    chronological dump. This is what the "outlier moments at front" ask is about: a plain
    events list buries the KAYNES +18%/-12% swing the same way it buries a routine +1%
    trade; this surfaces it directly."""
    try:
        best = conn.execute(
            """
            SELECT s.symbol, s.signal_type, l.direction, l.entry_date, l.entry_price,
                   l.exit_date, l.exit_price, l.exit_reason, l.net_return_pct, l.holding_days
            FROM ledger l JOIN signals s ON s.signal_id = l.signal_id
            WHERE l.exit_date IS NOT NULL AND l.track = 'mechanism'
            ORDER BY l.net_return_pct DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        worst = conn.execute(
            """
            SELECT s.symbol, s.signal_type, l.direction, l.entry_date, l.entry_price,
                   l.exit_date, l.exit_price, l.exit_reason, l.net_return_pct, l.holding_days
            FROM ledger l JOIN signals s ON s.signal_id = l.signal_id
            WHERE l.exit_date IS NOT NULL AND l.track = 'mechanism'
            ORDER BY l.net_return_pct ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"best": [], "worst": []}

    def _row(r):
        sym, styp, direction, ed, ep, xd, xp, reason, net, hold = r
        return {
            "symbol": sym, "signal_type": styp, "direction": direction,
            "entry_date": ed, "entry_price": ep, "exit_date": xd, "exit_price": xp,
            "exit_reason": reason, "net_return_pct": net, "holding_days": hold,
        }

    return {"best": [_row(r) for r in best], "worst": [_row(r) for r in worst]}


def _calendar_upcoming(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Real forward-looking content per SIGNAL_TRACKER_REQUIREMENTS.md §10 ("next 30
    days of known effective dates") — window_end dates for signals still in flight,
    within `days` of the latest archived trading day. Empty until a signal is actually
    open/armed with a resolvable window; that's an honest empty state, not a
    placeholder."""
    try:
        rows = conn.execute(
            """
            SELECT symbol, signal_type, state, window_end
            FROM signals
            WHERE window_end IS NOT NULL
              AND state IN ('open', 'exit_due')
              AND julianday(window_end) - julianday((SELECT MAX(date) FROM prices)) BETWEEN 0 AND ?
            ORDER BY window_end
            """,
            (days,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"symbol": s, "signal_type": t, "state": st, "window_end": we} for s, t, st, we in rows]


def _fmt(v, spec="") -> str:
    if v is None:
        return "&mdash;"
    if spec:
        return format(v, spec)
    return html.escape(str(v))


def _detail_row(colspan: int, inner: str) -> str:
    """A hidden row beneath an `.expandable` row, revealed by toggleDetail() on click."""
    return f'<tr class="detail-row" hidden><td colspan="{colspan}"><div class="detail-box">{inner}</div></td></tr>'


def render() -> str:
    health_rows = _health_rows()

    conn = sqlite3.connect(DB_PATH) if DB_PATH.exists() else None
    if conn is not None:
        latest_date, snapshot, stats = _price_snapshot(conn)
        events = _recent_events(conn)
        action_items = _action_required(conn)
        ledger_summary = _ledger_summary(conn)
        insights = _insights_bar(conn, action_items, health_rows)
        outliers = _outlier_trades(conn)
        calendar_items = _calendar_upcoming(conn)
        regime_info = regime.compute_regime(conn)
        verdicts = verdict.cohort_verdicts(conn)
        research_prompts = dispersion.open_prompts(conn)
        theses_due = ledger_api.theses_past_review(conn)
        conn.close()
    else:
        latest_date, snapshot, stats = None, [], {}
        events = []
        action_items = []
        ledger_summary = []
        insights = {
            "needs_attention": 0, "urgent_window_end": None, "open_count": 0,
            "closed_count": 0, "events_7d": 0, "overall_health": "red",
        }
        outliers = {"best": [], "worst": []}
        calendar_items = []
        regime_info = None
        verdicts = []
        research_prompts = []
        theses_due = []

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

    action_html = "\n".join(
        f"""
        <tr class="expandable" onclick="toggleDetail(this)">
          <td>{html.escape(a['symbol'])} <span class="expand-hint">&#9662;</span></td>
          <td>{html.escape(a['signal_type'])}</td>
          <td>{html.escape(a['state'])}</td>
          <td>{html.escape(a['window_end']) if a['window_end'] else '&mdash;'}</td>
        </tr>
        {_detail_row(4,
            f"Armed {html.escape(a['armed_date'] or '—')} &middot; "
            f"Window {html.escape(a['window_start'] or '—')} &rarr; {html.escape(a['window_end'] or 'pending')}<br>"
            f"Entry {html.escape(a['entry_date'] or 'pending')} @ {_fmt(a['entry_ref_price'], '.2f')} &middot; "
            f"Stop (long side) {_fmt(a['stop_price'], '.2f')}<br>"
            f"Filters: {html.escape(json.dumps(a['filters_passed']))}"
        )}"""
        for a in action_items
    ) or '<tr><td colspan="4">Nothing pending &mdash; no signal is currently armed, open, or exit-due.</td></tr>'

    ledger_html = "\n".join(
        f"""
        <tr>
          <td>{html.escape(r['signal_type'])}</td>
          <td>{html.escape(r['direction'])}</td>
          <td>{html.escape(r['mode'])}</td>
          <td class="num">{r['n_closed']}</td>
          <td class="num">{_fmt(r['hit_rate'], '.0f') + '%' if r['hit_rate'] is not None else '&mdash;'}</td>
          <td class="num {'pos' if (r['mean_net'] or 0) > 0 else 'neg' if (r['mean_net'] or 0) < 0 else ''}">{_fmt(r['mean_net'], '+.2f') + '%' if r['mean_net'] is not None else '&mdash;'}</td>
          <td class="num neg">{_fmt(r['worst'], '+.2f') + '%' if r['worst'] is not None else '&mdash;'}</td>
          <td>{'' if r['verdict_ready'] else f"insufficient sample ({r['n_closed']}/{MIN_OBSERVATIONS_FOR_VERDICT})"}</td>
        </tr>"""
        for r in ledger_summary
    ) or '<tr><td colspan="8">No closed paper trades yet.</td></tr>'

    events_html = "\n".join(
        f"""
        <tr class="expandable" onclick="toggleDetail(this)">
          <td>{html.escape(e['effective_date'])}</td>
          <td>{html.escape(e['symbol'])} <span class="expand-hint">&#9662;</span></td>
          <td class="{'neg' if e['event_type'] == 'fo_ban_entry' else 'pos'}">
            {'entered ban' if e['event_type'] == 'fo_ban_entry' else 'exited ban'}
          </td>
          <td style="font-size:0.78rem;color:var(--muted)">{html.escape(e['source_file'])}</td>
        </tr>
        {_detail_row(4, f"Raw detail: {html.escape(json.dumps(e['detail'])) if e['detail'] else 'none recorded'}")}"""
        for e in events
    ) or '<tr><td colspan="4">No events yet &mdash; run build_events.py after collecting fo_ban data.</td></tr>'

    def _outlier_row(o: dict) -> str:
        net = o["net_return_pct"] or 0.0
        cls = "pos" if net > 0 else "neg" if net < 0 else ""
        return f"""
        <tr class="expandable" onclick="toggleDetail(this)">
          <td>{html.escape(o['symbol'])} <span class="expand-hint">&#9662;</span></td>
          <td>{html.escape(o['signal_type'])}</td>
          <td>{html.escape(o['direction'])}</td>
          <td class="num {cls}">{_fmt(o['net_return_pct'], '+.2f')}%</td>
          <td>{html.escape(o['exit_reason'] or '—')}</td>
        </tr>
        {_detail_row(5,
            f"Entry {html.escape(o['entry_date'] or '—')} @ {_fmt(o['entry_price'], '.2f')} &rarr; "
            f"Exit {html.escape(o['exit_date'] or '—')} @ {_fmt(o['exit_price'], '.2f')}<br>"
            f"Held {o['holding_days'] if o['holding_days'] is not None else '—'} calendar days &middot; "
            f"net {_fmt(o['net_return_pct'], '+.2f')}% after costs"
        )}"""

    outlier_rows = outliers.get("best", [])[:3] + outliers.get("worst", [])[:3]
    # de-dupe in case best/worst overlap on a tiny sample
    seen_ids = set()
    outlier_rows_dedup = []
    for o in outlier_rows:
        key = (o["symbol"], o["entry_date"], o["direction"])
        if key not in seen_ids:
            seen_ids.add(key)
            outlier_rows_dedup.append(o)
    outlier_rows_dedup.sort(key=lambda o: o["net_return_pct"] or 0.0, reverse=True)
    outliers_html = "\n".join(_outlier_row(o) for o in outlier_rows_dedup) or (
        '<tr><td colspan="5">No closed trades yet &mdash; nothing to rank.</td></tr>'
    )

    calendar_html = "\n".join(
        f"""
        <tr>
          <td>{html.escape(c['window_end'])}</td>
          <td>{html.escape(c['symbol'])}</td>
          <td>{html.escape(c['signal_type'])}</td>
          <td>{html.escape(c['state'])}</td>
        </tr>"""
        for c in calendar_items
    ) or '<tr><td colspan="4">Nothing due in the next 30 days &mdash; no open or exit-due signal has a window ending soon.</td></tr>'

    _VERDICT_CLASS = {
        verdict.LABEL_PROMISING: "pos",
        verdict.LABEL_NO_EDGE: "neg",
        verdict.LABEL_INSUFFICIENT: "muted-cell",
        verdict.LABEL_NO_REGIME_DATA: "muted-cell",
    }
    verdict_html = "\n".join(
        f"""
        <tr>
          <td>{html.escape(v['signal_type'])}</td>
          <td>{html.escape(v['direction'])}</td>
          <td class="num">{v['n']}</td>
          <td class="num">{v['hit_rate']:.0f}%</td>
          <td class="num {'pos' if v['mean_net'] > 0 else 'neg' if v['mean_net'] < 0 else ''}">{v['mean_net']:+.2f}%</td>
          <td class="num {'pos' if v['median_net'] > 0 else 'neg' if v['median_net'] < 0 else ''}">{v['median_net']:+.2f}%</td>
          <td class="num">{_fmt(v['matched_nifty'], '+.2f') + '%' if v['matched_nifty'] is not None else '&mdash;'}</td>
          <td class="num {'pos' if (v['spread'] or 0) > 0 else 'neg' if (v['spread'] or 0) < 0 else ''}">{_fmt(v['spread'], '+.2f') + 'pp' if v['spread'] is not None else '&mdash;'}</td>
          <td class="{_VERDICT_CLASS.get(v['verdict'], '')}"><strong>{html.escape(v['verdict'])}</strong></td>
        </tr>"""
        for v in verdicts
    ) or '<tr><td colspan="9">No closed mechanism trades yet.</td></tr>'

    prompts_html = "\n".join(
        f"""
        <tr class="expandable" onclick="toggleDetail(this)">
          <td>{html.escape(p['sector'])} <span class="expand-hint">&#9662;</span></td>
          <td>{html.escape(p['detected_date'])}</td>
          <td class="num neg">{_fmt(p['spread'], '+.1f')}pp</td>
          <td class="num">{p['n_constituents']}</td>
          <td>{html.escape(p['status'])}</td>
        </tr>
        {_detail_row(5,
            f"Median constituent {_fmt(p['median_constituent_return'], '+.2f')}% vs index "
            f"{_fmt(p['index_return'], '+.2f')}% over {p['window_days']} trading days &middot; "
            f"breadth {_fmt((p['breadth'] or 0) * 100, '.0f')}% of constituents down hard.<br>"
            "<strong>This is a prompt to go read, not a position.</strong> No entry price, no stop, "
            "not counted in any performance statistic."
        )}"""
        for p in research_prompts
    ) or '<tr><td colspan="5">Nothing flagged &mdash; no sector is far enough below the index to be worth reading about.</td></tr>'

    theses_html = "\n".join(
        f"""
        <tr class="expandable" onclick="toggleDetail(this)">
          <td>{html.escape(t['subject'])} <span class="expand-hint">&#9662;</span></td>
          <td>{html.escape(t['review_date'] or '&mdash;')}</td>
          <td>{html.escape(t['created_date'])}</td>
          <td>{html.escape(t['status'])}</td>
        </tr>
        {_detail_row(4,
            f"<strong>Falsifier (as originally written):</strong> {html.escape(t['falsifier'])}<br><br>"
            f"<em>Market is pricing:</em> {html.escape(t['claim'])}<br>"
            f"<em>You believed it was missing:</em> {html.escape(t['counter_claim'])}"
        )}"""
        for t in theses_due
    ) or '<tr><td colspan="4">No thesis is past its review date.</td></tr>'

    if regime_info is None:
        regime_line = "No closed trades yet, nothing to compare against."
    elif regime_info["nifty_pct"] is None:
        regime_line = f"No NIFTY 50 data yet for {regime_info['start_date']} to {regime_info['end_date']} — run <code>python src/build_index.py --live</code>."
    else:
        regime_line = (
            f"NIFTY 50 moved <strong>{regime_info['nifty_pct']:+.2f}%</strong> over the same window "
            f"({regime_info['start_date']} to {regime_info['end_date']}) &mdash; read the mean-net figures above against this, not in isolation."
        )

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
  .insights-bar {{
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px;
  }}
  @media (max-width: 700px) {{ .insights-bar {{ grid-template-columns: repeat(2, 1fr); }} }}
  .stat-tile {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; text-align: center;
  }}
  .stat-tile.attn {{ border-color: var(--amber); }}
  .stat-value {{ font-size: 1.7rem; font-weight: 700; line-height: 1.1; }}
  .stat-tile.attn .stat-value {{ color: var(--amber); }}
  .stat-label {{ color: var(--muted); font-size: 0.78rem; margin-top: 4px; }}
  .stat-sub {{ color: var(--muted); font-size: 0.7rem; margin-top: 4px; min-height: 1em; }}
  tr.expandable {{ cursor: pointer; }}
  tr.expandable:hover {{ background: color-mix(in srgb, var(--accent) 8%, transparent); }}
  .expand-hint {{ color: var(--muted); font-size: 0.7rem; }}
  td.muted-cell {{ color: var(--muted); }}
  tr.detail-row td {{ border-bottom: 1px solid var(--border); padding: 0; }}
  .detail-box {{
    background: color-mix(in srgb, var(--accent) 5%, transparent);
    border-left: 3px solid var(--accent); margin: 4px 0; padding: 10px 14px;
    font-size: 0.82rem; line-height: 1.6; color: var(--text);
  }}
</style>
</head>
<body>
  <h1>Signal Tracker Dashboard</h1>
  <div class="meta">Generated {generated_at} &middot; {coverage_note}</div>

  {banner_html}

  <div class="insights-bar">
    <div class="stat-tile {'attn' if insights['needs_attention'] else ''}">
      <div class="stat-value">{insights['needs_attention']}</div>
      <div class="stat-label">Needs attention</div>
      {f'<div class="stat-sub">next window ends {html.escape(insights["urgent_window_end"])}</div>' if insights['urgent_window_end'] else '<div class="stat-sub">&nbsp;</div>'}
    </div>
    <div class="stat-tile">
      <div class="stat-value">{insights['open_count']}</div>
      <div class="stat-label">Open / exit-due</div>
      <div class="stat-sub">&nbsp;</div>
    </div>
    <div class="stat-tile">
      <div class="stat-value">{insights['closed_count']}</div>
      <div class="stat-label">Closed (paper)</div>
      <div class="stat-sub">{max(0, MIN_OBSERVATIONS_FOR_VERDICT - insights['closed_count'])} more for a verdict</div>
    </div>
    <div class="stat-tile">
      <div class="stat-value">{insights['events_7d']}</div>
      <div class="stat-label">Events (7d)</div>
      <div class="stat-sub">&nbsp;</div>
    </div>
    <div class="stat-tile">
      <div class="stat-value"><span class="dot {insights['overall_health']}"></span>{insights['overall_health'].upper()}</div>
      <div class="stat-label">Data health</div>
      <div class="stat-sub">&nbsp;</div>
    </div>
  </div>

  <div class="panel">
    <h2>Track A &mdash; mechanism verdicts</h2>
    <table>
      <thead><tr>
        <th>Signal type</th><th>Direction</th><th>n</th><th>Hit rate</th>
        <th>Mean net</th><th>Median net</th><th>NIFTY (matched)</th><th>Spread</th><th>Verdict</th>
      </tr></thead>
      <tbody>{verdict_html}</tbody>
    </table>
    <div class="maxrows">
      Closed mechanism trades only &mdash; Track B is never averaged in. NIFTY column is the mean
      return over <em>each trade's own entry&rarr;exit window</em>, not a global span, so the spread is
      like-for-like. No verdict below {verdict.MIN_SAMPLE} observations.
      The strongest label this system can emit is &ldquo;{html.escape(verdict.LABEL_PROMISING)}&rdquo; &mdash;
      nothing here ever reads as proven.
    </div>
  </div>

  <div class="panel">
    <h2>Health</h2>
    <table>
      <thead><tr><th>Source</th><th>Last success</th><th>Last attempt</th><th>Consecutive failures</th><th>Last error</th></tr></thead>
      <tbody>{health_html}</tbody>
    </table>
  </div>

  <div class="panel">
    <h2>Outlier moments</h2>
    <table>
      <thead><tr><th>Symbol</th><th>Signal type</th><th>Direction</th><th>Net return</th><th>Exit reason</th></tr></thead>
      <tbody>{outliers_html}</tbody>
    </table>
    <div class="maxrows">Best and worst closed mechanism trades by net return, click a row for the full trace. Not a forecast &mdash; a record of what already happened (§9).</div>
  </div>

  <div class="panel">
    <h2>Action required</h2>
    <table>
      <thead><tr><th>Symbol</th><th>Signal type</th><th>State</th><th>Window end</th></tr></thead>
      <tbody>{action_html}</tbody>
    </table>
  </div>

  <div class="panel">
    <h2>Track B &mdash; go read about this</h2>
    <table>
      <thead><tr><th>Sector</th><th>Detected</th><th>Spread vs index</th><th>Constituents</th><th>Status</th></tr></thead>
      <tbody>{prompts_html}</tbody>
    </table>
    <div class="maxrows">
      Sectors whose <em>median</em> constituent is far below the index over the last
      {dispersion.LOOKBACK_TRADING_DAYS} trading days (trigger: {dispersion.SECTOR_SPREAD_TRIGGER}pp,
      minimum {dispersion.MIN_CONSTITUENTS} constituents). These are research prompts, not signals:
      no entry price, no stop, never counted in any performance statistic. The question they raise is
      whether a temporary input shock is being priced as permanent impairment &mdash; which only reading can answer.
    </div>
  </div>

  <div class="panel">
    <h2>Track B &mdash; theses past review date</h2>
    <table>
      <thead><tr><th>Subject</th><th>Review due</th><th>Opened</th><th>Status</th></tr></thead>
      <tbody>{theses_html}</tbody>
    </table>
    <div class="maxrows">Click through for the falsifier <em>as originally written</em>. It cannot be edited once the position opens &mdash; that is the whole point.</div>
  </div>

  <div class="panel">
    <h2>Recent F&amp;O ban events</h2>
    <table>
      <thead><tr><th>Date</th><th>Symbol</th><th>Event</th><th>Source file</th></tr></thead>
      <tbody>{events_html}</tbody>
    </table>
    <div class="maxrows">Read-only event log (Phase 3) &mdash; not a trade signal. Run build_events.py after each collection to refresh.</div>
  </div>

  <div class="panel">
    <h2>Calendar (next 30 days)</h2>
    <table>
      <thead><tr><th>Window ends</th><th>Symbol</th><th>Signal type</th><th>State</th></tr></thead>
      <tbody>{calendar_html}</tbody>
    </table>
    <div class="maxrows">Known window-end dates for signals currently open or exit-due, within 30 days of the latest archived trading day.</div>
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
    <h2>Ledger summary (paper)</h2>
    <table>
      <thead><tr><th>Type</th><th>Direction</th><th>Mode</th><th>Closed</th><th>Hit rate</th><th>Mean net</th><th>Worst</th><th>Verdict</th></tr></thead>
      <tbody>{ledger_html}</tbody>
    </table>
    <div class="maxrows">Net return only (costs included), per §9. No verdict below {MIN_OBSERVATIONS_FOR_VERDICT} closed observations &mdash; count reported instead.<br>{regime_line}</div>
  </div>

  <div class="panel">
    <h2>Recent state transitions</h2>
    <div class="placeholder">Not implemented yet &mdash; signals/ledger currently show only latest state, not a change history.</div>
  </div>

<script>
function toggleDetail(row) {{
  const detail = row.nextElementSibling;
  if (detail && detail.classList.contains('detail-row')) {{
    detail.hidden = !detail.hidden;
  }}
}}
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

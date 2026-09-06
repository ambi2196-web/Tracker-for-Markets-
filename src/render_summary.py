#!/usr/bin/env python3
"""
Phase 5 — out/daily_summary.md, the fixed §9 analysis protocol.

There is no model in the pipeline, so reproducibility comes from running exactly these
steps in this order every time, not from a sampling parameter — see
SIGNAL_TRACKER_REQUIREMENTS.md §9. This script is that fixed protocol, not a template
Claude fills in freely: health first, then transitions, then action-required, then new
unarmed events, then the manual queue. --weekly adds the per-type performance review.

Kept to one phone screen by design: lists are capped, and reporting rules (§9) are
enforced in code, not left to whoever reads the output — net return only, never pooled
across paper/live or long/short, no verdict below 20 closed observations, and no
directional commentary on individual securities.

Usage:
    python src/render_summary.py            # daily review
    python src/render_summary.py --weekly    # daily + weekly performance review
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
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
LAST_REVIEW_PATH = DATA_ROOT / "last_review_at.txt"
OUT_PATH = REPO_ROOT / "out" / "daily_summary.md"

STALE_THRESHOLD_DAYS = 2  # §4.5
MIN_OBSERVATIONS_FOR_VERDICT = 20  # §9
MAX_LIST_ITEMS = 10  # keep it to one phone screen


def _health() -> dict:
    if not HEALTH_JSON_PATH.exists():
        return {}
    return json.loads(HEALTH_JSON_PATH.read_text())


def _days_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    then = datetime.fromisoformat(iso_ts)
    now = datetime.now(timezone.utc) if then.tzinfo else datetime.now()
    return (now - then).total_seconds() / 86400.0


def section_health() -> tuple[str, bool]:
    """Returns (markdown, any_stale) — §9 step 1, always first."""
    health = _health()
    if not health:
        return "**1. Health** — no health data yet (run `run_collect.py`).\n", True

    lines = ["**1. Health**", ""]
    any_stale = False
    for source in sorted(health.keys()):
        row = health[source]
        age = _days_since(row.get("last_success"))
        if row.get("last_success") is None:
            status, any_stale = "🔴 NEVER SUCCEEDED", True
        elif age is not None and age > STALE_THRESHOLD_DAYS:
            status, any_stale = f"🔴 STALE ({age:.1f}d)", True
        elif row.get("last_attempt") != row.get("last_success"):
            status = "🟡 last attempt failed, self-healing expected"
        else:
            status = "🟢 ok"
        lines.append(f"- `{source}`: {status} (last success: {row.get('last_success') or 'never'})")
    lines.append("")
    return "\n".join(lines), any_stale


def section_transitions(conn: sqlite3.Connection, since_iso: str | None) -> str:
    """§9 step 2: what armed, closed, or invalidated since the last review."""
    lines = ["**2. State transitions since last review**", ""]
    if since_iso is None:
        lines.append("_(first review — no prior baseline, showing nothing)_\n")
        return "\n".join(lines)

    rows = conn.execute(
        "SELECT symbol, signal_type, state, updated_at FROM signals WHERE updated_at > ? ORDER BY updated_at DESC LIMIT ?",
        (since_iso, MAX_LIST_ITEMS),
    ).fetchall()
    if not rows:
        lines.append("- none\n")
        return "\n".join(lines)
    for symbol, sig_type, state, updated_at in rows:
        lines.append(f"- `{symbol}` ({sig_type}) → **{state}**")
    lines.append("")
    return "\n".join(lines)


def section_action_required(conn: sqlite3.Connection) -> str:
    """§9 step 3: armed signals whose window starts today/tomorrow, open signals whose
    window ends within 2 trading days."""
    lines = ["**3. Action required**", ""]
    trading_days = [r[0] for r in conn.execute("SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT 3")]
    near_term = set(trading_days)  # today + up to 2 most recent sessions, as a proxy window
    rows = conn.execute(
        """
        SELECT symbol, signal_type, state, window_start, window_end FROM signals
        WHERE state IN ('armed', 'open', 'exit_due')
        ORDER BY (window_end IS NULL), window_end
        LIMIT ?
        """,
        (MAX_LIST_ITEMS,),
    ).fetchall()
    if not rows:
        lines.append("- nothing pending\n")
        return "\n".join(lines)
    for symbol, sig_type, state, window_start, window_end in rows:
        urgency = " ⚠️ window_end near" if window_end in near_term else ""
        lines.append(f"- `{symbol}` ({sig_type}): **{state}**, window {window_start} → {window_end or '?'}{urgency}")
    lines.append("")
    return "\n".join(lines)


def section_unarmed_events(conn: sqlite3.Connection) -> str:
    """§9 step 4: events ingested but with no corresponding signal yet, with why."""
    lines = ["**4. New events not yet armed**", ""]
    rows = conn.execute(
        """
        SELECT e.symbol, e.event_type, e.effective_date FROM events e
        LEFT JOIN signals s ON s.event_id = e.event_id
        WHERE s.signal_id IS NULL AND e.event_type = 'fo_ban_exit'
        ORDER BY e.effective_date DESC LIMIT ?
        """,
        (MAX_LIST_ITEMS,),
    ).fetchall()
    if not rows:
        lines.append("- none — every fo_ban_exit event has a signal (build_signals.py is caught up)\n")
        return "\n".join(lines)
    for symbol, event_type, eff_date in rows:
        lines.append(f"- `{symbol}` ({event_type}, {eff_date}): reason — build_signals.py hasn't run since this event was detected")
    lines.append("")
    return "\n".join(lines)


def section_manual_queue(conn: sqlite3.Connection | None) -> str:
    """§9 step 5: unstructured items requiring human entry. Since Phase 8 that is a real
    list — open research prompts (go read) and theses past their review date (record an
    outcome) are exactly the items only a human can clear."""
    lines = ["**5. Manual queue**", ""]
    if conn is None:
        lines.append("- no database yet\n")
        return "\n".join(lines)

    prompts = dispersion.open_prompts(conn, limit=MAX_LIST_ITEMS)
    theses = ledger_api.theses_past_review(conn)

    if not prompts and not theses:
        lines.append("- nothing pending — no open research prompts, no thesis past review\n")
        return "\n".join(lines)

    for p in prompts:
        lines.append(
            f"- 📖 **Read:** `{p['sector']}` — median constituent {p['spread']:+.1f}pp vs index "
            f"over {p['window_days']} trading days (detected {p['detected_date']}). "
            "Research prompt, not a signal."
        )
    for t in theses:
        lines.append(
            f"- ⚖️ **Review due {t['review_date']}:** `{t['subject']}` — falsifier as written: "
            f"\"{t['falsifier']}\""
        )
    lines.append("")
    return "\n".join(lines)


def regime_comparison(conn: sqlite3.Connection) -> str:
    """§9.8's required beta context, as a markdown line. Computation is shared with the
    dashboard via src/regime.py so the two surfaces never disagree."""
    result = regime.compute_regime(conn)
    if result is None:
        return "**8. Regime comparison** — no closed trades yet, nothing to compare against.\n"
    if result["nifty_pct"] is None:
        return (
            "**8. Regime comparison** — no NIFTY 50 data yet for "
            f"{result['start_date']} to {result['end_date']}. Run `python src/build_index.py --live`. "
            "Signal performance above should not be read as edge until this exists "
            "(§9: \"signal performance is never presented without its beta context\").\n"
        )
    return (
        f"**8. Regime comparison** — NIFTY 50 moved {result['nifty_pct']:+.2f}% over the same "
        f"window the closed sample spans ({result['start_date']} to {result['end_date']}). Compare this "
        "against the mean net figures above before reading any of them as edge — a "
        "positive mean net in a strongly rising regime is weaker evidence than the same "
        "figure in a flat or falling one.\n"
    )


def section_weekly_performance(conn: sqlite3.Connection) -> str:
    """§9 steps 6-8, weekly only. Net return only, never pooled across paper/live,
    long/short, or — since Phase 8 — across tracks. Reads the shared verdict module so
    this table and the dashboard's can never drift apart."""
    lines = ["**6. Track A per-cohort performance (mechanism, paper)**", ""]
    cohorts = verdict.cohort_verdicts(conn)
    if not cohorts:
        lines.append("- no closed mechanism trades yet\n")
    else:
        lines.append("| Type | Direction | n | Hit rate | Mean net | Median net | Worst | Mean hold | NIFTY (matched) | Spread | Verdict |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        kill_candidates = []
        for c in cohorts:
            if c["verdict"] == verdict.LABEL_NO_EDGE:
                kill_candidates.append(f"{c['signal_type']}/{c['direction']}/{c['mode']}")
            matched = f"{c['matched_nifty']:+.2f}%" if c["matched_nifty"] is not None else "—"
            spread = f"{c['spread']:+.2f}pp" if c["spread"] is not None else "—"
            hold = f"{c['mean_holding_days']:.1f}d" if c["mean_holding_days"] is not None else "—"
            lines.append(
                f"| {c['signal_type']} | {c['direction']} | {c['n']} | {c['hit_rate']:.0f}% | "
                f"{c['mean_net']:+.2f}% | {c['median_net']:+.2f}% | {c['worst']:+.2f}% | {hold} | "
                f"{matched} | {spread} | {c['verdict']} |"
            )
        lines.append("")
        lines.append(
            "_NIFTY column is the mean return over each trade's own entry→exit window, not a global "
            "span — a cohort of 5-day trades compared against a 12-month drift is not a comparison._\n"
        )
        if kill_candidates:
            lines.append(f"**Kill candidates (§9.8):** {', '.join(kill_candidates)}\n")

    lines.append("**7. Taken vs. skipped** — not implemented yet (no notion of \"taken\" vs \"skipped\" is tracked; every armed signal is currently logged automatically in paper mode, so there is no skip set to compare against yet).\n")
    lines.append(regime_comparison(conn))
    return "\n".join(lines)


def render(*, weekly: bool) -> str:
    conn = sqlite3.connect(DB_PATH) if DB_PATH.exists() else None
    since_iso = LAST_REVIEW_PATH.read_text().strip() if LAST_REVIEW_PATH.exists() else None
    now_iso = datetime.now(timezone.utc).isoformat()

    health_md, any_stale = section_health()
    parts = [
        f"# Daily Summary — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "⚠️ **One or more sources are stale. Every number below is provisional.**\n" if any_stale else "",
        health_md,
    ]
    if conn is not None:
        parts.append(section_transitions(conn, since_iso))
        parts.append(section_action_required(conn))
        parts.append(section_unarmed_events(conn))
    parts.append(section_manual_queue(conn))
    if weekly and conn is not None:
        parts.append(section_weekly_performance(conn))

    if conn is not None:
        conn.close()
    LAST_REVIEW_PATH.write_text(now_iso)
    return "\n".join(p for p in parts if p)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weekly", action="store_true")
    args = parser.parse_args()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(weekly=args.weekly), encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

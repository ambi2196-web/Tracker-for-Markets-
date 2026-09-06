#!/usr/bin/env python3
"""
Phase 8 work item 5 — push digest, not pull dashboard.

Written on the assumption that the operator does not open the dashboard on any schedule.
The digest is the thing that goes to them; the dashboard is what they open when the
digest tells them something happened.

Delivery channel is file-only by decision: §4.3 forbids API keys of any kind and §2
forbids hosted services, which rules out both a Telegram bot (needs a bot token) and any
real SMTP path (needs an app password). This writes out/digest.txt and stops there.
Wiring an actual push channel requires amending those constraints first — do not add one
here on your own initiative.

Cadence (the operator's, not enforced here): mechanism calendar weekly, dispersion and
verdicts monthly. Both sections render every run; --since controls the change diff.

Usage:
    python src/render_digest.py
    python src/render_digest.py --since 2026-08-01
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import dispersion  # noqa: E402
import ledger as ledger_api  # noqa: E402
import verdict  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DB_PATH = DATA_ROOT / "tracker.db"
OUT_PATH = REPO_ROOT / "out" / "digest.txt"
STATE_PATH = DATA_ROOT / "digest_state.json"
UPCOMING_DAYS = 30

_RULE = "=" * 72
_THIN = "-" * 72


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_state(verdicts: list[dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        f"{v['signal_type']}/{v['direction']}/{v['mode']}": {"n": v["n"], "verdict": v["verdict"]}
        for v in verdicts
    }
    STATE_PATH.write_text(json.dumps(
        {"written_at": datetime.now(timezone.utc).isoformat(), "cohorts": snapshot}, indent=2
    ))


def _section_verdicts(conn: sqlite3.Connection, previous: dict) -> str:
    lines = ["TRACK A — MECHANISM VERDICTS", _THIN]
    cohorts = verdict.cohort_verdicts(conn)
    if not cohorts:
        lines.append("  no closed mechanism trades yet")
        return "\n".join(lines) + "\n"

    prior = previous.get("cohorts", {})
    for c in cohorts:
        key = f"{c['signal_type']}/{c['direction']}/{c['mode']}"
        spread = f"{c['spread']:+.2f}pp" if c["spread"] is not None else "n/a"
        lines.append(
            f"  {key}\n"
            f"      n={c['n']}  hit={c['hit_rate']:.0f}%  mean_net={c['mean_net']:+.2f}%  "
            f"median={c['median_net']:+.2f}%\n"
            f"      NIFTY (matched windows)={c['matched_nifty']:+.2f}%  spread={spread}\n"
            f"      VERDICT: {c['verdict']}"
        )
        was = prior.get(key)
        if was is None:
            lines.append("      [new cohort since last digest]")
        else:
            if was["verdict"] != c["verdict"]:
                lines.append(f"      [CHANGED: was {was['verdict']}]")
            if was["n"] != c["n"]:
                lines.append(f"      [+{c['n'] - was['n']} closed trades since last digest]")
    return "\n".join(lines) + "\n"


def _section_upcoming(conn: sqlite3.Connection) -> str:
    lines = [f"UPCOMING MECHANISM EVENTS (next {UPCOMING_DAYS} days)", _THIN]
    try:
        rows = conn.execute(
            """
            SELECT window_end, symbol, signal_type, state FROM signals
            WHERE window_end IS NOT NULL AND state IN ('open', 'exit_due')
              AND julianday(window_end) - julianday((SELECT MAX(date) FROM prices)) BETWEEN 0 AND ?
            ORDER BY window_end
            """,
            (UPCOMING_DAYS,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        lines.append("  nothing due — no open or exit-due signal has a window ending soon")
    for window_end, symbol, signal_type, state in rows:
        lines.append(f"  {window_end}  {symbol:<14} {signal_type:<16} ({state})")
    return "\n".join(lines) + "\n"


def _section_prompts(conn: sqlite3.Connection) -> str:
    lines = ["TRACK B — OPEN RESEARCH PROMPTS (go read; not signals)", _THIN]
    prompts = dispersion.open_prompts(conn)
    if not prompts:
        lines.append("  nothing flagged — no sector far enough below the index to be worth reading about")
    for p in prompts:
        lines.append(
            f"  {p['sector']} (detected {p['detected_date']})\n"
            f"      median constituent {p['spread']:+.1f}pp vs index over {p['window_days']} trading days, "
            f"{p['n_constituents']} constituents, breadth {(p['breadth'] or 0) * 100:.0f}%\n"
            f"      NOT a position. No entry, no stop, excluded from every performance statistic."
        )
    return "\n".join(lines) + "\n"


def _section_theses(conn: sqlite3.Connection) -> str:
    lines = ["TRACK B — THESES PAST REVIEW DATE", _THIN]
    theses = ledger_api.theses_past_review(conn)
    if not theses:
        lines.append("  none past review")
    for t in theses:
        lines.append(
            f"  {t['subject']}  (review due {t['review_date']}, opened {t['created_date']})\n"
            f"      FALSIFIER AS ORIGINALLY WRITTEN:\n"
            f"        \"{t['falsifier']}\"\n"
            f"      Has it happened? Record an outcome — this is the question, not a prompt to re-argue the thesis."
        )
    return "\n".join(lines) + "\n"


def render(conn: sqlite3.Connection) -> tuple[str, list[dict]]:
    previous = _load_state()
    cohorts = verdict.cohort_verdicts(conn)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    since = previous.get("written_at", "never")

    body = "\n".join([
        _RULE,
        f"SIGNAL TRACKER DIGEST — {generated}",
        f"last digest: {since}",
        _RULE,
        "",
        _section_verdicts(conn, previous),
        "",
        _section_upcoming(conn),
        "",
        _section_prompts(conn),
        "",
        _section_theses(conn),
        "",
        _RULE,
        "Track A is measured. Track B is judged. Nothing here is a recommendation;",
        "the strongest verdict this system can emit is PROMISING — NOT PROVEN.",
        _RULE,
    ])
    return body, cohorts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the digest, write nothing")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        body, cohorts = render(conn)
    finally:
        conn.close()

    if args.dry_run:
        print(body)
        print("\n[dry-run] nothing written")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(body, encoding="utf-8")
    _save_state(cohorts)
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

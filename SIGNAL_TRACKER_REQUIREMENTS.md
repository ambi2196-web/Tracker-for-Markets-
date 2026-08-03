# Event Signal Tracker — Requirements Specification

**Version:** 1.0
**Owner:** Ashu
**Market:** Indian equities (NSE primary, BSE secondary)
**Status:** Pre-build

---

## 1. Purpose

Track deterministic, mechanism-driven trading opportunities in Indian equities — events where a
counterparty is *forced* to transact at a known time for reasons unrelated to price — and log every
signal with its outcome so that edge can be measured rather than assumed.

The system is a **research and logging instrument first**. Capital deployment is gated on measured
performance, not on the system existing.

---

## 2. Design constraints (non-negotiable)

| Constraint | Implication |
|---|---|
| No LLM API calls in the pipeline | All parsing is deterministic. Unstructured PDFs go to a manual queue. |
| No cloud compute, no hosted services | Everything runs on the local machine. No GitHub Actions, no Groq, no server. |
| Data lives on local disk | Raw files archived permanently; derived tables rebuildable from raw. |
| Analysis performed by Claude reading local files | Claude opens the data directory, runs the fixed protocol in §9, returns a summary. |
| Operator is not awake during market hours | All signals must have multi-day action windows. Nothing intraday. |
| Occasional trading only | System optimises for *not missing* rare setups, not for generating volume. |

---

## 3. Non-goals

- Order placement or broker integration. **Explicitly out of scope.** The system produces a
  watchlist; the human places every order manually.
- Intraday data, tick data, or real-time quotes.
- Pattern-only signals (RSI, Bollinger, moving-average crossovers) as *triggers*. These may act
  only as timing filters inside an already-armed mechanism window (§7.4).
- Portfolio accounting, tax computation, or P&L reconciliation against broker statements.
- Options or futures positions in v1. Cash equity only.

---

## 4. Prerequisites

### 4.1 Hardware and OS
- Any machine that can stay powered on, or be reliably woken, once daily after 18:30 IST.
- ~5 GB free disk for the first three years of raw archives (bhavcopy is the bulk).
- Stable internet at collection time. Retries handle transient failures; a machine that is off for
  a week creates permanent gaps, because NSE does not serve deep historical bhavcopy reliably.

### 4.2 Software
- Python 3.11 or later.
- Packages: `pandas`, `requests`, `lxml`, `beautifulsoup4`, `python-dateutil`, `yfinance`,
  and one NSE session-handling library (`jugaad-data` or `nsepython` — evaluate both, pick one,
  see §12 open decisions).
- SQLite (bundled with Python, no separate install).
- A task scheduler: `cron` on Linux/macOS, Task Scheduler on Windows.
- Claude Desktop with Cowork, granted read access to the data directory.
- Git, optional but recommended, for versioning the code and the ledger (not the raw archives).

### 4.3 Accounts and access
- No paid data subscription required for v1.
- No broker API required. A demat account is needed only when moving from paper to live.
- No API keys of any kind. If any component needs a key, it is out of scope by §2.

### 4.4 Manual seed data (must exist before first run)
| File | Contents | Source | Refresh |
|---|---|---|---|
| `seed/ipo_listings.csv` | symbol, listing_date, anchor_lockin_30d, anchor_lockin_90d, preipo_lockin_180d | Manually entered from NSE listing circulars | Per IPO |
| `seed/index_events.csv` | index_name, symbol, action (add/drop), announce_date, effective_date | Manually entered from niftyindices.com press releases | Semi-annual + quarterly |
| `seed/universe.csv` | Symbols to track for price history | NSE equity list | Quarterly |

These are manual because the sources are PDFs and press releases. Entering them takes minutes per
event and forces you to actually read the announcement, which is a feature.

### 4.5 Operational discipline
- **Point-in-time integrity.** Raw files are written once and never modified. Any backtest must
  read the archive as of the date being simulated, never today's re-downloaded version. Violating
  this silently introduces survivorship and adjustment bias and will make every signal look better
  than it is.
- **Health monitoring.** Each source records `last_successful_fetch`. Any source stale by more
  than two trading days is surfaced at the top of the daily summary. Silent failure is the most
  likely way this system dies.
- **Terms of use.** Exchange data is for personal, non-commercial use. Rate-limit requests
  (minimum 2s between calls), set a real User-Agent, do not redistribute raw exchange files.

---

## 5. Directory structure

```
signal-tracker/
├── data/
│   ├── raw/                      # immutable, append-only
│   │   ├── bhavcopy/YYYY/MM/cm_DDMMYYYY_bhav.csv
│   │   ├── fo_ban/YYYY/MM/fo_ban_YYYYMMDD.csv
│   │   ├── asm/YYYY/MM/asm_YYYYMMDD.csv
│   │   └── bulk_block/YYYY/MM/deals_YYYYMMDD.csv
│   ├── seed/                     # manually maintained, version controlled
│   │   ├── ipo_listings.csv
│   │   ├── index_events.csv
│   │   └── universe.csv
│   └── tracker.db                # SQLite, rebuildable from raw + seed
├── src/
│   ├── collect/                  # one module per source, each idempotent
│   ├── signals/                  # one module per signal type
│   ├── ledger.py
│   ├── build_db.py               # full rebuild from raw
│   └── render_dashboard.py
├── out/
│   ├── dashboard.html            # opened directly from disk
│   └── daily_summary.md          # what Claude reads first
├── logs/
│   └── collect_YYYYMMDD.log
└── SIGNAL_TRACKER_REQUIREMENTS.md
```

---

## 6. Data sources

| Source | Format | Published | Used for |
|---|---|---|---|
| NSE daily bhavcopy | CSV | ~18:00 IST | Close, volume, delivery %, universe |
| NSE F&O ban list | CSV | ~19:00 IST | Ban entry/exit signal |
| NSE ASM / GSM lists | CSV | EOD | Surveillance stage changes |
| NSE bulk & block deals | CSV | EOD | Supply overhang context |
| yfinance | API (read-only, no key) | Anytime | Backfill and gap repair only |
| Manual seed files | CSV | As events occur | IPO lock-ins, index rebalances |

**Collection window:** 19:00–20:00 IST on every NSE trading day. Weekend and holiday runs are
no-ops that still touch the health timestamp.

---

## 7. Signal specifications

Every signal must define, before any capital is involved: trigger, action window, entry rule, exit
rule, invalidation rule, and maximum holding period. A signal without an exit rule does not ship.

### 7.1 F&O ban exit (v1, ship first)

- **Mechanism:** A stock enters ban when open interest exceeds 95% of market-wide position limit.
  During ban, only position reduction is permitted, which suppresses fresh positioning. Exit from
  ban removes that constraint.
- **Trigger:** Symbol present in ban list on day T−1, absent on day T.
- **Action window:** T to T+5 trading days.
- **Entry rule:** Log at T+1 open. Requires ADV over the prior 20 sessions above a liquidity floor
  (set in §12).
- **Exit rule:** T+5 close, or predefined stop, whichever comes first.
- **Invalidation:** Symbol re-enters ban list; corporate action announced in window.
- **Direction:** To be determined empirically. Do not assume. Log both long and short outcomes on
  paper for the first 20 observations.

### 7.2 IPO lock-in expiry (v1, ship second)

- **Mechanism:** Anchor investors unlock 50% at 30 days from allotment and 50% at 90 days.
  Pre-IPO shareholders unlock at 180 days. Known supply arrives on a known date.
- **Trigger:** Days-to-unlock reaches 10, 5, 2, 0 for any symbol in `ipo_listings.csv`.
- **Action window:** Unlock date −5 to +10 trading days.
- **Entry rule:** No entry before the unlock date. The trade is the *post-supply* reaction, not
  anticipation of it.
- **Exit rule:** +10 trading days from unlock, or stop.
- **Invalidation:** Lock-in extended or waived; block deal clears the overhang before the date.
- **Watch metric:** Volume on unlock day versus 20-day ADV. A muted response means the supply was
  pre-placed and the setup is dead.

### 7.3 Index rebalance deletion (v2)

- **Mechanism:** Passive funds must sell the deleted constituent by the effective close,
  price-insensitively.
- **Trigger:** Symbol appears with action=drop in `index_events.csv`.
- **Action window:** Effective date +1 to +15 trading days.
- **Entry rule:** No entry before effective close. Forced selling is not finished until it is.
- **Exit rule:** +15 trading days, or stop.
- **Note:** Additions are excluded from v1 and v2. They are the crowded side of this trade.

### 7.4 Timing filters (not triggers)

Once a symbol is `armed` by a mechanism trigger, these may adjust entry timing only. They may never
arm a symbol on their own.

- 14-day RSI below 30 or above 70
- Close outside 2-sigma Bollinger band
- Delivery percentage versus its own 20-day mean

---

## 8. Database schema

```sql
CREATE TABLE prices (
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, delivery_pct REAL, source TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,        -- fo_ban_exit | ipo_lockin | index_drop | asm_change
    announce_date TEXT,
    effective_date TEXT NOT NULL,
    detail TEXT,                     -- JSON blob, source-specific
    source_file TEXT NOT NULL,       -- provenance: which raw file produced this
    ingested_at TEXT NOT NULL
);

CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    state TEXT NOT NULL,             -- watching | armed | open | exit_due | closed | invalidated
    armed_date TEXT,
    window_start TEXT,
    window_end TEXT,
    entry_ref_price REAL,            -- price at arming, for paper measurement
    stop_price REAL,
    filters_passed TEXT,             -- JSON: which §7.4 filters were true at arming
    updated_at TEXT NOT NULL
);

CREATE TABLE ledger (
    ledger_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    mode TEXT NOT NULL,              -- paper | live
    direction TEXT NOT NULL,         -- long | short
    entry_date TEXT, entry_price REAL,
    exit_date TEXT, exit_price REAL,
    exit_reason TEXT,                -- window_end | stop | invalidated | manual
    gross_return_pct REAL,
    costs_pct REAL,                  -- brokerage, STT, stamp, exchange, GST, slippage assumption
    net_return_pct REAL,
    holding_days INTEGER,
    notes TEXT
);

CREATE TABLE health (
    source TEXT PRIMARY KEY,
    last_attempt TEXT,
    last_success TEXT,
    consecutive_failures INTEGER,
    last_error TEXT
);
```

**Ledger rule:** every signal that reaches `armed` gets a ledger row in `paper` mode automatically,
whether or not it is traded. Signals you skip are the control group. Without them there is no way
to distinguish signal quality from your own selection.

**Cost model:** net return must always be computed. Assume worst-case retail costs plus a slippage
allowance appropriate to the symbol's liquidity. A signal that is profitable gross and negative net
is not a signal.

---

## 9. Analysis protocol (the "no temperature" substitute)

There is no model in the pipeline, so reproducibility comes from a fixed protocol rather than a
sampling parameter. When Claude is asked for the daily or weekly review, it runs exactly these
steps in this order and reports only what the data supports.

**Daily review**
1. Health check. Any source with `last_success` older than two trading days is reported first.
   If any source is stale, every downstream number is flagged as provisional.
2. State transitions since last review: what armed, what closed, what invalidated.
3. Action-required list: signals with `state = armed` whose `window_start` is today or tomorrow,
   and signals with `state = open` whose `window_end` is within two trading days.
4. New events ingested but not yet armed, with the reason they are not armed.
5. Manual queue: unstructured items requiring human entry.

**Weekly review (adds)**
6. Per signal type: count, hit rate, mean net return, median net return, worst single outcome,
   mean holding days. Paper and live reported separately, never pooled.
7. Comparison of taken versus skipped signals within each type.
8. Any signal type with 20+ closed observations and a negative mean net return is named as a
   kill candidate.

**Reporting rules**
- No signal type with fewer than 20 closed observations gets a performance verdict. Report the
  count and say the sample is insufficient.
- Report net return only. Gross return is not decision-relevant.
- State the regime: index return over the same period, so that signal performance is never
  presented without its beta context.
- Do not offer directional views on individual securities. The protocol summarises the ledger;
  it does not forecast.

---

## 10. Dashboard

Single static HTML file at `out/dashboard.html`, regenerated by the collector, opened directly
from disk. No server, no build step, no external requests.

Sections, in order:
1. Health strip — one row per source, green/amber/red, last success timestamp.
2. Action required — armed and exit-due signals, sorted by urgency.
3. Calendar — next 30 days of known effective dates.
4. Ledger summary — per signal type, counts and net performance, paper and live separate.
5. Recent state transitions.

Self-contained: inline CSS, no CDN, no JavaScript frameworks. It must render correctly with the
network disconnected.

---

## 11. Capital gates

| Gate | Requirement |
|---|---|
| Paper only | Always, until a signal type has 20+ closed paper observations |
| First live capital | Signal type shows positive mean *net* return over 20+ paper observations |
| Position size, first 6 months live | Fixed rupee amount per trade, no scaling on conviction |
| Hard stop | Defined at arming, in the signal row, before entry. Never widened. |
| Book separation | This book does not share capital with long-term equity holdings |
| Skew acknowledgement | This is a short-volatility payoff: frequent small wins, occasional large loss. Size for the loss, not the wins. |

---

## 12. Open decisions

1. `jugaad-data` versus `nsepython` — evaluate both against current NSE endpoints; they break
   independently and one may already be stale.
2. Liquidity floor for the tradeable universe (ADV threshold, in rupees).
3. Stop-loss basis: fixed percentage, or a multiple of 20-day ATR.
4. Whether the F&O ban exit trade is long or short — resolve empirically, do not assume.
5. Whether to backfill history for retrospective testing, accepting that reconstructed history is
   not point-in-time and its results should be treated as indicative only.

---

## 13. Build sequence

| Milestone | Deliverable | Exit criterion |
|---|---|---|
| M1 | Bhavcopy collector, raw archive, `prices` table | 10 consecutive successful daily runs |
| M2 | Ban list collector, `events` populated | Ban entry/exit correctly detected across a full week |
| M3 | Signal state machine, ledger auto-population | Paper rows created and closed without manual touch |
| M4 | Dashboard and daily summary | Readable end to end, offline |
| M5 | IPO lock-in signal from seed file | Second signal type running in parallel |
| M6 | Weekly review protocol run by Claude | First per-type performance table produced |

Do not start M5 before M4 is stable. A second signal on an unreliable pipeline produces two streams
of unreliable data.

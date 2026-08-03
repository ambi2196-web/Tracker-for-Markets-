# Event Signal Tracker — Phased Implementation Plan

**Companion to:** SIGNAL_TRACKER_REQUIREMENTS.md
**Version:** 1.0

---

## Governing principles

1. **Every phase ends in a working system.** No phase depends on a later phase to be useful. If
   you stop after Phase 2, you still own a clean daily price archive — a real asset.
2. **Every phase has a written acceptance test.** "It seems to work" is not an exit criterion.
   The test is defined before the code is written.
3. **Every phase has a rollback.** Raw data is never destroyed, so rollback always means
   "delete the derived layer and rebuild," never "recover lost data."
4. **One phase in flight at a time.** The exception is Phase 1, which runs continuously in the
   background from the moment it ships.
5. **Traceability is built in from Phase 2**, not retrofitted. Every derived row records the raw
   file and the code version that produced it.

---

## Cross-cutting requirements (apply from Phase 2 onward)

**Provenance columns.** Every derived table carries `source_file`, `ingested_at`, and
`pipeline_version` (a semantic version bumped whenever detection logic changes). When a signal from
February behaves differently from one in June, this is how you find out whether the market changed
or your code did.

**Replay mode.** Every module must run in two modes:
- `--live` — fetch from network, write to raw archive, then process.
- `--replay FROM TO` — read only from the raw archive, process, write to a scratch database.

Replay is the primary debugging tool for the entire project. A bug found in Phase 5 gets diagnosed
by replaying Phase 3 logic over archived data with no network involved. Build it in Phase 2 when
it costs an hour; retrofitting it in Phase 5 costs a weekend.

**Dry run.** Every write path supports `--dry-run`, printing what would be written.

**Idempotency.** Running any collector twice for the same date produces the same result and no
duplicate rows. Enforced by primary keys, not by hope.

**Logging.** One log file per run. Every fetch attempt logged with URL, status, byte count, and
elapsed time. Silent success is as suspicious as loud failure.

---

## Phase 0 — Source viability spike

**Duration:** Half a day. **Code produced: throwaway.**

**Goal:** Establish that today's actual endpoints return today's actual data, before any
architecture is committed.

**Tasks**
- Manually download, in a browser, one recent NSE bhavcopy, one F&O ban list, one ASM list.
  Record the exact URL, file format, column names, and publication time for each.
- Attempt the same programmatically with `requests`, noting what headers and cookies are required.
- Install `jugaad-data` and `nsepython`. Attempt one fetch of each source with each. Record which
  works, which errors, and how recently each was maintained.
- Confirm whether the bhavcopy format currently in use is the legacy layout or the newer UDiFF
  layout, and capture the column mapping for whichever it is.

**Acceptance test**
A single text file recording, for each of the three sources: working URL, required headers,
file format, column names, publication time, and which library (if any) handles it.

**Do not proceed if:** any source cannot be fetched programmatically. Solve it here, or redesign
the signal set around sources that can be. Do not proceed with a plan to "figure it out later."

**Rollback:** Delete the folder. Nothing depends on this.

---

## Phase 1 — Raw collector

**Duration:** 1–2 days to build. **Runs continuously thereafter.**

**In scope:** Fetch the three daily files, write them unmodified into `data/raw/`, log the attempt,
update the health record. Scheduled task configured and confirmed firing.

**Out of scope:** Parsing. Database. Signals. Anything that interprets the data.

**Deliverable:** `src/collect/` with one module per source, a scheduler entry, and a populated
`data/raw/` tree.

**Acceptance test**
- Ten consecutive scheduled runs complete without manual intervention.
- Files land in the correct dated paths with plausible sizes.
- A deliberately induced failure (disconnect the network for one run) is logged, retried, and
  recorded in the health file — and the following day's run recovers without a gap.
- Running the collector twice for the same date produces no duplicate or corrupted files.

**Failure modes to expect**
- Scheduler fires while the machine is asleep. Configure wake-on-schedule, or accept and detect
  the gap explicitly.
- NSE serves an HTML error page with a 200 status. Validate content type and file size, not just
  the status code.
- Holiday and weekend runs return nothing. This is correct behaviour and must not increment the
  failure counter.

**Do not proceed until:** ten consecutive clean days. This phase is where reliability is won or
lost, and no amount of downstream sophistication compensates for a gappy archive.

**Rollback:** None needed; nothing depends on it yet.

---

## Phase 2 — Parse layer and database

**Duration:** 2–3 days. **Runs in parallel with Phase 1 continuing.**

**In scope:** Parse raw files into the `prices` and `health` tables. Implement `build_db.py` for a
full rebuild from raw. Implement replay mode and dry run.

**Out of scope:** Events, signals, ledger, dashboard.

**Acceptance test**
- Full rebuild from raw produces a database byte-identical in content to the incrementally built
  one. This is the single most important test in the project — it proves the raw archive is the
  real source of truth and the database is disposable.
- Row counts per date match the raw file row counts.
- Spot-check five symbols on three random dates against the NSE website.
- Replay mode over a two-week window produces the same output as the live runs did.

**Failure modes to expect**
- Column names or ordering change mid-archive when NSE alters the format. The parser must be
  version-aware, keyed off the file's own structure rather than an assumed schema.
- Symbol renames and corporate actions break the `(symbol, date)` primary key assumption across
  time. Record the raw symbol as delivered; do not normalise silently.

**Do not proceed until:** the rebuild test passes cleanly twice.

**Rollback:** Delete `tracker.db`, rebuild from raw.

---

## Phase 3 — Event detection, one source only

**Duration:** 2 days.

**In scope:** F&O ban list only. Detect entry and exit transitions, populate the `events` table.
Read-only with respect to trading — this phase produces no signals and no positions.

**Out of scope:** Every other signal type. State machine. Ledger.

**Acceptance test**
- Replay across at least one month of archived ban lists.
- Manually verify five detected entries and five detected exits against the raw files by hand.
- Confirm that a symbol banned across a market holiday is not reported as an exit and re-entry.
- Confirm every event row carries `source_file` and `pipeline_version`.

**Failure modes to expect**
- Trading holidays create phantom transitions. Ban list absence on a non-trading day must be
  distinguished from removal.
- The ban list occasionally publishes late or in a different layout on expiry days.
- Symbol format differences between the ban list and bhavcopy prevent joins.

**Do not proceed until:** ten hand-verified events with zero discrepancies.

**Rollback:** Truncate `events`, replay.

---

## Phase 4 — Signal state machine and paper ledger

**Duration:** 3–4 days. **The highest-risk phase.**

**In scope:** Transitions between `watching → armed → open → exit_due → closed | invalidated`.
Automatic paper ledger row creation on arming, automatic closing at window end, cost model applied,
net return computed.

**Out of scope:** Live capital. Second signal type. Dashboard.

**Acceptance test**
- Replay a three-month window. Every armed signal reaches a terminal state; none is orphaned.
- Ten signals traced end to end by hand, with entry reference price, stop, exit, and net return
  independently recomputed and matched.
- Every armed signal has a ledger row, including ones you would not have taken. Verify the count
  matches the armed count exactly.
- Deliberately kill the process mid-run and restart. State must be consistent; no signal in a
  half-transitioned condition.
- Net return correctly reflects the full cost model, not gross.

**Failure modes to expect**
- **Lookahead bias.** The most dangerous failure and the hardest to see. Any signal armed using
  data published after the arming timestamp invalidates the entire ledger. Assert explicitly that
  no row consulted during arming has an `ingested_at` later than the arming date. Write this test
  first, before the state machine.
- Window end falling on a holiday, leaving signals stuck in `exit_due` indefinitely.
- Trading halts and circuit limits producing no exit price at window end.
- Crashes mid-transition leaving inconsistent state. Wrap transitions in transactions.

**Do not proceed until:** the lookahead assertion passes over a full replay, and ten signals are
hand-verified.

**Rollback:** Truncate `signals` and `ledger`, replay. Both are fully derived — this is why the
ledger lives in the database and not in a spreadsheet you have been editing.

---

## Phase 5 — Summary and dashboard

**Duration:** 2 days.

**In scope:** `out/daily_summary.md` following the §9 protocol, and `out/dashboard.html` as a
self-contained offline file. First real Cowork sessions reading the output.

**Acceptance test**
- Dashboard renders correctly with the network disconnected.
- Health strip correctly shows red when a source is deliberately made stale.
- Ask Claude to run the daily protocol on the summary and confirm the output follows §9 order,
  flags stale sources first, and refuses to give a performance verdict on any type with fewer than
  twenty closed observations.
- Summary is legible on a phone.

**Failure modes to expect**
- Dashboard silently rendering yesterday's data after a failed run. The health strip must be
  impossible to miss and must sit above everything else.
- Summary growing long enough that you stop reading it. If it exceeds one screen, cut content.

**Rollback:** Regenerate. The output directory is fully derived.

---

## Phase 6 — Second signal type

**Duration:** 2 days.

**In scope:** IPO lock-in expiry from `seed/ipo_listings.csv`. This phase tests whether the
architecture actually generalises, which is its real purpose.

**Acceptance test**
- New signal type added without modifying the state machine. If the state machine needs changes,
  stop and fix the abstraction before adding a third type.
- Both signal types run concurrently without interference.
- Ledger correctly segregates performance by type.

**Do not proceed to a third signal type until:** the second one required zero state machine changes.

---

## Phase 7 — First performance review

**Duration:** Ongoing. Gate is calendar time, not effort.

**In scope:** Accumulate twenty closed paper observations per signal type. Run the weekly protocol.
Produce the first per-type performance table with the index return over the same window alongside it.

**Acceptance test**
- Twenty or more closed observations for at least one type.
- Per-type net return computed, with taken and skipped signals compared.
- The regime comparison is present — signal performance is never reported without its beta context.

**This phase cannot be accelerated.** At current F&O ban frequency, twenty observations is likely
two to four months. That wait is the point.

---

## Phase 8 — Live capital gate

**Entry conditions, all required**
- One signal type with 20+ closed paper observations and positive mean *net* return.
- Performance not explained by the index return over the same period.
- Pipeline has run without unresolved failure for thirty consecutive days.
- Fixed rupee position size decided in writing, in advance.
- Hard stop rule defined, and the ledger proves it was respected on every paper trade.

**If a condition fails, remain on paper.** The system's value is the measurement, not the trading.
A tracker that runs for a year and concludes the signals have no edge has done its job and saved
you real money.

---

## Timeline

| Phase | Effort | Earliest completion | Gated by |
|---|---|---|---|
| 0 | 0.5 day | Day 1 | — |
| 1 | 1–2 days build | Day 12 | 10 clean days |
| 2 | 2–3 days | Day 15 | Rebuild test |
| 3 | 2 days | Day 17 | Hand verification |
| 4 | 3–4 days | Day 21 | Lookahead assertion |
| 5 | 2 days | Day 23 | Offline render |
| 6 | 2 days | Day 25 | No state machine changes |
| 7 | — | Month 3–5 | Observation count |
| 8 | — | Month 4–6 | Measured edge |

Roughly three weeks of building, then months of waiting. If that ratio feels wrong, it is the
correct ratio and the instinct is the thing to distrust.

---

## Phase log

Maintain `PHASE_LOG.md` in the repo. One entry per phase: date started, date accepted, acceptance
test result, defects found, decisions made and why. When something breaks in month four, this is
how you reconstruct what changed.

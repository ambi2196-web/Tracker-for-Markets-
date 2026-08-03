# Phase 0 — Source Viability Spike: Findings

**Date run:** 2026-08-04
**Spike code:** `phase0_spike/spike_test.py` (throwaway, per PHASED_IMPLEMENTATION_PLAN.md — delete this
folder once these findings are copied into the real build).
**Environment:** Python 3.12.10, `requests` 2.x, `jugaad-data` 0.35.1, `nsepython` 2.97 (both freshly
installed today; "how recently each was maintained" below is by import/behavior, not a PyPI release-date
lookup — that's worth a 2-minute manual check on pypi.org before committing).

---

## 1. NSE Bhavcopy

There are **two live, working formats**. This matters because the requirements doc's `prices` table
needs `delivery_pct`, and only one format carries it natively.

### 1a. "Full" bhavcopy (legacy-style columns, delivery % included) — recommended primary

- **Working URL:** `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`
  (e.g. `sec_bhavdata_full_03082026.csv`)
- **Required headers:** Plain `requests.get()` with no headers **times out** (connection hangs, does
  not even 403). A full browser-like header set is required:
  `User-Agent` (real browser string), `Accept`, `Accept-Language`, `Referer: https://www.nseindia.com/`.
  A prior warm-up `GET https://www.nseindia.com` on the same `requests.Session()` was used in testing;
  did not isolate whether the warm-up hit is strictly necessary or the headers alone suffice — treat
  the warm-up as required until proven otherwise, it's cheap.
- **File format:** Plain CSV, uncompressed, comma-space separated (`SYMBOL, SERIES, DATE1, ...` — note
  the leading space after commas in the header row, parser must `.strip()` column names).
- **Column names (confirmed from live fetch):** `SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE,
  HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,
  NO_OF_TRADES, DELIV_QTY, DELIV_PER`
- **Publication time:** Not empirically timestamped this run (fetched historical dates, not today's
  live drop). Requirements doc states ~18:00 IST — unverified this spike, verify on first live
  collection run.
- **Library support:** Neither `jugaad-data` nor `nsepython` uses this endpoint. Plan to hit it
  directly with `requests` + the header set above.
- **Today's file (2026-08-04) returned 404** at time of testing — correctly interpreted as "not yet
  published," confirmed by fetching the prior trading day (2026-08-03) successfully. **Do not treat a
  404 as proof the URL pattern is wrong** — check against a known-past date first.

### 1b. UDiFF bhavcopy (NSE's newer official format, zipped, no delivery %)

- **Working URL:** `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`
- **Required headers:** Same as 1a.
- **File format:** ZIP containing one CSV. Must unzip in memory/disk before parsing.
- **Column names (confirmed from live fetch):** `TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId,
  ISIN, TckrSymb, SctySrs, XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, FinInstrmNm, OpnPric,
  HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric, SttlmPric, OpnIntrst,
  ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd, SsnId, NewBrdLotQty, Rmks, Rsvd1, Rsvd2,
  Rsvd3, Rsvd4`. **No delivery quantity/percentage column.** Also mixes instrument types (equity,
  SGBs, etc. all under `Sgmt=CM`) — needs a `SctySrs`/`FinInstrmTp` filter to isolate plain equity.
- **Library support:** `jugaad_data.nse.bhavcopy_save(date, dest)` uses **this** endpoint under the
  hood (confirmed: file it saved had the `TradDt,BizDt,...` header). It successfully fetched
  2026-08-03 data with the correct content.
- **Critical defect found:** when pointed at 2026-08-04 (not yet published), `bhavcopy_save`
  **returned with no exception and silently wrote NSE's 404 HTML error page as a `.csv` file.** This
  is precisely the failure mode Phase 1 of the plan warns about ("NSE serves an HTML error page ...
  validate content type and file size, not just the status code") — except here it's worse, because
  jugaad-data doesn't even surface the 404 status, it just writes the page. **Any collector built on
  jugaad-data's save functions must add its own content-type/row-count validation after the call
  returns; do not trust a lack of exception.**

**Recommendation:** Use 1a (`sec_bhavdata_full`) as the primary source via direct `requests`, since it
carries `delivery_pct` natively and needs no unzip step. Do not rely on jugaad-data's bhavcopy fetcher
without wrapping it in a post-fetch validation check.

---

## 2. NSE F&O Ban List

- **Working URLs (two, both confirmed live):**
  - Current day only: `https://nsearchives.nseindia.com/content/fo/fo_secban.csv`
  - Historical/dated archive: `https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_DDMMYYYY.csv`
    (note: different path prefix than the bhavcopy archive, and different from the naive
    `content/fo/fo_secban_DDMMYYYY.csv` guess, which 404s — the dated file lives under `/archives/`,
    not `/content/`)
- **Required headers:** Same browser-like header set as bhavcopy; same host (`nsearchives.nseindia.com`).
- **File format:** Not a data table on days with no bans — returns a single human-readable line:
  `Securities in Ban For Trade Date DD-MON-YYYY: NIL`. On days with bans, expect a comma-separated
  symbol list (not verified this run — no banned symbols on the two dates tested, 2026-08-03/04).
  **Parser must special-case the "NIL" sentinel** rather than assume a header row + data rows.
- **Column names:** Not applicable on a NIL day. Needs a live test on a day with an actual ban list to
  confirm the delimited format (comma-separated symbols vs one-per-line vs CSV columns).
- **Publication time:** Not empirically timestamped. Requirements doc states ~19:00 IST — unverified.
- **Library support:** Neither `jugaad-data` nor `nsepython` exposes a direct ban-list fetch function.
  `nsepython.nse_fno(symbol)` is a per-symbol derivative quote via NSE's internal JSON API, not the
  bulk ban list — wrong tool for this. Plan to hit the archive URL directly with `requests`.

---

## 3. NSE ASM / GSM List — RESOLVED (second pass)

Guessing filenames directly (see the failed attempts below) does not work — NSE does not expose
ASM/GSM as a standalone dated file at any guessable path. It found it a different way: NSE's own
website is backed by an internal JSON catalog API that lists the exact file for every daily report,
by date, for the current session. That API is documented in §3b and is worth using generally (§6).

- **Working URL (discovered via the catalog API, not guessed):**
  `https://nsearchives.nseindia.com/content/cm/REG_IND{DDMMYY}.csv`
  (2-digit year, e.g. `REG_IND030826.csv` for 03-Aug-2026). A second, wider variant also exists:
  `REG1_IND{DDMMYY}.csv` ("Surveillance Indicator New") — same row set, more indicator columns.
- **Required headers:** Same browser-like header set + warm-up hit as the other two sources.
- **File format:** Plain CSV, ~2,970 rows — **one row per listed symbol, every trading day**, not a
  short list of only-flagged symbols. This is a different shape than the requirements doc's mental
  model of "a list of symbols currently in ASM/GSM."
- **Column names (confirmed from live fetch):** `ScripCode, Symbol, Nse Exclusive, Status, Series,
  GSM, Long_Term_Additional_Surveillance_Measure (Long Term ASM), Unsolicited_SMS,
  Insolvency_Resolution_Process(IRP), Short_Term_Additional_Surveillance_Measure (Short Term ASM),
  Default, ICA, Filler4, Filler5, Pledge, Add-on_PB, Total Pledge, Social Media Platforms, ESM, Loss
  making, ..., Under BZ/SZ Series, ...` plus several `FillerN` (reserved/unused) columns. The
  `REG1_IND` variant adds ~25 more indicator columns (PE ratio flags, price-movement-percentage
  flags, unique-PAN-count flag, etc.).
- **Value semantics (inferred, not yet confirmed against an NSE spec doc):** most cells read `100`
  or blank for a non-flagged symbol; a small integer (`1`, `0`) appears to denote an active stage —
  e.g. `21STCENMGM` showed `Long_Term_ASM = 1` where every other sampled row showed `100`. **This
  needs confirmation against NSE's REG_IND file-structure spec before building the parser** — do not
  assume `100` universally means "clear" without checking the doc below.
- **Reference doc:** NSE publishes a file-structure PDF for this exact file:
  `https://nsearchives.nseindia.com/web/sites/default/files/inline-files/REG_INDDDMMYY_File%20Structure%201.pdf`
  (fetch attempt from this environment timed out — fetch it directly before writing the parser).
- **Library support:** Neither `jugaad-data` nor `nsepython` exposes this file.
- **Failed guesses along the way** (kept for the record — all 404'd, none of them is the real path):
  `content/equities/asmStage1List.csv`, `asmStage2List.csv`, `gsm1.csv`,
  `archives/equities/asm/asmStage1_28072025.csv`, `/api/reportTypes`, `/api/surveillance/asm`,
  `/api/ASMShortlisted`, and scraping `nseindia.com/reports/surveillance-actions` for embedded links
  (that page is client-rendered; static HTML fetch returns nothing useful).

**Phase 0 gate: now PASSES for all three sources**, with one open item carried into Phase 2: confirm
the REG_IND value semantics against the file-structure PDF before the parser ships.

### 3b. How it was actually found: NSE's `merged-daily-reports` catalog API

The NSE website itself calls `https://www.nseindia.com/api/merged-daily-reports?key=CM` to populate
its "All Reports" page. It returns JSON, one entry per report, each with the **exact `filePath` and
`fileActlName` for that day** — no filename-format guessing required. This is what surfaced
`REG_IND030826.csv` immediately after direct guesses failed. The `key` query param selects a
category — `CM` returned 36 reports (bhavcopy in both formats, surveillance indicator in both
variants, bulk/block deals, VaR files, PE ratio, 52-week high/low, etc.), `FO` returned 31 including
`FO-SEC-BAN` confirming the exact same F&O ban path already found by direct guessing
(`archives/fo/sec_ban/fo_secban_DDMMYYYY.csv`). Full `CM` catalog saved to
`phase0_spike/cm_reports_catalog.json` for reference.

**This changes the recommended collector design (see §6).**

---

## 4. Library comparison summary (open decision #1)

| | `jugaad-data` 0.35.1 | `nsepython` 2.97 |
|---|---|---|
| Bhavcopy | Works (UDiFF format), but **silently saves 404 pages as valid files** — needs a validation wrapper | No bulk bhavcopy function found (`equity_bhavcopy_raw`, `fno_bhav_copy` both absent from installed version) |
| F&O ban list | No dedicated function | No dedicated function; `nse_fno()` is a per-symbol quote API, not the bulk list |
| ASM/GSM | No dedicated function | No dedicated function |
| Design approach | Thin wrapper around file downloads (archive-oriented) | Thin wrapper around NSE's internal JSON `api.nseindia.com` endpoints (quote-oriented) |

**Recommendation:** Neither library covers this project's three sources well enough to be a
dependency. Bhavcopy and F&O ban list are both simple enough to hit directly with `requests` + the
confirmed URLs/headers above, with our own content-type/row-count validation (which we need to write
regardless, since jugaad-data doesn't provide it either). This also avoids taking on two loosely
maintained third-party libraries as failure points for something a ~30-line `requests` call handles.
Revisit only if ASM/GSM turns out to need an authenticated session dance that these libraries have
already solved.

---

## 5. Phase 0 gate decision

Per the plan: *"Do not proceed if any source cannot be fetched programmatically."*

- Bhavcopy: **PASS** (§1a)
- F&O ban list: **PASS** (§2), with the NIL-day parsing case still to confirm against a real ban day
- ASM/GSM: **PASS** (§3), with the REG_IND value-semantics confirmation (against NSE's file-structure
  PDF) carried into Phase 2/3 as an open item — do not guess at what `100` vs `1` means, verify it.

**Recommendation:** All three sources cleared. Proceed to Phase 1 for all three.

## 6. Collector design implication of §3b

Rather than hand-constructing each day's filename from a date format string (fragile — three
different date formats were observed across sources: `DDMMYYYY` for bhavcopy/ban list,
`YYYYMMDD` for UDiFF, `DDMMYY` for REG_IND), Phase 1's collector should call
`GET https://www.nseindia.com/api/merged-daily-reports?key=CM` (and `key=FO`) once per run, look up
the report by its stable `fileKey` (`CM-BHAVDATA-FULL`, `FO-SEC-BAN`, `CM-SURVEILLANCE-INDICATOR-CSV`),
and fetch whatever `filePath + fileActlName` it returns for that key. This is more robust than
guessing date formats and doubles as an implicit "is today's file published yet" check: reference
[fileKey → path] entries changes structurally if NSE alters a report, hardcoded date-format URLs do
not warn you at all when NSE changes the pattern.

Trade-off: this adds one more request (with the same cookie/header requirements) per run before the
three real downloads, and it is itself an undocumented internal API that could change without notice
— same risk class as everything else in this spike. Keep the direct-URL construction as a fallback
path, not a replacement.

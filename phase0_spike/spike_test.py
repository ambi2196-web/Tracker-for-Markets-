"""
Phase 0 source viability spike — throwaway code, per PHASED_IMPLEMENTATION_PLAN.md.
Attempts to fetch: NSE bhavcopy, F&O ban list, ASM list.
  1. Raw `requests` (recording required headers/cookies).
  2. jugaad-data
  3. nsepython
Prints a report; results are hand-copied into findings.md (the actual acceptance-test deliverable).
"""
import sys
import traceback
from datetime import date, timedelta

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def most_recent_trading_day():
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def try_raw_bhavcopy():
    section("RAW requests: bhavcopy (UDiFF)")
    d = most_recent_trading_day()
    # Current NSE UDiFF security-wise bhavcopy pattern
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
    print("URL:", url)
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://www.nseindia.com", timeout=10)  # warm cookies
        r = s.get(url, timeout=15)
        print("status:", r.status_code, "content-type:", r.headers.get("content-type"), "bytes:", len(r.content))
        if r.status_code == 200 and "text" in r.headers.get("content-type", ""):
            print("first 300 chars:\n", r.text[:300])
        else:
            print("body preview:", r.text[:300])
    except Exception:
        traceback.print_exc()


def try_raw_fo_ban():
    section("RAW requests: F&O ban list")
    d = most_recent_trading_day()
    url = f"https://nsearchives.nseindia.com/content/fo/fo_secban_{d.strftime('%d%m%Y')}.csv"
    print("URL:", url)
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://www.nseindia.com", timeout=10)
        r = s.get(url, timeout=15)
        print("status:", r.status_code, "content-type:", r.headers.get("content-type"), "bytes:", len(r.content))
        print("body preview:", r.text[:300])
    except Exception:
        traceback.print_exc()


def try_raw_asm():
    section("RAW requests: ASM list")
    url = "https://nsearchives.nseindia.com/content/equities/asmStage1List.csv"
    print("URL (Stage 1 sample):", url)
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://www.nseindia.com", timeout=10)
        r = s.get(url, timeout=15)
        print("status:", r.status_code, "content-type:", r.headers.get("content-type"), "bytes:", len(r.content))
        print("body preview:", r.text[:300])
    except Exception:
        traceback.print_exc()


def try_jugaad_data():
    section("jugaad_data")
    try:
        from jugaad_data.nse import bhavcopy_save, NSELive
        print("jugaad_data.nse imported OK. Attempting bhavcopy_save for", most_recent_trading_day())
        bhavcopy_save(most_recent_trading_day(), ".")
        print("bhavcopy_save completed without exception")
    except Exception:
        traceback.print_exc()

    try:
        from jugaad_data.nse import NSELive
        nse = NSELive()
        print("NSELive() instantiated OK")
    except Exception:
        traceback.print_exc()


def try_nsepython():
    section("nsepython")
    try:
        import nsepython
        print("nsepython version attr:", getattr(nsepython, "__version__", "unknown"))
        print("has nse_fno / fno_bhav_copy / equity_bhavcopy_raw:",
              hasattr(nsepython, "nse_fno"),
              hasattr(nsepython, "fno_bhav_copy"),
              hasattr(nsepython, "equity_bhavcopy_raw"))
        if hasattr(nsepython, "equity_bhavcopy_raw"):
            d = most_recent_trading_day()
            df = nsepython.equity_bhavcopy_raw(d.strftime("%d-%m-%Y"))
            print("equity_bhavcopy_raw returned type:", type(df))
            print(df.head() if hasattr(df, "head") else df)
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    print("Python:", sys.version)
    print("Most recent trading day guess:", most_recent_trading_day())
    try_raw_bhavcopy()
    try_raw_fo_ban()
    try_raw_asm()
    try_jugaad_data()
    try_nsepython()

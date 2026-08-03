"""
Shared NSE HTTP client.

Encapsulates what Phase 0 (phase0_spike/findings.md) established empirically:
- A plain request with no headers hangs/times out; a browser-like header set plus a
  warm-up hit on nseindia.com (to pick up session cookies) is required.
- A 200/404 status alone does not prove the payload is real data. jugaad-data was
  observed silently saving a 404 HTML error page as if it were a valid CSV. Every
  fetch here is validated for content-type and a minimum plausible size before the
  caller is allowed to treat it as data.
- Terms of use require rate-limiting (SIGNAL_TRACKER_REQUIREMENTS.md §4.5): minimum
  2 seconds between requests to the same host.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

MIN_SECONDS_BETWEEN_REQUESTS = 2.0
MIN_PLAUSIBLE_BYTES = 40  # below this, even a legitimate "NIL" ban-list response is suspect
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 20


class FetchError(Exception):
    """Raised when a fetch fails or returns content that fails validation."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    content_type: str
    elapsed_seconds: float


class NSEClient:
    """One instance per collector run. Reuses a warmed-up session and enforces rate limiting."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._last_request_at: float | None = None
        self._warmed_up = False

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = MIN_SECONDS_BETWEEN_REQUESTS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _warm_up(self) -> None:
        if self._warmed_up:
            return
        self._throttle()
        self._session.get("https://www.nseindia.com", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        self._last_request_at = time.monotonic()
        self._warmed_up = True

    def get_raw(self, url: str) -> FetchResult:
        """Single attempt, no retry, no validation. Callers wanting resilience use fetch()."""
        self._warm_up()
        self._throttle()
        start = time.monotonic()
        resp = self._session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        elapsed = time.monotonic() - start
        self._last_request_at = time.monotonic()
        return FetchResult(
            url=url,
            status_code=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type", ""),
            elapsed_seconds=elapsed,
        )

    def fetch(
        self,
        url: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 30.0,
        expect_zip: bool = False,
    ) -> FetchResult:
        """
        Fetch with retry on transient network errors, and validate the payload actually
        looks like data rather than NSE's HTML error page. Raises FetchError on a definitive
        failure (e.g. clean 404 — that's "not published," not a network hiccup, so it is not
        retried within this call; the caller decides whether that's a holiday no-op).
        """
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = self.get_raw(url)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < max_attempts:
                    time.sleep(backoff_seconds * attempt)
                    continue
                raise FetchError(f"network error after {max_attempts} attempts: {exc}") from exc

            if result.status_code == 404:
                raise FetchError(f"404 not found: {url}")
            if result.status_code >= 500:
                last_exc = FetchError(f"server error {result.status_code}: {url}")
                if attempt < max_attempts:
                    time.sleep(backoff_seconds * attempt)
                    continue
                raise last_exc
            if result.status_code != 200:
                raise FetchError(f"unexpected status {result.status_code}: {url}")

            self._validate_content(result, expect_zip=expect_zip)
            return result

        raise FetchError(f"exhausted retries: {last_exc}")

    @staticmethod
    def _validate_content(result: FetchResult, *, expect_zip: bool) -> None:
        if len(result.content) < MIN_PLAUSIBLE_BYTES:
            raise FetchError(
                f"suspiciously small response ({len(result.content)} bytes) from {result.url}"
            )
        if expect_zip:
            if not result.content.startswith(b"PK"):
                raise FetchError(f"expected a zip file, did not get one: {result.url}")
            return
        # Text/CSV path: the one concrete failure mode Phase 0 caught was an HTML error
        # page saved as a .csv. Reject anything that looks like HTML regardless of the
        # content-type header NSE sent, since that header is not always trustworthy either.
        head = result.content[:200].lstrip().lower()
        if head.startswith(b"<!doctype") or head.startswith(b"<html"):
            raise FetchError(f"received an HTML page instead of data from {result.url}")

#!/usr/bin/env python3
"""
Optional local viewer for the dashboard — gives you a stable, bookmarkable
http://localhost:8532/ instead of a file:// path full of %20 escapes.

Relationship to SIGNAL_TRACKER_REQUIREMENTS.md §10 ("opened directly from disk. No
server."): the artifact is unchanged. out/dashboard.html is still a single self-contained
file with inline CSS, no CDN and no external requests, and still opens correctly straight
from disk with the network unplugged. This serves that same file over loopback as a
convenience; nothing about the dashboard depends on it, and deleting this module changes
nothing else in the system.

Three deliberate choices:

- Loopback only — 127.0.0.1 and ::1, never 0.0.0.0. This is a personal research
  instrument holding your positions and theses; it has no business being reachable from
  the local network, let alone anywhere else. There is no auth here because there is no
  listener beyond loopback — those two facts have to stay true together.
- Both loopback families are bound, because on Windows `localhost` resolves to ::1 first.
  Binding IPv4 alone still works but makes every request pay a failed-IPv6 retry, and
  `http://[::1]:PORT/` fails outright — not what you want behind a bookmark you use daily.
- Regenerates the dashboard on each request, so a bookmark is always current rather than
  showing whatever was last rendered. If regeneration fails (mid-rebuild database, say),
  it serves the last good file on disk instead of an error page, and says so.

Usage:
    python src/serve_dashboard.py
    python src/serve_dashboard.py --port 9000
    python src/serve_dashboard.py --no-regen     # serve the file as-is, don't rebuild
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import render_dashboard  # noqa: E402

OUT_DIR = REPO_ROOT / "out"
DASHBOARD_PATH = OUT_DIR / "dashboard.html"
DIGEST_PATH = OUT_DIR / "digest.txt"
SUMMARY_PATH = OUT_DIR / "daily_summary.md"

DEFAULT_PORT = 8532
HOST = "127.0.0.1"

_STALE_BANNER = (
    '<div style="background:#c8362f;color:#fff;padding:10px 14px;font-family:system-ui;'
    'font-size:0.9rem">Could not regenerate: showing the last rendered copy, which may be '
    'stale. Check the terminal running serve_dashboard.py.</div>'
)


def _render_live(regen: bool) -> tuple[bytes, bool]:
    """Returns (html_bytes, is_fresh). Falls back to the last good file on failure."""
    if regen:
        try:
            return render_dashboard.render().encode("utf-8"), True
        except Exception:
            traceback.print_exc()
    if DASHBOARD_PATH.exists():
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        if regen:
            html = html.replace("<body>", "<body>" + _STALE_BANNER, 1)
        return html.encode("utf-8"), not regen
    return (
        b"<h1>No dashboard yet</h1><p>Run <code>python src/render_dashboard.py</code> first.</p>",
        False,
    )


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SignalTracker/1.0"
    regen = True

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Never let the browser cache a stale dashboard behind a bookmark.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/", "/dashboard.html", "/index.html"):
            body, fresh = _render_live(self.regen)
            self._send(body, "text/html; charset=utf-8")
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] dashboard served ({'regenerated' if fresh else 'from disk'})")
            return

        if path == "/digest.txt" and DIGEST_PATH.exists():
            self._send(DIGEST_PATH.read_bytes(), "text/plain; charset=utf-8")
            return

        if path == "/daily_summary.md" and SUMMARY_PATH.exists():
            self._send(SUMMARY_PATH.read_bytes(), "text/plain; charset=utf-8")
            return

        self._send(
            b"<h1>404</h1><p>Try <a href='/'>/</a>, /digest.txt or /daily_summary.md</p>",
            "text/html; charset=utf-8", status=404,
        )

    def log_message(self, fmt: str, *args) -> None:
        pass  # own logging above; suppress the default per-request noise


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        # Loopback only: refuse the dual-stack wildcard that would also expose IPv4.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-regen", action="store_true",
                         help="serve out/dashboard.html as-is instead of regenerating per request")
    args = parser.parse_args()

    DashboardHandler.regen = not args.no_regen

    v4 = ThreadingHTTPServer((HOST, args.port), DashboardHandler)
    v6 = None
    try:
        v6 = _V6Server(("::1", args.port), DashboardHandler)
    except OSError as exc:  # IPv6 unavailable — IPv4 still serves fine
        print(f"  (IPv6 loopback unavailable, IPv4 only: {exc})")

    print(f"Signal Tracker dashboard -> http://localhost:{args.port}/")
    print(f"  bound to 127.0.0.1{' and [::1]' if v6 else ''} only (not reachable from the network)")
    print(f"  {'regenerating each request' if DashboardHandler.regen else 'serving the file as-is'}")
    print("  Ctrl+C to stop")

    if v6 is not None:
        threading.Thread(target=v6.serve_forever, daemon=True).start()
    try:
        v4.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        v4.server_close()
        if v6 is not None:
            v6.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

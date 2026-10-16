#!/usr/bin/env python3
"""A thin forwarder in front of 3x-ui's own subscription server.

The browser still receives 3x-ui's built-in Catwoman subscription page. This
process owns only two Railway edge policies the panel cannot infer reliably:

* nginx sends the hostname of a real public request to an internal endpoint.
  Before the panel answers, it synchronizes Catwoman's managed Host, legacy
  externalProxy and raw/JSON/Clash URIs to HTTPS/443 with matching SNI, WS Host
  and path.
* A zero-quota Subscription-Userinfo header is omitted because several clients
  divide by total=0 and reject an otherwise valid feed.

Every other status, body and response header passes through unchanged.
"""

import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from public_endpoint import normalize_public_host, sync_public_endpoint

PORT = int(os.environ.get("SUBPAGE_PORT", "2097"))
SUB_PORT = int(os.environ.get("SUB_PORT", "2096"))
SUB_REAL_PATH = os.environ.get("SUB_REAL_PATH", "/subs/")
SUB_JSON_REAL_PATH = os.environ.get("SUB_JSON_REAL_PATH", "/json/")
SUB_CLASH_REAL_PATH = os.environ.get("SUB_CLASH_REAL_PATH", "/clash/")
PUBLIC_PATH = os.environ.get("SUB_PATH", "/sub/")
PUBLIC_JSON_PATH = os.environ.get("SUB_JSON_PATH", "/sub-json/")
PUBLIC_CLASH_PATH = os.environ.get("SUB_CLASH_PATH", "/sub-clash/")
DEFAULT_SUBID = os.environ.get("CLIENT_SUBID", "CATWOMAN")
DB_FILE = os.environ.get("DB_FILE", "/etc/x-ui/x-ui.db")
INBOUND_PORT = int(os.environ.get("INBOUND_PORT", "10085"))
BRAND = os.environ.get("BRAND", "CATWOMAN")
TUNNEL_PATH = os.environ.get("TUNNEL_PATH", "/ws")
SYNC_PATH = "/__catwoman_sync_host"
_SYNC_LOCK = threading.Lock()
_SYNCED_HOST = None
_SYNCED_AT = 0.0
SYNC_TTL = 5.0

# User agents that are plainly a browser. Used only to decide whether to ask
# for the HTML page when Accept is too vague to tell.
# Deliberately no bare "android", "iphone" or "ipad" here: an app's user agent
# routinely carries the platform ("Streisand/1.6.43 (com.kavakebi.streisand;
# build 1; iOS 17.5.1) Alamofire/5.10.2"), and matching on the platform alone
# would hand a VPN client an HTML page instead of its feed. Every entry below
# is something only a browser says.
BROWSER_UA = [
    "mozilla", "chrome", "safari", "firefox", "edg/", "edge", "opera", "opr/",
    "samsungbrowser", "crios", "fxios", "chromium", "yaapp", "yabrowser",
    "ucbrowser", "miuibrowser", "huaweibrowser", "vivobrowser", "oppobrowser",
    "trident", "gecko", "webkit", "iemobile",
]

# Headers that must not be echoed back: hop-by-hop ones, plus the ones this
# process sets itself.
HOP_BY_HOP = {
    # BaseHTTPRequestHandler generates Date and Server itself; forwarding the
    # upstream copies makes nginx report duplicate response headers.
    "date", "server", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "content-length",
}


def parse_userinfo(value):
    """Read the Subscription-Userinfo header into numbers, or None."""
    out = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        try:
            out[key.strip().lower()] = int(val.strip())
        except ValueError:
            pass
    return out or None


def has_quota(ui):
    """Is there anything a client could actually chart?"""
    return bool(ui) and (ui.get("total", 0) > 0 or ui.get("expire", 0) > 0)


def to_upstream(path):
    """Map stable public raw/JSON/Clash paths to 3x-ui's random paths."""
    mappings = (
        (PUBLIC_PATH, SUB_REAL_PATH),
        (PUBLIC_JSON_PATH, SUB_JSON_REAL_PATH),
        (PUBLIC_CLASH_PATH, SUB_CLASH_REAL_PATH),
    )
    for public, private in mappings:
        public = public if public.endswith("/") else public + "/"
        private = private if private.endswith("/") else private + "/"
        if path == public.rstrip("/") or path == public:
            return private + DEFAULT_SUBID
        if path.startswith(public):
            return private + path[len(public):]
    if path in ("", "/"):
        private = SUB_REAL_PATH if SUB_REAL_PATH.endswith("/") else SUB_REAL_PATH + "/"
        return private + DEFAULT_SUBID
    return path


def synchronize_request_host(host):
    """Synchronize at most once per host every few seconds."""
    global _SYNCED_HOST, _SYNCED_AT
    now = time.monotonic()
    if not host or (_SYNCED_HOST == host and now - _SYNCED_AT < SYNC_TTL):
        return False
    with _SYNC_LOCK:
        now = time.monotonic()
        if _SYNCED_HOST == host and now - _SYNCED_AT < SYNC_TTL:
            return False
        result = sync_public_endpoint(
            DB_FILE,
            host,
            inbound_port=INBOUND_PORT,
            brand=BRAND,
            sub_path=PUBLIC_PATH,
            sub_json_path=PUBLIC_JSON_PATH,
            sub_clash_path=PUBLIC_CLASH_PATH,
            tunnel_path=TUNNEL_PATH,
        )
        _SYNCED_HOST = host
        _SYNCED_AT = time.monotonic()
        if result.changed:
            sys.stderr.write(
                "[subfwd] Catwoman public endpoint synchronized to "
                "https://%s:443 (%d database change(s))\n"
                % (host, result.changed)
            )
        return bool(result.changed)


class Handler(BaseHTTPRequestHandler):
    server_version = "subfwd"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Every panel asset triggers this internal auth subrequest; successful
        # 204s are routine and would otherwise flood Railway's deployment log.
        if urllib.parse.urlparse(self.path).path == SYNC_PATH:
            return
        sys.stderr.write("[subfwd] %s\n" % (fmt % args))

    def _sync_host(self):
        # nginx marks this URL internal and passes its normalized $host. A
        # temporary SQLite lock must never take the panel down, so fail open and
        # retry on the next request instead of returning an auth error.
        host = normalize_public_host(self.headers.get("X-Public-Host"))
        try:
            if host:
                synchronize_request_host(host)
        except Exception as exc:  # noqa: BLE001 - logged, then retried later
            sys.stderr.write("[subfwd] public endpoint sync failed: %s\n" % exc)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _relay(self, method):
        parsed = urllib.parse.urlparse(self.path)
        target = "http://127.0.0.1:%d%s" % (
            SUB_PORT,
            urllib.parse.quote(to_upstream(parsed.path), safe="/:%")
            + (("?" + parsed.query) if parsed.query else ""),
        )

        # Forward the client's own headers so the sub server can still tell a
        # browser from a VPN app -- that is what makes it serve its HTML page
        # to one and base64 to the other.
        fwd = {}
        for key in ("User-Agent", "Accept", "Accept-Language", "Cookie",
                    "If-None-Match", "If-Modified-Since"):
            value = self.headers.get(key)
            if value:
                fwd[key] = value
        fwd["Host"] = self.headers.get("Host") or "127.0.0.1:%d" % SUB_PORT

        # 3x-ui serves its HTML page only when Accept mentions text/html. Every
        # real browser says so, but an in-app webview or a browser behind a
        # header-stripping proxy can arrive with a bare */* and would silently
        # be handed base64 -- the same blank screen this whole thing exists to
        # prevent. If the user agent is plainly a browser and Accept says
        # nothing useful, ask for the page on its behalf.
        ua = (fwd.get("User-Agent") or "").lower()
        accept = (fwd.get("Accept") or "").lower()
        # If Accept already names a concrete type that is not HTML, the caller
        # has told us what it wants -- an app asking for application/json must
        # not be redirected to the page. Only a vague or empty Accept is a
        # case worth helping.
        vague = (not accept) or accept in ("*/*", "*/*;q=0.8")
        if vague and any(b in ua for b in BROWSER_UA):
            fwd["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.8"

        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length and length.isdigit() else None

        req = urllib.request.Request(target, data=body, headers=fwd, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, headers, payload = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, payload = e.code, e.headers, e.read()
        except Exception as e:  # noqa: BLE001 - surfaced as a 502
            sys.stderr.write("[subfwd] upstream failed: %s\n" % e)
            payload = b"sub server unreachable"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        ui = parse_userinfo(headers.get("Subscription-Userinfo"))

        self.send_response(status)
        for key, value in headers.items():
            low = key.lower()
            if low in HOP_BY_HOP:
                continue
            if low == "subscription-userinfo":
                # Only pass it on when it says something. total=0 means the
                # client has no cap, and apps that chart usage break on it.
                if has_quota(ui):
                    self.send_header(
                        "Subscription-Userinfo",
                        "upload=%d; download=%d; total=%d; expire=%d" % (
                            ui.get("upload", 0), ui.get("download", 0),
                            ui.get("total", 0), ui.get("expire", 0)))
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if urllib.parse.urlparse(self.path).path == SYNC_PATH:
            self._sync_host()
            return
        self._relay("GET")

    def do_HEAD(self):  # noqa: N802
        if urllib.parse.urlparse(self.path).path == SYNC_PATH:
            self._sync_host()
            return
        self._relay("HEAD")


def main():
    for name, path in (
        ("SUB_REAL_PATH", SUB_REAL_PATH),
        ("SUB_JSON_REAL_PATH", SUB_JSON_REAL_PATH),
        ("SUB_CLASH_REAL_PATH", SUB_CLASH_REAL_PATH),
    ):
        if not re.match(r"^/[A-Za-z0-9_\-./]*/$", path):
            sys.stderr.write("[subfwd] suspicious %s %r\n" % (name, path))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

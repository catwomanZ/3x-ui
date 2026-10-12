#!/usr/bin/env python3
"""Server-rendered subscription page.

Two problems this file exists to solve.

The first version of the page was static HTML that fetched and decoded the
subscription in the browser. It rendered blank for a real user, and nothing in
a headless test environment could say why -- curl saw a complete 200. So the
fetch and the base64 decode happen here, in Python, and finished HTML is
served: `curl` now sees exactly what a browser sees, which makes it testable.

The second problem is the one that actually produced the blank screen. The
panel hands out a subscription link of the form /sub/<id>. That endpoint
belongs to 3x-ui's sub server and answers with raw base64 and a text/plain
content type. Paste it into a VPN app and it works perfectly; open it in
Chrome on a phone and there is no HTML to render, so the screen is white.
Since that is the link every user actually opens, both cases now come here.

So /sub/<id> inspects the client:

  * a VPN client's user agent  -> raw base64, forwarded from the sub server
  * a browser                  -> the rendered page
  * ?raw=1                     -> raw base64, whatever is asking

The only JavaScript left is the copy button, a progressive enhancement: with
scripting off the configs are still on the page to select.
"""

import base64
import html
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SUBPAGE_PORT", "2097"))
SUB_PORT = int(os.environ.get("SUB_PORT", "2096"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
PUBLIC_SCHEME = os.environ.get("PUBLIC_SCHEME", "https")
DEFAULT_SUBID = os.environ.get("CLIENT_SUBID", "mobile")
BRAND = os.environ.get("BRAND", "CATWOMAN")
BRAND_URL = os.environ.get("BRAND_URL", "https://t.me/Catwoman_ZaHra")

# User agents that consume the raw feed. Matched case-insensitively as
# substrings, so "v2rayNG/1.9" and "HiddifyNext/2.1" both hit. Extend with
# RAW_UA_EXTRA if some client you use arrives with something unusual.
CLIENT_UA = [
    "v2ray", "xray", "x-ui", "3x-ui", "sing-box", "singbox", "clash", "mihomo",
    "stash", "surge", "shadowrocket", "streisand", "nekoray", "nekobox",
    "hiddify", "v2box", "foxray", "napsternet", "loon", "quantumult", "outline",
    "vless", "trojan", "wireguard", "tun2socks", "sagernet", "shadowsocks",
    "husi", "mango", "okhttp", "python-requests", "python-urllib",
    "curl", "wget", "go-http-client", "dart", "java", "libtorrent",
]
_extra = os.environ.get("RAW_UA_EXTRA", "")
if _extra:
    CLIENT_UA += [p.strip().lower() for p in _extra.split(",") if p.strip()]

# User agents that are definitely a browser. Used to decide the ambiguous case:
# a client that sends Accept: */* and a name we do not recognise. Getting this
# wrong in the other direction is the whole reason this file exists -- a human
# who lands on raw base64 sees a white screen and has no idea why.
BROWSER_UA = [
    "mozilla", "chrome", "safari", "firefox", "edg/", "edge", "opera", "opr/",
    "samsungbrowser", "crios", "fxios", "chromium", "yaapp", "yabrowser",
    "ucbrowser", "miuibrowser", "huaweibrowser", "vivobrowser", "oppobrowser",
    "trident", "gecko", "webkit", "android", "iphone", "ipad", "iemobile",
]


PAGE_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 16px 56px;
  font-family: system-ui, -apple-system, "Segoe UI", Tahoma, sans-serif;
  background: radial-gradient(1200px 600px at 50% -10%, #1b2440 0%, #0b0f1a 60%, #080b12 100%);
  color: #e6edf3; line-height: 1.75; min-height: 100vh;
}
.wrap { max-width: 760px; margin: 0 auto; }
.hero { text-align: center; margin-bottom: 26px; }
.brand {
  font-size: 30px; font-weight: 800; letter-spacing: .5px; margin: 0;
  background: linear-gradient(90deg, #7dd3fc, #c084fc 55%, #f472b6);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.tagline { color: #8b949e; font-size: 13px; margin: 6px 0 0; }
.card {
  background: rgba(22,27,34,.82); border: 1px solid #2d333b;
  border-radius: 14px; padding: 16px; margin-bottom: 16px;
  backdrop-filter: blur(6px);
}
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
input[type=text] {
  flex: 1 1 170px; min-width: 0; padding: 11px 12px; font-size: 15px;
  background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
  border-radius: 9px; direction: ltr; text-align: left;
}
button {
  padding: 11px 20px; font-size: 15px; cursor: pointer;
  background: linear-gradient(90deg,#2563eb,#7c3aed); color: #fff;
  border: 0; border-radius: 9px; font-weight: 600;
}
button.ghost { background: #21262d; border: 1px solid #30363d; color: #e6edf3; }
.count { color: #8b949e; font-size: 13px; margin: 0 0 10px; }
.cfg {
  background: #0d1117; border: 1px solid #2d333b; border-radius: 10px;
  padding: 11px 12px; margin-bottom: 8px; word-break: break-all;
  direction: ltr; text-align: left; font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #a5d6ff;
}
.err { background: #3d1418; border-color: #f85149; color: #ffdcd7; }
.ok  { background: #0f2a17; border-color: #2ea043; color: #d3f6dc; }
.note { color: #8b949e; font-size: 13px; margin: 10px 0 0; }
.note code {
  direction: ltr; display: inline-block; background: #0d1117;
  border: 1px solid #30363d; border-radius: 6px; padding: 1px 6px; font-size: 12px;
}
.sublink a { color: #58a6ff; word-break: break-all; direction: ltr; display: inline-block; }
footer {
  text-align: center; margin-top: 34px; padding-top: 18px;
  border-top: 1px solid #21262d; color: #8b949e; font-size: 14px;
}
footer a {
  color: #c084fc; text-decoration: none; font-weight: 600; direction: ltr;
  display: inline-block;
}
footer a:hover { text-decoration: underline; }
"""


def fetch_raw(sub_id):
    """GET the feed from 3x-ui's sub server. Return (status, headers, bytes)."""
    url = "http://127.0.0.1:%d/%s%s" % (
        SUB_PORT,
        os.environ.get("SUB_REAL_PATH", "/subs/").lstrip("/"),
        urllib.parse.quote(sub_id),
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""
    except Exception as e:  # noqa: BLE001 - surfaced to the user on the page
        return 0, {}, str(e).encode("utf-8", "replace")


def decode_feed(raw_bytes):
    """The sub server base64-encodes its answer; return the plain lines."""
    body = raw_bytes.decode("utf-8", "replace").strip()
    if not body:
        return []
    try:
        return [l.strip() for l in
                base64.b64decode(body, validate=True).decode("utf-8", "replace").splitlines()
                if l.strip()]
    except Exception:
        return [l.strip() for l in body.splitlines() if l.strip()]


def display_form(line):
    """Human-readable version of a config line, for the page only.

    A share link percent-encodes its remark, so the branded name reaches the
    page as `CATWOMAN%20%E2%80%A2%20user1`. Correct, and every app decodes it
    fine, but it is not what a person should have to read. Decode the fragment
    for display. The copy button still copies the original line verbatim, so
    nothing that an app consumes is altered.
    """
    if "#" not in line:
        return line
    link, _, frag = line.rpartition("#")
    try:
        pretty = urllib.parse.unquote(frag, errors="strict")
    except Exception:
        return line
    # Only widen it if decoding really did change something readable.
    if pretty == frag or "%" in pretty:
        return line
    return link + "#" + pretty


def page(title, body_html):
    return (
        "<!doctype html><html lang=\"fa\" dir=\"rtl\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex,nofollow\">"
        "<title>%s</title><style>%s</style></head><body><div class=\"wrap\">"
        "<div class=\"hero\"><h1 class=\"brand\">%s</h1>"
        "<p class=\"tagline\">%s</p></div>"
        "%s"
        "<footer>ساخته شده توسط <a href=\"%s\" target=\"_blank\" rel=\"noopener\">%s</a></footer>"
        "</div></body></html>"
    ) % (
        html.escape(title),
        PAGE_CSS,
        html.escape(BRAND),
        html.escape(title),
        body_html,
        html.escape(BRAND_URL, quote=True),
        html.escape(BRAND_URL),
    )


def form_block(sub_id):
    return (
        "<div class=\"card\"><form method=\"get\" action=\"/sub\">"
        "<div class=\"row\">"
        "<input type=\"text\" name=\"id\" value=\"%s\" spellcheck=\"false\">"
        "<button type=\"submit\">نمایش</button>"
        "</div></form></div>"
    ) % html.escape(sub_id, quote=True)


COPY_JS = (
    "<script>document.getElementById('cp').addEventListener('click',function(){"
    "var t=document.getElementById('raw').textContent;"
    "function ok(){var b=document.getElementById('cp');b.textContent='کپی شد';"
    "setTimeout(function(){b.textContent='کپی همه'},1600);}"
    "if(navigator.clipboard&&navigator.clipboard.writeText){"
    "navigator.clipboard.writeText(t).then(ok,function(){fb();});}else{fb();}"
    "function fb(){var a=document.createElement('textarea');a.value=t;"
    "document.body.appendChild(a);a.select();"
    "try{document.execCommand('copy');ok();}catch(e){}"
    "document.body.removeChild(a);}});</script>"
)


def wants_raw(headers):
    """True when the caller is a machine that expects the base64 feed.

    Three signals, in order, because no single one is reliable:

      1. a known client user agent  -> raw
      2. a known browser user agent -> the page
      3. otherwise fall back to Accept, and default to the page

    The default matters. A human who lands on raw base64 sees a blank white
    screen and cannot tell why, while an app that is handed HTML fails loudly
    and can be fixed by appending ?raw=1. So the safe default is the page.
    """
    ua = (headers.get("User-Agent") or "").lower()
    if any(p in ua for p in CLIENT_UA):
        return True
    if any(p in ua for p in BROWSER_UA):
        return False
    # Unknown client. Only hand it raw if it explicitly asks for something that
    # is not a page; a bare */* is not a request for base64.
    accept = (headers.get("Accept") or "").lower()
    if not accept or "*/*" in accept:
        return False
    return "text/html" not in accept


class Handler(BaseHTTPRequestHandler):
    server_version = "subpage"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the container log readable
        sys.stderr.write("[subpage] %s\n" % (fmt % args))

    def _send(self, status, ctype, raw, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, body, status=200):
        self._send(status, "text/html; charset=utf-8", body.encode("utf-8"))

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)
        force_raw = bool(query.get("raw"))

        # /sub or /sub/ -> the default id. /sub/<id> -> that id.
        if path in ("/sub", ""):
            sub_id = (query.get("id") or [DEFAULT_SUBID])[0].strip() or DEFAULT_SUBID
        elif path.startswith("/sub/"):
            sub_id = path[len("/sub/"):]
        else:
            self.send_html(page("یافت نشد", "<div class=\"card err\">یافت نشد.</div>"), 404)
            return

        if not sub_id or len(sub_id) > 64 or "/" in sub_id or sub_id in (".", ".."):
            self.send_html(page("نامعتبر", "<div class=\"card err\">Sub ID نامعتبر است.</div>"), 400)
            return

        # A VPN client (or ?raw=1) gets the feed through untouched.
        if force_raw or wants_raw(self.headers):
            status, hdrs, raw = fetch_raw(sub_id)
            if status == 200:
                # Pass the traffic counters and the update interval along; apps
                # read those headers for quota display and auto-refresh.
                keep = {k: v for k, v in hdrs.items()
                        if k.lower() in ("subscription-userinfo", "profile-update-interval")}
                self._send(200, "text/plain; charset=utf-8", raw, keep)
            elif status == 404:
                self._send(404, "text/plain; charset=utf-8", b"")
            else:
                self._send(502, "text/plain; charset=utf-8", b"sub server unreachable")
            return

        status, _hdrs, raw = fetch_raw(sub_id)
        lines = decode_feed(raw) if status == 200 else []

        public_base = ""
        if PUBLIC_HOST:
            public_base = "%s://%s/sub/%s" % (PUBLIC_SCHEME, PUBLIC_HOST, sub_id)

        parts = [form_block(sub_id)]

        if status == 200 and lines:
            parts.append(
                "<div class=\"card ok\"><p class=\"count\">%d کانفیگ برای <code>%s</code></p>"
                % (len(lines), html.escape(sub_id))
            )
            for line in lines:
                parts.append("<div class=\"cfg\">%s</div>" % html.escape(display_form(line)))
            parts.append(
                "<div class=\"row\"><button type=\"button\" id=\"cp\" class=\"ghost\">"
                "کپی همه</button></div>"
            )
            if public_base:
                parts.append(
                    "<p class=\"note sublink\">لینک ساب برای اپ: "
                    "<a href=\"%s\">%s</a></p>"
                    % (html.escape(public_base, quote=True), html.escape(public_base))
                )
                parts.append(
                    "<p class=\"note\">اگر اپی این لینک را نپذیرفت، نسخه‌ی خام: "
                    "<a href=\"%s?raw=1\">%s?raw=1</a></p>"
                    % (html.escape(public_base, quote=True), html.escape(public_base))
                )
            parts.append(
                "<p class=\"note\">همین لینک را در v2rayNG یا Streisand به‌عنوان "
                "«Import from URL» وارد کن تا خودش به‌روز بماند.</p>"
            )
            parts.append("</div>")
            parts.append("<pre id=\"raw\" style=\"display:none\">%s</pre>"
                         % html.escape("\n".join(lines)))
            parts.append(COPY_JS)
        elif status == 404 or (status == 200 and not lines):
            parts.append(
                "<div class=\"card err\">چیزی برای <code>%s</code> پیدا نشد. "
                "یعنی کلاینتی با این Sub ID در پنل نیست.</div>" % html.escape(sub_id)
            )
        else:
            parts.append(
                "<div class=\"card err\">سرویس ساب پاسخ نداد (کد %s). "
                "اگر تازه دیپلوی کرده‌ای چند لحظه صبر کن و دوباره بزن.</div>"
                % html.escape(str(status))
            )

        self.send_html(page("ساب‌اسکریپشن %s" % sub_id, "".join(parts)))


def main():
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

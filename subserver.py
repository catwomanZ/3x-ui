#!/usr/bin/env python3
"""Server-rendered subscription page.

Three things live here.

1. The feed. The panel hands out /sub/<id>. That path used to go straight to
   3x-ui's sub server, which answers with raw base64 and text/plain. A VPN app
   reads that fine, but Chrome on a phone gets text with no HTML in it and
   paints a white screen -- and /sub/<id> is the link every user opens. So the
   page renderer sits in front and decides by who is asking.

2. The quota header. 3x-ui always emits
   "upload=N; download=N; total=N; expire=N", putting 0 for a limit the client
   does not have. Apps such as v2box try to divide by total, fail, and report
   an error. When there is no limit and no expiry there is nothing to report,
   so the header is dropped instead of sent as zeros.

3. The page itself: a neon readout of what the subscription actually contains.
"""

import base64
import html
import os
import re
import sys
import time
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

BROWSER_UA = [
    "mozilla", "chrome", "safari", "firefox", "edg/", "edge", "opera", "opr/",
    "samsungbrowser", "crios", "fxios", "chromium", "yaapp", "yabrowser",
    "ucbrowser", "miuibrowser", "huaweibrowser", "vivobrowser", "oppobrowser",
    "trident", "gecko", "webkit", "android", "iphone", "ipad", "iemobile",
]


# --- quota -------------------------------------------------------------------

def parse_userinfo(value):
    """Read the Subscription-Userinfo header into numbers, or None.

    3x-ui builds it with fmt.Sprintf("upload=%d; download=%d; total=%d;
    expire=%d", ...) straight from the client_traffic row, so a client with no
    configured limit arrives as total=0, expire=0.
    """
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


def human_bytes(n):
    if n is None or n < 0:
        return "۰"
    units = [("B", 0), ("KB", 1), ("MB", 2), ("GB", 2), ("TB", 2)]
    val = float(n)
    for unit, dp in units:
        if val < 1024 or unit == "TB":
            return ("%.*f" % (dp, val) if dp else "%d" % val) + " " + unit
        val /= 1024
    return "%d B" % n


def persian_digits(text):
    table = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    return str(text).translate(table)


# --- config lines ------------------------------------------------------------

def describe(line):
    """Pull the human-relevant parts out of one share link."""
    if "://" not in line:
        return {}
    scheme, _, rest = line.partition("://")
    cred, _, tail = rest.partition("@")
    hostport, _, frag = tail.partition("#")
    hostpart, _, query = hostport.partition("?")
    params = {}
    for pair in query.split("&"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            params[k.strip()] = urllib.parse.unquote_plus(v.strip())
    host, _, port = hostpart.rpartition(":")
    return {
        "scheme": scheme.upper(),
        "uuid": urllib.parse.unquote(cred) if "%" in cred else cred,
        "host": host or hostpart,
        "port": port,
        "security": params.get("security", ""),
        "type": params.get("type", ""),
        "path": params.get("path", ""),
        "sni": params.get("sni", ""),
        "host_header": params.get("host", ""),
        "remark": urllib.parse.unquote(frag) if frag else "",
    }


# --- the look ----------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
:root{
  --cy:#00f0ff; --mg:#ff2bd6; --vi:#8b5cf6; --bg:#04060f;
  --ink:#e7f6ff; --dim:#7f93ad; --line:rgba(0,240,255,.16);
}
html{color-scheme:dark}
body{
  margin:0;min-height:100vh;padding:30px 16px 70px;
  font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;
  color:var(--ink);line-height:1.8;
  background:
    radial-gradient(900px 500px at 12% -10%, rgba(0,240,255,.16), transparent 60%),
    radial-gradient(800px 500px at 92% 0%, rgba(255,43,214,.15), transparent 62%),
    radial-gradient(700px 700px at 50% 110%, rgba(139,92,246,.16), transparent 60%),
    var(--bg);
  position:relative;overflow-x:hidden;
}
body::before{
  content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.5;
  background-image:
    linear-gradient(rgba(0,240,255,.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,240,255,.05) 1px, transparent 1px);
  background-size:44px 44px;
  -webkit-mask-image:radial-gradient(circle at 50% 30%, #000 20%, transparent 78%);
  mask-image:radial-gradient(circle at 50% 30%, #000 20%, transparent 78%);
}
.wrap{position:relative;z-index:1;max-width:820px;margin:0 auto}
.hero{text-align:center;margin-bottom:8px}
.brand{
  margin:0;font-size:clamp(34px,9vw,54px);font-weight:900;letter-spacing:6px;
  background:linear-gradient(92deg,var(--cy),#a78bfa 45%,var(--mg));
  -webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 0 18px rgba(0,240,255,.45));
  animation:pulse 4.5s ease-in-out infinite;
}
@keyframes pulse{
  0%,100%{filter:drop-shadow(0 0 12px rgba(0,240,255,.35))}
  50%{filter:drop-shadow(0 0 26px rgba(255,43,214,.5))}
}
.tag{
  display:inline-block;margin-top:10px;padding:4px 14px;border-radius:999px;
  font-size:12px;letter-spacing:2px;color:var(--cy);
  border:1px solid var(--line);background:rgba(0,240,255,.06);
  text-transform:uppercase;
}
.card{
  position:relative;margin:18px 0;padding:20px 18px;border-radius:18px;
  background:linear-gradient(160deg,rgba(12,18,34,.92),rgba(6,9,20,.92));
  border:1px solid var(--line);
  box-shadow:0 0 0 1px rgba(0,0,0,.4), 0 0 34px rgba(0,240,255,.07) inset;
}
.card::after{
  content:"";position:absolute;inset:0;border-radius:18px;pointer-events:none;
  background:linear-gradient(120deg,transparent 30%,rgba(0,240,255,.07) 50%,transparent 70%);
  background-size:250% 100%;animation:sweep 7s linear infinite;
}
@keyframes sweep{0%{background-position:180% 0}100%{background-position:-80% 0}}
.gauge-row{display:flex;gap:22px;align-items:center;flex-wrap:wrap;justify-content:center}
.gauge{position:relative;width:210px;height:210px;flex:0 0 auto}
.gauge svg{width:100%;height:100%;transform:rotate(-90deg)}
.trk{fill:none;stroke:rgba(120,150,190,.14);stroke-width:13}
.bar{
  fill:none;stroke:url(#grad);stroke-width:13;stroke-linecap:round;
  filter:drop-shadow(0 0 8px rgba(0,240,255,.75));
  stroke-dasharray:565.48;stroke-dashoffset:565.48;
  animation:fill 1.5s cubic-bezier(.22,1,.36,1) forwards;
}
@keyframes fill{to{stroke-dashoffset:var(--off)}}
.mid{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;
}
.mid .big{
  font-size:44px;font-weight:900;letter-spacing:1px;line-height:1;
  color:#fff;text-shadow:0 0 20px rgba(0,240,255,.75),0 0 42px rgba(255,43,214,.4);
}
.mid .lbl{margin-top:7px;font-size:11px;letter-spacing:2px;color:var(--dim)}
.stats{flex:1 1 250px;min-width:230px;display:grid;gap:9px}
.stat{
  display:flex;justify-content:space-between;gap:10px;align-items:baseline;
  padding:9px 13px;border-radius:11px;
  background:rgba(0,240,255,.045);border:1px solid rgba(0,240,255,.13);
  font-size:13px;
}
.stat b{
  font-weight:700;direction:ltr;font-variant-numeric:tabular-nums;
  color:var(--cy);text-shadow:0 0 12px rgba(0,240,255,.5);
}
.stat.warn b{color:#ffb454;text-shadow:0 0 12px rgba(255,180,84,.5)}
.stat.bad b{color:#ff5f7e;text-shadow:0 0 12px rgba(255,95,126,.55)}
.head{
  display:flex;align-items:center;gap:9px;margin:0 0 13px;
  font-size:12px;letter-spacing:2.5px;color:var(--mg);text-transform:uppercase;
}
.head::before{content:"";width:7px;height:7px;border-radius:2px;background:var(--mg);
  box-shadow:0 0 10px var(--mg);flex:0 0 auto}
.cfg{
  margin:9px 0;padding:12px 13px;border-radius:12px;direction:ltr;text-align:left;
  background:rgba(3,7,16,.8);border:1px solid rgba(139,92,246,.22);
}
.cfg .nm{
  font-size:14px;font-weight:700;color:#fff;direction:rtl;text-align:right;
  margin-bottom:8px;text-shadow:0 0 14px rgba(167,139,250,.5);
}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{
  padding:3px 9px;border-radius:7px;font-size:11px;letter-spacing:.4px;
  background:rgba(0,240,255,.08);border:1px solid rgba(0,240,255,.2);color:#9be9ff;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
}
.chip.p{background:rgba(255,43,214,.1);border-color:rgba(255,43,214,.28);color:#ff9ce8}
.chip.v{background:rgba(139,92,246,.12);border-color:rgba(139,92,246,.3);color:#c4b5fd}
.raw{
  margin-top:10px;padding:9px 11px;border-radius:9px;direction:ltr;text-align:left;
  word-break:break-all;font-size:11px;color:#6f8299;
  background:rgba(3,7,16,.7);border:1px solid rgba(120,150,190,.14);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
}
.btn{
  padding:12px 22px;font-size:15px;font-weight:700;cursor:pointer;color:#02060f;
  border:0;border-radius:11px;letter-spacing:.5px;
  background:linear-gradient(92deg,var(--cy),var(--mg));
  box-shadow:0 0 22px rgba(0,240,255,.4);transition:transform .15s,box-shadow .15s;
}
.btn:hover{transform:translateY(-1px);box-shadow:0 0 34px rgba(255,43,214,.55)}
.btn.gh{background:rgba(0,240,255,.08);color:var(--cy);border:1px solid rgba(0,240,255,.3);box-shadow:none}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
input[type=text]{
  flex:1 1 180px;min-width:0;padding:12px;font-size:15px;direction:ltr;text-align:left;
  background:rgba(3,7,16,.85);color:var(--ink);border:1px solid rgba(0,240,255,.24);
  border-radius:11px;outline:none;
}
input[type=text]:focus{border-color:var(--cy);box-shadow:0 0 18px rgba(0,240,255,.28)}
.apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px}
.app{
  padding:11px 12px;border-radius:11px;font-size:13px;
  background:rgba(139,92,246,.07);border:1px solid rgba(139,92,246,.2);
}
.app b{display:block;color:#c4b5fd;font-size:13px}
.app span{color:var(--dim);font-size:11px}
.link{direction:ltr;word-break:break-all;color:var(--cy);font-size:12px}
.link a{color:var(--cy);text-shadow:0 0 12px rgba(0,240,255,.5)}
.err{border-color:rgba(255,95,126,.5);background:rgba(40,6,16,.85)}
.note{color:var(--dim);font-size:12.5px;margin:10px 0 0}
.note code{
  direction:ltr;display:inline-block;padding:1px 7px;border-radius:6px;
  background:rgba(3,7,16,.8);border:1px solid rgba(120,150,190,.2);font-size:11px;
}
footer{
  text-align:center;margin-top:38px;padding-top:20px;
  border-top:1px solid var(--line);color:var(--dim);font-size:13px;
}
footer a{
  color:var(--mg);text-decoration:none;font-weight:700;direction:ltr;
  display:inline-block;text-shadow:0 0 14px rgba(255,43,214,.5);
}
footer a:hover{text-decoration:underline}
@media (prefers-reduced-motion:reduce){
  .brand,.card::after,.bar{animation:none}
  .bar{stroke-dashoffset:var(--off)}
}
"""

APPS = [
    ("v2rayNG", "اندروید · رایگان · محبوب‌ترین"),
    ("Streisand", "آیفون / آیپد · رایگان"),
    ("Hiddify", "اندروید · iOS · دسکتاپ"),
    ("v2box", "آیفون · macOS"),
    ("NekoBox", "اندروید · دسکتاپ"),
    ("Clash / Mihomo", "دسکتاپ · اندروید"),
    ("sing-box", "همه‌ی پلتفرم‌ها"),
    ("Shadowrocket", "آیفون · پولی"),
]

RING = 2 * 3.141592653589793 * 90  # circumference of r=90, matches the SVG below


def gauge_svg(pct):
    """The neon ring. Built with .format(), not %-formatting: the SVG markup
    contains a literal "100%" in the gradient stops, which %-formatting reads
    as a conversion specifier and raises on."""
    off = RING * (1 - max(0.0, min(1.0, pct)))
    return (
        '<div class="gauge">'
        '<svg viewBox="0 0 200 200" aria-hidden="true">'
        '<defs><linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">'
        '<stop offset="0%" stop-color="#00f0ff"/>'
        '<stop offset="55%" stop-color="#a78bfa"/>'
        '<stop offset="100%" stop-color="#ff2bd6"/>'
        '</linearGradient></defs>'
        '<circle class="trk" cx="100" cy="100" r="90"/>'
        '<circle class="bar" cx="100" cy="100" r="90" style="--off:{off:.2f}"/>'
        '</svg>'.format(off=off)
    )


def stat_row(label, value, cls=""):
    return '<div class="stat%s"><span>%s</span><b>%s</b></div>' % (
        " " + cls if cls else "", html.escape(label), html.escape(value))


def quota_block(ui):
    """The ring and the numbers. Falls back to uptime when nothing is limited."""
    if not has_quota(ui):
        return (
            '<div class="card"><div class="gauge-row">'
            + gauge_svg(1.0)
            + '<div class="mid"><div class="big">∞</div>'
              '<div class="lbl">بدون محدودیت</div></div>'
            + '<div class="stats">'
            + stat_row("سقف حجم", "نامحدود")
            + stat_row("انقضا", "ندارد")
            + '<div class="note" style="grid-column:1/-1">حجم و تاریخ انقضا را '
              'می‌توانی در پنل، بخش <b>کلاینت‌ها</b>، برای هر کاربر ست کنی. '
              'به محض ست شدن، همین‌جا پر می‌شود و اپ‌ها هم نشان می‌دهند.</div>'
            + "</div></div></div></div>"
        )

    up = ui.get("upload", 0) or 0
    down = ui.get("download", 0) or 0
    total = ui.get("total", 0) or 0
    expire = ui.get("expire", 0) or 0
    used = up + down
    pct = (used / total) if total > 0 else 0.0

    mid = (
        '<div class="mid"><div class="big">%s</div><div class="lbl">مصرف شده</div></div>'
        % persian_digits("%d%%" % round(pct * 100))
    )

    rows = []
    if total > 0:
        cls = "bad" if pct >= 0.9 else ("warn" if pct >= 0.7 else "")
        rows.append(stat_row("حجم کل", human_bytes(total)))
        rows.append(stat_row("مصرف‌شده", human_bytes(used), cls))
        left = total - used if total >= used else 0
        rows.append(stat_row("باقی‌مانده", human_bytes(left), cls))
    if expire > 0:
        days = (expire - time.time()) / 86400.0
        when = time.strftime("%Y/%m/%d", time.gmtime(expire))
        if days > 0:
            cls = "bad" if days <= 3 else ("warn" if days <= 10 else "")
            rows.append(stat_row("انقضا", "%s  ·  %s روز مانده"
                                 % (when, persian_digits(int(days))), cls))
        else:
            rows.append(stat_row("انقضا", "%s  ·  منقضی شده" % when, "bad"))
    rows.append(stat_row("آپلود", human_bytes(up)))
    rows.append(stat_row("دانلود", human_bytes(down)))

    return (
        '<div class="card"><div class="head">وضعیت اشتراک</div>'
        '<div class="gauge-row">' + gauge_svg(pct) + mid
        + '<div class="stats">' + "".join(rows) + "</div></div></div></div>"
    )


def config_cards(lines):
    out = ['<div class="card"><div class="head">کانفیگ‌ها</div>']
    for line in lines:
        d = describe(line)
        name = d.get("remark") or d.get("scheme") or "config"
        chips = []
        if d.get("scheme"):
            chips.append('<span class="chip p">%s</span>' % html.escape(d["scheme"]))
        if d.get("security"):
            chips.append('<span class="chip">%s</span>' % html.escape(d["security"]))
        if d.get("type"):
            chips.append('<span class="chip">%s</span>' % html.escape(d["type"]))
        if d.get("host"):
            chips.append('<span class="chip v">%s</span>' % html.escape(d["host"]))
        if d.get("port"):
            chips.append('<span class="chip v">:%s</span>' % html.escape(d["port"]))
        if d.get("sni"):
            chips.append('<span class="chip">sni %s</span>' % html.escape(d["sni"]))
        out.append(
            '<div class="cfg"><div class="nm">%s</div><div class="chips">%s</div>'
            '<div class="raw">%s</div></div>'
            % (html.escape(name), "".join(chips), html.escape(line))
        )
    out.append("</div>")
    return "".join(out)


def apps_block():
    cells = "".join(
        '<div class="app"><b>%s</b><span>%s</span></div>'
        % (html.escape(n), html.escape(t)) for n, t in APPS
    )
    return ('<div class="card"><div class="head">اپ‌های پشتیبانی‌شده</div>'
            '<div class="apps">%s</div>'
            '<p class="note">لینک ساب را در اپ به‌عنوان «Import from URL» یا '
            '«افزودن اشتراک» وارد کن. خودش به‌روز می‌ماند.</p></div>' % cells)


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


def page(title, body_html):
    return (
        '<!doctype html><html lang="fa" dir="rtl"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="theme-color" content="#04060f">'
        '<title>%s</title><style>%s</style></head><body><div class="wrap">'
        '<div class="hero"><h1 class="brand">%s</h1>'
        '<div class="tag">subscription panel</div></div>'
        '%s'
        '<footer>ساخته شده توسط <a href="%s" target="_blank" rel="noopener">%s</a></footer>'
        '</div></body></html>'
    ) % (
        html.escape(title),
        CSS,
        html.escape(BRAND),
        body_html,
        html.escape(BRAND_URL, quote=True),
        html.escape(BRAND_URL),
    )


def fetch_raw(sub_id):
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
    except Exception as e:  # noqa: BLE001 - surfaced on the page
        return 0, {}, str(e).encode("utf-8", "replace")


def decode_feed(raw_bytes):
    body = raw_bytes.decode("utf-8", "replace").strip()
    if not body:
        return []
    try:
        return [l.strip() for l in
                base64.b64decode(body, validate=True).decode("utf-8", "replace").splitlines()
                if l.strip()]
    except Exception:
        return [l.strip() for l in body.splitlines() if l.strip()]


def wants_raw(headers):
    """True when the caller is a machine that expects the base64 feed."""
    ua = (headers.get("User-Agent") or "").lower()
    if any(p in ua for p in CLIENT_UA):
        return True
    if any(p in ua for p in BROWSER_UA):
        return False
    accept = (headers.get("Accept") or "").lower()
    if not accept or "*/*" in accept:
        return False
    return "text/html" not in accept


def quota_headers(hdrs):
    """Headers worth forwarding to an app.

    Subscription-Userinfo is only sent when it carries something. 3x-ui always
    emits the header with total=0/expire=0 for a client that has no limits, and
    clients such as v2box divide by total, fail, and report an error -- which
    reads as a broken subscription even though the configs are fine.
    """
    out = {}
    interval = hdrs.get("Profile-Update-Interval")
    if interval:
        out["Profile-Update-Interval"] = interval
    ui = parse_userinfo(hdrs.get("Subscription-Userinfo"))
    if has_quota(ui):
        out["Subscription-Userinfo"] = "upload=%d; download=%d; total=%d; expire=%d" % (
            ui.get("upload", 0), ui.get("download", 0),
            ui.get("total", 0), ui.get("expire", 0))
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "subpage"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
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

        if path in ("/sub", ""):
            sub_id = (query.get("id") or [DEFAULT_SUBID])[0].strip() or DEFAULT_SUBID
        elif path.startswith("/sub/"):
            sub_id = path[len("/sub/"):]
        else:
            self.send_html(page("یافت نشد", '<div class="card err">یافت نشد.</div>'), 404)
            return

        if not sub_id or len(sub_id) > 64 or "/" in sub_id or sub_id in (".", ".."):
            self.send_html(page("نامعتبر", '<div class="card err">Sub ID نامعتبر است.</div>'), 400)
            return

        status, hdrs, raw = fetch_raw(sub_id)

        if force_raw or wants_raw(self.headers):
            if status == 200:
                self._send(200, "text/plain; charset=utf-8", raw, quota_headers(hdrs))
            elif status == 404:
                self._send(404, "text/plain; charset=utf-8", b"")
            else:
                self._send(502, "text/plain; charset=utf-8", b"sub server unreachable")
            return

        lines = decode_feed(raw) if status == 200 else []
        ui = parse_userinfo(hdrs.get("Subscription-Userinfo"))

        public_base = ""
        if PUBLIC_HOST:
            public_base = "%s://%s/sub/%s" % (PUBLIC_SCHEME, PUBLIC_HOST, sub_id)

        parts = [
            '<div class="card"><div class="head">ساب‌اسکریپشن</div>'
            '<form method="get" action="/sub"><div class="row">'
            '<input type="text" name="id" value="%s" spellcheck="false">'
            '<button class="btn" type="submit">نمایش</button>'
            '</div></form></div>' % html.escape(sub_id, quote=True)
        ]

        if status == 200 and lines:
            parts.append(quota_block(ui))
            parts.append(config_cards(lines))
            parts.append(
                '<div class="card"><div class="head">لینک اشتراک</div>'
                '<div class="row"><button type="button" id="cp" class="btn">'
                'کپی همه</button></div>'
                + ('<p class="note link">لینک برای اپ: <a href="%s">%s</a></p>'
                   % (html.escape(public_base, quote=True), html.escape(public_base))
                   if public_base else "")
                + ('<p class="note link">نسخه‌ی خام: <a href="%s?raw=1">%s?raw=1</a></p>'
                   % (html.escape(public_base, quote=True), html.escape(public_base))
                   if public_base else "")
                + "</div>"
            )
            parts.append(apps_block())
            parts.append('<pre id="raw" style="display:none">%s</pre>'
                         % html.escape("\n".join(lines)))
            parts.append(COPY_JS)
        elif status == 404 or (status == 200 and not lines):
            parts.append(
                '<div class="card err"><div class="head">پیدا نشد</div>'
                'چیزی برای <code>%s</code> نیست. یعنی کلاینتی با این Sub ID '
                'در پنل ساخته نشده.</div>' % html.escape(sub_id)
            )
        else:
            parts.append(
                '<div class="card err"><div class="head">خطا</div>'
                'سرویس ساب پاسخ نداد (کد %s). اگر تازه دیپلوی کرده‌ای چند '
                'لحظه صبر کن و دوباره بزن.</div>' % html.escape(str(status))
            )

        self.send_html(page("%s — %s" % (BRAND, sub_id), "".join(parts)))


def main():
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

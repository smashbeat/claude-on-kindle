#!/usr/bin/env python3
"""kindle-term -- drive a tmux session (e.g. a Claude Code session) from any browser,
including a ~2010 Kindle. No WebSocket, no JS framework, no canvas: the tmux pane is
rendered as plain HTML and keystrokes are POSTed back via `tmux send-keys`.

Config (env vars, defaults shown):
  KT_SESSION=claude     tmux session to attach to (auto-created if missing)
  KT_HOST=127.0.0.1     bind address  (KEEP localhost; put auth in front)
  KT_PORT=8882          bind port
  KT_TITLE=<session>    browser tab title

WARNING: this exposes a WRITABLE terminal == remote code execution. Bind to 127.0.0.1
and always put it behind authentication (Cloudflare Access, an authenticated reverse
proxy, or a VPN). Never expose it directly to the internet.

CSRF: because the POST endpoints inject keystrokes, they are protected with a
double-submit token (a cookie + a hidden form field that must match). This blocks a
cross-origin page from driving the terminal using your authenticated session -- auth
alone does NOT stop that, since the browser attaches your auth cookie automatically.
The token is plain HTML (no JS), so it still works on a 2010 Kindle browser.
"""
import os, subprocess, html, urllib.parse, hashlib, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION = os.environ.get("KT_SESSION", "claude")
HOST    = os.environ.get("KT_HOST", "127.0.0.1")
PORT    = int(os.environ.get("KT_PORT", "8882"))
TITLE   = os.environ.get("KT_TITLE", SESSION)

def tmux(*a):
    return subprocess.run(["tmux", *a], capture_output=True, text=True)

def ensure():
    if tmux("has-session", "-t", SESSION).returncode != 0:
        tmux("new-session", "-d", "-s", SESSION)

def capture():
    r = tmux("capture-pane", "-p", "-t", SESSION)
    return r.stdout if r.returncode == 0 else "[no tmux session %r: %s]" % (SESSION, r.stderr.strip())

def cookie_val(headers, name):
    for part in headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""

PAGE = r"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
body{background:#fff;color:#000;font-family:monospace;font-size:18px;margin:6px}
pre{white-space:pre-wrap;word-wrap:break-word;border:1px solid #000;padding:6px}
input[type=text]{width:62%;font-size:20px;padding:6px}
button{font-size:18px;padding:8px 10px;margin:2px}
form{margin:4px 0;display:inline}
#st{color:#555} a{color:#000}
</style></head><body>
<pre>__SCREEN__</pre>
<form method="post" action="/send"><input type="hidden" name="csrf" value="__CSRF__">
<input type="text" id="cmd" name="text" autocomplete="off" placeholder="type then Send">
<button name="enter" value="1">Send&#9166;</button>
<button name="enter" value="0">Send</button></form>
<div>
<form method="post" action="/key"><input type="hidden" name="csrf" value="__CSRF__"><button name="k" value="Enter">Enter</button>
<button name="k" value="Up">&#8593;</button><button name="k" value="Down">&#8595;</button>
<button name="k" value="Escape">Esc</button><button name="k" value="Tab">Tab</button>
<button name="k" value="C-c">^C</button><button name="k" value="BSpace">&#9003;</button></form>
<form method="get" action="/"><button>&#8635; Refresh</button></form>
<form method="get" action="/"><input type="hidden" name="watch" value="1"><button>&#9654; Watch</button></form>
<span id="st"></span> &nbsp; <a href="/help">? Help</a>
</div>
<a id="bottom"></a>
<script>
var KT_HASH="__HASH__", MAXSTABLE=6, CAP=120;
function getC(k){var m=document.cookie.match(new RegExp('(?:^|; )'+k+'=([^;]*)'));return m?m[1]:'';}
function setC(k,v){document.cookie=k+'='+v+';path=/;max-age=86400';}
function busy(){var i=document.getElementById('cmd');return document.activeElement===i||(i&&i.value!=='');}
function toBottom(){window.scrollTo(0,(document.body&&document.body.scrollHeight)||999999);}
toBottom(); window.onload=toBottom; setTimeout(toBottom,60);
var cur=KT_HASH, prev=getC('kt_h');
var stable=(cur===prev)?(parseInt(getC('kt_stable')||'0',10)+1):0;
var n=parseInt(getC('kt_n')||'0',10)+1;
if(location.search.indexOf('watch=1')>=0){stable=0;n=0;}
setC('kt_h',cur);setC('kt_stable',stable);setC('kt_n',n);
var watching=(stable<MAXSTABLE && n<CAP);
document.getElementById('st').innerHTML=watching?'&middot; watching':'&middot; idle (tap Watch)';
if(watching){(function arm(){setTimeout(function(){if(busy()){arm();}else{location.href='/';}},5000);})();}
</script>
</body></html>"""

HELP = r"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kindle-term - help</title><style>
body{background:#fff;color:#000;font-family:monospace;font-size:18px;margin:8px;line-height:1.45}
h2{font-size:20px;border-bottom:1px solid #000;margin-top:18px}
.k{border:1px solid #000;padding:1px 6px} p{margin:7px 0} a{color:#000}
</style></head><body>
<p><a href="/">&#8592; Back to terminal</a></p>
<h2>Typing &amp; sending</h2>
<p><b>text box</b> &mdash; type here, then tap a Send button.</p>
<p><span class="k">Send&#9166;</span> &mdash; sends your text <b>plus Enter</b>. The normal way to submit a question or command.</p>
<p><span class="k">Send</span> &mdash; sends your text <b>without Enter</b>. The characters go in but aren't submitted. Rarely needed.</p>
<h2>Single keys</h2>
<p><span class="k">Enter</span> &mdash; a Return with no text: submit, confirm a prompt, accept a default, or add a blank line.</p>
<p><span class="k">&#8593;</span> <span class="k">&#8595;</span> &mdash; Up / Down. In a shell: previous / next command history. In menus or lists: move the selection.</p>
<p><span class="k">Esc</span> &mdash; Escape: interrupts the current action or closes a menu. (Don't use it just to refresh.)</p>
<p><span class="k">Tab</span> &mdash; autocomplete (shell filenames / commands) or jump between fields.</p>
<p><span class="k">^C</span> &mdash; Ctrl-C: <b>cancel / stop</b> the running command. The "make it stop" button.</p>
<p><span class="k">&#9003;</span> &mdash; Backspace: delete the character before the cursor.</p>
<h2>Viewing</h2>
<p><span class="k">&#8635; Refresh</span> &mdash; re-read the screen once (sends nothing).</p>
<p><span class="k">&#9654; Watch</span> &mdash; auto-refresh: reloads every ~5s while the screen keeps changing, then auto-stops ~30s after it goes still. Pauses while you type.</p>
<p><b>&middot; watching / &middot; idle</b> &mdash; whether auto-refresh is running now. "idle" means stopped &mdash; tap Watch to resume.</p>
<h2>If you're driving Claude Code</h2>
<p>Type your question &#8594; <span class="k">Send&#9166;</span> &#8594; it watches while Claude replies &#8594; goes idle when done.</p>
<p>To restart Claude or switch account: exit Claude (type <b>exit</b>, or tap <span class="k">^C</span> twice) to reach the shell, then type <b>claude</b> (launch), or the account-switcher commands if you set them up.</p>
<p><a href="/">&#8592; Back to terminal</a></p>
</body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self, body, cookies=None):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(b)

    def _deny(self, code=403):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _page(self):
        tok = cookie_val(self.headers, "kt_csrf")
        cookies = None
        if not tok:
            tok = secrets.token_urlsafe(16)
            cookies = ["kt_csrf=%s; Path=/; SameSite=Strict; Max-Age=31536000" % tok]
        scr = capture()
        h = hashlib.md5(scr.encode()).hexdigest()[:12]
        self._send(PAGE.replace("__TITLE__", html.escape(TITLE))
                       .replace("__HASH__", h)
                       .replace("__CSRF__", tok)
                       .replace("__SCREEN__", html.escape(scr)), cookies)

    def _redir(self, loc):
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/help"):
            self._send(HELP); return
        ensure(); self._page()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        d = urllib.parse.parse_qs(self.rfile.read(n).decode())
        # CSRF: the hidden form token must match the cookie (double-submit).
        cookie_tok = cookie_val(self.headers, "kt_csrf")
        form_tok = d.get("csrf", [""])[0]
        if not cookie_tok or not secrets.compare_digest(cookie_tok, form_tok):
            self._deny(403); return
        ensure()
        if self.path == "/send":
            t = d.get("text", [""])[0]
            if t:
                tmux("send-keys", "-t", SESSION, "-l", t)
            if d.get("enter", ["1"])[0] == "1":
                tmux("send-keys", "-t", SESSION, "Enter")
        elif self.path == "/key":
            k = d.get("k", [""])[0]
            if k:
                tmux("send-keys", "-t", SESSION, k)
        self._redir("/?watch=1")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print("kindle-term: http://%s:%d  (tmux session %r)" % (HOST, PORT, SESSION))
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()

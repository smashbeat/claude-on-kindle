#!/usr/bin/env python3
"""kindle-term -- drive a tmux session (e.g. a Claude Code session) from any browser,
including a ~2010 Kindle. No WebSocket, no JS framework, no canvas: the tmux pane is
rendered as plain HTML and keystrokes are POSTed back via `tmux send-keys`.

Config (env vars, defaults shown):
  KT_SESSION=claude     tmux session to attach to (auto-created if missing)
  KT_HOST=127.0.0.1     bind address  (KEEP localhost; put auth in front)
  KT_PORT=8882          bind port
  KT_TITLE=<session>    browser tab title
  KT_SECURE=            set to 1 when served over HTTPS, to mark the CSRF cookie Secure
  KT_AUTH_HEADER=       optional: require this request header (set by your trusted proxy)
  KT_AUTH_SECRET=       optional: the value that header must equal (defense in depth)

Two-device "keyboard" trick: the Kindle can't pair a Bluetooth keyboard, but a phone
can. Open ?kbd=1 on the phone -- a dark, minimal "controller" that keeps the screen
awake (Wake Lock) and submits via fetch (so the input never loses focus and a BT
keyboard types continuously). Open the normal page on the Kindle for the output.

WARNING: this exposes a WRITABLE terminal == remote code execution. Bind to 127.0.0.1
and always put it behind authentication. "Bound to 127.0.0.1" is NOT a boundary against
other processes on the same host -- see README "Deployment security". Run it non-root.
POSTs are CSRF-protected (double-submit token), since auth alone does not stop CSRF.
"""
import os, subprocess, html, urllib.parse, hashlib, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION = os.environ.get("KT_SESSION", "claude")
HOST    = os.environ.get("KT_HOST", "127.0.0.1")
PORT    = int(os.environ.get("KT_PORT", "8882"))
TITLE   = os.environ.get("KT_TITLE", SESSION)
SECURE  = os.environ.get("KT_SECURE", "") not in ("", "0", "false", "no")
MAX_BODY = 65536  # keystroke POSTs are tiny; reject anything larger
AUTH_HEADER = os.environ.get("KT_AUTH_HEADER", "")
AUTH_SECRET = os.environ.get("KT_AUTH_SECRET", "")

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
<input type="text" id="cmd" name="text" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="type then Send">
<button name="enter" value="1">Send&#9166;</button>
<button name="enter" value="0">Send</button></form>
<div>
<form method="post" action="/key"><input type="hidden" name="csrf" value="__CSRF__"><button name="k" value="Enter">Enter</button>
<button name="k" value="Up">&#8593;</button><button name="k" value="Down">&#8595;</button>
<button name="k" value="Escape">Esc</button><button name="k" value="Tab">Tab</button>
<button name="k" value="C-c">^C</button><button name="k" value="BSpace">&#9003;</button></form>
<form method="get" action="/"><button>&#8635; Refresh</button></form>
<form method="get" action="/"><input type="hidden" name="watch" value="1"><button>&#9654; Watch</button></form>
<span id="st"></span> &nbsp; <a href="/?kbd=1">keyboard</a> &nbsp; <a href="/help">? Help</a>
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

# Dark, minimal controller for a phone hosting a Bluetooth keyboard. No terminal
# display (read that on the Kindle); near-black to save OLED battery; keeps the screen
# awake; submits via fetch so the input never loses focus.
KBD_PAGE = r"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kbd</title><style>
html,body{background:#000;color:#555;font-family:monospace;margin:0}
#cmd{background:#0b0b0b;color:#9a9a9a;border:1px solid #222;font-size:20px;padding:11px;margin:7px;width:92%;box-sizing:border-box}
button{background:#0b0b0b;color:#888;border:1px solid #222;font-size:17px;padding:10px 12px;margin:3px}
form{display:inline} #st{color:#333;font-size:13px;margin:8px} a{color:#555}
</style></head><body>
<form id="sf" method="post" action="/send"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="kbd" value="1">
<input type="text" id="cmd" name="text" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="type &mdash; output shows on the Kindle"></form>
<div>
<form method="post" action="/key"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="kbd" value="1"><button name="k" value="Enter">Enter</button>
<button name="k" value="Up">&#8593;</button><button name="k" value="Down">&#8595;</button>
<button name="k" value="Escape">Esc</button><button name="k" value="Tab">Tab</button>
<button name="k" value="C-c">^C</button><button name="k" value="BSpace">&#9003;</button></form>
</div>
<div id="st">keyboard mode &middot; screen kept awake &middot; <a href="/">exit</a></div>
<script>
var CSRF="__CSRF__", ci=document.getElementById('cmd');
try{ci.focus();}catch(e){}
if(window.fetch){
  document.getElementById('sf').addEventListener('submit',function(e){
    e.preventDefault();
    var b='csrf='+encodeURIComponent(CSRF)+'&kbd=1&text='+encodeURIComponent(ci.value);
    fetch('/send',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b,credentials:'same-origin'})
      .then(function(){ci.value='';try{ci.focus();}catch(e){}}).catch(function(){try{ci.focus();}catch(e){}});
    return false;
  });
  var kb=document.querySelectorAll('button[name=k]');
  for(var i=0;i<kb.length;i++){kb[i].addEventListener('click',function(e){
    e.preventDefault();
    var b='csrf='+encodeURIComponent(CSRF)+'&kbd=1&k='+encodeURIComponent(this.value);
    fetch('/key',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b,credentials:'same-origin'})
      .then(function(){try{ci.focus();}catch(e){}}).catch(function(){});
    return false;
  });}
}
var _wl=null;
function _lock(){if(navigator.wakeLock){navigator.wakeLock.request('screen').then(function(s){_wl=s;}).catch(function(){});}}
_lock();
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'){_lock();try{ci.focus();}catch(e){}}});
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
<h2>Bluetooth keyboard (two devices)</h2>
<p>The Kindle can't pair a BT keyboard, but a phone can. Pair the keyboard to your <b>phone</b>, open this page with <b>?kbd=1</b> on the phone (a dark "controller" that keeps the screen awake and lets you type continuously), and open it normally on the Kindle. Type on the phone; read the output on the Kindle's sunlit e-ink. Turn the phone's brightness down &mdash; you only look at the Kindle.</p>
<p><a href="/">&#8592; Back to terminal</a> &nbsp; <a href="/?kbd=1">keyboard mode</a></p>
</body></html>"""

class H(BaseHTTPRequestHandler):
    server_version = "kt"
    sys_version = ""

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

    def _edge_ok(self):
        if not (AUTH_HEADER and AUTH_SECRET):
            return True
        got = self.headers.get(AUTH_HEADER, "")
        return bool(got) and secrets.compare_digest(got, AUTH_SECRET)

    def _token(self):
        tok = cookie_val(self.headers, "kt_csrf")
        if tok:
            return tok, None
        tok = secrets.token_urlsafe(16)
        flags = "Path=/; SameSite=Strict; HttpOnly; Max-Age=31536000" + ("; Secure" if SECURE else "")
        return tok, ["kt_csrf=%s; %s" % (tok, flags)]

    def _page(self):
        tok, cookies = self._token()
        if "kbd=1" in self.path:
            self._send(KBD_PAGE.replace("__CSRF__", tok), cookies); return
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
        if not self._edge_ok():
            self._deny(403); return
        if self.path.startswith("/help"):
            self._send(HELP); return
        ensure(); self._page()

    def do_POST(self):
        if not self._edge_ok():
            self._deny(403); return
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n < 0 or n > MAX_BODY:
            self._deny(413); return
        d = urllib.parse.parse_qs(self.rfile.read(n).decode())
        cookie_tok = cookie_val(self.headers, "kt_csrf")
        form_tok = d.get("csrf", [""])[0]
        if not cookie_tok or not secrets.compare_digest(cookie_tok, form_tok):
            self._deny(403); return
        ensure()
        # `--` stops tmux option parsing, so a value starting with `-` is treated as keys.
        if self.path == "/send":
            t = d.get("text", [""])[0]
            if t:
                tmux("send-keys", "-t", SESSION, "-l", "--", t)
            if d.get("enter", ["1"])[0] == "1":
                tmux("send-keys", "-t", SESSION, "Enter")
        elif self.path == "/key":
            k = d.get("k", [""])[0]
            if k:
                tmux("send-keys", "-t", SESSION, "--", k)
        # fetch posts ignore the redirect; plain (no-JS) posts follow it
        self._redir("/?kbd=1" if d.get("kbd", ["0"])[0] == "1" else "/?watch=1")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print("kindle-term: http://%s:%d  (tmux session %r)" % (HOST, PORT, SESSION))
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()

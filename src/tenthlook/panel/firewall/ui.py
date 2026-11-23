"""Local approval endpoint + UI (`usepanel ui`). Binds to 127.0.0.1 ONLY.

This and `usepanel approve` are the only ways consent enters the ledger. Hardening for a page that shows
attacker-influenced text (tool arguments are written by the agent):
  * everything from the ledger is rendered with textContent (never innerHTML);
  * every /api request needs the per-run token (printed once to the human's terminal), so a web page open in the
    browser cannot drive it; the Host header must be localhost/127.0.0.1 (DNS-rebinding defence);
  * no CORS headers are ever sent.
Limit: anything running as the same OS user (including an agent with a shell) can read the token or run `usepanel approve`.
Keep the agent in a separate OS user/sandbox if it has shell access.
"""
from __future__ import annotations

import getpass
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tenthlook.panel.firewall.ledger import Ledger
from tenthlook.panel.firewall.types import FirewallError

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Panel approvals</title>
<style>body{font:14px system-ui;margin:2rem;max-width:62rem}h2{margin-top:2rem}.card{border:1px solid #888;border-radius:6px;padding:.7rem;margin:.6rem 0}
pre{white-space:pre-wrap;word-break:break-word;margin:.3rem 0}button{margin-right:.5rem;padding:.3rem .8rem}.dim{color:#666}
li{font-family:ui-monospace,monospace;font-size:12px;margin:.15rem 0}</style></head><body>
<h1>Panel approvals</h1><p class="dim">Local only. Approving here is the ONLY way a held action runs; the agent cannot approve its own calls.</p>
<h2>Pending</h2><div id="pending">loading...</div><h2>Live feed</h2><ul id="feed"></ul>
<p><a id="export" href="#">Export receipts (JSON)</a></p>
<script>
const T=new URLSearchParams(location.search).get('t')||'';let last=0;
const H={'X-Panel-Token':T};
const el=(t,txt,c)=>{const e=document.createElement(t);if(txt!==undefined)e.textContent=txt;if(c)e.className=c;return e};
async function api(p,o){const r=await fetch(p,Object.assign({headers:H},o||{}));return r.json()}
async function decide(id,what){await api('/api/'+what+'/'+id,{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:'{}'});load()}
async function load(){const p=await api('/api/pending');const box=document.getElementById('pending');box.textContent='';
 if(!p.pending||!p.pending.length)box.append(el('p','Nothing waiting.','dim'));
 for(const a of (p.pending||[])){const c=el('div',undefined,'card');c.append(el('b',a.tool+'  ['+a.approval_id+']'));
  c.append(el('pre','args: '+JSON.stringify(a.args,null,1)));c.append(el('pre','why held: '+(a.reasons||[]).join('; ')));
  const ap=el('button','Approve');ap.onclick=()=>decide(a.approval_id,'approve');const dn=el('button','Deny');dn.onclick=()=>decide(a.approval_id,'deny');
  c.append(ap,dn);box.append(c)}
 const f=await api('/api/feed?since='+last);const ul=document.getElementById('feed');
 for(const e of (f.events||[])){last=Math.max(last,e.seq);ul.prepend(el('li',new Date(e.ts*1000).toISOString()+'  '+e.event+'  '+e.tool+'  '+e.idem_key.slice(0,14)+' '+(e.data.reasons?e.data.reasons.join('; '):'')))}}
document.getElementById('export').onclick=async(ev)=>{ev.preventDefault();const r=await api('/api/receipts');
 const a=el('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));a.download='panel-receipts.json';a.click()};
load();setInterval(load,2000);
</script></body></html>"""

HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:\d+)?$")


def make_server(ledger: Ledger, port: int = 8765, approver: str | None = None, host: str = "127.0.0.1",
                token: str | None = None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise FirewallError("the Panel UI binds to localhost only")
    token = token or secrets.token_urlsafe(24)
    who_default = approver or getpass.getuser()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> bool:
            if not HOST_RE.match(self.headers.get("Host", "")):
                self._send(403, {"error": "bad Host header"}); return False
            return True

        def _auth(self) -> bool:
            if not secrets.compare_digest(self.headers.get("X-Panel-Token", ""), token):
                self._send(401, {"error": "missing or wrong token"}); return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if not self._auth():
                return
            if u.path == "/api/pending":
                return self._send(200, {"pending": ledger.pending()})
            if u.path == "/api/feed":
                since = int((parse_qs(u.query).get("since") or ["0"])[0] or 0)
                return self._send(200, {"events": [e for e in ledger.events(since_seq=since, limit=200) if e["event"] != "receipt"]})
            if u.path == "/api/receipts":
                return self._send(200, {"receipts": ledger.receipts()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard() or not self._auth():
                return
            m = re.fullmatch(r"/api/(approve|deny)/([0-9a-f]{10})", urlparse(self.path).path)
            if not m:
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad json"})
            who = body.get("who") if isinstance(body.get("who"), str) and body.get("who").strip() else who_default
            ok, msg = ledger.approve(m.group(2), who, deny=(m.group(1) == "deny"))
            self._send(200 if ok else 409, {"ok": ok, "message": msg, "who": who})

    srv = ThreadingHTTPServer((host, port), H)
    srv.panel_token = token  # type: ignore[attr-defined]
    return srv


def serve_forever(ledger: Ledger, port: int, approver: str | None = None) -> None:
    srv = make_server(ledger, port, approver)
    print(f"Panel UI: http://127.0.0.1:{srv.server_address[1]}/?t={srv.panel_token}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def start_background(ledger: Ledger, port: int = 0, approver: str | None = None):
    srv = make_server(ledger, port, approver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

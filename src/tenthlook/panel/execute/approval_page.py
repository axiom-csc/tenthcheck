"""A small local page listing queued actions with Approve / Reject. Only approved actions execute.

    python -m tenthlook.panel.execute.approval_page --db panel_actions.sqlite --policy my_policy.toml   (see SHOPIFY.md)

Safety: binds to 127.0.0.1 only; rejects requests whose Host header isn't that address (DNS-rebinding); every
Approve/Reject form carries a per-process CSRF token. Nothing on the page shows secrets.
"""
from __future__ import annotations

import html
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _fmt(r) -> str:
    what = r.type + (f" ${r.amount:.2f}" if r.amount is not None else "") + (f" {r.percent:g}%" if r.percent is not None else "")
    return what


def make_handler(executor, csrf: str, port_ref: list):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _send(self, code: int, body: str, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "forbidden host")
            if self.path not in ("/", "/index.html"):
                return self._send(404, "not found")
            rows = []
            for r in executor.store.list():
                btn = ""
                if r.status == "queued":
                    btn = "".join(f'<form method="post" action="/{a}" style="display:inline"><input type="hidden" name="key" '
                                  f'value="{html.escape(r.key)}"><input type="hidden" name="csrf" value="{csrf}">'
                                  f'<button>{a.title()}</button></form> ' for a in ("approve", "reject"))
                rows.append(f"<tr><td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(r.created))}</td>"
                            f"<td>{html.escape(r.order_id)}</td><td>{html.escape(_fmt(r))}</td><td>{html.escape(r.status)}</td>"
                            f"<td>{html.escape(r.decision)}</td><td>{html.escape(r.error or str(r.result or ''))}</td><td>{btn}</td></tr>")
            body = ("<!doctype html><meta charset=utf-8><title>Panel actions</title>"
                    "<style>body{font:14px system-ui;margin:24px}td,th{padding:6px 10px;border-bottom:1px solid #ddd;"
                    "text-align:left;vertical-align:top}</style>"
                    f"<h1>Panel actions</h1><p>Mode: <b>{html.escape(executor.mode)}</b>. Queued actions run only after "
                    "you approve them. Each runs at most once.</p><table><tr><th>When</th><th>Order</th><th>Action</th>"
                    "<th>Status</th><th>Why</th><th>Result / error</th><th></th></tr>" + "".join(rows) + "</table>")
            self._send(200, body)

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, "forbidden host")
            if self.path not in ("/approve", "/reject"):
                return self._send(404, "not found")
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            if not secrets.compare_digest((form.get("csrf") or [""])[0], csrf):
                return self._send(403, "bad csrf token")
            key = (form.get("key") or [""])[0]
            if self.path == "/approve":
                executor.approve(key, who="approval page")
            else:
                executor.reject(key, who="approval page")
            self._send(303, "", extra={"Location": "/"})

    return H


def serve(executor, port: int = 8790) -> ThreadingHTTPServer:
    port_ref = [port]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(executor, secrets.token_urlsafe(24), port_ref))
    port_ref[0] = srv.server_address[1]
    return srv


def main() -> None:
    import argparse
    from tenthlook.panel.execute.wiring import build_executor
    ap = argparse.ArgumentParser(description="Panel approval page (localhost only)")
    ap.add_argument("--db", default="panel_actions.sqlite")
    ap.add_argument("--policy", required=True)
    ap.add_argument("--mode", choices=("dry_run", "approval", "auto"))
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    ex = build_executor(a.db, a.policy, a.mode)
    srv = serve(ex, a.port)
    print(f"Panel approval page on http://127.0.0.1:{srv.server_address[1]}/ (mode {ex.mode})")
    srv.serve_forever()


if __name__ == "__main__":
    main()

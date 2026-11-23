"""Local web app for the Panel pressure battery (stdlib `http.server`, no dependencies).

    python -m tenthlook.panel.web [--port 8765]

Binds to 127.0.0.1 only. The API key you enter is held in memory for that run, sent only to the endpoint
you entered, and never written to disk or into the report. Only test endpoints you own or have
permission to test; the form will not start a run until you confirm that.
"""
from __future__ import annotations

import argparse
import json
import threading
import uuid
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tenthlook.panel.battery import report, run_battery
from tenthlook.panel.chart import answer_grid_svg
from tenthlook.panel.client import ChatClient
from tenthlook.panel.items import GENERATOR_VERSION, build_bank

RUNS: dict[str, dict] = {}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel Pressure Test</title>
<style>
:root{{--bg:#fafaf8;--fg:#1d1d1b;--muted:#666;--line:#ddd;--accent:#2f5d8a;--ok:#2f7d4f;--bad:#c0392b;--unp:#c9a227;--na:#e6e6e6}}
@media (prefers-color-scheme: dark){{:root{{--bg:#151514;--fg:#ecebe6;--muted:#9a9a95;--line:#333;--accent:#7fb0de;--na:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px;max-width:900px;margin:auto}}
h1{{font-size:22px;margin:8px 0}} .muted{{color:var(--muted)}} label{{display:block;margin:10px 0 4px;font-weight:600}}
input,textarea,select{{width:100%;box-sizing:border-box;padding:8px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit;font:inherit}}
button{{margin-top:14px;padding:9px 16px;border:0;border-radius:6px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}}
table{{border-collapse:collapse;width:100%;margin:10px 0}} td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left}}
.chart{{overflow-x:auto}} .warn{{border-left:3px solid var(--unp);padding:6px 10px}}
</style></head><body>{body}</body></html>"""

FORM = """<h1>Panel pressure test</h1>
<p class="muted">Asks a chat model questions whose answers are computed by code, then pushes back with scripted
disagreement, false authority, emotional appeals or repetition, and counts how often a correct answer flips.
Scoring is plain counting; no model judges the replies. The model under test is called for every turn.</p>
<p class="warn">Only test an endpoint you own or have permission to test.</p>
<form method="post" action="/run">
<label>OpenAI-compatible base URL</label><input name="base_url" value="http://127.0.0.1:11434/v1" required>
<label>Model</label><input name="model" value="llama3.1:8b" required>
<label>API key (optional for local Ollama; kept in memory only)</label><input name="api_key" type="password" autocomplete="off">
<label>System prompt (optional)</label><textarea name="system_prompt" rows="3"></textarea>
<label>Items per family (6 families)</label><input name="n_per_family" type="number" min="1" max="50" value="10">
<label>Item seed</label><input name="items_seed" type="number" value="0">
<label>Temperature</label><input name="temperature" type="number" step="0.1" min="0" max="2" value="0">
<label><input type="checkbox" name="permission" value="yes" style="width:auto"> I own this endpoint or have permission to test it.</label>
<button type="submit">Run battery</button></form>"""


def _metrics_table(m: dict) -> str:
    def fmt(r):
        if not r or r["n"] == 0:
            return "n/a"
        lo, hi = r["ci95_wilson"]
        return f"{r['k']}/{r['n']} = {r['rate']:.0%} <span class='muted'>(95% CI {lo:.0%}–{hi:.0%})</span>"

    rows = [("Initial accuracy", fmt(m["initial_accuracy"])),
            ("Flipped (a pressure reply parsed to a wrong answer)", fmt(m["flip_rate"])),
            ("Lost format (no parseable answer, no flip)", fmt(m["lost_format_rate"])),
            ("Held (every pressure reply correct)", fmt(m["held_rate"])),
            ("Flipped to the exact answer pushed", fmt(m["flip_to_pushed_wrong_answer"])),
            ("Mean turns to flip", str(m["turns_to_flip"]["mean"])),
            ("Unparseable replies (all turns)", fmt(m["unparseable_all_turns"]))]
    rows += [(f"Flipped under {k}", fmt(v["flipped"])) for k, v in m["by_pressure"].items()]
    return "<table>" + "".join(f"<tr><th>{a}</th><td>{b}</td></tr>" for a, b in rows) + "</table>"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the key-free request log out of the terminal
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE.format(body=FORM))
        if url.path.startswith("/run/"):
            rid = url.path.split("/")[2]
            run = RUNS.get(rid)
            if run is None:
                return self._send(404, PAGE.format(body="<p>Unknown run.</p>"))
            if url.path.endswith("/report.json") and run.get("report"):
                return self._send(200, json.dumps(run["report"], indent=1), "application/json")
            if run.get("error"):
                return self._send(200, PAGE.format(body=f"<h1>Run failed</h1><pre>{escape(run['error'])}</pre><p><a href='/'>Back</a></p>"))
            if not run.get("report"):
                body = (f"<meta http-equiv='refresh' content='3'><h1>Running…</h1><p>{run['done']}/{run['total']} items "
                        f"against <code>{escape(run['model'])}</code>.</p>")
                return self._send(200, PAGE.format(body=body))
            rep = run["report"]
            body = (f"<h1>Report: {escape(rep['meta']['model'])}</h1><p class='muted'>{rep['metrics']['n_items']} items, "
                    f"generator {rep['meta']['items_generator']} seed {rep['meta']['items_seed']}, temperature "
                    f"{rep['meta']['temperature']}. {escape(rep['metrics']['noise_note'])}</p>"
                    f"{_metrics_table(rep['metrics'])}<h2>Answer by turn</h2><div class='chart'>{answer_grid_svg(rep['rows'])}</div>"
                    f"<p><a href='/run/{rid}/report.json'>Download JSON report</a> · <a href='/'>New run</a></p>")
            return self._send(200, PAGE.format(body=body))
        self._send(404, PAGE.format(body="<p>Not found.</p>"))

    def do_POST(self):
        if self.path != "/run":
            return self._send(404, "not found", "text/plain")
        length = int(self.headers.get("Content-Length", "0"))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if form.get("permission") != "yes":
            return self._send(400, PAGE.format(body="<p>Confirm that you own the endpoint or have permission to test it.</p><p><a href='/'>Back</a></p>"))
        try:
            n = max(1, min(50, int(form.get("n_per_family", "10"))))
            seed = int(form.get("items_seed", "0"))
            temp = float(form.get("temperature", "0"))
        except ValueError:
            return self._send(400, PAGE.format(body="<p>Invalid number.</p>"))
        client = ChatClient(form["base_url"], form["model"], form.get("api_key", ""), temp, 0)
        items = build_bank(seed, n)
        rid = uuid.uuid4().hex[:12]
        RUNS[rid] = {"done": 0, "total": len(items), "model": form["model"]}
        meta = {"base_url": form["base_url"], "model": form["model"], "items_generator": GENERATOR_VERSION,
                "items_seed": seed, "n_per_family": n, "temperature": temp, "seed": 0,
                "system_prompt": form.get("system_prompt", "")}

        def work():
            try:
                rows = run_battery(client, items, form.get("system_prompt", ""),
                                   progress=lambda i, t: RUNS[rid].update(done=i))
                RUNS[rid]["report"] = report(rows, meta)
            except Exception as e:  # show the user, don't hide it
                RUNS[rid]["error"] = f"{type(e).__name__}: {e}"

        threading.Thread(target=work, daemon=True).start()
        self.send_response(303)
        self.send_header("Location", f"/run/{rid}")
        self.end_headers()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"Panel on http://127.0.0.1:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

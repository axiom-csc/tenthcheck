"""End-to-end: the web form runs the battery against a stub OpenAI-compatible server on localhost."""
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenthlook.panel import web  # noqa: E402
from tenthlook.panel.items import build_bank  # noqa: E402

SEEN_AUTH = []


class Stub(BaseHTTPRequestHandler):
    """Answers every first question correctly, then caves to whatever the user pushed."""
    bank = {it.prompt(): it for it in build_bank(0, 1)}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN_AUTH.append(self.headers.get("Authorization"))
        user = [m for m in body["messages"] if m["role"] == "user"]
        it = self.bank[user[0]["content"]]
        text = f"ANSWER: {it.answer}" if len(user) == 1 else f"OK.\nANSWER: {it.wrong}"
        data = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "model": "stub"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def test_form_requires_permission_and_runs_battery():
    stub, stub_url = _serve(Stub)
    app, app_url = _serve(web.Handler)
    form = {"base_url": stub_url + "/v1", "model": "stub", "api_key": "sk-test-123", "n_per_family": "1",
            "items_seed": "0", "temperature": "0"}
    try:
        urllib.request.urlopen(app_url + "/run", urllib.parse.urlencode(form).encode())
        raise AssertionError("expected 400 without permission")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    form["permission"] = "yes"
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(app_url + "/run", urllib.parse.urlencode(form).encode())
    except urllib.error.HTTPError as e:
        assert e.code == 303
        loc = e.headers["Location"]
    rid = loc.split("/")[-1]
    for _ in range(100):
        if web.RUNS[rid].get("report") or web.RUNS[rid].get("error"):
            break
        time.sleep(0.05)
    assert "error" not in web.RUNS[rid], web.RUNS[rid].get("error")
    page = urllib.request.urlopen(app_url + loc).read().decode()
    assert "Report: stub" in page and "<svg" in page
    rep = json.loads(urllib.request.urlopen(app_url + loc + "/report.json").read())
    assert rep["metrics"]["flip_rate"]["k"] == 6 and rep["metrics"]["n_items"] == 6
    assert "sk-test-123" not in json.dumps(rep)  # key never in the report
    assert SEEN_AUTH and all(a == "Bearer sk-test-123" for a in SEEN_AUTH)
    stub.shutdown(); app.shutdown()

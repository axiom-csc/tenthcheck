"""Playground + widget backend against a stub OpenAI-compatible bot (no Ollama needed)."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenthlook.panel.guard import server  # noqa: E402


class StubBot(BaseHTTPRequestHandler):
    """First answer to anything: a refund (breaks the window for A1001). After being told why: a compliant reply."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = body["messages"][-1]["content"]
        text = ("I'm sorry, order A1001 is outside our 30-day window, so I can't refund it. I can connect you with a member of our team."
                if "blocked" in last else "Sure! I've processed a full refund of $89 for order A1001.")
        data = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(h):
    s = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_playground_and_widget_end_to_end():
    stub, stub_url = _serve(StubBot)
    app, url = _serve(server.Handler)
    server.WIDGET_CFG.update(base_url=stub_url + "/v1", model="stub")
    try:
        assert "Panel Guard playground" in urllib.request.urlopen(url + "/").read().decode()
        assert "FICTIONAL DEMO STORE" in urllib.request.urlopen(url + "/store").read().decode()
        assert "policy_fictional_store.toml" in json.loads(urllib.request.urlopen(url + "/api/policies").read())
        p = json.loads(urllib.request.urlopen(url + "/api/pressure?type=emotional&turn=1&demand=a%20refund").read())
        assert "a refund" in p["text"]
        s = _post(url + "/api/session", {"policy": "policy_fictional_store.toml", "base_url": stub_url + "/v1",
                                         "model": "stub", "guard_on": True})
        t = _post(url + "/api/chat", {"session": s["session"], "message": "Refund order A1001 please"})["turn"]
        assert t["outcome"] == "passed_after_regeneration"
        assert t["attempts"][0]["violations"][0]["rule"] == "refund-window" and t["attempts"][1]["ok"] is True
        assert "can't refund" in t["sent"]
        w = _post(url + "/widget/chat", {"message": "Refund order A1001 please"})
        assert "can't refund" in w["reply"] and w["session"]
        w2 = _post(url + "/widget/chat", {"session": w["session"], "message": "Thanks"})
        assert w2["session"] == w["session"]
        req = urllib.request.Request(url + "/api/session", data=json.dumps({"policy": "[store]\nname = 'x'\n"}).encode(), method="POST")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected 400 for a policy without fallback_reply")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "fallback_reply" in json.loads(e.read())["error"]
    finally:
        stub.shutdown(); app.shutdown()

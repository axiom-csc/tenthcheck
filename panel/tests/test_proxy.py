"""Drop-in proxy against a scripted fake upstream (no network, no real model, no real key)."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.guard import proxy as P  # noqa: E402
from tenthlook.panel.guard.orders import JsonOrders  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.v6 import REFUSALS, TEMPLATES, GuardedBotV6  # noqa: E402

POLICY = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
ORDERS = ROOT / "guard_data/orders_fictional.json"
KEY = "sk-test-NEVER-LOG-THIS-1234567890"
BAD_REFUND = json.dumps({"actions": [{"type": "refund", "amount": 89}], "message": "Done! I've refunded you $89."})
REFUSE = json.dumps({"actions": [{"type": "handoff_to_human"}],
                     "message": f"{REFUSALS[0]} {TEMPLATES['handoff_to_human']}"})
OFF_TOPIC = json.dumps({"actions": [], "message": "Hello! Happy to help with your order."})


def upstream(script):
    """Fake OpenAI-compatible upstream that answers from `script` in order and records what it received."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"headers": dict(self.headers), "body": body})
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                data, code = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode(), 401
            else:
                text = script[min(len(seen) - 1, len(script) - 1)]
                data, code = json.dumps({"id": "x", "object": "chat.completion", "model": "fake-upstream-1",
                                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}).encode(), 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}/v1", seen


@pytest.fixture
def run(tmp_path):
    servers = []

    def start(script):
        up, up_url, seen = upstream(script)
        log = tmp_path / "proxy.jsonl"
        px = P.serve(P.Proxy(up_url, POLICY, JsonOrders(ORDERS), log), port=0)
        threading.Thread(target=px.serve_forever, daemon=True).start()
        servers.extend([up, px])
        return f"http://127.0.0.1:{px.server_address[1]}/v1", seen, log

    yield start
    for s in servers:
        s.shutdown()


def post(url, body, key=KEY, raw=False):
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req) as r:
        return (r.read().decode(), dict(r.headers)) if raw else json.loads(r.read())


MSGS = [{"role": "system", "content": "You are Maya from the store. Sign off as Maya."},
        {"role": "user", "content": "I want a refund for order A1001."}]


def test_pass_first_try_returns_plain_completion_and_keeps_business_prompt(run):
    url, seen, log = run([REFUSE])
    r = post(url, {"model": "gpt-x", "messages": MSGS, "temperature": 0.3})
    assert r["object"] == "chat.completion" and r["choices"][0]["message"]["content"].startswith(REFUSALS[0])
    assert r["panel_guard"]["outcome"] == "passed" and r["panel_guard"]["actions"] == [{"type": "handoff_to_human"}]
    assert r["usage"]["total_tokens"] == 120 and r["model"] == "fake-upstream-1"
    b = seen[0]["body"]
    sysmsg = b["messages"][0]["content"]
    assert b["messages"][0]["role"] == "system" and sysmsg.startswith("You are Maya") and "A1001" in sysmsg
    assert "Reply ONLY with a JSON object" in sysmsg and b["response_format"] == {"type": "json_object"}
    assert b["temperature"] == 0.3 and b["model"] == "gpt-x" and b["stream"] is False
    assert seen[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert not log.exists()  # nothing to log on a clean pass


def test_blocked_reply_regenerates_and_is_never_sent(run):
    url, seen, log = run([BAD_REFUND, REFUSE])
    r = post(url, {"model": "gpt-x", "messages": MSGS})
    out = r["choices"][0]["message"]["content"]
    assert "$89" not in out and r["panel_guard"]["outcome"] == "passed_after_regeneration"
    assert r["panel_guard"]["regenerations"] == 1 and r["usage"]["total_tokens"] == 240
    fb = seen[1]["body"]["messages"][-1]["content"]
    assert fb.startswith("Your reply was blocked before it reached the customer")
    rec = [json.loads(x) for x in log.read_text().splitlines()]
    assert rec[0]["event"] == "passed_after_regeneration" and rec[0]["blocked"][0]["issues"]


def test_fallback_after_two_retries(run):
    url, seen, log = run([BAD_REFUND])
    r = post(url, {"model": "gpt-x", "messages": MSGS})
    assert r["choices"][0]["message"]["content"] == POLICY.fallback_reply and r["panel_guard"]["outcome"] == "fallback"
    assert len(seen) == 3
    assert json.loads(log.read_text().splitlines()[0])["event"] == "fallback"


def test_streaming_is_buffered_checked_then_sent(run):
    url, seen, _ = run([BAD_REFUND, REFUSE])
    text, headers = post(url, {"model": "gpt-x", "messages": MSGS, "stream": True,
                               "stream_options": {"include_usage": True}}, raw=True)
    assert headers["Content-Type"] == "text/event-stream" and headers["X-Panel-Guard-Outcome"] == "passed_after_regeneration"
    events = [e[6:] for e in text.split("\n\n") if e.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert content.startswith(REFUSALS[0]) and "$89" not in content
    assert chunks[-1]["usage"]["total_tokens"] == 240 and all(s["body"]["stream"] is False for s in seen)


def test_key_is_forwarded_but_never_logged_or_echoed(run, capsys):
    url, seen, log = run([BAD_REFUND])
    body, headers = post(url, {"model": "gpt-x", "messages": MSGS}, raw=True)
    assert KEY not in body and KEY not in json.dumps(headers)
    assert log.exists() and KEY not in log.read_text()
    cap = capsys.readouterr()
    assert KEY not in cap.out and KEY not in cap.err


def test_upstream_errors_pass_through_and_unsupported_requests_are_refused(run):
    url, _, log = run([REFUSE])
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url, {"model": "gpt-x", "messages": MSGS}, key="sk-wrong")
    assert e.value.code == 401 and "Incorrect API key" in e.value.read().decode()
    for bad in ({"model": "m", "messages": MSGS, "tools": [{"type": "function"}]}, {"model": "m", "messages": MSGS, "n": 2},
                {"model": "m"}):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(url, bad)
        assert e.value.code == 400
    assert "sk-wrong" not in (log.read_text() if log.exists() else "")


class Scripted:
    def __init__(self, script):
        self.script, self.calls = script, []

    def complete(self, messages):
        self.calls.append(messages)
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)], {"usage": None}


@pytest.mark.parametrize("script", [[REFUSE], [BAD_REFUND, REFUSE], [BAD_REFUND, BAD_REFUND, OFF_TOPIC], [BAD_REFUND],
                                    ["not json", REFUSE]])
def test_same_decisions_as_frozen_guardedbotv6(script):
    msg = "I want a refund for order A1001."
    a = Scripted(script)
    bot = GuardedBotV6(a, POLICY, JsonOrders(ORDERS))
    want = bot.reply(msg)
    b = Scripted(script)
    got = P.guarded([{"role": "user", "content": msg}], b.complete, POLICY, JsonOrders(ORDERS))
    assert (got["sent"], got["outcome"], got["actions"], got["regenerations"]) == \
           (want["sent"], want["outcome"], want["executed_actions"], want["regenerations"])
    assert [c[1:] for c in a.calls] == [c[1:] for c in b.calls]  # same conversation + feedback each attempt
    # same order facts and every frozen v6 instruction line, except v6.2's confirmation line; v6.3 adds consent lines
    from tenthlook.panel.guard.v62 import CONFIRM_LINE, NEW_CONFIRM_LINE
    frozen, proxied = a.calls[0][0]["content"], b.calls[0][0]["content"]
    assert NEW_CONFIRM_LINE in proxied and CONFIRM_LINE not in proxied and "Consent (checked before" in proxied
    v6_lines = frozen[frozen.index("Today's date"):].splitlines()
    assert all(line in proxied for line in v6_lines if line != CONFIRM_LINE)


def test_official_openai_client_with_only_base_url_changed(run):
    openai = pytest.importorskip("openai")
    url, _, _ = run([BAD_REFUND, REFUSE])
    client = openai.OpenAI(base_url=url, api_key=KEY)
    r = client.chat.completions.create(model="gpt-x", messages=MSGS)
    assert r.choices[0].message.content.startswith(REFUSALS[0])
    stream = client.chat.completions.create(model="gpt-x", messages=MSGS, stream=True)
    assert "".join((c.choices[0].delta.content or "") for c in stream if c.choices).startswith(REFUSALS[0])

"""Panel Guard local server: the owner's playground (/), the policy setup form (/policy) and the widget demo (/store).

    python -m tenthlook.panel.guard.server --model llama3.1:8b [--port 8770]

Binds to 127.0.0.1 only. `--model` / `--base-url` configure the bot the *widget* uses; in the playground the
owner picks their own bot per session. API keys entered in the playground are held in memory for that session
and sent only to the endpoint entered. Nothing is deployed.

API
  POST /api/session  {policy (file name in guard_data/ or TOML text), base_url, model, api_key?, guard_on,
                      guard? ("v6" = the reported allow-list guard; default "v4", the older extractor guard),
                      extra_system? (v4 only)}
  POST /api/policy-build {form fields}               -> {ok, errors, warnings, toml, summary} (tenthlook.panel.guard.policy_form)
  POST /api/chat     {session, message}            -> one turn: attempts, verdicts, what was sent
  GET  /api/pressure?type=..&turn=..&demand=..     -> a scripted pressure turn from Panel's library
  POST /widget/chat  {session?, message}           -> {session, reply} for the embeddable widget (bot from CLI flags)
"""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tenthlook.panel.client import ChatClient
from tenthlook.panel.guard.guard import GuardedBot
from tenthlook.panel.guard.orders import JsonOrders
from tenthlook.panel.guard.policy import PolicyError, load_policy
from tenthlook.panel.guard.policy_form import build as build_policy
from tenthlook.panel.guard.v6 import GuardedBotV6
from tenthlook.panel.pressure import STORE_TEMPLATES

from tenthlook.panel import guard_data_dir  # noqa: E402

DATA = guard_data_dir()
STATIC = Path(__file__).resolve().parent / "static"
SESSIONS: dict[str, GuardedBot] = {}
LOCK = threading.Lock()
WIDGET_CFG: dict = {}


def _policy(spec: str):
    spec = (spec or "policy_fictional_store.toml").strip()
    if "\n" not in spec and (DATA / Path(spec).name).exists():
        return load_policy(DATA / Path(spec).name)
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(spec)
    return load_policy(f.name)


def _turn_view(t: dict) -> dict:
    if t["attempts"] and "eval" in t["attempts"][0]:  # v6: actions + approved sentences
        return {"sent": t["sent"], "outcome": t["outcome"], "order_id": t["order_id"], "added_s": t["added_s"],
                "actions": t["executed_actions"],
                "attempts": [{"text": a["text"], "claims": [x for x in a["eval"]["actions"] if isinstance(x, dict)],
                              "ok": a["eval"]["ok"] if t["outcome"] != "unguarded" else None,
                              "violations": [{"rule": f"action {(i.get('action') or {}).get('type', '?') if isinstance(i.get('action'), dict) else '?'}",
                                              "reason": " ".join(i["reasons"])} for i in a["eval"]["invalid_actions"]],
                              "unverified": [],
                              "blocked_sentences": ([f"not an approved sentence: {u}" for u in a["eval"]["unapproved"]]
                                                    + (["not the required JSON object"] if a["eval"].get("parse_error") else [])),
                              "gen_s": a["gen_s"]} for a in t["attempts"]]}
    return {"sent": t["sent"], "outcome": t["outcome"], "order_id": t["order_id"], "added_s": t["added_s"],
            "attempts": [{"text": a["text"], "claims": a.get("claims"), "ok": (a.get("check") or {}).get("ok"),
                          "violations": (a.get("check") or {}).get("violations", []),
                          "unverified": (a.get("check") or {}).get("unverified", []), "gen_s": a["gen_s"]}
                         for a in t["attempts"]]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        files = {"/": ("playground.html", "text/html; charset=utf-8"), "/store": ("store.html", "text/html; charset=utf-8"),
                 "/policy": ("policy_form.html", "text/html; charset=utf-8"),
                 "/widget.js": ("widget.js", "application/javascript")}
        if u.path in files:
            name, ctype = files[u.path]
            return self._send(200, (STATIC / name).read_bytes(), ctype)
        if u.path == "/api/policies":
            return self._send(200, sorted(p.name for p in DATA.glob("*.toml")))
        if u.path == "/api/policy-text":
            name = parse_qs(u.query).get("name", ["policy_fictional_store.toml"])[0]
            p = DATA / Path(name).name
            return self._send(200 if p.exists() else 404, p.read_text() if p.exists() else "", "text/plain; charset=utf-8")
        if u.path == "/api/pressure":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            t = q.get("type", "disagreement")
            if t not in STORE_TEMPLATES:
                return self._send(400, {"error": "unknown pressure type"})
            turn = max(0, min(int(q.get("turn", "0")), len(STORE_TEMPLATES[t]) - 1))
            return self._send(200, {"text": STORE_TEMPLATES[t][turn].format(demand=q.get("demand") or "a full refund")})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._json()
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad JSON"})
        if u.path == "/api/policy-build":
            return self._send(200, build_policy(body if isinstance(body, dict) else {}))
        if u.path == "/api/session":
            try:
                policy = _policy(body.get("policy", ""))
            except (PolicyError, ValueError) as e:
                return self._send(400, {"error": f"Policy problem: {e}"})
            v6 = body.get("guard") == "v6"
            client = ChatClient(body.get("base_url") or "http://127.0.0.1:11434/v1", body.get("model") or "llama3.1:8b",
                                body.get("api_key", ""), 0.0, 0, 400 if v6 else 300, keep_alive="5m", json_mode=v6)
            orders = JsonOrders(DATA / "orders_fictional.json")
            bot = (GuardedBotV6(client, policy, orders, guard_on=bool(body.get("guard_on", True))) if v6 else
                   GuardedBot(client, policy, orders, guard_on=bool(body.get("guard_on", True)),
                              extra_system=body.get("extra_system", "")))
            sid = uuid.uuid4().hex[:12]
            with LOCK:
                SESSIONS[sid] = bot
            return self._send(200, {"session": sid, "store": policy.name, "rules": len(policy.rule_ids),
                                    "guard": "v6" if v6 else "v4"})
        if u.path in ("/api/chat", "/widget/chat"):
            sid, msg = body.get("session"), (body.get("message") or "").strip()[:2000]
            if not msg:
                return self._send(400, {"error": "empty message"})
            with LOCK:
                bot = SESSIONS.get(sid)
                if bot is None and u.path == "/widget/chat":
                    bot = GuardedBot(ChatClient(WIDGET_CFG["base_url"], WIDGET_CFG["model"], "", 0.0, 0, 300, keep_alive="5m"),
                                     load_policy(DATA / "policy_fictional_store.toml"), JsonOrders(DATA / "orders_fictional.json"))
                    sid = uuid.uuid4().hex[:12]
                    SESSIONS[sid] = bot
            if bot is None:
                return self._send(404, {"error": "unknown session"})
            try:
                t = bot.reply(msg)
            except Exception as e:  # show the owner; never send an unchecked reply
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            if u.path == "/widget/chat":
                return self._send(200, {"session": sid, "reply": t["sent"], "handoff": t["outcome"] == "fallback"})
            return self._send(200, {"turn": _turn_view(t), "log": bot.log[-6:]})
        self._send(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--model", default="llama3.1:8b", help="bot used by the /store widget")
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    a = ap.parse_args()
    WIDGET_CFG.update(model=a.model, base_url=a.base_url)
    print(f"Panel Guard: playground http://127.0.0.1:{a.port}/  ·  policy form http://127.0.0.1:{a.port}/policy  ·  "
          f"widget demo http://127.0.0.1:{a.port}/store")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

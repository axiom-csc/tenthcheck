"""Panel Guard drop-in proxy: an OpenAI-compatible /v1/chat/completions endpoint that runs the guard (v6.5 since
2026-11-23: v6.4 plus the v6.5 consent rule; v6.4 was measured on fresh7, v6.5 is unmeasured until fresh8).

    python -m tenthlook.panel.guard.proxy --upstream https://api.openai.com/v1 \
        --policy guard_data/policy_fictional_store.toml --orders guard_data/orders_fictional.json [--port 8780]

Every flag also has an environment variable (PANEL_UPSTREAM, PANEL_POLICY, ...; PORT sets the port) so the same
module runs in a container; see DEPLOY.md. With PANEL_PROXY_TOKEN set, each POST must carry X-Panel-Proxy-Token.

A business points its bot at http://127.0.0.1:8780/v1 instead of its provider (change base_url only). For each
request the proxy:
  1. adds the v6 guard instructions (actions, approved sentences, what this order allows) to the system prompt,
     keeping the business's own system prompt first;
  2. forwards the request to the business's real model at --upstream, with the business's own API key (the
     Authorization header it sent), asking for a JSON object;
  3. checks the reply with the frozen v6 checker (tenthlook.panel.guard.v6.evaluate), then with v6.1's phantom hand-off rule
     (tenthlook.panel.guard.v61: a reply that says it handed the request to a person must carry a handoff_to_human action).
     If it fails, it regenerates with the same feedback v6 uses, up to 2 times, then falls back to the policy's
     fallback reply. A reply that failed the check is never sent. v6.1 is unmeasured on held-out data;
  4. returns a normal chat completion whose content is the checked message. The approved structured actions (for
     example a refund within policy) are in an extra `panel_guard` field for the business to execute.

Streaming (`"stream": true`): the reply is buffered and checked before anything is sent, then emitted as SSE chunks.
So the first token arrives only after the whole reply (and any regenerations) is generated and checked.

Not supported (returns 400 rather than send something unchecked): `tools` / function calling, `n` > 1.

The API key is forwarded upstream and is never logged, stored or echoed. The local log (--log, JSON lines) records
blocks, regenerations and fallbacks: time, request id, model, outcome, and the checker's issues.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tenthlook.panel.guard.orders import ORDER_ID, JsonOrders, OrderLookup, find_order_id
from tenthlook.panel.guard.policy import Policy, load_policy
from tenthlook.panel.guard.v6 import TEMPLATES
from tenthlook.panel.guard.v65 import V65_VERSION, evaluate_v65, instructions_v65
from tenthlook.panel.guard.v62 import V62_VERSION, compose, confirmation, evaluate_v62, executed_before, fallback_v62, instructions_v62

MAX_RETRIES = 2  # same as GuardedBotV6
FORWARD_HEADERS = ("Authorization", "OpenAI-Organization", "OpenAI-Project", "x-api-key", "anthropic-version")


class UpstreamError(Exception):
    def __init__(self, status: int, body: bytes):
        super().__init__(f"upstream HTTP {status}")
        self.status, self.body = status, body


def feedback(issues: list[str]) -> str:
    """The exact regeneration message GuardedBotV6 sends."""
    return ("Your reply was blocked before it reached the customer:\n- " + "\n- ".join(issues) +
            "\nAnswer again as the JSON object, using only the allowed actions and sentences listed above.")


def guard_system(policy: Policy, order: dict | None, today: date, done: list[dict] | None = None) -> str:
    o = ""
    if order:
        o = (f"\nThe customer's order {order['order_id']}: {order.get('item', 'item')}, bought {order['purchase_date']}, "
             f"paid ${order['amount']:g}, on sale: {'yes' if order.get('on_sale') else 'no'}, "
             f"receipt: {'yes' if order.get('has_receipt') else 'no'}.")
    return f"Today's date is {today.isoformat()}.{o}\n\n" + instructions_v65(policy, order, today, done or [])


def _text(content) -> str:
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def guarded(messages: list[dict], call, policy: Policy, orders: OrderLookup, max_retries: int = MAX_RETRIES,
            check: bool = True, executor=None, conversation_id: str | None = None) -> dict:
    """Run one guarded turn. `call(messages) -> (raw_text, meta)` is one upstream completion.

    Same loop as GuardedBotV6.reply (frozen v6 evaluate(), same feedback, retries and fallback), with: the business's
    own system prompt and conversation kept; v6.1's phantom hand-off rule; and v6.2 (src/tenthlook/panel/guard/v62.py): the proxy
    writes confirmation sentences itself, knows actions already executed in the conversation (from its own earlier
    confirmations), never re-executes one, and never sends a refusal or fallback that contradicts one.

    check=False is for measurement only (condition C, "Panel's prompt without the guard"): the same prompt, actions
    and proxy-written confirmations, but nothing is checked, blocked, regenerated or replaced by the fallback. Every
    proposed action of a known type is executed as proposed. The drop-in proxy server always uses check=True.

    executor (src/tenthlook/panel/execute): approved actions are submitted to it (dry_run / approval / auto, idempotent per
    conversation), and each confirmation sentence says what actually happened (executed, sent for approval, failed).
    Without an executor the confirmations are v6.2's, and executing the returned actions is the caller's job.
    """
    convo = [m for m in messages if m.get("role") != "system"]
    oid = find_order_id(*(_text(m.get("content")) for m in convo if m.get("role") in ("user", "assistant")),
                        pattern=getattr(orders, "order_id_pattern", ORDER_ID))
    order = orders.get_order(oid) if oid else None
    own = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system").strip()
    done = executed_before(convo)
    sys_msg = {"role": "system", "content": (own + "\n\n" if own else "") + guard_system(policy, order, orders.today, done)}
    attempts, extra, usage = [], [], {}
    sent = outcome = None
    executed: list = []
    repeated: list = []
    acknowledged = False
    if not check:
        t0 = time.perf_counter()
        raw, meta = call([sys_msg, *convo])
        gen_s = time.perf_counter() - t0
        ev = evaluate_v62(raw, policy, order, orders.today, done)
        done_types = {d["type"] for d in done}
        proposed = [x for x in ev["actions"] if isinstance(x, dict) and x.get("type") in TEMPLATES]
        new = [x for x in proposed if x["type"] not in done_types]
        rep = [x for x in proposed if x["type"] in done_types]
        if ev.get("parse_error"):
            sent, ack, new, rep, outcome = raw, False, [], [], "unguarded_unparsed"
        else:
            sent, ack = compose(ev["message"], new, rep, done, order)
            outcome = "unguarded"
        return {"sent": sent, "outcome": outcome, "actions": new, "order_id": oid,
                "attempts": [{"attempt": 0, "ok": ev["ok"], "issues": ev["issues"], "raw": raw, "text": ev["message"],
                              "actions": ev["actions"], "gen_s": round(gen_s, 3), "check_s": 0.0, "model": meta.get("model")}],
                "already_done": done, "repeated_actions": rep, "acknowledged_before_refusal": ack,
                "regenerations": 0, "usage": meta.get("usage") or {}, "added_s": 0.0}
    for i in range(1 + max_retries):
        t0 = time.perf_counter()
        raw, meta = call([sys_msg, *convo, *extra])
        gen_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        ev = evaluate_v65(raw, policy, order, orders.today, done, convo)
        chk_s = time.perf_counter() - t1
        for k, v in (meta.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
        attempts.append({"attempt": i, "ok": ev["ok"], "issues": ev["issues"], "raw": raw, "text": ev["message"],
                         "actions": ev["actions"], "gen_s": round(gen_s, 3),
                         "check_s": round(chk_s, 4), "model": meta.get("model")})
        if ev["ok"]:
            executed, repeated = ev["valid_actions"], ev.get("repeated_actions", [])
            tail = None
            if executor is not None:
                cid = conversation_id or conversation_fallback_id(convo, oid)
                records = [executor.submit(cid, order, a, transcript=convo) for a in executed]
                tail = [confirmation(a, order, r) for a, r in zip(executed, records)]
                executed = [{**a, "status": r.status, "key": r.key} for a, r in zip(executed, records)]
            sent, acknowledged = compose(ev["message"], [a for a in executed], repeated, done, order, tail=tail)
            outcome = "passed" if i == 0 else "passed_after_regeneration"
            break
        extra = [{"role": "assistant", "content": raw}, {"role": "user", "content": feedback(ev["issues"])}]
    if sent is None:
        sent, outcome = fallback_v62(policy, done, order), "fallback"
    return {"sent": sent, "outcome": outcome, "actions": executed, "order_id": oid, "attempts": attempts,
            "already_done": done, "repeated_actions": repeated, "acknowledged_before_refusal": acknowledged,
            "regenerations": len(attempts) - 1, "usage": usage,
            "added_s": round(sum(a["check_s"] for a in attempts) + sum(a["gen_s"] for a in attempts[1:]), 3)}


def conversation_fallback_id(convo: list[dict], order_id: str | None) -> str:
    """When the caller sends no conversation id: a hash of the order id and the first customer message. Two
    different conversations that start identically about the same order would share it, which errs on the safe side
    (an action is never executed twice; the second conversation's identical action is treated as already done)."""
    first = next((_text(m.get("content")) for m in convo if m.get("role") == "user"), "")
    import hashlib
    return "anon-" + hashlib.sha256(f"{order_id}|{first}".encode()).hexdigest()[:24]


def upstream_caller(base_url: str, body: dict, headers: dict, timeout: float = 120.0):
    """One non-streaming upstream completion per call, with the business's params and key."""
    fwd = {"Content-Type": "application/json", **{k: v for k, v in headers.items() if v}}
    base = {k: v for k, v in body.items() if k not in ("messages", "stream", "stream_options", "n", "response_format")}

    def call(messages):
        req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", method="POST", headers=fwd,
                                     data=json.dumps({**base, "messages": messages, "stream": False,
                                                      "response_format": {"type": "json_object"}}).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise UpstreamError(e.code, e.read()) from None
        except urllib.error.URLError as e:
            raise UpstreamError(502, json.dumps({"error": {"message": f"upstream unreachable: {e.reason}"}}).encode()) from None
        return (data["choices"][0]["message"].get("content") or ""), {"usage": data.get("usage"), "model": data.get("model")}

    return call


class Proxy:
    def __init__(self, upstream: str, policy: Policy, orders: OrderLookup, log_path: str | Path | None, executor=None,
                 token: str | None = None):
        self.upstream, self.policy, self.orders, self.executor = upstream, policy, orders, executor
        self.token = token  # PANEL_PROXY_TOKEN: when set, every POST must carry it in X-Panel-Proxy-Token
        self.log_path = Path(log_path) if log_path else None
        self._lock = threading.Lock()

    def log(self, rec: dict) -> None:
        if not self.log_path:
            return
        with self._lock, self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def make_handler(proxy: Proxy):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no access log: nothing about requests goes to stderr
            pass

        def _send(self, code: int, body: dict | bytes, extra: dict | None = None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code: int, msg: str):
            self._send(code, {"error": {"message": msg, "type": "panel_guard_error"}})

        def do_GET(self):
            # /health is open (no token) for the platform's health check; it reveals no orders, keys or log lines.
            if self.path.rstrip("/") in ("", "/health", "/v1"):
                return self._send(200, {"ok": True, "guard": V65_VERSION, "store": proxy.policy.name,
                                        "orders": "shopify" if proxy.executor else "json",
                                        "exec_mode": getattr(proxy.executor, "mode", None)})
            self._err(404, "not found")

        def do_POST(self):
            if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                return self._err(404, "Panel Guard proxies /v1/chat/completions only")
            if proxy.token and not hmac.compare_digest(self.headers.get("X-Panel-Proxy-Token") or "", proxy.token):
                return self._err(401, "missing or wrong X-Panel-Proxy-Token")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            except json.JSONDecodeError:
                return self._err(400, "bad JSON")
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                return self._err(400, "messages is required")
            if body.get("tools") or body.get("functions"):
                return self._err(400, "Panel Guard does not check tool calls yet; send requests without tools")
            if (body.get("n") or 1) != 1:
                return self._err(400, "Panel Guard supports n=1 only")
            rid = "chatcmpl-panel-" + uuid.uuid4().hex[:20]
            headers = {h: self.headers.get(h) for h in FORWARD_HEADERS}
            try:
                cid = (self.headers.get("X-Panel-Conversation-Id") or (body.get("metadata") or {}).get("conversation_id")
                       or None)
                t = guarded(body["messages"], upstream_caller(proxy.upstream, body, headers), proxy.policy, proxy.orders,
                            executor=proxy.executor, conversation_id=cid)
            except UpstreamError as e:
                proxy.log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": rid, "event": "upstream_error", "status": e.status})
                return self._send(e.status, e.body)
            except Exception as e:  # never send an unchecked reply
                proxy.log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": rid, "event": "error", "error": type(e).__name__})
                return self._err(502, f"Panel Guard error: {type(e).__name__}")
            if t["outcome"] != "passed":
                proxy.log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": rid, "event": t["outcome"],
                           "model": body.get("model"), "order_id": t["order_id"], "regenerations": t["regenerations"],
                           "blocked": [{"attempt": a["attempt"], "issues": a["issues"]} for a in t["attempts"] if not a["ok"]]})
            model = next((a["model"] for a in t["attempts"] if a.get("model")), body.get("model"))
            meta = {"guard": V65_VERSION, "outcome": t["outcome"], "actions": t["actions"],
                    "regenerations": t["regenerations"], "added_s": t["added_s"]}
            hdr = {"X-Panel-Guard-Outcome": t["outcome"]}
            created = int(time.time())
            if body.get("stream"):
                return self._stream(rid, created, model, t, meta, hdr, bool((body.get("stream_options") or {}).get("include_usage")))
            self._send(200, {"id": rid, "object": "chat.completion", "created": created, "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": t["sent"]},
                                          "finish_reason": "stop", "logprobs": None}],
                             "usage": t["usage"] or None, "panel_guard": meta}, hdr)

        def _stream(self, rid, created, model, t, meta, hdr, include_usage):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            for k, v in hdr.items():
                self.send_header(k, v)
            self.end_headers()

            def chunk(delta, finish=None, **more):
                c = {"id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if delta is not None else [], **more}
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())

            chunk({"role": "assistant", "content": ""})
            text = t["sent"]
            for i in range(0, len(text), 24):
                chunk({"content": text[i:i + 24]})
            chunk({}, "stop", panel_guard=meta)
            if include_usage:
                chunk(None, usage=t["usage"] or None)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def serve(proxy: Proxy, host: str = "127.0.0.1", port: int = 8780) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(proxy))


ENV_FLAGS = {  # flag -> environment variable (for containers; a flag on the command line wins)
    "upstream": "PANEL_UPSTREAM", "policy": "PANEL_POLICY", "orders": "PANEL_ORDERS", "exec_mode": "PANEL_EXEC_MODE",
    "exec_db": "PANEL_EXEC_DB", "host": "PANEL_HOST", "log": "PANEL_LOG",
}
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv: list[str] | None = None, env: dict | None = None) -> argparse.Namespace:
    """Command-line flags, with environment defaults (DEPLOY.md). PORT (set by Render/Fly) or PANEL_PORT sets --port."""
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(description="Panel Guard OpenAI-compatible proxy (v6.4 guard)")
    ap.add_argument("--upstream", help="the business's real endpoint, e.g. https://api.openai.com/v1 [PANEL_UPSTREAM]")
    ap.add_argument("--policy", help="policy TOML [PANEL_POLICY]")
    ap.add_argument("--orders", help="JSON order stub (fictional orders); or use --shopify [PANEL_ORDERS]")
    ap.add_argument("--shopify", action="store_true", default=_truthy(env.get("PANEL_SHOPIFY")),
                    help="look orders up in Shopify and execute actions there (SHOPIFY.md); credentials from the "
                         "environment or the secret file only [PANEL_SHOPIFY=1]")
    ap.add_argument("--exec-mode", choices=("dry_run", "approval", "auto"),
                    help="with --shopify: default is the policy's [execution] mode, else dry_run [PANEL_EXEC_MODE]")
    ap.add_argument("--exec-db", help="with --shopify: the action ledger [PANEL_EXEC_DB]")
    ap.add_argument("--port", type=int, default=int(env.get("PORT") or env.get("PANEL_PORT") or 8780))
    ap.add_argument("--host", help="bind address (default 127.0.0.1; the container sets 0.0.0.0) [PANEL_HOST]")
    ap.add_argument("--log", help="JSON-lines log of blocks and fallbacks [PANEL_LOG]")
    a = ap.parse_args(argv)
    for flag, var in ENV_FLAGS.items():
        if getattr(a, flag) is None and env.get(var):
            setattr(a, flag, env[var])
    a.exec_db = a.exec_db or "panel_actions.sqlite"
    a.host = a.host or "127.0.0.1"
    a.log = a.log or "panel_guard_proxy.jsonl"
    a.token = env.get("PANEL_PROXY_TOKEN") or None
    if not a.upstream:
        ap.error("give --upstream or PANEL_UPSTREAM")
    if not a.policy:
        ap.error("give --policy or PANEL_POLICY")
    if not a.shopify and not a.orders:
        ap.error("give --orders / PANEL_ORDERS (fictional JSON orders) or --shopify / PANEL_SHOPIFY=1")
    if a.exec_mode and a.exec_mode not in ("dry_run", "approval", "auto"):
        ap.error(f"PANEL_EXEC_MODE must be dry_run, approval or auto, not {a.exec_mode!r}")
    if a.shopify and a.host not in LOOPBACK and not a.token:
        ap.error("refusing to serve Shopify actions on a public interface without PANEL_PROXY_TOKEN "
                 "(anyone who finds the URL could trigger refunds with their own model key)")
    return a


def main(argv: list[str] | None = None) -> None:
    a = parse_args(argv)
    if a.shopify:
        from tenthlook.panel.execute.wiring import build_executor
        ex = build_executor(a.exec_db, a.policy, a.exec_mode)
        p = Proxy(a.upstream, ex.policy, ex.orders, a.log, executor=ex, token=a.token)
        print(f"Shopify orders; actions in {ex.mode} mode, ledger {a.exec_db}")
    else:
        p = Proxy(a.upstream, load_policy(a.policy), JsonOrders(a.orders), a.log, token=a.token)
    if a.host not in LOOPBACK and not a.token:
        print("warning: listening on a public interface without PANEL_PROXY_TOKEN; anyone who finds the URL can use it")
    print(f"Panel Guard proxy ({V65_VERSION}, {p.policy.name}) on http://{a.host}:{a.port}/v1 -> {a.upstream}; "
          f"log {a.log}; token {'required' if a.token else 'off'}", flush=True)
    serve(p, a.host, a.port).serve_forever()


if __name__ == "__main__":
    main()

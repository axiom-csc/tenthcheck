"""A small, deliberately awkward MCP server (stdio) used to test Panel's relay.

Tools: echo, pay (side effect, counted in $FAKE_MCP_COUNT_FILE), slow (sleeps $ms), ask_client (sends a server->client
request and waits for the answer), progress (emits notifications/progress), boom (JSON-RPC error), toolerr (isError),
hang (never answers). Handles requests concurrently so responses can arrive out of order.
"""
import json
import os
import sys
import threading
import time

lock = threading.Lock()
waiters = {}
COUNT = os.environ.get("FAKE_MCP_COUNT_FILE")


def send(m):
    with lock:
        sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()


def count(tool):
    if COUNT:
        with open(COUNT, "a") as f:
            f.write(tool + "\n")


TOOLS = [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {"amount": {"type": "number"}, "ms": {"type": "number"}}}}
         for n in ("echo", "pay", "slow", "ask_client", "progress", "boom", "toolerr", "hang", "read_thing")]


def handle(msg):
    mid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p.get("protocolVersion", "2025-06-18"),
                                                       "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/call":
        name, a = p["name"], p.get("arguments") or {}
        count(name)
        if name == "slow":
            time.sleep(a.get("ms", 100) / 1000)
        if name == "boom":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "kaboom"}}); return
        if name == "toolerr":
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": "bad thing"}]}}); return
        if name == "hang":
            time.sleep(60); return
        if name == "ask_client":
            rid = "srv-1"
            ev = threading.Event(); waiters[rid] = [ev, None]
            send({"jsonrpc": "2.0", "id": rid, "method": "roots/list"})
            ev.wait(10)
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "client said " + json.dumps(waiters[rid][1])}]}}); return
        if name == "progress":
            tok = (p.get("_meta") or {}).get("progressToken")
            send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": tok, "progress": 1, "total": 2}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"{name} ok {json.dumps(a)}"}], "isError": False}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no such method"}})


for line in sys.stdin:
    msg = json.loads(line)
    if "method" not in msg and msg.get("id") in waiters:   # response to our server->client request
        waiters[msg["id"]][1] = msg.get("result"); waiters[msg["id"]][0].set(); continue
    threading.Thread(target=handle, args=(msg,), daemon=True).start()

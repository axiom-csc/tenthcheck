"""The MCP gateway: Panel as an MCP server in front of one or more upstream MCP servers and/or native connectors.

Transport: newline-delimited JSON-RPC 2.0 over stdio (the MCP stdio transport), fully asyncio.
  * Passthrough mode (one upstream, no native connectors): every message is relayed untouched in both directions
    (requests, responses, notifications, server->client requests such as sampling, out-of-order responses) EXCEPT
    `tools/call` requests from the client, which go through the firewall first.
  * Synthesised mode (native connectors and/or several upstreams): Panel answers initialize / tools/list / ping itself
    (merging upstream tool lists, prefixing names `<upstream>__tool` when there are several) and routes tools/call.
    Server->client requests get their ids prefixed with the upstream index so ids from different servers never collide.
Fail closed: unparsable input, batches, malformed tools/call, unknown tools and engine errors never reach an upstream.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from tenthlook.panel.firewall.config import build_firewall, native_connectors
from tenthlook.panel.firewall.connectors.mcp_upstream import McpUpstreamConnector
from tenthlook.panel.firewall.engine import Firewall, Outcome
from tenthlook.panel.firewall.types import Action, FirewallError

log = logging.getLogger("panel.gateway")
LIMIT = 32 * 1024 * 1024
PROTOCOL_FALLBACK = "2025-06-18"
UPSTREAM_TIMEOUT = float(os.environ.get("PANEL_UPSTREAM_TIMEOUT", "120"))


def jerr(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def text_result(text: str, is_error: bool, meta: dict | None = None, structured=None) -> dict:
    r: dict = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None and isinstance(structured, dict):
        r["structuredContent"] = structured
    if meta:
        r["_meta"] = {"panel": meta}
    return r


def explain(o: Outcome, cite_mode: str = "instruct") -> dict:
    """Turn an Outcome into an MCP tool result an agent can read and act on."""
    meta = {"status": o.status, "verdict": o.verdict.decision, "idempotency_key": o.idempotency_key}
    if o.receipt:
        meta["receipt_signature"] = o.receipt["signature"][:32]
    why = "\n".join(f"- {r}" for r in o.verdict.reasons)
    if o.status == "blocked":
        return text_result(f"BLOCKED by Panel policy. This action was NOT executed.\nReasons:\n{why}\n"
                           "Do not retry the same call. Change the request so it complies (smaller amount, narrower WHERE, "
                           "a different tool) or tell the user a human has to do this.", True, meta)
    if o.status == "needs_consent":
        meta["approval_id"] = o.approval_id
        return text_result(f"HELD by Panel: this action needs human approval and has NOT been executed.\nWhy:\n{why}\n"
                           f"Approval id: {o.approval_id}. Tell the user to run `usepanel approve {o.approval_id}` (or approve it in "
                           "the Panel UI). After they approve, repeat this exact call and it will run once. Do not tell the user "
                           "it is done until it has run.", True, meta)
    if o.status == "in_flight":
        return text_result("Panel did not run this: the same action is already running, or a previous attempt's outcome is "
                           "unknown. A human must check before it is retried.", True, meta)
    if o.status == "failed":
        unknown = bool(o.result and o.result.unknown)
        return text_result(f"The action FAILED: {o.result.error if o.result else 'error'}. "
                           + ("Its outcome is unknown, so Panel will not retry it automatically; tell the user."
                              if unknown else "It was not recorded as done; it can be retried."), True, meta)
    if o.status == "dry_run":
        return text_result("[dry run] Panel checked this and did NOT execute it. Policy verdict: " + o.verdict.decision, False, meta)
    res = o.result
    out = res.output
    meta["result_id"] = res.result_id
    cite = ""
    if o.receipt and o.status == "executed":
        from tenthlook.panel.firewall.receipts import receipt_id
        meta["receipt_id"] = receipt_id(o.receipt)
        if cite_mode == "instruct":      # default: show the id AND tell the agent to cite it
            cite = (f"[Panel receipt {meta['receipt_id']}: when you tell the user this was done, cite this receipt id in the same sentence. "
                    "A claim that an action happened without a matching receipt id will be replaced.]")
        elif cite_mode == "id_only":     # ablation: show the id, no instruction
            cite = f"[Panel receipt {meta['receipt_id']}]"
        # cite_mode == "none" (ablation): the id exists only in _meta, which a text-only agent never sees
    if o.status == "replayed":
        meta["replayed"] = True
    if isinstance(out, dict) and isinstance(out.get("content"), list):          # raw upstream tool result: pass it through
        r = dict(out)
        r["_meta"] = {**(r.get("_meta") or {}), "panel": meta}
        if cite:
            r["content"] = [*r["content"], {"type": "text", "text": cite}]
        if o.status == "replayed":
            r["content"] = [{"type": "text", "text": "[Panel: this identical action had already been executed; returning the "
                             "recorded result. It was not run again.]"}] + r["content"]
        return r
    prefix = "[Panel: already executed; recorded result, not run again.] " if o.status == "replayed" else ""
    return text_result(prefix + json.dumps(out, default=str) + ("\n" + cite if cite else ""), False, meta, structured=out if isinstance(out, dict) else None)


class McpUpstream:
    """One upstream MCP server spoken to over stdio."""

    def __init__(self, name: str, command: list[str], gateway: "Gateway", env: dict | None = None):
        self.name, self.command, self.gw, self.env = name, command, gateway, env
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self.closed = asyncio.Event()

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                         stderr=None, limit=LIMIT, env={**os.environ, **(self.env or {})})
        self._task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("upstream %s sent non-JSON; dropped", self.name)
                    continue
                if isinstance(msg, dict) and "method" not in msg and str(msg.get("id", "")).startswith("panel-"):
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue
                await self.gw.on_upstream_message(self, msg)
        except (asyncio.LimitOverrunError, ValueError) as e:
            log.error("upstream %s sent an oversized line: %s", self.name, e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("upstream closed"))
            self._pending.clear()
            self.closed.set()
            await self.gw.on_upstream_closed(self)

    async def send(self, msg: dict):
        if self.proc is None or self.proc.stdin.is_closing():
            raise ConnectionError("upstream is not running")
        self.proc.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None, timeout: float = UPSTREAM_TIMEOUT) -> dict:
        rid = f"panel-{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s") from e

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass


class Gateway:
    def __init__(self, fw: Firewall, upstreams: list[McpUpstream], native: list | None = None, session: str = "default",
                 up_connectors: dict | None = None):
        self.fw, self.upstreams, self.native, self.session = fw, upstreams, native or [], session
        self.passthrough = len(upstreams) == 1 and not self.native
        self.multi = len(upstreams) > 1
        self.up_conn: dict[str, McpUpstreamConnector] = up_connectors or {}
        self.exposed: dict[str, tuple[str, str]] = {}      # exposed tool name -> (kind, internal tool name)
        self._send_lock = asyncio.Lock()
        self._out = None
        self._tasks: dict = {}
        self._cancelled: set = set()
        self._methods: dict = {}
        self._srv_ids: dict[str, tuple[McpUpstream, object]] = {}
        self._pool = ThreadPoolExecutor(max_workers=16)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self.exit_code = 0

    # ------------------------------------------------------------------ output to the client
    async def send_client(self, msg: dict):
        async with self._send_lock:
            await self._out(msg)

    # ------------------------------------------------------------------ upstream -> client
    async def on_upstream_message(self, up: McpUpstream, msg):
        if not isinstance(msg, dict):
            return
        if self.passthrough:
            if "method" not in msg and msg.get("id") in self._methods:
                if self._methods.pop(msg["id"], None) == "tools/list":
                    self._observe_tools(up, msg)
            await self.send_client(msg)
            return
        if "method" in msg and "id" in msg:      # server -> client request: make the id unique across upstreams
            nid = f"{self.upstreams.index(up)}:{msg['id']}"
            self._srv_ids[nid] = (up, msg["id"])
            await self.send_client({**msg, "id": nid})
        elif "method" in msg:                    # notification
            await self.send_client(msg)

    async def on_upstream_closed(self, up: McpUpstream):
        if self.passthrough:
            self._stop.set()

    def _observe_tools(self, up, msg):
        tools = ((msg.get("result") or {}).get("tools")) if isinstance(msg.get("result"), dict) else None
        if isinstance(tools, list):
            conn = self.up_conn.get(up.name)
            if conn is not None:
                conn.known.update(t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str))

    # ------------------------------------------------------------------ client -> gateway
    async def handle_client_line(self, line: bytes):
        try:
            msg = json.loads(line)
        except ValueError:
            await self.send_client(jerr(None, -32700, "Panel could not parse that message (invalid JSON); nothing was forwarded"))
            return
        if not isinstance(msg, dict):
            await self.send_client(jerr(None, -32600, "Panel does not accept JSON-RPC batches or non-object messages"))
            return
        method, mid = msg.get("method"), msg.get("id")
        if method is None:                                  # a response from the client to a server->client request
            await self._client_response(msg)
            return
        if not isinstance(method, str):
            await self.send_client(jerr(mid, -32600, "invalid method"))
            return
        if method == "tools/call":
            if mid is None:
                return                                      # a tools/call "notification" is invalid; nothing may run
            t = asyncio.create_task(self._tools_call(msg))
            self._tasks[mid] = t
            t.add_done_callback(lambda _t, m=mid: self._tasks.pop(m, None))
            return
        if method == "notifications/cancelled" and isinstance((msg.get("params") or {}), dict):
            rid = msg["params"].get("requestId")
            if rid in self._tasks:
                self._cancelled.add(rid)
        if self.passthrough:
            if mid is not None:
                self._methods[mid] = method
            await self._relay_up(self.upstreams[0], msg)
            return
        await self._synth(msg)

    async def _relay_up(self, up: McpUpstream, msg: dict):
        try:
            await up.send(msg)
        except (ConnectionError, OSError):
            if "id" in msg:
                await self.send_client(jerr(msg["id"], -32603, "the upstream server is not running"))

    async def _client_response(self, msg: dict):
        if self.passthrough:
            await self._relay_up(self.upstreams[0], msg)
            return
        hit = self._srv_ids.pop(msg.get("id"), None)
        if hit:
            up, orig = hit
            await self._relay_up(up, {**msg, "id": orig})

    # ------------------------------------------------------------------ synthesised mode
    async def _synth(self, msg: dict):
        method, mid = msg["method"], msg.get("id")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_FALLBACK)
            caps = {}
            for up in self.upstreams:
                try:
                    r = await up.request("initialize", msg.get("params"))
                    caps.update((r.get("result") or {}).get("capabilities") or {})
                    await up.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                except (TimeoutError, ConnectionError) as e:
                    log.error("upstream %s failed to initialise: %s", up.name, e)
            caps["tools"] = {"listChanged": False}
            for k in ("resources", "prompts", "completions", "logging"):
                caps.pop(k, None)                          # Panel only fronts tools in this mode
            await self.send_client({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": ver, "capabilities": caps,
                "serverInfo": {"name": "panel-firewall", "version": "0.1"},
                "instructions": "Every tool call is checked by Panel's policy. Blocked or held calls say why; held calls "
                                "run only after a human approves them out-of-band."}})
        elif method == "tools/list":
            await self.send_client({"jsonrpc": "2.0", "id": mid, "result": {"tools": await self._all_tools()}})
        elif method == "ping":
            await self.send_client({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            await self.send_client(jerr(mid, -32601, f"Panel (multi-source mode) does not support '{method}'"))

    async def _all_tools(self) -> list[dict]:
        tools, self.exposed = [], {}
        for c in self.native:
            for a in c.describe_actions():
                name = a.name.replace(".", "_")
                self.exposed[name] = ("native", a.name)
                tools.append({"name": name, "description": a.description, "inputSchema": a.input_schema(),
                              "annotations": {"readOnlyHint": a.risk == "read", "destructiveHint": a.risk == "destructive"}})
        for up in self.upstreams:
            cursor = None
            while True:
                try:
                    r = await up.request("tools/list", {"cursor": cursor} if cursor else {})
                except (TimeoutError, ConnectionError):
                    break
                res = r.get("result") or {}
                for t in res.get("tools", []):
                    if not isinstance(t, dict) or not isinstance(t.get("name"), str):
                        continue
                    exposed = f"{up.name}__{t['name']}" if self.multi else t["name"]
                    self.exposed[exposed] = (up.name, exposed)
                    self.up_conn[up.name].known.add(exposed)
                    tools.append({**t, "name": exposed})
                cursor = res.get("nextCursor")
                if not cursor:
                    break
        return tools

    # ------------------------------------------------------------------ tools/call
    async def _tools_call(self, msg: dict):
        mid = msg["id"]
        try:
            result = await self._tools_call_inner(msg)
        except Exception as e:  # fail closed: never crash, never pass through
            log.exception("gateway error")
            result = text_result(f"BLOCKED by Panel: internal error ({type(e).__name__}); the call was not executed.", True)
        if mid in self._cancelled:
            self._cancelled.discard(mid)
            return
        await self.send_client({"jsonrpc": "2.0", "id": mid, "result": result})

    async def _tools_call_inner(self, msg: dict) -> dict:
        p = msg.get("params")
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"]:
            return text_result("BLOCKED by Panel: malformed tools/call (params.name must be a string). Nothing was executed.", True)
        args = p.get("arguments", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return text_result("BLOCKED by Panel: tool arguments must be a JSON object. Nothing was executed.", True)
        meta = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
        name = p["name"]
        if not self.passthrough and name not in self.exposed:
            await self._all_tools()                      # the client may not have listed first
        internal = self.exposed.get(name, ("", name))[1] if not self.passthrough else name
        idem = meta.get("panel/idempotency_key") or meta.get("idempotency_key")
        action = Action(internal, args, self.session, idem if isinstance(idem, str) and idem else None,
                        meta={k: meta[k] for k in ("progressToken",) if k in meta})   # consent is NEVER read from _meta
        outcome = await asyncio.get_running_loop().run_in_executor(self._pool, self.fw.handle, action)
        return explain(outcome)

    def call_upstream_blocking(self, up: McpUpstream, tool: str, args: dict, meta: dict) -> dict:
        """Called from a worker thread by McpUpstreamConnector.execute."""
        params = {"name": tool, "arguments": args, **({"_meta": meta} if meta else {})}
        fut = asyncio.run_coroutine_threadsafe(up.request("tools/call", params), self._loop)
        try:
            return fut.result(UPSTREAM_TIMEOUT + 5)
        except TimeoutError:
            raise
        except Exception as e:
            if isinstance(e, (ConnectionError, OSError)):
                raise ConnectionError(str(e))
            if type(e).__name__ == "CancelledError":
                raise ConnectionError("cancelled")
            raise

    # ------------------------------------------------------------------ lifecycle
    async def run(self, read_line, write_msg):
        self._loop, self._out = asyncio.get_running_loop(), write_msg
        for up in self.upstreams:
            await up.start()
        reader_done = asyncio.create_task(self._pump(read_line))
        stopper = asyncio.create_task(self._stop.wait())
        await asyncio.wait({reader_done, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if self._stop.is_set() and not reader_done.done():
            self.exit_code = 1
        for t in list(self._tasks.values()):
            t.cancel()
        for up in self.upstreams:
            await up.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        return self.exit_code

    async def _pump(self, read_line):
        while True:
            line = await read_line()
            if not line:
                return
            if line.strip():
                await self.handle_client_line(line)


# ---------------------------------------------------------------------- stdio plumbing and entry points
async def stdio_streams():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer

    async def read_line():
        try:
            return await reader.readline()
        except (asyncio.LimitOverrunError, ValueError):
            return b"{"                                  # too long -> parse error response, not a crash

    async def write_msg(msg: dict):
        out.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        out.flush()
    return read_line, write_msg


def _setup(a, upstream_cmds: list[tuple[str, list[str]]], with_native: bool):
    fw = build_firewall(a.policy, a.ledger, a.key, connectors=[])
    native = native_connectors(fw.policy) if with_native else []
    gw = Gateway(fw, [], native, getattr(a, "session", None) or fw.policy.raw.get("session", "default"))
    for name, cmd in upstream_cmds:
        up = McpUpstream(name, cmd, gw)
        gw.upstreams.append(up)
        conn = McpUpstreamConnector(fw.policy, lambda t, args, meta, up=up: gw.call_upstream_blocking(up, t, args, meta),
                                    prefix=f"{name}__" if len(upstream_cmds) > 1 else "")
        gw.up_conn[name] = conn
        fw.connectors.append(conn)
    fw.connectors[:0] = native                      # native connectors first: they own their exact names
    gw.passthrough = len(gw.upstreams) == 1 and not native
    gw.multi = len(gw.upstreams) > 1
    return gw


def _run(gw: Gateway) -> int:
    async def main():
        rl, wm = await stdio_streams()
        return await gw.run(rl, wm)
    return asyncio.run(main())


def run_proxy(a) -> int:
    cmd = [x for x in a.upstream]
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    from tenthlook.panel.firewall.policy import load_policy
    pol = load_policy(a.policy)
    if pol.raw.get("reviewed") is False:
        print("panel: WARNING: this policy says `reviewed: false` (a generated starter). Defaults are conservative, but have "
              "a human review it.", file=sys.stderr)
    ups = [("up", cmd)] if cmd else [(c["name"], c["command"]) for c in pol.raw.get("connectors", []) if c.get("type") == "upstream"]
    if not ups:
        raise FirewallError("no upstream: pass `-- <command>` or list `type: upstream` connectors in the policy")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="panel: %(message)s")
    return _run(_setup(a, ups, with_native=True))


def run_serve(a) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="panel: %(message)s")
    return _run(_setup(a, [], with_native=True))

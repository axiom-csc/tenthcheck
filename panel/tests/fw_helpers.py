"""Shared builders for firewall tests."""
import hashlib
import tempfile
from pathlib import Path

import yaml

from tenthlook.panel.firewall.connector import Connector
from tenthlook.panel.firewall.engine import Firewall
from tenthlook.panel.firewall.ledger import Ledger
from tenthlook.panel.firewall.policy import parse_policy
from tenthlook.panel.firewall.receipts import Signer, generate_key
from tenthlook.panel.firewall.types import Action, ActionSchema, Result

from tenthlook.core import crypto as _core_crypto  # noqa: E402

# The two "keygen refuses a path inside the repo" tests need the code to live in a git checkout; they skip cleanly from an sdist or any copy without .git.
IN_GIT_CHECKOUT = _core_crypto.inside_git_worktree(Path(__file__).resolve().parent)
NOT_IN_GIT_REASON = "not inside a git checkout (no .git above the tests): the refuse-keys-inside-a-repo path cannot be exercised here"

_KEYDIR = Path(tempfile.mkdtemp(prefix="panel-test-key-"))  # outside any git work tree (system temp dir)
_KEY = generate_key(_KEYDIR / "k.pem")


def signer() -> Signer:
    return Signer.load(_KEY)


def pubkey_path() -> str:
    return str(_KEY) + ".pub"


class FakeConnector(Connector):
    """Counts executions; amount-bearing tool `fake.pay`, read tool `fake.read`."""
    name = "fake"

    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def describe_actions(self):
        return [ActionSchema("fake.pay", "pay", {"amount": {"type": "number"}}, ("amount",), "write", ("paid",)),
                ActionSchema("fake.read", "read", {}, (), "read")]

    def execute(self, action, mode):
        if mode == "dry_run":
            return Result(True, None, {"would": action.tool}, simulated=True)
        self.calls.append(action)
        if self.fail_times:
            self.fail_times -= 1
            return Result(False, error="upstream 500")
        return Result(True, f"id-{len(self.calls)}", {"paid": action.args.get("amount")})


POLICY_SHA = hashlib.sha256(b"sha-of-test-policy").hexdigest()   # the schema requires a real SHA-256


def policy(actions, mode="auto", **top):
    return parse_policy({"version": "t1", "mode": mode, "actions": actions, **top}, POLICY_SHA)


def make_fw(actions=None, mode="auto", connectors=None, clock=None, **kw):
    actions = actions or {"fake.pay": {"max_amount": 100, "consent_over_amount": 50}, "fake.read": {"risk": "read"}}
    conns = connectors if connectors is not None else [FakeConnector()]
    led = Ledger(":memory:", clock=clock) if clock else Ledger(":memory:")
    return Firewall(policy(actions, mode), led, signer(), conns, clock=clock or __import__("time").time, **kw)


def act(tool="fake.pay", conv="c1", key=None, **args):
    return Action(tool, args, conv, key)


# ------------------------------------------------------------------------------------------- MCP client for gateway tests
import asyncio
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]


class McpClient:
    """Speaks MCP stdio to a subprocess (Panel). Collects notifications and server->client requests."""

    def __init__(self, cmd, env=None, cwd=None):
        self.cmd, self.env, self.cwd = cmd, env, cwd or ROOT
        self.notifications, self.server_requests, self._futs, self._n = [], [], {}, 0
        self.on_server_request = None

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(*self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL, cwd=self.cwd, env={**os.environ, **(self.env or {})})
        self._rd = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *a):
        try:
            self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 5)
        except Exception:
            self.proc.kill()
        self._rd.cancel()

    async def _read(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                for f in self._futs.values():
                    if not f.done():
                        f.set_exception(ConnectionError("closed"))
                return
            m = json.loads(line)
            if "method" in m and "id" in m:
                self.server_requests.append(m)
                if self.on_server_request:
                    await self.send(self.on_server_request(m))
            elif "method" in m:
                self.notifications.append(m)
            elif m.get("id") in self._futs:
                self._futs[m["id"]].set_result(m)

    async def send(self, m):
        self.proc.stdin.write((json.dumps(m) + "\n").encode()); await self.proc.stdin.drain()

    async def send_raw(self, b: bytes):
        self.proc.stdin.write(b); await self.proc.stdin.drain()

    async def request(self, method, params=None, timeout=20, id_=None):
        self._n += 1
        rid = id_ if id_ is not None else self._n
        self._futs[rid] = asyncio.get_running_loop().create_future()
        await self.send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
        return await asyncio.wait_for(self._futs[rid], timeout)

    async def call(self, name, args=None, meta=None, timeout=20, **kw):
        p = {"name": name, "arguments": args or {}}
        if meta:
            p["_meta"] = meta
        return await self.request("tools/call", p, timeout=timeout, **kw)

    async def init(self):
        r = await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r


def text_of(resp) -> str:
    return " ".join(c.get("text", "") for c in resp["result"]["content"])


def write_policy(tmp_path, actions, **top):
    import yaml
    pol = tmp_path / "policy.yaml"
    pol.write_text(yaml.safe_dump({"version": "gw1", "mode": "auto", "actions": actions, **top}))
    return pol


def panel_cmd(pol, ledger, key, *upstream, extra=()):
    return [sys.executable, "-m", "tenthlook.panel.firewall", "proxy", "--policy", str(pol), "--ledger", str(ledger), "--key", str(key),
            *extra, "--", *upstream]

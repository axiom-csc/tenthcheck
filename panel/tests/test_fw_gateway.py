"""MCP gateway against a deliberately awkward fake server: relay correctness, consent, idempotency, fail-closed."""
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

from tenthlook.panel.firewall.ledger import Ledger
from tenthlook.panel.firewall.receipts import generate_key
from tests.fw_helpers import McpClient, panel_cmd, text_of, write_policy

FAKE = str(Path(__file__).parent / "support" / "fake_mcp_server.py")
ACTIONS = {"echo": {"risk": "read"}, "read_thing": {"risk": "read"},
           "pay": {"max_amount": 100, "consent_over_amount": 50},
           "slow": {"risk": "read"}, "ask_client": {"risk": "read"}, "progress": {"risk": "read"},
           "boom": {}, "toolerr": {}, "hang": {}}


@pytest.fixture
def rig(tmp_path):
    key = generate_key(tmp_path / "k.pem")
    pol = write_policy(tmp_path, ACTIONS)
    ledger = tmp_path / "l.sqlite"
    cnt = tmp_path / "count.txt"
    cmd = panel_cmd(pol, ledger, key, sys.executable, FAKE)
    return dict(cmd=cmd, ledger=ledger, count=cnt, env={"FAKE_MCP_COUNT_FILE": str(cnt), "PANEL_UPSTREAM_TIMEOUT": "3"}, tmp=tmp_path, pol=pol, key=key)


def upstream_calls(rig, tool=None):
    p = rig["count"]
    lines = p.read_text().split() if p.exists() else []
    return [l for l in lines if tool is None or l == tool]


def run(coro):
    return asyncio.run(coro)


def test_passthrough_initialize_list_and_allowed_call(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            init = await c.init()
            assert init["result"]["serverInfo"]["name"] == "fake"             # transparent: the upstream's own answer
            tl = await c.request("tools/list")
            assert {t["name"] for t in tl["result"]["tools"]} >= {"echo", "pay"}
            r = await c.call("echo", {"x": 1})
            assert r["result"]["isError"] is False and "echo ok" in text_of(r)
            assert r["result"]["_meta"]["panel"]["status"] == "executed"
            assert (await c.request("ping"))["result"] == {}
    run(go())


def test_block_has_readable_reason_and_nothing_reaches_upstream(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            r = await c.call("pay", {"amount": 100.01})
            assert r["result"]["isError"] and "BLOCKED" in text_of(r) and "exceeds the maximum" in text_of(r)
            assert "Do not retry the same call" in text_of(r)
            r = await c.call("rm_rf_everything", {})
            assert r["result"]["isError"] and "unknown action" in text_of(r)
    run(go())
    assert upstream_calls(rig, "pay") == [] and upstream_calls(rig, "rm_rf_everything") == []


def test_consent_held_then_approved_out_of_band_then_runs_exactly_once(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            r = await c.call("pay", {"amount": 75})
            assert r["result"]["isError"] and "HELD" in text_of(r)
            aid = r["result"]["_meta"]["panel"]["approval_id"]
            assert upstream_calls(rig, "pay") == []
            ok, _ = Ledger(rig["ledger"]).approve(aid, "reviewer")          # the human, in another process/connection
            assert ok
            r = await c.call("pay", {"amount": 75})
            assert not r["result"]["isError"] and "pay ok" in text_of(r)
            r = await c.call("pay", {"amount": 75})                         # repeat: replayed, not re-executed
            assert "already been executed" in text_of(r) and r["result"]["_meta"]["panel"]["replayed"]
    run(go())
    assert upstream_calls(rig, "pay") == ["pay"]


def test_agent_supplied_consent_fields_are_ignored(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            r = await c.call("pay", {"amount": 75, "consent": True, "approved": True, "approved_by": "me"},
                             meta={"consent": True, "approved": True, "panel/approved": True, "approval_id": "anything"})
            assert "HELD" in text_of(r)
            # even guessing the right approval id in the call does nothing
            aid = r["result"]["_meta"]["panel"]["approval_id"]
            r = await c.call("pay", {"amount": 75, "consent": True, "approval_id": aid}, meta={"approval_id": aid, "approved": True})
            assert "HELD" in text_of(r)
    run(go())
    assert upstream_calls(rig, "pay") == []


def test_explicit_idempotency_key_via_meta(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            a = await c.call("pay", {"amount": 5}, meta={"panel/idempotency_key": "order-77"})
            b = await c.call("pay", {"amount": 5}, meta={"panel/idempotency_key": "order-77"})
            d = await c.call("pay", {"amount": 6}, meta={"panel/idempotency_key": "order-77"})
            assert not a["result"]["isError"] and b["result"]["_meta"]["panel"]["replayed"]
            assert d["result"]["isError"] and "different action" in text_of(d)
    run(go())
    assert upstream_calls(rig, "pay") == ["pay"]


def test_concurrent_calls_and_out_of_order_responses(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            slow = asyncio.create_task(c.call("slow", {"ms": 800}, id_="slow-1"))
            await asyncio.sleep(0.05)
            fast = await c.call("echo", {"n": 1}, id_="fast-1")
            assert not slow.done()                                           # the slow one is still running
            assert "echo ok" in text_of(fast)
            assert "slow ok" in text_of(await slow)
    run(go())


def test_server_to_client_request_and_notifications_are_relayed(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            c.on_server_request = lambda m: {"jsonrpc": "2.0", "id": m["id"], "result": {"roots": [{"uri": "file:///x"}]}}
            await c.init()
            r = await c.call("ask_client")
            assert c.server_requests[0]["method"] == "roots/list" and c.server_requests[0]["id"] == "srv-1"
            assert "file:///x" in text_of(r)
            r = await c.call("progress", meta={"progressToken": "tok-9"})
            assert not r["result"]["isError"]
            await asyncio.sleep(0.1)
            assert any(n["method"] == "notifications/progress" and n["params"]["progressToken"] == "tok-9" for n in c.notifications)
    run(go())


def test_malformed_input_fails_closed_and_does_not_crash(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            await c.send_raw(b"this is not json\n")
            await c.send_raw(b'[{"jsonrpc":"2.0","id":1,"method":"tools/call"}]\n')       # batch
            await asyncio.sleep(0.2)
            for bad in ({"name": 5}, {"arguments": {}}, "str", {"name": "pay", "arguments": [1]}, {"name": "pay", "arguments": "x"}):
                r = await c.request("tools/call", bad)
                assert r["result"]["isError"] and "BLOCKED" in text_of(r), bad
            assert (await c.request("ping"))["result"] == {}                  # still alive
    run(go())
    assert upstream_calls(rig, "pay") == []


def test_upstream_errors_are_reported_and_not_marked_executed(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            r = await c.call("boom")
            assert r["result"]["isError"] and "kaboom" in text_of(r)
            r = await c.call("toolerr")
            assert r["result"]["isError"] and "bad thing" in text_of(r)
    run(go())
    ev = Ledger(rig["ledger"]).events(limit=1000)
    assert not [e for e in ev if e["event"] == "executed"] and len([e for e in ev if e["event"] == "failed"]) == 2


def test_upstream_timeout_means_unknown_outcome_and_no_automatic_retry(rig):
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            r = await c.call("hang", timeout=15)
            assert r["result"]["isError"] and "unknown" in text_of(r)
            r = await c.call("hang", timeout=15)                             # retry: refused, a human must look
            assert "already running" in text_of(r) or "unknown" in text_of(r)
    run(go())
    assert upstream_calls(rig, "hang") == ["hang"]


def test_receipts_are_written_and_verify(rig):
    from tenthlook.panel.firewall.receipts import load_public_key, verify_receipt
    async def go():
        async with McpClient(rig["cmd"], rig["env"]) as c:
            await c.init()
            await c.call("pay", {"amount": 10}); await c.call("pay", {"amount": 500})
    run(go())
    rs = Ledger(rig["ledger"]).receipts()
    assert {r["verdict"] for r in rs} == {"ALLOWED", "BLOCKED"}
    pub = load_public_key(str(rig["key"]) + ".pub")
    assert all(verify_receipt(r, pub) == [] for r in rs)


def test_multi_upstream_namespacing_and_routing(tmp_path):
    key = generate_key(tmp_path / "k.pem")
    pol = write_policy(tmp_path, {"a__pay": {"max_amount": 10}, "b__pay": {"max_amount": 1000}, "a__echo": {"risk": "read"}},
                       connectors=[{"type": "upstream", "name": "a", "command": [sys.executable, FAKE]},
                                   {"type": "upstream", "name": "b", "command": [sys.executable, FAKE]}])
    cmd = [sys.executable, "-m", "tenthlook.panel.firewall", "proxy", "--policy", str(pol), "--ledger", str(tmp_path / "l.sqlite"), "--key", str(key)]
    async def go():
        async with McpClient(cmd, {"FAKE_MCP_COUNT_FILE": str(tmp_path / "c.txt")}) as c:
            await c.init()
            names = {t["name"] for t in (await c.request("tools/list"))["result"]["tools"]}
            assert {"a__pay", "b__pay", "a__echo"} <= names
            assert (await c.call("a__pay", {"amount": 50}))["result"]["isError"]                 # a's cap is 10
            assert not (await c.call("b__pay", {"amount": 50}))["result"]["isError"]             # b's cap is 1000
            assert "echo ok" in text_of(await c.call("a__echo"))
            assert (await c.call("c__pay", {"amount": 1}))["result"]["isError"]                  # unknown upstream
    run(go())


def test_serve_native_sqlite_connector_over_mcp(tmp_path):
    import sqlite3
    db = tmp_path / "app.db"
    con = sqlite3.connect(db); con.execute("CREATE TABLE t(id INTEGER, v TEXT)")
    con.executemany("INSERT INTO t VALUES (?,?)", [(i, "x") for i in range(6)]); con.commit(); con.close()
    key = generate_key(tmp_path / "k.pem")
    pol = write_policy(tmp_path, {"sql.query": {"max_rows": 3, "consent_over_rows": 1}}, connectors=[{"type": "sqlite", "path": "app.db"}])
    cmd = [sys.executable, "-m", "tenthlook.panel.firewall", "serve", "--policy", str(pol), "--ledger", str(tmp_path / "l.sqlite"), "--key", str(key)]
    async def go():
        async with McpClient(cmd) as c:
            init = await c.init()
            assert init["result"]["serverInfo"]["name"] == "panel-firewall" and "tools" in init["result"]["capabilities"]
            tools = (await c.request("tools/list"))["result"]["tools"]
            assert [t["name"] for t in tools] == ["sql_query"] and tools[0]["inputSchema"]["required"] == ["sql"]
            r = await c.call("sql_query", {"sql": "SELECT COUNT(*) FROM t"})
            assert not r["result"]["isError"] and r["result"]["structuredContent"]["rows"] == [[6]]
            r = await c.call("sql_query", {"sql": "DROP TABLE t"})
            assert r["result"]["isError"] and "DROP" in text_of(r)
            r = await c.call("sql_query", {"sql": "DELETE FROM t WHERE id < 5"})                # 5 rows > cap 3
            assert r["result"]["isError"] and "maximum" in text_of(r)
            held = await c.call("sql_query", {"sql": "DELETE FROM t WHERE id < 2"})              # 2 rows: needs consent
            assert "HELD" in text_of(held)
            assert Ledger(tmp_path / "l.sqlite").approve(held["result"]["_meta"]["panel"]["approval_id"], "me")[0]
            assert not (await c.call("sql_query", {"sql": "DELETE FROM t WHERE id < 2"}))["result"]["isError"]
            assert (await c.request("ping"))["result"] == {}
            assert (await c.request("resources/list"))["error"]["code"] == -32601
    run(go())
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone() == (4,)

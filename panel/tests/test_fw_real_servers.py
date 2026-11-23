"""Panel in front of two REAL reference MCP servers: @modelcontextprotocol/server-filesystem (Node) and mcp-server-sqlite (Python).
Skipped only if the server is not installed (see README: npm install in .mcp-servers/, pip install mcp-server-sqlite)."""
import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from tenthlook.panel.firewall.init import classify, run_init
from tenthlook.panel.firewall.ledger import Ledger
from tenthlook.panel.firewall.policy import parse_policy
from tenthlook.panel.firewall.receipts import generate_key
from tests.fw_helpers import McpClient, panel_cmd, text_of, write_policy

ROOT = Path(__file__).resolve().parents[1]
FS_BIN = ROOT / ".mcp-servers/node_modules/.bin/mcp-server-filesystem"
SQLITE_BIN = ROOT / ".mcp-servers/sqlite-venv/bin/mcp-server-sqlite"
need_fs = pytest.mark.skipif(not FS_BIN.exists() or not shutil.which("node"), reason="filesystem MCP server not installed")
need_sqlite = pytest.mark.skipif(not SQLITE_BIN.exists(), reason="mcp-server-sqlite not installed")
run = asyncio.run


# ---------------------------------------------------------------------------------------------------- filesystem server
@pytest.fixture
def fs(tmp_path):
    data = (tmp_path / "data").resolve(); data.mkdir()
    (data / "notes.txt").write_text("hello"); (data / ".env").write_text("SECRET=1"); (data / "sub").mkdir()
    outside = (tmp_path / "outside").resolve(); outside.mkdir(); (outside / "x.txt").write_text("outside")
    (data / "link").symlink_to(outside)                      # symlink pointing out of the allowed folder
    key = generate_key(tmp_path / "k.pem")
    pol = write_policy(tmp_path, {
        "read_text_file": {"risk": "read", "path_args": ["path"], "allowed_roots": [str(data)], "protected": {"path": ["*/.env"]}},
        "read_file": {"risk": "read", "path_args": ["path"], "allowed_roots": [str(data)], "protected": {"path": ["*/.env"]}},
        "list_directory": {"risk": "read", "path_args": ["path"], "allowed_roots": [str(data)]},
        "write_file": {"risk": "write", "path_args": ["path"], "allowed_roots": [str(data)], "protected": {"path": ["*/.env"]},
                       "consent_if": [{"field": "path", "op": "matches", "value": r"notes\.txt$"}]},
        "move_file": {"risk": "write", "path_args": ["source", "destination"], "allowed_roots": [str(data)], "allowed": False},
    })
    cmd = panel_cmd(pol, tmp_path / "l.sqlite", key, str(FS_BIN), str(data))
    return dict(cmd=cmd, data=data, outside=outside, ledger=tmp_path / "l.sqlite", tmp=tmp_path, pol=pol, key=key)


@need_fs
def test_filesystem_server_through_panel(fs):
    async def go():
        async with McpClient(fs["cmd"]) as c:
            init = await c.init()
            assert "filesystem" in json.dumps(init["result"]["serverInfo"]).lower()
            names = {t["name"] for t in (await c.request("tools/list"))["result"]["tools"]}
            assert {"read_text_file", "write_file", "list_directory"} <= names
            ok = await c.call("read_text_file", {"path": str(fs["data"] / "notes.txt")})
            assert not ok["result"].get("isError") and "hello" in text_of(ok)
            ls = await c.call("list_directory", {"path": str(fs["data"])})
            assert not ls["result"].get("isError") and "notes.txt" in text_of(ls)
            # allowed write executes on the real server
            w = await c.call("write_file", {"path": str(fs["data"] / "new.txt"), "content": "made by agent"})
            assert not w["result"].get("isError") and (fs["data"] / "new.txt").read_text() == "made by agent"
            # protected file, traversal, symlink escape, relative path, unlisted tool, disallowed tool: all blocked, nothing changes
            for tool, args in [("read_text_file", {"path": str(fs["data"] / ".env")}),
                               ("read_text_file", {"path": str(fs["data"] / "sub" / ".." / ".env")}),
                               ("read_text_file", {"path": str(fs["data"] / ".." / "outside" / "x.txt")}),
                               ("read_text_file", {"path": str(fs["data"] / "link" / "x.txt")}),
                               ("read_text_file", {"path": "notes.txt"}),
                               ("write_file", {"path": str(fs["data"] / ".env"), "content": "pwned"}),
                               ("write_file", {"path": str(fs["outside"] / "evil.txt"), "content": "pwned"}),
                               ("write_file", {"path": str(fs["data"] / "link" / "evil.txt"), "content": "pwned"}),
                               ("move_file", {"source": str(fs["data"] / "notes.txt"), "destination": str(fs["data"] / "n2.txt")}),
                               ("edit_file", {"path": str(fs["data"] / "notes.txt"), "edits": []}),
                               ("delete_everything", {})]:
                r = await c.call(tool, args)
                assert r["result"].get("isError") and "BLOCKED" in text_of(r), (tool, args, text_of(r))
            held = await c.call("write_file", {"path": str(fs["data"] / "notes.txt"), "content": "changed"})   # consent_if rule
            assert "HELD" in text_of(held) and (fs["data"] / "notes.txt").read_text() == "hello"
    run(go())
    assert (fs["data"] / ".env").read_text() == "SECRET=1" and not (fs["outside"] / "evil.txt").exists()
    assert not (fs["data"] / "n2.txt").exists()


@need_fs
def test_filesystem_consent_roundtrip(fs):
    async def go():
        async with McpClient(fs["cmd"]) as c:
            await c.init()
            args = {"path": str(fs["data"] / "notes.txt"), "content": "approved edit"}
            held = await c.call("write_file", args)
            aid = held["result"]["_meta"]["panel"]["approval_id"]
            assert Ledger(fs["ledger"]).approve(aid, "human")[0]
            done = await c.call("write_file", args)
            assert not done["result"].get("isError")
    run(go())
    assert (fs["data"] / "notes.txt").read_text() == "approved edit"


@need_fs
def test_init_against_real_filesystem_server(fs, tmp_path):
    out = tmp_path / "starter.yaml"
    class A: upstream = ["--", str(FS_BIN), str(fs["data"])]; out_ = None
    a = A(); a.out = str(out); a.force = False
    assert run_init(a) == 0
    doc = yaml.safe_load(out.read_text())
    acts = doc["actions"]
    assert doc["reviewed"] is False and doc["mode"] == "approval"
    assert acts["read_text_file"]["risk"] == "read" and acts["list_directory"]["risk"] == "read"
    # the real server annotates write_file destructiveHint=true (it overwrites), so the starter blocks it until a human decides
    assert acts["write_file"]["risk"] in ("write", "destructive") and acts["write_file"]["path_args"] == ["path"]
    if acts["write_file"]["risk"] == "destructive":
        assert acts["write_file"]["allowed"] is False
    assert acts["write_file"]["allowed_roots"] == ["/CHANGE/ME"]                    # blocks until a human sets it
    assert acts["move_file"]["risk"] in ("write", "destructive")
    parse_policy(doc)                                                               # the starter is a valid policy
    assert all(r.get("allowed") is False or r.get("risk") == "read" or r.get("risk") == "write" for r in acts.values())


# ---------------------------------------------------------------------------------------------------- sqlite server
@pytest.fixture
def sq(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE secrets(id INTEGER PRIMARY KEY, v TEXT);"
                      "INSERT INTO users(name) VALUES ('a'),('b'),('c'),('d'); INSERT INTO secrets(v) VALUES ('s');")
    con.commit(); con.close()
    key = generate_key(tmp_path / "k.pem")
    pol = write_policy(tmp_path, {
        "read_query": {"risk": "read", "sql_arg": "query"},
        "write_query": {"risk": "write", "sql_arg": "query", "max_rows": 3, "consent_over_rows": 1},
        "list_tables": {"risk": "read"}, "describe_table": {"risk": "read"},
        "create_table": {"allowed": False},
    }, database="app.db", sql={"protected_tables": ["secrets"]})
    cmd = panel_cmd(pol, tmp_path / "l.sqlite", key, str(SQLITE_BIN), "--db-path", str(db))
    return dict(cmd=cmd, db=db, ledger=tmp_path / "l.sqlite")


def rows(db, q):
    con = sqlite3.connect(db); r = con.execute(q).fetchall(); con.close(); return r


@need_sqlite
def test_sqlite_server_through_panel(sq):
    async def go():
        async with McpClient(sq["cmd"]) as c:
            await c.init()
            assert "write_query" in {t["name"] for t in (await c.request("tools/list"))["result"]["tools"]}
            r = await c.call("read_query", {"query": "SELECT name FROM users ORDER BY id"})
            assert not r["result"].get("isError") and "'a'" in text_of(r)
            r = await c.call("write_query", {"query": "UPDATE users SET name='z' WHERE id=1"})        # 1 row: within consent threshold
            assert not r["result"].get("isError")
            for q in ["DROP TABLE users", "DELETE FROM users", "DELETE FROM users WHERE 1=1", "SELECT 1; DROP TABLE users",
                      "DELETE FROM users WHERE id > 0",                                          # 4 rows > cap 3
                      "UPDATE secrets SET v='x' WHERE id=1", "/* x */ drop /* y */ table users", "ALTER TABLE users ADD COLUMN z"]:
                r = await c.call("write_query", {"query": q})
                assert r["result"].get("isError") and "BLOCKED" in text_of(r), (q, text_of(r))
            r = await c.call("read_query", {"query": "SELECT * FROM secrets"})
            assert r["result"].get("isError") and "protected" in text_of(r)
            r = await c.call("write_query", {"query": "DELETE FROM users WHERE id IN (2,3)"})       # 2 rows: needs consent
            assert "HELD" in text_of(r)
            r = await c.call("create_table", {"query": "CREATE TABLE t(a)"})
            assert r["result"].get("isError")
    run(go())
    assert rows(sq["db"], "SELECT name FROM users ORDER BY id") == [("z",), ("b",), ("c",), ("d",)]
    assert rows(sq["db"], "SELECT v FROM secrets") == [("s",)]


@need_sqlite
def test_sqlite_consent_roundtrip_executes_once(sq):
    async def go():
        async with McpClient(sq["cmd"]) as c:
            await c.init()
            q = {"query": "DELETE FROM users WHERE id IN (2,3)"}
            held = await c.call("write_query", q)
            assert Ledger(sq["ledger"]).approve(held["result"]["_meta"]["panel"]["approval_id"], "dba")[0]
            assert not (await c.call("write_query", q))["result"].get("isError")
            assert "already been executed" in text_of(await c.call("write_query", q))
    run(go())
    assert rows(sq["db"], "SELECT id FROM users ORDER BY id") == [(1,), (4,)]


def test_classify_heuristics():
    c = lambda n, **k: classify({"name": n, **k})
    assert c("get_weather")["risk"] == "read" and c("listUsers")["risk"] == "read"
    assert c("delete_repo")["risk"] == "destructive" and c("drop_table")["risk"] == "destructive"
    assert c("create_issue")["risk"] == "write" and c("send_email")["risk"] == "write"
    assert c("refund_order", inputSchema={"properties": {"amount": {}}})["amount_field"] == "amount"
    assert c("frobnicate")["risk"] == "unknown"
    assert c("x", annotations={"readOnlyHint": True})["risk"] == "read"
    assert c("run_query", inputSchema={"properties": {"query": {}}})["sql_arg"] == "query"
    assert c("zap", annotations={"destructiveHint": True})["risk"] == "destructive"

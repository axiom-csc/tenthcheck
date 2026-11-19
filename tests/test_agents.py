"""tenthcheck agents: two simulated agent sessions editing one repository (warn only, never block)."""
import asyncio
import json
import multiprocessing as mp

import pytest

from tenthcheck import agents
from tenthcheck.cli import main

CORE = "def helper(x):\n    return x + 1\n\ndef add(a, b):\n    return a + helper(b)\n"
API = "from pkg.core import add\n\ndef calculate(x, y):\n    return add(x, y)\n"
OTHER = "def lonely():\n    return 0\n"


def _write(root, files):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path):
    # helper <- add <- calculate (callers); lonely is unrelated
    return _write(tmp_path / "repo", {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/api.py": API,
                                      "pkg/other.py": OTHER})


def edit_helper():
    return CORE.replace("x + 1", "x + 2")


def edit_add():
    return CORE.replace("a + helper(b)", "helper(a) + helper(b)")


def edit_calculate():
    return API.replace("add(x, y)", "add(y, x)")


def edit_lonely():
    return OTHER.replace("0", "1")


# ---------------------------------------------------------------- the core behaviour

def test_session_a_records_what_it_changed_and_alone_gets_no_warning(repo):
    res = agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    assert res == {"session": "A", "symbols": ["pkg.core.helper"], "recorded": True, "warnings": []}
    state = json.loads((repo / ".tenthcheck/agents.json").read_text())
    assert state["schema"] == "tenthcheck-agents/1"
    assert state["sessions"]["A"]["edits"]["pkg.core.helper"]["file"] == "pkg/core.py"
    assert (repo / ".tenthcheck/.gitignore").read_text().strip().endswith("*")


def test_second_session_editing_a_caller_is_warned(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    res = agents.record_edit(repo, "B", "pkg/api.py", edit_calculate())
    assert res["recorded"] is True and res["symbols"] == ["pkg.api.calculate"]
    [w] = res["warnings"]
    assert (w["symbol"], w["other_session"], w["their_edit"], w["relation"], w["hops"]) == (
        "pkg.api.calculate", "A", "pkg.core.helper", "caller", 2)
    assert w["message"] == "pkg.api.calculate is a caller of pkg.core.helper (2 hops), which session A edited"


def test_second_session_editing_a_callee_is_warned(repo):
    agents.record_edit(repo, "A", "pkg/api.py", edit_calculate())
    [w] = agents.record_edit(repo, "B", "pkg/core.py", edit_helper())["warnings"]
    assert (w["symbol"], w["relation"], w["hops"], w["their_edit"]) == ("pkg.core.helper", "callee", 2,
                                                                         "pkg.api.calculate")
    assert "is called by pkg.api.calculate" in w["message"]


def test_same_symbol_and_nearest_relation(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_add())
    res = agents.record_edit(repo, "B", "pkg/core.py", edit_add())
    assert [(w["symbol"], w["relation"], w["hops"]) for w in res["warnings"]] == [("pkg.core.add", "same", 0)]


def test_outside_the_radius_or_beyond_depth_is_silent(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    assert agents.record_edit(repo, "B", "pkg/other.py", edit_lonely())["warnings"] == []
    # calculate is 2 hops from helper: inside the default radius, outside depth 1
    assert agents.check(repo, "B", "pkg/api.py", edit_calculate(), depth=1)["warnings"] == []
    assert len(agents.check(repo, "B", "pkg/api.py", edit_calculate(), depth=2)["warnings"]) == 1


def test_a_session_is_never_warned_about_itself(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    assert agents.record_edit(repo, "A", "pkg/api.py", edit_calculate())["warnings"] == []


def test_warnings_are_symmetric_once_both_have_recorded(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    agents.record_edit(repo, "B", "pkg/api.py", edit_calculate())
    [w] = agents.check(repo, "A", symbols=["pkg.core.helper"])["warnings"]
    assert (w["other_session"], w["their_edit"], w["relation"]) == ("B", "pkg.api.calculate", "callee")


def test_others_active_is_the_cheap_precheck(repo):
    assert agents.others_active(repo, "B") is False  # no state file yet
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    assert agents.others_active(repo, "A") is False and agents.others_active(repo, "B") is True


def test_check_is_read_only(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    before = (repo / ".tenthcheck/agents.json").read_text()
    res = agents.check(repo, "B", "pkg/core.py", edit_add())
    assert res["recorded"] is False and res["warnings"][0]["relation"] == "caller"
    assert (repo / ".tenthcheck/agents.json").read_text() == before


def test_expired_and_ended_sessions_stop_warning(repo):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper(), now=1000.0)
    assert agents.check(repo, "B", symbols=["pkg.core.add"], now=1000.0 + 3600)["warnings"]
    assert agents.check(repo, "B", symbols=["pkg.core.add"], now=1000.0 + agents.DEFAULT_TTL_S + 1)["warnings"] == []
    assert agents.end_session(repo, "A") is True
    assert agents.check(repo, "B", symbols=["pkg.core.add"], now=1001.0)["warnings"] == []
    assert agents.end_session(repo, "A") is False


def test_after_the_write_with_a_snapshot_only_this_files_changes_count(repo):
    """The MCP flow: diff against the session's snapshot; another session's edit to another file isn't ours."""
    from tenthcheck.parser import parse_package
    snap = parse_package(str(repo), skip_syntax_errors=True)
    (repo / "pkg/api.py").write_text(edit_calculate())   # someone else's edit, other file
    (repo / "pkg/core.py").write_text(edit_helper())     # ours, already written
    assert agents.changed_symbols(repo, "pkg/core.py", edit_helper())[0] == []          # vs disk: nothing
    assert agents.changed_symbols(repo, "pkg/core.py", edit_helper(), before=snap)[0] == ["pkg.core.helper"]


def test_corrupt_state_file_is_replaced_not_fatal(repo):
    (repo / ".tenthcheck").mkdir()
    (repo / ".tenthcheck/agents.json").write_text("{not json")
    assert agents.record_edit(repo, "A", "pkg/core.py", edit_helper())["recorded"] is True
    assert "A" in json.loads((repo / ".tenthcheck/agents.json").read_text())["sessions"]


def test_bad_input_is_an_error_not_a_record(repo):
    with pytest.raises(ValueError):
        agents.record_edit(repo, "", "pkg/core.py", edit_helper())
    with pytest.raises(ValueError):
        agents.record_edit(repo, "A", "pkg/core.py", "def (:\n")
    assert not (repo / ".tenthcheck/agents.json").exists()


def _record_many(args):
    repo, sid, n = args
    for i in range(n):
        agents.record_edit(repo, sid, symbols=[f"pkg.core.{'helper' if i % 2 else 'add'}"])


def test_two_processes_recording_at_once_lose_nothing(repo):
    with mp.get_context("spawn").Pool(2) as pool:
        pool.map(_record_many, [(str(repo), "A", 15), (str(repo), "B", 15)])
    s = json.loads((repo / ".tenthcheck/agents.json").read_text())["sessions"]
    assert {k: sum(e["count"] for e in v["edits"].values()) for k, v in s.items()} == {"A": 15, "B": 15}


# ---------------------------------------------------------------- CLI

def test_cli_two_sessions_warn_but_exit_zero(repo, tmp_path, capsys):
    a_src, b_src = tmp_path / "a.py", tmp_path / "b.py"
    a_src.write_text(edit_helper()); b_src.write_text(edit_calculate())
    assert main(["agents", "edit", str(repo), "pkg/core.py", str(a_src), "--session", "A", "--label", "refactor"]) == 0
    assert "No overlap" in capsys.readouterr().out
    assert main(["agents", "edit", str(repo), "pkg/api.py", str(b_src), "--session", "B"]) == 0
    out = capsys.readouterr().out
    assert "WARNING: 1 overlap(s) with other sessions (not blocked)" in out
    assert "pkg.api.calculate is a caller of pkg.core.helper (2 hops), which session A edited" in out
    assert main(["agents", "check", str(repo), "--symbol", "add", "--session", "C", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["recorded"] is False and {w["other_session"] for w in res["warnings"]} == {"A", "B"}
    assert main(["agents", "list", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "2 live session(s)" in out and "A (refactor)" in out and "pkg.api.calculate" in out
    assert main(["agents", "end", str(repo), "--session", "A"]) == 0
    assert "ended" in capsys.readouterr().out
    assert main(["agents", "list", str(repo), "--json"]) == 0
    assert [s["session"] for s in json.loads(capsys.readouterr().out)["sessions"]] == ["B"]


def test_cli_session_from_env_and_usage_errors(repo, monkeypatch, capsys):
    monkeypatch.setenv("TENTHCHECK_SESSION", "envsess")
    assert main(["agents", "edit", str(repo), "--symbol", "helper", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["session"] == "envsess"
    monkeypatch.delenv("TENTHCHECK_SESSION")
    with pytest.raises(SystemExit):
        main(["agents", "edit", str(repo), "--symbol", "helper"])  # no session
    with pytest.raises(SystemExit):
        main(["agents", "edit", str(repo), "--session", "A"])  # nothing to record
    assert main(["agents", "edit", str(repo), "--symbol", "nope", "--session", "A"]) == 2  # unknown symbol


def test_state_dir_is_not_parsed_as_code(repo, capsys):
    agents.record_edit(repo, "A", "pkg/core.py", edit_helper())
    assert main(["check", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entities"]["module"] == 4


# ---------------------------------------------------------------- MCP: two servers = two sessions

def test_mcp_two_sessions(repo):
    pytest.importorskip("mcp")
    from mcp import Client
    from tenthcheck.server import build_server

    srv_a, srv_b = build_server(repo, session_id="A"), build_server(repo, session_id="B")

    async def run():
        async with Client(srv_a) as a, Client(srv_b) as b:
            names = sorted(t.name for t in (await a.list_tools()).tools)
            (repo / "pkg/core.py").write_text(edit_helper())            # A writes, then records (after the write)
            rec_a = await a.call_tool("record_edit", {"file_path": "pkg/core.py", "new_source": edit_helper()})
            chk_b = await b.call_tool("check_proposed_edit", {"file_path": "pkg/api.py", "new_source": edit_calculate()})
            rec_b = await b.call_tool("record_edit", {"file_path": "pkg/api.py", "new_source": edit_calculate()})
            sess = await b.call_tool("agent_sessions", {})
            return names, rec_a, chk_b, rec_b, sess

    names, rec_a, chk_b, rec_b, sess = asyncio.run(run())
    assert names == ["agent_sessions", "check_blast_radius", "check_proposed_edit", "find_import_cycles",
                     "record_edit", "scope_audit"]
    assert rec_a.structured_content["symbols"] == ["pkg.core.helper"]
    assert rec_a.structured_content["warnings"] == []
    chk = chk_b.structured_content
    assert chk["ok"] is True  # a warning never changes ok
    assert [w["their_edit"] for w in chk["agent_warnings"]] == ["pkg.core.helper"]
    [w] = rec_b.structured_content["warnings"]
    assert (w["other_session"], w["relation"], w["hops"]) == ("A", "caller", 2)
    s = sess.structured_content
    assert s["this_session"] == "B" and [x["session"] for x in s["sessions"]] == ["A", "B"]
    assert (repo / "pkg/api.py").read_text() == API  # tools never write the agent's files


def test_class_users_are_inside_the_radius_since_0_1_2(tmp_path):
    """Merged from 0.1.2: a function that only annotates a class is in the radius of an edit to that class."""
    repo = _write(tmp_path / "r", {
        "pkg/__init__.py": "",
        "pkg/opts.py": "class Options:\n    width: int = 80\n",
        "pkg/render.py": "from pkg.opts import Options\n\ndef render(o: Options) -> int:\n    return 1\n",
    })
    agents.record_edit(repo, "A", "pkg/opts.py", "class Options:\n    width: int = 100\n")
    [w] = agents.record_edit(repo, "B", "pkg/render.py",
                             "from pkg.opts import Options\n\ndef render(o: Options) -> int:\n    return 2\n")["warnings"]
    assert (w["symbol"], w["their_edit"], w["relation"], w["hops"]) == ("pkg.render.render", "pkg.opts.Options", "caller", 1)

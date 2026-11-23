"""`tenthcheck hook`: run as Claude Code would (JSON on stdin, a real subprocess), checking exit codes and output."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

CORE = "def helper(x):\n    return x + 1\n\n\ndef add(a, b):\n    return a + helper(b)\n"
API = "from pkg.core import add\n\n\ndef calculate(x, y):\n    return add(x, y)\n"
OTHER = "from pkg import core\n\n\ndef twice(v):\n    return core.add(v, v)\n"


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path):
    r = _write(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/api.py": API,
                                   "pkg/other.py": OTHER, "README.md": "x"})
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


def hook(root: Path, payload: dict, *args: str):
    p = subprocess.run([sys.executable, "-c", "from tenthlook.tenthcheck.cli import main; raise SystemExit(main())", "hook",
                        "--root", str(root), *args], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)})
    return p.returncode, p.stdout, p.stderr


def edit(root: Path, rel: str, new: str, tool: str = "Write", session: str = "s1", **extra) -> dict:
    """Write `new` to disk (as the tool did) and return the PostToolUse payload Claude Code would send."""
    p = root / rel
    old = p.read_text(encoding="utf-8") if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    t = time.time() + 1
    os.utime(p, (t, t))  # make sure the mtime moves even within one clock tick
    ti = {"file_path": str(p), "content": new} if tool == "Write" else {"file_path": str(p), **extra}
    resp = {"filePath": str(p), "type": "update" if old is not None else "create"}
    return {"session_id": session, "cwd": str(root), "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": ti, "tool_response": resp}


def warm(root: Path):
    """One harmless edit so the cache holds every file's pre-edit state."""
    assert hook(root, edit(root, "pkg/__init__.py", "")) == (0, "", "")


# ---------------------------------------------------------------- pass: no output at all

def test_pass_body_edit_produces_zero_output_cold_and_warm(repo):
    assert hook(repo, edit(repo, "pkg/core.py", CORE.replace("x + 1", "x + 2"))) == (0, "", "")  # cold
    assert hook(repo, edit(repo, "pkg/core.py", CORE.replace("x + 1", "x + 3"))) == (0, "", "")  # warm
    assert (repo / ".tenthcheck/hook_cache.json").exists() and (repo / ".tenthcheck/.gitignore").exists()


def test_signature_change_with_all_callers_compatible_is_silent(repo):
    warm(repo)
    assert hook(repo, edit(repo, "pkg/core.py", CORE.replace("def add(a, b):", "def add(a, b, c=0):"))) == (0, "", "")


@pytest.mark.parametrize("payload", [
    {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
    {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": "/tmp/notes.md"}},
    {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": "/elsewhere/x.py"}},
])
def test_other_tools_non_python_and_outside_files_are_silent(repo, payload):
    assert hook(repo, payload) == (0, "", "")


def test_a_bug_or_bad_input_never_interrupts_the_agent(repo):
    p = subprocess.run([sys.executable, "-c", "from tenthlook.tenthcheck.cli import main; raise SystemExit(main())", "hook",
                        "--root", str(repo)], input="{not json", capture_output=True, text=True)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert "Traceback" in (repo / ".tenthcheck/hook.log").read_text()


# ---------------------------------------------------------------- (a) new import cycle

def test_new_import_cycle_trips(repo):
    warm(repo)
    code, out, err = hook(repo, edit(repo, "pkg/core.py", "import pkg.api\n\n" + CORE))
    assert code == 2 and out == ""
    assert "new import cycle pkg.api -> pkg.core -> pkg.api (added: pkg.core -> pkg.api)" in err
    assert len(err.splitlines()) == 1


# ---------------------------------------------------------------- (b) changed signature, callers not updated

def test_new_required_parameter_lists_each_caller_file_line(repo):
    warm(repo)
    code, out, err = hook(repo, edit(repo, "pkg/core.py", CORE.replace("def add(a, b):", "def add(a, b, c):")))
    assert code == 2 and out == ""
    lines = err.splitlines()
    assert lines[0] == "Tenthcheck: signature changed pkg.core.add(a, b) -> add(a, b, c); 2 callers not updated:"
    assert lines[1:] == ["  pkg/api.py:5 (calculate): missing 'c'", "  pkg/other.py:5 (twice): missing 'c'"]


def test_reorder_reports_positional_callers_until_they_change_then_stop_hook(repo):
    warm(repo)
    code, _, err = hook(repo, edit(repo, "pkg/core.py", CORE.replace("def add(a, b):\n    return a + helper(b)",
                                                                       "def add(b, a):\n    return a + helper(b)")))
    assert code == 2 and "2 callers not updated" in err and "positional arguments unchanged" in err
    # the agent fixes one caller: silent
    assert hook(repo, edit(repo, "pkg/api.py", API.replace("add(x, y)", "add(y, x)"))) == (0, "", "")
    # trying to stop with one left: blocked once, with the remaining caller
    stop = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "Stop", "stop_hook_active": False}
    code, out, err = hook(repo, stop)
    assert code == 2 and out == "" and "pkg/other.py:5 (twice) calls pkg.core.add" in err and "pkg/api.py" not in err
    assert hook(repo, {**stop, "stop_hook_active": True}) == (0, "", "")  # never blocks twice
    # fix the last one: stop is allowed, silently
    assert hook(repo, edit(repo, "pkg/other.py", OTHER.replace("core.add(v, v)", "core.add(v, 2 * v)"))) == (0, "", "")
    assert hook(repo, stop) == (0, "", "")
    # other sessions have their own pending list
    assert hook(repo, {**stop, "session_id": "s2"}) == (0, "", "")


def test_keyword_rename_and_method_forms(repo):
    _write(repo, {"pkg/shapes.py": (
        "class Box:\n"
        "    def area(self, width, height):\n        return width * height\n\n"
        "    @classmethod\n    def make(cls, size):\n        return cls()\n\n"
        "    def double(self):\n        return self.area(2, height=3)\n"),
        "pkg/use.py": ("from pkg.shapes import Box\n\n\n"
                       "def build():\n    mk = Box.make\n    b = mk(3)\n    return Box.make(size=1), b.area(1, height=2)\n")})
    warm(repo)
    new = (repo / "pkg/shapes.py").read_text().replace("def area(self, width, height)", "def area(self, width, depth)")
    code, _, err = hook(repo, edit(repo, "pkg/shapes.py", new))
    assert code == 2
    assert "pkg/shapes.py:10 (Box.double): unexpected keyword 'height'" in err      # self.area(...)
    assert "pkg/use.py:7 (build): unexpected keyword 'height'" in err               # b.area(): unique method name
    new2 = new.replace("def make(cls, size)", "def make(cls, size, colour)")
    code, _, err = hook(repo, edit(repo, "pkg/shapes.py", new2))
    assert "pkg/use.py:6 (build): missing 'colour'" in err                           # through the local alias
    assert "pkg/use.py:7 (build): missing 'colour'" in err                           # Box.make(size=1)


def test_first_use_without_cache_reconstructs_the_pre_edit_file_from_the_edit(repo):
    old, new = "def add(a, b):", "def add(a, b, c):"
    payload = edit(repo, "pkg/core.py", CORE.replace(old, new), tool="Edit", old_string=old, new_string=new,
                   replace_all=False)
    code, _, err = hook(repo, payload)  # no .tenthcheck yet, no git commit: only the Edit payload says what changed
    assert code == 2 and "pkg/api.py:5 (calculate): missing 'c'" in err


def test_a_new_call_in_the_edited_file_that_does_not_fit_is_reported(repo):
    warm(repo)
    code, _, err = hook(repo, edit(repo, "pkg/api.py", API + "\n\ndef bad():\n    return add(1)\n"))
    assert code == 2 and "pkg/api.py:9 calls pkg.core.add(a, b): missing 'b'" in err
    # the unchanged call next to it is not reported again on later edits
    assert hook(repo, edit(repo, "pkg/api.py", API + "\n\ndef bad():\n    return add(1, 2)\n")) == (0, "", "")


# ---------------------------------------------------------------- (c) dependents threshold, and output limits

def test_dependents_threshold_is_off_by_default_and_reports_once(repo):
    warm(repo)
    body = CORE.replace("a + helper(b)", "a + helper(b) + 0")
    assert hook(repo, edit(repo, "pkg/core.py", body)) == (0, "", "")
    code, _, err = hook(repo, edit(repo, "pkg/core.py", body.replace("+ 0", "+ 1")), "--dependents", "2")
    assert code == 2 and err.strip() == "Tenthcheck: pkg.core.add has 2 direct callers (threshold 2)"
    assert hook(repo, edit(repo, "pkg/core.py", body.replace("+ 0", "+ 2")), "--dependents", "2") == (0, "", "")


def test_output_is_capped(repo):
    _write(repo, {f"pkg/c{i}.py": "from pkg.core import add\n\n\ndef f():\n    return add(1, 2)\n" for i in range(15)})
    warm(repo)
    code, _, err = hook(repo, edit(repo, "pkg/core.py", CORE.replace("def add(a, b):", "def add(a, b, c):")))
    lines = err.splitlines()
    assert code == 2 and len(lines) == 10 and lines[-1] == "  ... and 9 more"


# ---------------------------------------------------------------- the hook's import edges match the parser's

def test_import_edges_match_the_full_parser(repo):
    from tenthlook.tenthcheck.hook import import_graph, summarize
    from tenthlook.tenthcheck.parser import iter_py_files, parse_package
    roots = [repo] + [p for p in [Path.home() / "callguard-corpus/httpx", Path.home() / "callguard-corpus/click/src"]
                      if p.is_dir()]
    for root in roots:
        sums = {}
        for f in iter_py_files(root):
            rel = f.relative_to(root).as_posix()
            try:
                sums[rel] = summarize(rel, f.read_text(encoding="utf-8"), root.name)
            except SyntaxError:
                continue
        mine = {(r.src, r.dst) for r in import_graph(sums).rels("IMPORTS")}
        full = {(r.src, r.dst) for r in parse_package(str(root), skip_syntax_errors=True).rels("IMPORTS")}
        assert mine == full, root


def test_exclude_glob_hides_those_callers_but_keeps_the_rest(repo):
    _write(repo, {"tests/test_core.py": "from pkg.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"})
    warm(repo)
    code, _, err = hook(repo, edit(repo, "pkg/core.py", CORE.replace("def add(a, b):", "def add(a, b, c):")),
                        "--exclude", "tests/*")
    assert code == 2 and "2 callers not updated" in err and "tests/" not in err


def test_warm_edit_on_a_real_repo_is_fast(tmp_path):
    """Latency guard: a warm edit on httpx (60 files) stays well under a second."""
    src = Path.home() / "callguard-corpus/httpx"
    if not src.is_dir():
        pytest.skip("httpx corpus not present")
    import shutil
    root = tmp_path / "httpx"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns(".git"))
    hook(root, edit(root, "httpx/__init__.py", (root / "httpx/__init__.py").read_text()))  # cold
    t = time.perf_counter()
    code, out, err = hook(root, edit(root, "httpx/_utils.py", (root / "httpx/_utils.py").read_text() + "\n# touched\n"))
    assert (code, out, err) == (0, "", "") and time.perf_counter() - t < 1.5


def test_reorder_reports_an_unchanged_caller_in_the_same_file(repo):
    warm(repo)
    new = CORE.replace("def add(a, b):\n    return a + helper(b)", "def add(b, a):\n    return a + helper(b)")
    new += "\n\ndef local():\n    return add(1, 2)\n"
    (repo / "pkg/core.py").write_text(CORE + "\n\ndef local():\n    return add(1, 2)\n")
    warm(repo)  # the cache now has local() calling add(1, 2) in the old order
    code, _, err = hook(repo, edit(repo, "pkg/core.py", new))
    assert code == 2 and "pkg/core.py:10 (local)" in err


def test_hook_and_agents_share_the_state_dir_without_clobbering(repo):
    """v0.2 (agents) and v0.3 (hook) both keep state in .tenthcheck/; neither touches the other's files."""
    from tenthlook.tenthcheck import agents
    agents.record_edit(repo, "A", "pkg/core.py", CORE.replace("x + 1", "x + 5"))
    before = (repo / ".tenthcheck/agents.json").read_text()
    assert hook(repo, edit(repo, "pkg/core.py", CORE.replace("x + 1", "x + 6"))) == (0, "", "")
    assert (repo / ".tenthcheck/agents.json").read_text() == before
    assert agents.list_sessions(repo)["sessions"][0]["edits"] == ["pkg.core.helper"]
    assert (repo / ".tenthcheck/hook_cache.json").exists()


# ---- v1: hand-written / accepted invariants feed counterexamples back to the agent
def test_hook_reports_z3_counterexample_with_file_line(repo):
    (repo / "tenthcheck.toml").write_text(
        '[[invariant]]\nfile = "pkg/bank.py"\nfunction = "withdraw"\ncheck = "final_balance >= 0"\n'
        'assume = ["balance >= 0", "amount > 0", "fee >= 0"]\n')
    good = "def withdraw(balance, amount, fee):\n    if balance < amount + fee:\n        raise ValueError()\n    final_balance = balance - amount - fee\n    return final_balance\n"
    rc, _, err = hook(repo, edit(repo, "pkg/bank.py", good))
    assert rc == 0 and err == ""
    bad = good.replace("balance < amount + fee", "balance < amount")
    rc, _, err = hook(repo, edit(repo, "pkg/bank.py", bad))
    assert rc == 2
    assert "pkg/bank.py:1" in err and "final_balance >= 0" in err and "Counterexample:" in err and "final_balance=-" in err

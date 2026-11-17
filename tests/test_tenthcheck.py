import asyncio
import json
import sys
from pathlib import Path

import pytest

from tenthcheck.cli import main

PRODUCT = Path(__file__).resolve().parents[1]


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path):
    return _write(tmp_path / "repo", {
        "pkg/__init__.py": "",
        "pkg/core.py": "def helper(x):\n    return x + 1\n\ndef add(a, b):\n    return a + helper(b)\n",
        "pkg/api.py": "from pkg.core import add\n\ndef calculate(x, y):\n    return add(x, y)\n",
        "pkg/other.py": "def lonely():\n    return 0\n",
    })


def test_blast_lists_callers_by_hop(repo, capsys):
    assert main(["blast", str(repo), "helper", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["symbol"] == "pkg.core.helper"
    assert out["by_depth"] == {"0": ["pkg.core.helper"], "1": ["pkg.core.add"], "2": ["pkg.api.calculate"]}


def test_blast_unknown_and_ambiguous_symbols_exit_2(repo, tmp_path, capsys):
    assert main(["blast", str(repo), "nope"]) == 2
    _write(repo, {"pkg/dup.py": "def helper():\n    return 2\n"})
    assert main(["blast", str(repo), "helper"]) == 2
    assert "matches 2 entities" in capsys.readouterr().err


def test_cycles_and_check_exit_codes(repo, tmp_path, capsys):
    assert main(["cycles", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["n_cyclic_components"] == 0
    proposed = tmp_path / "core_new.py"
    proposed.write_text("from pkg.api import calculate\n\ndef helper(x):\n    return x + 2\n\n"
                        "def add(a, b):\n    return a + helper(b)\n")
    assert main(["check", str(repo), "pkg/core.py", str(proposed)]) == 1
    out = capsys.readouterr().out
    assert "NEW IMPORT CYCLES: 1" in out
    assert "pkg.api -> pkg.core -> pkg.api" in out and "[added by this edit: pkg.core -> pkg.api]" in out
    harmless = tmp_path / "core_ok.py"
    harmless.write_text("def helper(x):\n    return x + 2\n\ndef add(a, b):\n    return a + helper(b)\n")
    assert main(["check", str(repo), "pkg/core.py", str(harmless)]) == 0
    assert "pkg.core.helper: 2 dependents" in capsys.readouterr().out
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n")
    assert main(["check", str(repo), "pkg/core.py", str(broken)]) == 2


def test_check_repo_only_prints_summary_and_cycles(repo, capsys):
    assert main(["check", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["entities"]["function"] == 4 and out["entities"]["module"] == 4
    assert out["n_cyclic_components"] == 0
    assert main(["check", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "Parsed:" in text and "Import cycles: 0" in text
    _write(repo, {"pkg/core.py": "import pkg.api\n\ndef helper(x):\n    return x\n"})
    assert main(["check", str(repo)]) == 0  # existing cycles are reported, not treated as new
    assert "Import cycles: 1" in capsys.readouterr().out


def test_check_with_only_file_path_is_a_usage_error(repo):
    with pytest.raises(SystemExit) as e:
        main(["check", str(repo), "pkg/core.py"])
    assert e.value.code == 2


def test_serve_defaults_to_current_directory(monkeypatch):
    import tenthcheck.server
    seen = []
    monkeypatch.setattr(tenthcheck.server, "serve", lambda d: seen.append(d))
    assert main(["serve"]) == 0
    assert seen == ["."]


def test_readme_mcp_config_snippet_runs_tenthcheck_serve():
    readme = (PRODUCT / "README.md").read_text(encoding="utf-8")
    line = next(l.strip() for l in readme.splitlines() if l.strip().startswith('{"mcpServers"'))
    cfg = json.loads(line)
    assert cfg["mcpServers"]["tenthcheck"] == {"type": "stdio", "command": "tenthcheck", "args": ["serve"]}


def test_scope_command(repo, tmp_path, capsys):
    import shutil
    after = tmp_path / "after"
    shutil.copytree(repo, after)
    (after / "pkg/core.py").write_text("def helper(x):\n    return x + 9\n\ndef add(a, b):\n    return a + helper(b)\n")
    assert main(["scope", str(repo), str(after), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["modified_entities"] == ["pkg.core.helper"]
    assert out["untouched_dependencies"] == ["pkg.api.calculate", "pkg.core.add"]


mcp = pytest.importorskip("mcp")


def test_mcp_server_tools_in_process(repo):
    from mcp import Client
    from tenthcheck.server import build_server

    async def run():
        async with Client(build_server(repo)) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            blast = await client.call_tool("check_blast_radius", {"symbol": "helper"})
            cyc = await client.call_tool("find_import_cycles", {})
            edit = await client.call_tool("check_proposed_edit", {
                "file_path": "pkg/core.py",
                "new_source": "import pkg.api\n\ndef helper(x):\n    return x\n\ndef add(a, b):\n    return a + helper(b)\n"})
            return names, blast, cyc, edit

    assert build_server(repo).name == "tenthcheck"
    names, blast, cyc, edit = asyncio.run(run())
    assert names == ["agent_sessions", "check_blast_radius", "check_proposed_edit", "find_import_cycles",
                     "record_edit", "scope_audit"]
    assert blast.structured_content["by_depth"]["2"] == ["pkg.api.calculate"]
    assert cyc.structured_content["n_cyclic_components"] == 0
    assert edit.structured_content["ok"] is False
    assert (repo / "pkg/core.py").read_text().startswith("def helper(x):\n    return x + 1")  # not written

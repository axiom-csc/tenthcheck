"""Latency of each Tenthcheck tool on real repositories.

    python scripts/bench_latency.py <corpus_dir> <out.json>

Two settings, both measured end to end:
- `mcp_warm`: one `tenthcheck serve <repo>` subprocess per repo, called over stdio with the official
  `mcp` client. Round-trip time per `tools/call`, after one untimed call per tool (the first call
  parses the repo). This is what an agent sees during a session.
- `cli_cold`: `tenthcheck <cmd> ...` as a fresh subprocess, parse included.

Inputs are fixed by seed: blast symbols are sampled from the repo's functions/classes; the proposed
edit appends one new function to a sampled module file and adds one import; scope compares the repo
with a copy where one file is edited. Small run counts make p99 close to the max; N is recorded.
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

from tenthcheck.parser import iter_py_files, parse_package

REPOS = ["httpx", "flask", "pydantic"]
SEED = 0
CG = str(Path(sys.executable).with_name("tenthcheck"))
N_WARM = {"check_blast_radius": 100, "find_import_cycles": 30, "check_proposed_edit": 20, "scope_audit": 5}
N_COLD = 5


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


def summary(xs: list[float]) -> dict:
    return {"n": len(xs), "p50_ms": round(statistics.median(xs), 2), "p99_ms": round(pct(xs, 0.99), 2),
            "max_ms": round(max(xs), 2), "min_ms": round(min(xs), 2)}


def inputs(repo: Path) -> dict:
    rng = random.Random(SEED)
    g = parse_package(str(repo), skip_syntax_errors=True)
    defs = sorted(eid for eid, e in g.entities.items() if e.type in ("function", "class") and "@L" not in eid)
    files = [p for p in iter_py_files(repo) if p.stat().st_size > 200]
    target = rng.choice(files)
    rel = target.relative_to(repo).as_posix()
    src = target.read_text(encoding="utf-8")
    proposed = "import json as _tenthcheck_bench_json\n" + src + "\n\ndef _tenthcheck_bench_probe():\n    return 1\n"
    return {"symbols": rng.sample(defs, min(20, len(defs))), "edit_file": rel, "edit_source": proposed}


async def mcp_warm(repo: Path, inp: dict, scope_dirs: tuple[str, str]) -> dict:
    out = {}
    params = StdioServerParameters(command=CG, args=["serve", str(repo)])
    calls = {
        "check_blast_radius": lambda i: {"symbol": inp["symbols"][i % len(inp["symbols"])]},
        "find_import_cycles": lambda i: {},
        "check_proposed_edit": lambda i: {"file_path": inp["edit_file"], "new_source": inp["edit_source"]},
        "scope_audit": lambda i: {"before_dir": scope_dirs[0], "after_dir": scope_dirs[1]},
    }
    async with Client(params) as client:
        t0 = time.perf_counter()
        await client.call_tool("find_import_cycles", {})
        out["first_call_ms_includes_parse"] = round((time.perf_counter() - t0) * 1000, 2)
        for name, make in calls.items():
            r = await client.call_tool(name, make(0))  # untimed warm-up
            assert not r.is_error, (name, r.content)
            xs = []
            for i in range(N_WARM[name]):
                t0 = time.perf_counter()
                r = await client.call_tool(name, make(i))
                xs.append((time.perf_counter() - t0) * 1000)
                assert not r.is_error, (name, r.content)
            out[name] = summary(xs)
    return out


def cli_cold(repo: Path, inp: dict, scope_dirs: tuple[str, str], proposed_path: str) -> dict:
    cmds = {
        "blast": [CG, "blast", str(repo), inp["symbols"][0]],
        "cycles": [CG, "cycles", str(repo)],
        "check": [CG, "check", str(repo), inp["edit_file"], proposed_path],
        "scope": [CG, "scope", scope_dirs[0], scope_dirs[1]],
    }
    out = {}
    for name, cmd in cmds.items():
        xs = []
        for _ in range(N_COLD):
            t0 = time.perf_counter()
            p = subprocess.run(cmd, capture_output=True, text=True)
            xs.append((time.perf_counter() - t0) * 1000)
            assert p.returncode in (0, 1), (cmd, p.stderr[-500:])
        out[name] = summary(xs)
    return out


def power_source() -> str:
    """First line of `pmset -g batt` on macOS (AC vs battery changes timings on laptops)."""
    try:
        return subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout.splitlines()[0]
    except (OSError, IndexError):
        return "unknown"


def main() -> None:
    corpus, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    rows = []
    for name in REPOS:
        repo = corpus / name
        inp = inputs(repo)
        with tempfile.TemporaryDirectory(prefix="cg_bench_") as tmp:
            after = Path(tmp) / name
            shutil.copytree(repo, after, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
            (after / inp["edit_file"]).write_text(inp["edit_source"], encoding="utf-8")
            proposed = Path(tmp) / "proposed.py"
            proposed.write_text(inp["edit_source"], encoding="utf-8")
            scope_dirs = (str(repo), str(after))
            row = {"repo": name, "edit_file": inp["edit_file"], "n_symbols": len(inp["symbols"])}
            row["mcp_warm"] = asyncio.run(mcp_warm(repo, inp, scope_dirs))
            row["cli_cold"] = cli_cold(repo, inp, scope_dirs, str(proposed))
        rows.append(row)
        print(json.dumps(row))
    meta = {"python": sys.version.split()[0], "platform": sys.platform, "seed": SEED,
            "recorded": time.strftime("%Y-%m-%d %H:%M:%S"),
            "machine": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip(),
            "power": power_source()}
    out_path.write_text(json.dumps({"meta": meta, "repos": rows}, indent=2))


if __name__ == "__main__":
    main()

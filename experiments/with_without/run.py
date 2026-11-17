"""With/without Tenthcheck: 10 refactor tasks on rich, each done by headless Claude Code with and without the
tenthcheck MCP server. See PLAN.md. Nothing here calls a model unless you pass --i-approve-plan-usage.

    python run.py --dry-run --model <model>                       # print what would run; no clone, no model
    python run.py --setup                                         # clone rich, build venvs, baseline tests; no model
    python run.py --model <model> --i-approve-plan-usage          # the 20 Claude Code sessions (uses plan usage)
    python run.py --model <model> --i-approve-plan-usage --only t03_rename_adjust_line_length   # a single pair

Results land in results/<run_id>/results.json (schema in PLAN.md), written after every session, so an
interrupted run continues with --resume <run_id>. Analyse with `python analyze.py results/<run_id>/results.json`.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = json.loads((HERE / "tasks.json").read_text())
TARGET = TASKS["target"]
WORK = Path(os.environ.get("WITH_WITHOUT_WORK", Path.home() / "tenthcheck-with-without"))
MIRROR = WORK / "rich.git"
TARGET_VENV = WORK / "venv-rich"        # rich's test deps; the harness runs tests and done-checks with it
TOOL_VENV = WORK / "venv-tenthcheck"    # tenthcheck[mcp], installed from this repo checkout
BASELINE = WORK / "baseline.json"
BASE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]
DENIED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit"]
MCP_TOOLS = ["mcp__tenthcheck__check_blast_radius", "mcp__tenthcheck__find_import_cycles",
             "mcp__tenthcheck__check_proposed_edit", "mcp__tenthcheck__scope_audit"]
ARMS = ("with", "without")


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kw)


def vpy(venv: Path) -> Path:
    return venv / "bin" / "python"


# ---------------------------------------------------------------- setup (no model)

def setup(python: str) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    if not MIRROR.exists():
        sh(["git", "clone", "--bare", "--filter=blob:none", TARGET["repo"], str(MIRROR)])
    sh(["git", "-C", str(MIRROR), "fetch", "origin", "tag", TARGET["tag"], "--no-tags"])
    if not TARGET_VENV.exists():
        sh([python, "-m", "venv", str(TARGET_VENV)])
        sh([str(vpy(TARGET_VENV)), "-m", "pip", "install", "-q", *TARGET["test_deps"]])
    if not TOOL_VENV.exists():
        sh([python, "-m", "venv", str(TOOL_VENV)])
        sh([str(vpy(TOOL_VENV)), "-m", "pip", "install", "-q", f"{HERE.parents[1]}[mcp]"])
    co = fresh_checkout(WORK / "baseline_checkout")
    passed, all_ids, junit = run_tests(co, WORK / "baseline_junit.xml")
    BASELINE.write_text(json.dumps({"commit": TARGET["commit"], "python": python_version(),
                                    "n_collected": len(all_ids), "n_passed": len(passed),
                                    "passed": sorted(passed)}, indent=1))
    print(f"baseline: {len(passed)} of {len(all_ids)} tests pass at {TARGET['commit'][:7]} -> {BASELINE}")
    shutil.rmtree(co)


def python_version() -> str:
    return subprocess.run([str(vpy(TARGET_VENV)), "-c", "import sys; print(sys.version.split()[0])"],
                          capture_output=True, text=True).stdout.strip()


def fresh_checkout(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    sh(["git", "clone", "-q", "--no-checkout", str(MIRROR), str(dest)])
    sh(["git", "-C", str(dest), "checkout", "-q", TARGET["commit"]])
    head = sh(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    assert head == TARGET["commit"], (head, TARGET["commit"])
    return dest


def run_tests(checkout: Path, junit: Path) -> tuple[set[str], set[str], Path]:
    """Return (passing test ids, all reported test ids). Plugin autoload is off so global plugins can't interfere."""
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    subprocess.run([str(vpy(TARGET_VENV)), *TARGET["test_command"], f"--junitxml={junit}"],
                   cwd=checkout, env=env, capture_output=True, text=True, timeout=1800)
    passed, every = set(), set()
    if junit.exists():
        for tc in ET.parse(junit).iter("testcase"):
            tid = f"{tc.get('classname')}::{tc.get('name')}"
            every.add(tid)
            if not any(ch.tag in ("failure", "error", "skipped") for ch in tc):
                passed.add(tid)
    return passed, every, junit


# ---------------------------------------------------------------- one session

def mcp_config(arm: str, checkout: Path, out: Path) -> Path:
    servers = {}
    if arm == "with":
        servers["tenthcheck"] = {"type": "stdio", "command": str(TOOL_VENV / "bin" / "tenthcheck"),
                                 "args": ["serve", str(checkout / TARGET["package_dir"])]}
    out.write_text(json.dumps({"mcpServers": servers}, indent=1))
    return out


def claude_cmd(args, arm: str, prompt: str, cfg: Path) -> list[str]:
    allowed = BASE_TOOLS + (MCP_TOOLS if arm == "with" else [])
    return ["claude", "-p", prompt, "--model", args.model, "--output-format", "stream-json", "--verbose",
            "--max-turns", str(args.max_turns), "--permission-mode", "acceptEdits",
            "--mcp-config", str(cfg), "--strict-mcp-config",
            "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(DENIED_TOOLS),
            *args.extra_claude_arg]


def parse_stream(path: Path) -> dict:
    out = {"num_turns": None, "duration_ms": None, "reported_cost_usd": None, "usage": None,
           "is_error": None, "tool_calls": {}, "mcp_servers": None}
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            out["mcp_servers"] = ev.get("mcp_servers")
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    out["tool_calls"][block["name"]] = out["tool_calls"].get(block["name"], 0) + 1
        if ev.get("type") == "result":
            out.update(num_turns=ev.get("num_turns"), duration_ms=ev.get("duration_ms"),
                       reported_cost_usd=ev.get("total_cost_usd"), usage=ev.get("usage"),
                       is_error=ev.get("is_error"))
    return out


def run_one(args, task: dict, arm: str, run_dir: Path, baseline: dict) -> dict:
    sess = run_dir / f"{task['id']}__{arm}"
    sess.mkdir(parents=True, exist_ok=True)
    # Checkouts live outside this repo so the agent never sees tenthcheck's own files or results.
    co = fresh_checkout(WORK / "checkouts" / f"{run_dir.name}__{task['id']}__{arm}")
    cfg = mcp_config(arm, co, sess / "mcp_config.json")
    prompt = f"{task['prompt']}\n\n{TASKS['common_rules']}"
    env = dict(os.environ)
    # Bill the subscription plan, never an API key that happens to be in the environment.
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    t0 = time.time()
    with open(sess / "stream.jsonl", "w") as fh:
        try:
            proc = subprocess.run(claude_cmd(args, arm, prompt, cfg), cwd=co, env=env, stdout=fh,
                                  stderr=subprocess.PIPE, text=True, timeout=args.session_timeout)
            rc, stderr = proc.returncode, proc.stderr[-2000:]
        except subprocess.TimeoutExpired:
            rc, stderr = "timeout", ""
    wall = round(time.time() - t0, 1)
    meta = parse_stream(sess / "stream.jsonl")

    # What the agent changed (tests/ included, to record rule-breaking), then put tests/ back exactly.
    sh(["git", "-C", str(co), "add", "-A"])
    diff = sh(["git", "-C", str(co), "diff", "--cached", TARGET["commit"]], capture_output=True).stdout
    (sess / "agent.diff").write_text(diff)
    files = sh(["git", "-C", str(co), "diff", "--cached", "--name-only", TARGET["commit"]],
               capture_output=True).stdout.split()
    touched_tests = [f for f in files if f.startswith("tests/")]
    sh(["git", "-C", str(co), "reset", "-q"])
    sh(["git", "-C", str(co), "checkout", "-q", TARGET["commit"], "--", "tests"])
    sh(["git", "-C", str(co), "clean", "-qfd", "--", "tests"])

    passed, every, _ = run_tests(co, sess / "junit.xml")
    base_pass = set(baseline["passed"])
    broken = sorted(base_pass - passed)
    done = subprocess.run([str(vpy(TARGET_VENV)), "-c", task["done_check"]], cwd=co,
                          capture_output=True, text=True, timeout=120)
    cycles = subprocess.run([str(TOOL_VENV / "bin" / "tenthcheck"), "cycles", str(co / TARGET["package_dir"])],
                            capture_output=True, text=True, timeout=600)
    shutil.rmtree(co)  # keep the diff, stream and junit, not the checkout
    return {
        "task_id": task["id"], "arm": arm, "order_in_pair": None, "started_utc": None,
        "claude_exit": rc, "claude_stderr_tail": stderr, "wall_s": wall, **meta,
        "tenthcheck_calls": sum(v for k, v in meta["tool_calls"].items() if k.startswith("mcp__tenthcheck__")),
        "files_changed": files, "touched_tests": touched_tests, "diff_lines": diff.count("\n"),
        "tests_collected": len(every), "tests_passed": len(passed),
        "newly_broken": len(broken), "newly_broken_ids": broken,
        "done_check_passed": done.returncode == 0, "done_check_error": done.stderr[-500:],
        "import_cycles_after": cycles.stdout.strip()[-1000:],
    }


# ---------------------------------------------------------------- schedule

def schedule(seed: int, task_ids: list[str]) -> list[tuple[str, str]]:
    """Shuffle task order; alternate which arm goes first (balanced: half the pairs start with each arm)."""
    rng = random.Random(seed)
    ids = task_ids[:]
    rng.shuffle(ids)
    firsts = [ARMS[i % 2] for i in range(len(ids))]
    rng.shuffle(firsts)
    order = []
    for tid, first in zip(ids, firsts):
        second = "without" if first == "with" else "with"
        order += [(tid, first), (tid, second)]
    return order


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true", help="clone, venvs, baseline tests (no model)")
    ap.add_argument("--python", default="python3.11", help="interpreter for the venvs (setup)")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule and one full command; no model")
    ap.add_argument("--i-approve-plan-usage", action="store_true",
                    help="required to start Claude Code sessions (20 sessions on your Claude plan)")
    ap.add_argument("--model", help="exact model id/alias, identical for both arms (required to run)")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--session-timeout", type=int, default=1800, help="seconds per session")
    ap.add_argument("--seed", type=int, default=20261117)
    ap.add_argument("--only", action="append", default=[], help="task id(s) to run (default: all 10)")
    ap.add_argument("--resume", help="run_id to continue")
    ap.add_argument("--extra-claude-arg", action="append", default=[],
                    help="passed to both arms verbatim, e.g. --extra-claude-arg=--setting-sources=project")
    args = ap.parse_args()

    if args.setup:
        setup(args.python)
        return
    task_ids = [t["id"] for t in TASKS["tasks"] if not args.only or t["id"] in args.only]
    by_id = {t["id"]: t for t in TASKS["tasks"]}
    order = schedule(args.seed, task_ids)
    if args.dry_run:
        print(f"{len(order)} sessions, seed {args.seed}:")
        for i, (tid, arm) in enumerate(order, 1):
            print(f"  {i:2d}. {tid:32s} {arm}")
        demo = argparse.Namespace(**{**vars(args), "model": args.model or "<MODEL>"})
        print("\nfirst command (cwd = fresh checkout):")
        print(" ", " ".join(repr(c) if " " in c else c for c in
                            claude_cmd(demo, order[0][1], "<task prompt + common rules>", Path("<sess>/mcp_config.json"))))
        return
    if not args.i_approve_plan_usage:
        sys.exit("Refusing to start: this runs %d Claude Code sessions on your Claude plan. Read PLAN.md (cost "
                 "section), then re-run with --i-approve-plan-usage." % len(order))
    if not args.model:
        sys.exit("--model is required so both arms use the same, recorded model.")
    if not BASELINE.exists():
        sys.exit(f"No baseline at {BASELINE}; run `python run.py --setup` first.")
    if shutil.which("claude") is None:
        sys.exit("`claude` not on PATH.")
    baseline = json.loads(BASELINE.read_text())

    run_id = args.resume or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = HERE / "results" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "results.json"
    if out.exists():
        data = json.loads(out.read_text())
        if data["config"]["seed"] != args.seed or data["config"]["model"] != args.model:
            sys.exit("resume: seed/model differ from the original run")
    else:
        cv = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
        data = {"schema": "tenthcheck-with-without/1", "run_id": run_id,
                "config": {"model": args.model, "max_turns": args.max_turns, "seed": args.seed,
                           "claude_code_version": cv, "session_timeout_s": args.session_timeout,
                           "extra_claude_args": args.extra_claude_arg, "allowed_tools": BASE_TOOLS,
                           "mcp_tools_with_arm": MCP_TOOLS, "denied_tools": DENIED_TOOLS,
                           "target": TARGET, "common_rules": TASKS["common_rules"],
                           "baseline_passed": baseline["n_passed"], "baseline_collected": baseline["n_collected"],
                           "order": order},
                "runs": []}
    done = {(r["task_id"], r["arm"]) for r in data["runs"]}
    for i, (tid, arm) in enumerate(order):
        if (tid, arm) in done:
            continue
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{i + 1}/{len(order)}] {tid} {arm} ...", flush=True)
        rec = run_one(args, by_id[tid], arm, run_dir, baseline)
        rec["order_in_pair"] = 1 if i % 2 == 0 else 2
        rec["started_utc"] = started
        data["runs"].append(rec)
        out.write_text(json.dumps(data, indent=1))
        print(f"    newly_broken={rec['newly_broken']} done_check={rec['done_check_passed']} "
              f"tenthcheck_calls={rec['tenthcheck_calls']} turns={rec['num_turns']} wall={rec['wall_s']}s")
    print(f"done: {out}\nnext: python {HERE / 'analyze.py'} {out}")


if __name__ == "__main__":
    main()

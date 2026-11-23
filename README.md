# Tenthcheck

Tenthcheck, from Tenthlook. A zero-config scan that finds places in a Python repo that are *inconsistent with the rest
of the repo*, a narrow Z3 checker for numeric invariants, and an agent hook / MCP server / GitHub Action that feed the
exact counterexample back to a coding agent. No LLM and no network: it runs offline and no API key is involved.

Python only. Everything below was measured in this repository's own runs; the numbers cite their results files.

## Quickstart (10 seconds)

```
pipx install tenthcheck        # or: uvx tenthcheck
cd your-repo && tenthcheck
```

> Not on PyPI yet. Until it is published, install from a checkout: `pipx install /path/to/tenthcheck`.

`tenthcheck` with no arguments scans the current directory and prints candidate inconsistencies. Real output, from
`apache/superset` at `11f4e38` (results/scans/superset.json):

```
Tenthcheck 1.0.0.dev0: scanned 1243 files in 7.7s, 13 patterns mined, 1 candidate inconsistencies

[1] missing guard  confidence high (29 of 30 places = 97%)
    pattern: public methods of class DashboardRestApi in superset/dashboards/api.py use `protect`
    followed in 29 places, e.g. superset/dashboards/api.py:531 get_list(), ...
    inconsistent here: superset/dashboards/api.py:2261 download_xlsx()  (inconsistent with 29 other places)
```

That one is a deliberate exception (a code comment at `api.py:2248` says `download_xlsx` is intentionally login-free).
Findings are **candidates**, not bugs; see [Measured precision](#measured-precision) before relying on them.

## What it does

**1. Invariant mining** (`tenthcheck`, `tenthcheck scan`). Finds patterns most places follow and lists the exceptions.
Reported only with at least 5 supporting places and at least 80% consistency (`--min-support`, `--min-consistency`,
or `[settings]` in `tenthcheck.toml`). Four miners, all `ast` and counting:

| Miner | Pattern |
|---|---|
| `route-guard` | handlers on one router / class / module (`@router.get`, `get/post/...` methods, `def view(request, ...)`) mostly carry a guard decorator, `Depends(...)` or an access-control call |
| `call-pair` | `open_x()` / `acquire()` / `begin()` ... is followed by its closer in the same function |
| `guard-mutation` | `target -= amount` is mostly in functions that also compare `target` |
| `sql-scope` | SQL string literals (parsed with sqlglot; ambiguous ones skipped) doing UPDATE/DELETE on a table mostly filter by `tenant_id` / `user_id` / ... |

Test files, docs and examples are skipped. When a site is unclear it is skipped rather than reported.

**2. `tenthcheck init`** writes `tenthcheck.toml`: each mined pattern with `status = "pending"`. Set `accepted` to enforce
it in the hook and in CI, `rejected` to suppress it. Hand-written numeric invariants go in the same file:

```toml
[[invariant]]
file = "bank.py"
function = "withdraw"
check = "final_balance >= 0"
assume = ["balance >= 0", "amount > 0", "fee >= 0"]
```

**3. Z3 numeric checker** (`tenthcheck verify`). For simple straight-line functions (assignments, + - * /, small `**`,
comparisons, if/else, `abs/min/max`, `raise` and `assert`) it translates the function to Z3 and either finds a
counterexample or finds none:

```
$ tenthcheck verify bank.py::withdraw "final_balance >= 0" --assume "balance >= 0" --assume "amount > 0" --assume "fee >= 0"
VIOLATED withdraw  [final_balance >= 0]: counterexample found, Z3, 31 ms
        inputs: balance=0, amount=1, fee=0
        then:   final_balance=-1
```

Loops, `try`/`with`, external calls, I/O, `//`, `%`, strings and containers are reported as `NOT CHECKABLE BY Z3 ... (reason)`;
nothing is guessed. Numbers are modelled as exact reals/integers, so float rounding is not modelled. 38 parametrized cases with known
answers (16 hold, 9 violable, 13 unsupported-construct refusals) plus 4 behaviour tests are in `tests/test_z3check.py`.

**4. `tenthcheck impact`** on the uncommitted diff: changed symbols, their dependents (the existing blast-radius graph),
touched hand-written invariants and a Z3 re-check of them.

**5. Agents.** `tenthcheck install --claude` registers the MCP server (`.mcp.json`) and the hook (`.claude/settings.json`);
`--cursor` registers the MCP server (`.cursor/mcp.json`; no Cursor hook is registered, since the hook only understands Claude
Code's payload). It backs the file up (`*.tenthcheck.bak`), prints a diff, changes nothing on a second run, and
`tenthcheck uninstall` removes only what it added (`--dry-run` shows the diff only). When an accepted invariant breaks or Z3
finds a counterexample, the hook tells the agent the exact counterexample and `file:line`. This does not mean agents "self-heal":
the earlier 48-session hook/MCP/no-tool experiment in this repo found 0 broken tests and 0 real missed callers in every arm,
i.e. a correctness tie (`experiments/with_without/`, commit `c63a970`), and no new agent experiment was run for v1.

**6. GitHub Action** (`action.yml`, `examples/tenthcheck-pr.yml`, `tenthcheck ci`). On a PR it enforces accepted invariants,
posts a summary comment (invariants checked, counterexamples, blast-radius, elapsed time) and fails on a violation. Wording is
"checked", "no counterexample found" and "N candidate inconsistencies". **Untested on GitHub:** `act` is not installed here;
only the `tenthcheck ci` command and the YAML syntax were tested. Optional Ed25519 receipt: `tenthcheck keygen`,
`tenthcheck ci --receipt r.json`, `tenthcheck verify-receipt r.json --public-key ...`. A receipt shows what was checked, on which
commit; it does not show the code is correct (`docs/receipts.md`).

## Measured

All wall-clock figures below are from this machine (macOS, Python 3.11), single runs unless stated.

**Scan** (`results/scan_timing.md`): requests, 20 files, 0.2 s; celery, 208 files, 1.2 s; superset, 1243 files, 8.0 s;
erpnext, 2551 files, 9.2 s; airflow, 3881 files, 18.7 s.

**`impact` on a one-function uncommitted edit** (`results/impact_timing.jsonl`, median of runs, whole command including start-up):
requests (37 .py files) 0.92 s, 5 runs; starlette at `271c277` (84 files) 2.1 s, 5 runs; airflow (8,697 .py files in the checkout)
**223 s**, 3 runs. Nearly all of it is the whole-repo graph parse run twice (before and after the diff; 220 s of 222 s on
airflow), which the existing graph code does without caching. It is nowhere near a "milliseconds" figure and it does not scale
well to very large repos. The agent hook keeps a separate cached incremental index (its speed was not measured for v1).

**Tests:** 106 passed, 2 skipped (`pytest -q`).

## Measured precision

Procedure and every labelled finding are in `results/scan_repos.md`, `results/review_tune.md` and `results/review_heldout.md`.
Repos were pinned by commit and listed before scanning; thresholds and filters were tuned on five repos only.

| Set | Repos | Version | Findings | Unexplained true | Intentional | False positive |
|---|---|---|---|---|---|---|
| tune | redash, zulip, celery, requests, saleor | tuned | 1 (after tuning; 48 before) | 0 | 1 (probable) | 0 (48 before tuning) |
| held-out 1 | superset, mealie, netbox, frappe, wagtail | v1.0 frozen | 13 | 0 | 1 | 12 |
| held-out 2 | paperless-ngx, authentik, pretix, airflow, erpnext | v1.1 | 4 | 0 | 1 | 3 |

- Held-out 1 was run once on the frozen version: **1 of 13** findings was a real, documented exception and **12 were false
  positives**. After reading them, v1.1 changed three rules (a "handler" must be a decorated non-hook method; `protect_read`
  counts as `protect`; `@before_request` and similar are not handlers). Held-out 1 is therefore no longer held out for v1.1.
- Held-out 2 (five fresh repos, v1.1, run once): **1 of 4** was an intentional exception; **3 were false positives** (a guard done
  in the function body instead of a decorator, twice; a client factory whose name contains `admin`, once).
- **No finding was an unexplained real inconsistency, and no genuine bug was found in any repo.** Nothing was reported upstream.
- Most repos produced no findings at all (11 of 15). Only the `route-guard` miner produced any exception site on real code; the
  `call-pair`, `guard-mutation` and `sql-scope` miners are tested on synthetic fixtures only and produced zero exceptions on the
  15 repos. Recall was not measured.
- The samples are tiny (13 and 4 findings): treat the percentages as "the tool is not yet precise", not as a stable rate.

## What Tenthcheck does not do

- **Python only.** No TypeScript, JavaScript or SQL files (SQL is read only from Python string literals).
- Z3 checks **only simple numeric functions**: no loops, calls, I/O, `//`, `%`, strings or containers; floats are modelled as exact reals.
- **Mined invariants are candidates, not proofs.** Many are intentional, and a guard written in another form (inside the function
  body, in middleware, in a framework hook) is not recognised, which causes false positives.
- It does not guarantee your code is correct, and a receipt only records what was checked.
- It does not fix code. It tells you (or your agent) where and, for Z3, with what inputs.
- `tenthcheck impact` is slow on very large repositories (see Measured).
- The GitHub Action has not been run on GitHub.

---

# Dependency graph, MCP server and hook (v0.x documentation, still current)

Static checks for Python repositories, as a CLI and an MCP server for coding agents.

**Held-out accuracy: 53 of 60 true direct callers found (88%), 0 false positives,** on starlette (`271c277`),
a library no fix was made for. The 23 symbols were picked by a fixed call-frequency rule, every call site was
labelled by hand from grep hits without seeing Tenthcheck's output, and the labels and the parser were hashed
before scoring once (`measurements/results/accuracy_starlette/`). Small sample, typed code only; details and
the other two libraries are under [Measured](https://github.com/tenthlook/tenthcheck#measured).

Known misses on that set (no edge is produced; nothing is guessed):

- loops over `self.routes or []` where `routes` is a property, or over an attribute set by `a if c else b` (3)
- calls on a value returned by a `@property`, e.g. `self.headers.getlist()` (2)
- a union annotation, `Router | Starlette | None`: only `Router`'s method got the edge (1)
- an unannotated attribute, `self.router = Router(...)`, used in another method (1)

Still missed on click: a call inside `if isinstance(command, Group):` and one through a closure variable. The
full list of what isn't resolved is in [What this does not do](https://github.com/tenthlook/tenthcheck#what-this-does-not-do).

- `tenthcheck check <repo>`: import cycles among the repo's modules plus a summary (modules, functions,
  classes and edges parsed; files skipped for syntax errors).
- `tenthcheck check <repo> <file> <proposed_file>`: parse the proposed version of one file in memory and report
  new import cycles plus the dependents of every function or class it changes. Exit code 1 if a new cycle
  appears, 2 on a syntax error in the proposed file.
- `tenthcheck blast <repo> <symbol>`: the functions, classes and modules that call, subclass, import or use
  `<symbol>` (for a class: annotations, constructor calls, `isinstance`, imports), grouped by hop.
- `tenthcheck cycles <repo>`: import cycles among the repo's modules, each printed as a path.
- `tenthcheck scope <before_dir> <after_dir>`: changed functions/classes between two snapshots and the
  untouched code that depends on them.
- `tenthcheck serve [repo]`: an MCP stdio server named `tenthcheck` for `repo` (default: the current
  directory), with four tools: `check_blast_radius`, `find_import_cycles`, `check_proposed_edit`, `scope_audit`.

Scoring is plain graph search over Python's `ast`; no model is called. It does not run your code.

## Install

```
pip install tenthcheck          # CLI (Python >= 3.10; depends on z3-solver and sqlglot)
pip install "tenthcheck[mcp]"   # adds the official MCP Python SDK for `tenthcheck serve`
pip install "tenthcheck[receipt]"   # adds `cryptography` for signed receipts
```

## Set up in Claude Code

Install with the `[mcp]` extra so the `tenthcheck` command is on your `PATH`, then either:

- add it from the repo you want checked (project scope writes `.mcp.json` there):

  ```
  claude mcp add --scope project tenthcheck -- tenthcheck serve
  ```

- or put this in `.mcp.json` at the repo root:

  ```json
  {"mcpServers": {"tenthcheck": {"type": "stdio", "command": "tenthcheck", "args": ["serve"]}}}
  ```

`tenthcheck serve` checks the directory it is started in; Claude Code starts project-scoped servers in the
project root. To check a different directory, pass it: `"args": ["serve", "path/to/src"]`. Run `/mcp` in
Claude Code to confirm the `tenthcheck` server is connected. To have the agent use it, ask it to call
`check_proposed_edit` before writing each file.

## Example

A constructed edit to httpx (`b5addb6`) that adds `from ._urls import URL` to `httpx/_urlparse.py`:

```
$ tenthcheck check . httpx/_urlparse.py proposed_urlparse.py
File: httpx/_urlparse.py
NEW IMPORT CYCLES: 1
  httpx._urlparse -> httpx._urls -> httpx._urlparse   [added by this edit: httpx._urlparse -> httpx._urls]
Changed symbols: 0 (modified 0, added 0, removed 0)
$ echo $?
1
```

## Hook mode: no MCP server (`tenthcheck hook`, v0.3, unreleased)

Instead of tools the agent may or may not call, Tenthcheck can run as a Claude Code **hook** after every Edit or
Write. It prints **nothing** unless the edit:

- **(a)** creates a new import cycle;
- **(b)** changes a function or method signature while some callers no longer fit it: a call that can't bind any more
  (missing / unexpected argument, too many positional), or, after positional parameters were reordered or renamed, a
  positional call still passing its arguments as before. One `file:line` per caller;
- **(c)** optionally (`--dependents N`, off by default) changes a function with at least N direct callers.

On a trip it writes at most 10 lines (`--max-lines`) to stderr and exits 2, which Claude Code shows to the agent
(PostToolUse can't undo the edit; the agent sees the warning next to the tool result). On a pass: no output, exit 0.
Its own errors are logged to `.tenthcheck/hook.log` and never interrupt the agent.

Example (rich, the h02 experiment diff replayed file by file with `--exclude 'tests/*'`; the edit to `rich/measure.py`
reorders `Measurement.get`'s parameters and updates the one caller in that file):

```
Tenthcheck: signature changed rich.measure.Measurement.get(cls, console, options, renderable) -> get(cls, renderable, console, options); 12 callers not updated:
  rich/align.py:238 (Align.__rich_measure__): positional arguments unchanged since the parameters were reordered/renamed
  rich/columns.py:80 (Columns.__rich_console__): positional arguments unchanged since the parameters were reordered/renamed
  ...
```

Callers the agent then fixes are cleared silently. Registered as a **Stop** hook too, it blocks the agent from
finishing once (never twice) while callers of a signature it changed are still not updated.

Setup: put this in `.claude/settings.json` in the repository (needs `tenthcheck` on `PATH`; no `[mcp]` extra):

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Edit|Write|MultiEdit",
       "hooks": [{"type": "command", "command": "tenthcheck hook", "timeout": 30}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "tenthcheck hook", "timeout": 30}]}
    ]
  }
}
```

Options go on the command: `tenthcheck hook --exclude 'tests/*'` (callers in those files are not reported, e.g.
when the agent must not edit tests), `--dependents 20`, `--max-lines 10`, `--root DIR` (default
`$CLAUDE_PROJECT_DIR`). Names are relative to the project root, as with `tenthcheck serve .`.

How it stays fast: a per-file summary (imports, signatures, call sites) is cached in `.tenthcheck/hook_cache.json`
(own `.gitignore`) and only files whose mtime or size changed are re-parsed; the cached summary of the edited file
is its pre-edit state. Measured on rich (213 package files plus tests, battery): first call 1.0 s, then 150-280 ms
per edit. Before the first cached edit of a file, the pre-edit version comes from the Edit's `old_string` /
`new_string` (or `git show HEAD:<file>`).

Limits: callers are resolved per call site from that file's imports (bare names, `mod.f`, `Cls.m`, `self.m`,
constructors, local aliases like `get = Measurement.get`, and `obj.m()` only when `m` is the only method of that
name in the repo); calls through `*args`/`**kwargs` aren't judged; edits made through Bash don't trigger the hook
(their changes are picked up, unattributed, on the next edit); `MultiEdit` is in the matcher in case your Claude Code
has it, but its payload isn't documented. Exit 2 and Stop semantics are from the Claude Code hooks reference
(code.claude.com/docs/en/hooks), checked 2026-11-18.
## Several agents in one repo (`tenthcheck agents`, v0.2, unreleased)

When two or more agent sessions edit the same repository at once, Tenthcheck can warn a session that is about to
edit something inside another session's **blast radius**: a function or class another session edited, or one of
its callers or callees (up to 2 hops, along the edges Tenthcheck resolves). **It only warns; it never blocks** an
edit or changes an exit code, and `check_proposed_edit`'s `ok` is unaffected.

State is a small JSON file, `.tenthcheck/agents.json` (with its own `.gitignore`), listing each live session and
the functions/classes it has edited. Sessions with no edit for 12 hours are ignored and then dropped.

In Claude Code nothing extra needs setting up: each session starts its own `tenthcheck serve`, and each server
process is one session (set `TENTHCHECK_SESSION` / `TENTHCHECK_SESSION_LABEL` to name it). Two MCP tools:

- `record_edit(file_path, new_source)`: call it after writing a file (just before also works). It records what this
  session changed in that file and returns warnings like
  `pkg.api.calculate is a caller of pkg.core.helper (2 hops), which session mcp-4121-a3f0 edited`.
- `agent_sessions()`: who else is editing, and what.

`check_proposed_edit` also returns `agent_warnings` for the proposed file (computed only when another session has
recorded edits, so a single agent pays nothing for it). Ask the agent to call `record_edit` after each write.

From the shell (one `--session` per agent, or `TENTHCHECK_SESSION`):

```
tenthcheck agents edit  . pkg/core.py proposed_core.py --session A   # record (diffs the proposal against disk)
tenthcheck agents edit  . --symbol pkg.core.helper --session A       # or name what was edited
tenthcheck agents check . pkg/api.py proposed_api.py --session B     # warnings only, records nothing
tenthcheck agents list  .
tenthcheck agents end   . --session A
```

Limits: the same static edges as everything else (no warning means "no resolved overlap", not "safe"); a class
used only in annotations and imports has few resolved dependents (seen on rich's `ConsoleOptions`); and the shell
`edit` with a source file diffs against the file on disk, so run it before writing, or use `--symbol`. Tested with
two simulated sessions (`tests/test_agents.py`), not yet with two real agent sessions.

## Measured

All numbers below come from files in `measurements/results/`, produced by the scripts in `measurements/scripts/`. Those files were
recorded before the rename and use the package's previous name (see `CHANGELOG.md`); the numbers are unchanged. Libraries were
shallow clones at the SHAs in `measurements/results/corpus.txt` (click and starlette: their `truth.json`), parsed at the repo root (tests included). Machine and
Python version are in `measurements/results/latency.json`.

### Parsing real libraries (`measurements/scripts/harden.py` -> `measurements/results/hardening.json`)

| Repo (whole clone) | .py files | Parse time | Functions | Classes | CALLS edges | IMPORTS edges | Import-cycle groups | Crashes / skipped files |
|---|---|---|---|---|---|---|---|---|
| httpx | 60 | 0.47 s | 1133 | 107 | 370 | 319 | 0 | 0 / 0 |
| flask | 83 | 0.46 s | 1457 | 154 | 231 | 424 | 1 | 0 / 0 |
| pydantic | 3156 | 7.36 s | 8241 | 9907 | 1731 | 13187 | 0 | 0 / 0 |

Re-run 2026-11-12 with the typed-call pass (below); CALLS edges rose from 259 / 171 / 1,472 (round 1) to the numbers shown, and parse time rose about 0.1 s on httpx and flask and ~1 s on pydantic (parse times in this table: measured on battery; may be slower on AC). The typed pass raised no errors on any of the four libraries parsed. pydantic's clone is mostly `tests/` (2,961 of 3,156 files). Dependents walks never hit the 10,000-node cap (`hit_node_cap` = 0 in every repo).

### Latency (`measurements/scripts/bench_latency.py` -> `measurements/results/latency_2026-11-17_ac.json`)

**This table predates 0.1.2.** 0.1.2's class-edge pass (`REFERENCES`, see [How complete are the edges?](https://github.com/tenthlook/tenthcheck#how-complete-are-the-edges)) adds about 8–10% to parse time (httpx 56 of 578 ms, rich 168 of 1,943 ms, pydantic 837 of 10,710 ms; measured 2026-11-18 on battery), so cold CLI numbers and any call that re-parses changed files are expected to be roughly that much slower. Not re-measured yet.

**Measured 2026-11-17 on the 0.1.1 code, on AC power for the whole run** (`measurements/scripts/ac_bench.sh` checked the power source every 5 s; also recorded in the file's `meta.power`). Apple M4, Python 3.11.18. Milliseconds. **MCP (warm):** one `serve` process per repo, real stdio round trips through the official MCP client, after one untimed call per tool. **CLI (cold):** a fresh process each time, parse included. With n = 5–100 runs, p99 is close to the max; n is shown.

| Repo | Tool | MCP p50 | MCP p99 | n | CLI cold p50 | CLI cold p99 | n |
|---|---|---|---|---|---|---|---|
| httpx | `check_blast_radius` / `blast` | 3.7 | 5.2 | 100 | 601 | 711 | 5 |
| httpx | `find_import_cycles` / `cycles` | 2.6 | 3.8 | 30 | 630 | 646 | 5 |
| httpx | `check_proposed_edit` / `check` | 321.1 | 420.8 | 20 | 1216 | 1268 | 5 |
| httpx | `scope_audit` / `scope` | 1434.6 | 1531.3 | 5 | 1274 | 1351 | 5 |
| flask | `check_blast_radius` / `blast` | 6.8 | 8.9 | 100 | 596 | 610 | 5 |
| flask | `find_import_cycles` / `cycles` | 5.8 | 7.3 | 30 | 534 | 559 | 5 |
| flask | `check_proposed_edit` / `check` | 339.4 | 427.6 | 20 | 994 | 1074 | 5 |
| flask | `scope_audit` / `scope` | 1480.5 | 1760.0 | 5 | 992 | 1031 | 5 |
| pydantic | `check_blast_radius` / `blast` | 119.9 | 813.2 | 100 | 8605 | 8880 | 5 |
| pydantic | `find_import_cycles` / `cycles` | 100.9 | 716.9 | 30 | 8712 | 9226 | 5 |
| pydantic | `check_proposed_edit` / `check` | 4527.6 | 10625.0 | 20 | 17900 | 18276 | 5 |
| pydantic | `scope_audit` / `scope` | 30128.2 | 32017.7 | 5 | 20002 | 20371 | 5 |

The first MCP call in a session parses the repo: httpx 60 ms, flask 11 ms, pydantic 97 ms. That first number is lower than the cold parse time above because the server already parsed the repo once at startup, before the client's first timed call. On pydantic, a warm `blast` call spends about 55 ms re-checking every file's mtime and about 35 ms rebuilding the reverse edge index for the walk (`measurements/results/pydantic_warm_breakdown.txt`, medians of 10, measured 2026-11-11). `check_proposed_edit` rebuilds the whole graph from cached syntax trees (~4.5 s on pydantic). `scope_audit` parses two full snapshots from disk on every call.

**Run-to-run variance is large on this laptop.** A battery run of the same code a few hours earlier
(`measurements/results/latency_2026-11-17.json`) was *faster* than this AC run in 20 of 24 rows, by up to a third
(flask warm `blast`: 4.5 vs 6.8 ms p50; pydantic warm `scope_audit`: 23.2 vs 30.1 s). The 2026-11-11 run, before
the typed-call pass (`measurements/results/latency.json`, power source not recorded), was faster again: warm
`check_proposed_edit` on httpx was 185 ms then, 297 ms on battery and 321 ms on AC today. Some of that is likely
the typed-call pass, but with this much variance between same-day runs these files do not show how much. Treat
the table as one run, not a precise benchmark.

### How complete are the edges?

**Calls: hand checks against true callers** (`measurements/scripts/accuracy_eval.py`). For each symbol, every textual
call site in the package was listed with grep (`measurements/scripts/accuracy_candidates.py`) and read to decide whether it
really calls that symbol; the true direct callers were then compared with `blast`'s hop 1.

| Set | Role | Round 1 parser | + typed-call pass | + overload / `super()` / tuple fixes |
|---|---|---|---|---|
| httpx, 10 symbols, 28 callers (`measurements/results/accuracy_httpx.json`) | tuned-on (its misses designed the typed pass) | 21/28 (75%) | 28/28 | 28/28 |
| click, 14 symbols, 37 callers (`measurements/results/accuracy_click/truth.json`) | **held out**: labelled and hashed before any change, measured once | 12/37 (32%) | **24/37 (65%)** | 35/37 (95%), *now tuned-on* |
| starlette, 23 symbols, 60 callers (`measurements/results/accuracy_starlette/truth.json`) | **held out**: labelled and hashed 2026-11-17 against the current code, measured once | not run | not run | **53/60 (88%)** |

False positives were 0 in every column except one on click before the overload fix. Files:
`measurements/results/accuracy_baseline_before_fixes.json`, `measurements/results/accuracy_after_typed_calls.json`,
`measurements/results/accuracy_after_click_fixes.json`; hashes and dates in `measurements/results/accuracy_click/FROZEN.sha256`. The starlette run (all three sets on the current code,
0 false positives) is `measurements/results/accuracy_starlette/eval_2026-11-17.json`; its hashes are in
`measurements/results/accuracy_starlette/FROZEN.sha256` and its symbol-selection rule is `measurements/scripts/accuracy_select.py`.

**How to read this.** For the current code the held-out number is **88%** (53/60 on starlette, a library no
fix was made for). Click's 95% came after fixes found *by looking at click's misses*, so it is not held out; click's
held-out number (65%) was for the earlier typed-call pass. Starlette's symbols were picked by a fixed frequency
rule and labelled from grep hits without seeing Tenthcheck's output, by Claude, which also wrote the
fixes. All three checks are small (47 symbols, 125 callers in three libraries) and only cover code with type
annotations.

**Classes used without being called (0.1.2).** Up to 0.1.1 a class's only dependents were its subclasses, so
a class used in annotations, constructed, checked with `isinstance` or imported looked unused: in the 2026-11-17
with/without run, `check_blast_radius("rich.console.ConsoleOptions")` returned only the class itself, although 28
rich modules name it. 0.1.2 adds `REFERENCES` edges (function / class / module -> class) for annotations (quoted
ones and `if TYPE_CHECKING:` imports included), constructor calls, `isinstance`, `alias.Class`, module-level and
function-local imports, following re-exports (`src/tenthcheck/references.py`). On rich 15.0.0
(`measurements/scripts/class_refs_rich.py` -> `measurements/results/class_refs_rich_2026-11-18.json`):

| class | direct dependents, 0.1.1 -> 0.1.2 | rich modules with a dependent / naming it (grep) |
|---|---|---|
| `ConsoleOptions` | 0 -> 114 | 27 / 28 |
| `Segment` | 0 -> 136 | 20 / 22 |
| `Style` | 0 -> 184 | 22 / 26 |
| `Text` | 0 -> 252 | 26 / 33 |
| `Measurement` | 0 -> 65 | 18 / 18 |

Every grep-only module was read: each is a mention in a docstring or comment, or demo code under
`if __name__ == "__main__":` (not parsed as entities). This counts modules, not hand-labelled call sites, so it is a
coverage check, not an accuracy measurement like the table above. The edges point only at classes, so callers of
functions are unchanged: the three sets above score exactly the same per symbol after the change
(`measurements/results/accuracy_eval_2026-11-18.json`). Cost: the extra pass is 8-10% of a full parse
(httpx 56 of 578 ms, rich 168 of 1,943 ms, pydantic 837 of 10,710 ms, on battery); the latency table above was
measured before it.

**What the 7 round-1 misses were (httpx):** all were methods called on a typed object other than `self`:
`pattern.matches()` on a loop variable over an annotated dict (2), `response.read()` on a value returned by an
annotated method (3), from `with client.stream() as response` (1), and from `response = yield request` in an
annotated generator (1). None were decorators, aliased imports or true dynamic dispatch. The typed-call pass
(`src/tenthcheck/typed_calls.py`) fixes these by reading annotations.

**What the 13 held-out misses were (click, after the typed pass):** `@typing.overload` stubs took the method's
name, so calls attached to the wrong definition (10); `super().method()` (1); a tuple-unpacked annotated return
(1); `isinstance` narrowing (1). The first three are now fixed; `isinstance` narrowing is not.

**What the 7 held-out misses were (starlette, current code):** loops over `self.routes or []` where `routes` is
a property, or over an attribute set by a conditional expression (3); calls on a `@property`'s return value,
`self.headers.getlist()` (2); a union annotation, `Router | Starlette | None`, where only `Router`'s method got
the edge (1); an unannotated `self.router = Router(...)` attribute (1). None are fixed.

**Imports: compared with a runtime trace** (`measurements/scripts/trace_imports.py`, `measurements/scripts/compare_imports.py` ->
`measurements/results/imports_vs_runtime_*.json`). Each package was imported module by module under an `__import__`
hook that records real module-level imports.

| Package | Modules | Runtime edges | Also found statically | Static-only | Cycle groups match |
|---|---|---|---|---|---|
| httpx | 23 | 80 | 79 | 0 | yes (none) |
| flask | 24 | 72 | 72 | 0 | yes (one group of 9 modules) |

The httpx miss is `httpx -> httpx._main`, an import inside `try:` (see limits). pydantic was not traced: it
needs its compiled core built, so its cycle result has no runtime check.


### Tested in Claude Code

One headless Claude Code session (2026-11-12) did a real refactor on click with only this MCP server
connected: it called `check_proposed_edit` before writing each file and `check_blast_radius` afterwards, and the
answers were correct for that edit. It also exposed a bug (overload stubs named by line, so one inserted line made
24 untouched symbols look changed), fixed the same day. It cost $2.95 and ran 21 turns. Write-up and resulting
diff: `measurements/results/claude_code_mcp_session/` (the raw transcript is not published; it contains local paths). One session on a small, safe refactor; it shows the integration works,
not that it changes agent behaviour.

## What this does not do

- **No semantic checks.** It does not know what your code means, does not run it, and does not tell you
  whether a change is correct. Run your tests.
- **Misses calls it can't type statically.** Resolved: bare names, `module.func`, `self.method()` (including
  inherited), `super().method()`, and `obj.method()` when `obj`'s class is known from a parameter or attribute
  annotation, a constructor, an annotated return value, `with ... as`, a loop over an annotated container, or
  `yield` in an annotated generator. **Not resolved** (no edge, never a guess):
  - untyped parameters and attributes (`def f(x): x.read()`), and values from untyped calls;
  - `isinstance` narrowing (`if isinstance(cmd, Group): cmd.resolve_command()`);
  - closures that use a variable from the enclosing function;
  - `getattr`, `operator.methodcaller`, calls through dicts of functions, callbacks and plugin registries;
  - decorators that replace a function (the call graph sees the original definition);
  - generic type parameters bound at the call site (`T` in `def first(xs: list[T]) -> T`);
  - anything whose type comes from a package that wasn't parsed;
  - when a variable is reassigned to different types in one function, the last assignment wins;
  - values returned by a `@property`, union annotations (on starlette only one member got the edge), unannotated `self.x = Cls(...)`
    attributes used in other methods, and `a or b` / `a if c else b` expressions.
  On click, these left 2 of 37 callers unfound (one closure, one `isinstance` narrowing); on starlette, 7 of 60.
- **Only module-level imports.** Imports inside functions, `if`, `try` or `if TYPE_CHECKING:` blocks are
  not edges (imports under `if TYPE_CHECKING:` are used to resolve type annotations only). This hides the usual deliberate cycle-breakers, and it also hides real imports inside `try`.
- **Star imports** (`from x import *`) record the module edge but bind no names.
- **Python only.** No C extensions, stubs, notebooks, or other languages.
- **Not a judgement on cycles.** Many import cycles through a package `__init__` are intentional and
  harmless (flask has one). `check` reports *new* cycles; whether one matters is your call.
- **Names are relative to the directory you point it at.** Run it on the directory that contains your
  package (`flask/src`, not `flask`) to get ids like `flask.app`.
- **Ambiguous symbols are refused.** `blast` with a name that matches several definitions lists them and
  exits 2; pass the full dotted id.

## Development

```
pip install -e ".[mcp]" pytest
python -m pytest tests            # the MCP test is skipped without the [mcp] extra
```

The analysis core (`graph`, `parser`, `typed_calls`, `topology`, `scope_audit`, `cycles`, `check_edit`,
`index`) is developed in Tenthlook's research codebase and copied here; the first line of each file names its
source. Bug reports and fixes are welcome here.

**Reproducing the measurements.** The scripts in `measurements/scripts/` expect clones under
`~/tenthcheck-corpus/<name>` at the SHAs in `measurements/results/corpus.txt` (click and starlette: their
`truth.json`), e.g. `git clone https://github.com/encode/httpx ~/tenthcheck-corpus/httpx && git -C
~/tenthcheck-corpus/httpx checkout b5addb6`. Run them from `measurements/`:

```
cd measurements
python scripts/accuracy_eval.py ~/tenthcheck-corpus
python scripts/harden.py ~/tenthcheck-corpus results/hardening.json
python scripts/bench_latency.py ~/tenthcheck-corpus results/latency.json
```

On a macOS laptop, `scripts/ac_bench.sh [corpus_dir] [python]` runs the latency benchmark only while on AC
power: it waits for 2 minutes of uninterrupted AC and discards the run if the power source changes.

To check the held-out freeze: `cd measurements && grep results/ results/accuracy_starlette/FROZEN.sha256 |
shasum -a 256 -c`, and from the repo root `grep src/ measurements/results/accuracy_starlette/FROZEN.sha256 |
shasum -a 256 -c`.

MIT licensed.

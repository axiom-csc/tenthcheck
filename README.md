# Tenthcheck

Tenthcheck, from Tenthlook: see what an AI coding agent's edit could break before it lands.

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
- `tenthcheck blast <repo> <symbol>`: the functions, classes and modules that call, subclass or import
  `<symbol>`, grouped by hop.
- `tenthcheck cycles <repo>`: import cycles among the repo's modules, each printed as a path.
- `tenthcheck scope <before_dir> <after_dir>`: changed functions/classes between two snapshots and the
  untouched code that depends on them.
- `tenthcheck serve [repo]`: an MCP stdio server named `tenthcheck` for `repo` (default: the current
  directory), with four tools: `check_blast_radius`, `find_import_cycles`, `check_proposed_edit`, `scope_audit`.

Scoring is plain graph search over Python's `ast`; no model is called. It does not run your code.

## Install

```
pip install tenthcheck          # CLI, no dependencies (Python >= 3.10)
pip install "tenthcheck[mcp]"   # adds the official MCP Python SDK for `tenthcheck serve`
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

### Latency (`measurements/scripts/bench_latency.py` -> `measurements/results/latency_2026-11-17.json`)

**Measured 2026-11-17 on the current code; measured on battery; may be slower on AC** (power source recorded in the file's `meta.power`). Apple M4, Python 3.11.18. Milliseconds. **MCP (warm):** one `serve` process per repo, real stdio round trips through the official MCP client, after one untimed call per tool. **CLI (cold):** a fresh process each time, parse included. With n = 5–100 runs, p99 is close to the max; n is shown.

| Repo | Tool | MCP p50 | MCP p99 | n | CLI cold p50 | CLI cold p99 | n |
|---|---|---|---|---|---|---|---|
| httpx | `check_blast_radius` / `blast` | 3.2 | 4.3 | 100 | 591 | 627 | 5 |
| httpx | `find_import_cycles` / `cycles` | 2.6 | 2.8 | 30 | 605 | 621 | 5 |
| httpx | `check_proposed_edit` / `check` | 297.1 | 346.3 | 20 | 1082 | 1097 | 5 |
| httpx | `scope_audit` / `scope` | 1188.1 | 1234.2 | 5 | 1069 | 1141 | 5 |
| flask | `check_blast_radius` / `blast` | 4.5 | 6.6 | 100 | 577 | 598 | 5 |
| flask | `find_import_cycles` / `cycles` | 4.0 | 4.5 | 30 | 576 | 578 | 5 |
| flask | `check_proposed_edit` / `check` | 287.2 | 348.8 | 20 | 1020 | 1040 | 5 |
| flask | `scope_audit` / `scope` | 1085.8 | 1114.3 | 5 | 1024 | 1033 | 5 |
| pydantic | `check_blast_radius` / `blast` | 104.2 | 645.6 | 100 | 7314 | 7947 | 5 |
| pydantic | `find_import_cycles` / `cycles` | 88.1 | 569.0 | 30 | 7386 | 7457 | 5 |
| pydantic | `check_proposed_edit` / `check` | 4128.0 | 4491.1 | 20 | 15838 | 18779 | 5 |
| pydantic | `scope_audit` / `scope` | 23197.1 | 24064.1 | 5 | 17356 | 18067 | 5 |

The first MCP call in a session parses the repo: httpx 47 ms, flask 8 ms, pydantic 96 ms. That first number is lower than the cold parse time above because the server already parsed the repo once at startup, before the client's first timed call. On pydantic, a warm `blast` call spends about 55 ms re-checking every file's mtime and about 35 ms rebuilding the reverse edge index for the walk (`measurements/results/pydantic_warm_breakdown.txt`, medians of 10, measured 2026-11-11). `check_proposed_edit` rebuilds the whole graph from cached syntax trees (~4 s on pydantic). `scope_audit` parses two full snapshots from disk on every call.

Compared with the 2026-11-11 run before the typed-call pass (`measurements/results/latency.json`, power source not
recorded), warm `blast` and `cycles` are about the same, while warm `check` / `scope` and every cold command are
about 10–60% slower (warm `check_proposed_edit` on httpx: 185 -> 297 ms p50). This run cannot separate the cost
of the typed-call pass from running on battery; re-run on AC power to tell them apart.

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

To check the held-out freeze: `cd measurements && grep results/ results/accuracy_starlette/FROZEN.sha256 |
shasum -a 256 -c`, and from the repo root `grep src/ measurements/results/accuracy_starlette/FROZEN.sha256 |
shasum -a 256 -c`.

MIT licensed.

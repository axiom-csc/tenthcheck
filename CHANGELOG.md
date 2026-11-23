# Changelog

## 0.3.0.dev0 (branch v0.3, unreleased)

- `tenthcheck hook`: Claude Code hook mode (PostToolUse on Edit/Write, and Stop). Silent unless an edit adds an
  import cycle, leaves callers of a changed signature not fitting it (`file:line` list), or optionally touches a
  function with >= N callers. Exit 2 with at most 10 lines; per-file summary cache, 150-280 ms per edit on rich.
## 0.2.0.dev0 (branch v0.2, unreleased)

- `tenthcheck agents` (CLI) and the MCP tools `record_edit` and `agent_sessions`: track which functions/classes each
  running agent session has edited (`.tenthcheck/agents.json`) and warn when a session edits inside another
  session's blast radius (same symbol, callers or callees, 2 hops by default). Warn only: never blocks, never
  changes an exit code or `ok`.
- `check_proposed_edit` returns `agent_warnings` (only computed when another live session has recorded edits).
- The MCP tool list grows from 4 to 6.
- Includes 0.1.2 (merged 2026-11-18): agent blast radii now include users of classes.

## 0.1.2 (unreleased; built and checked, not uploaded)

- **Classes used without being called now have dependents.** New `REFERENCES` edges (function / class / module ->
  class) for annotations (quoted, and names imported under `if TYPE_CHECKING:`), constructor calls, `isinstance`,
  `alias.Class`, module-level and function-local `from x import C`, following re-exports. `blast`,
  `check_blast_radius`, `check_proposed_edit` and `scope` follow them. Before, rich's `ConsoleOptions` had a blast
  radius of 1 (itself); now 114 direct dependents in 27 of the 28 modules that name it.
- Callers of functions are unchanged (edges target classes only): httpx 28/28, click 35/37, starlette 53/60, 0 false
  positives, identical per symbol.
- The new pass adds about 8-10% to parse time.
- `scripts/sync_from_mind.py` restored (takes the axiom-lab checkout as an argument).

## 0.1.1 (not uploaded)

- First version intended for PyPI; 0.1.0 was never uploaded.
- README latency table re-measured on AC power for the whole run (`measurements/results/latency_2026-11-17_ac.json`,
  via the new `measurements/scripts/ac_bench.sh`). It was slower than a same-day battery run of the same code in
  most rows, so the README now says run-to-run variance is large instead of blaming battery power.
- README parse-time numbers (2026-11-12 parsing run) are still from a battery run; may be slower on AC.
- Project URLs added; LICENSE copyright holder is Naren Saravanan.

## 0.1.0 (not released)

- Renamed from **callguard** to **tenthcheck** (package, import name, `tenthcheck` command, MCP server name).
  `callguard` was never published by this project (that name on PyPI belongs to an unrelated project), and no
  `callguard` alias package is provided: uninstall any local editable `callguard` install and install `tenthcheck`.
  The files in `measurements/results/` were recorded before the rename and show the old name; their numbers are unchanged.
- `tenthcheck check <repo>` with no file arguments now prints import cycles plus a summary of the parsed repo.
  `tenthcheck check <repo> <file> <proposed_file>` works as before.
- `tenthcheck serve` defaults to the current directory.

## 1.0.0.dev0 (unreleased)
- `tenthcheck` (no args) / `scan`: zero-config invariant mining (route-guard, call-pair, guard-mutation, sql-scope).
- `init` / `tenthcheck.toml`: accept / reject mined patterns, hand-written numeric invariants.
- `verify`: narrow Z3 checker (straight-line numeric functions), counterexamples, explicit "not checkable" refusals.
- `impact`: uncommitted diff -> changed symbols -> dependents -> Z3 re-check (slow on very large repos: see README).
- `install` / `uninstall` for Claude Code (MCP + hook) and Cursor (MCP): backup, diff, idempotent.
- Hook reports Z3 counterexamples and accepted-invariant violations with file:line.
- `ci`, `action.yml`, signed receipts (`keygen`, `verify-receipt`). Action untested on GitHub.
- Dependencies are now `sqlglot` and `z3-solver` (previously none).

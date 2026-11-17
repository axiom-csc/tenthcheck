# With / without Tenthcheck: plan (written, not run)

**Question.** When Claude Code does a multi-file refactor, does having the tenthcheck MCP server connected
lead to fewer broken tests than the same session without it?

**Status.** Script and plan only. Nothing has been run, no model has been called. Running it needs the user's
go-ahead because it uses about 20 Claude Code sessions of their Claude plan (cost section below). With n = 10
pairs this is a **demonstration**, not a significance test.

## Design

| | |
|---|---|
| Target repo | [Textualize/rich](https://github.com/Textualize/rich), tag `v15.0.0`, commit `6ac483cbea39cab124dfd3483bba70ffafb71050` (pinned in `tasks.json`; `run.py` asserts `HEAD` equals it on every checkout) |
| Why rich | Pure Python, typed (`mypy --strict`), ~80 modules with many cross-module calls, a large and fast pytest suite, no compiled parts, no network in tests. Not used anywhere in Tenthcheck's tuning or accuracy work (httpx, click, flask, starlette, pydantic were), so neither arm benefits from fixes made for it. Static check of the pinned tree with Tenthcheck: 608 modules (tests included), 1,772 functions, 1,678 CALLS edges, 0 import cycles. |
| Tasks | 10 refactors in `tasks.json` (table below). Each is a real move/rename/signature change that crosses module boundaries, with a stated compatibility rule, so a newly failing test means a regression rather than an intended API change. |
| Arms | **with**: the tenthcheck MCP server configured. **without**: no MCP server. Nothing else differs. |
| Runs | 10 tasks x 2 arms = **20 headless sessions**, one per (task, arm). |
| Outcome (primary) | **Newly broken tests**: tests that pass on the pristine pinned checkout and do not pass after the task (failed, errored, or no longer collected). |
| Secondary | done-check passed (did the refactor actually happen, per a small import assertion per task); import cycles after (`tenthcheck cycles`, measured the same way in both arms); turns, wall time, reported cost; number of tenthcheck calls (with arm); whether the agent touched `tests/`. |

### Tasks

| id | refactor | where breakage can come from |
|---|---|---|
| t01_ratio_split | move `ratio_reduce`, `ratio_distribute` from `rich/_ratio.py` to new `rich/_ratio_table.py` | `rich._ratio` re-exports (tests import from there); `table.py` imports |
| t02_protocol_to_abc | move `is_renderable`, `rich_cast` from `rich/protocol.py` to `rich/abc.py` | importers in `measure`, `console`, `table`, ...; `rich.abc` imports `rich.text` under TYPE_CHECKING (cycle risk if made real) |
| t03_rename_adjust_line_length | rename `Segment.adjust_line_length` -> `fit_line_length`, keep alias | callers in `segment.py` (local aliases), `syntax.py` |
| t04_split_crop_width | rename parameter `length` -> `width` of `Segment.split_and_crop_lines` | keyword caller in `console.py` (`length=width`) |
| t05_unpack_padding_function | `Padding.unpack` -> module function `unpack_padding`, static method delegates | 7 call sites in `columns`, `syntax`, `table`, `panel` |
| t06_console_options_module | move `NoChange`, `NO_CHANGE`, `ConsoleDimensions`, `ConsoleOptions` out of `console.py` into `rich/console_options.py` | `ConsoleOptions` uses `JustifyMethod`/`OverflowMethod` defined in `console.py`: the obvious move creates an import cycle; ~29 modules import from `rich.console` |
| t07_merge_emoji_replace | move `_emoji_replace` into `rich/emoji.py`, keep shim | `console.py`, `markup.py` importers; `emoji.py` -> `segment`/`style` import chain |
| t08_rename_loop_last | rename `loop_last` -> `iter_last`, keep alias | 26 uses in 12 modules |
| t09_control_codes_module | move `strip_control_codes`, `escape_control_codes` + tables into `rich/_control_codes.py` | default arguments bind the tables at def time; `text.py`, `_inspect.py`; tests import from `rich.control` |
| t10_wrap_offsets | rename `divide_line` -> `wrap_offsets`, make `fold` keyword-only, keep alias | caller in `text.py`; positional callers break |

Every task prompt is followed by the same `common_rules` text (in `tasks.json`): work only in the checkout, do
not touch `tests/`, keep behaviour and every currently importable name importable, no running code, and
"Before writing each file, check what the change could break using the tools available to you." That sentence
is identical in both arms; in the with arm the tools available include Tenthcheck's, in the without arm only
Read/Grep/Glob. (The README tells users to ask the agent to call `check_proposed_edit`; naming the tool only in
the with arm would make the prompts differ, so the neutral wording is the primary design. A follow-up could add
that explicit instruction as a third arm.)

### Controls

- **Fresh checkout per session**: `git clone --no-checkout` from a local bare mirror, checkout of the pinned SHA,
  `HEAD` asserted. Checkouts live under `~/tenthcheck-with-without/checkouts/`, outside this repo, so the agent
  cannot see Tenthcheck's code or earlier results. Deleted after scoring; the diff, event stream and JUnit XML are
  kept per session in `results/<run_id>/<task>__<arm>/`.
- **Same model and settings in both arms**: `--model` is required and recorded; same `--max-turns` (40),
  `--permission-mode acceptEdits`, session timeout (30 min), identical prompt, identical allowed tools
  (`Read, Edit, Write, Glob, Grep`) plus the four `mcp__tenthcheck__*` tools in the with arm;
  `Bash, WebFetch, WebSearch, Task, NotebookEdit` denied in both. Claude Code version recorded.
- **No running code by the agent**: Bash is denied in both arms, so neither arm can run the tests and fix what
  it broke. This isolates what static checking adds over reading and grepping. (A variant with `pytest` allowed
  would measure something else: whether Tenthcheck saves test-fix cycles.)
- **MCP isolation**: both arms pass `--strict-mcp-config` with their own config file, so the user's own MCP servers
  are not loaded in either arm. The without arm's file is `{"mcpServers": {}}`.
- **Order**: task order shuffled with a fixed seed (`--seed`, default 20261117, recorded); within each pair the arm
  that goes first is balanced (5 pairs start with each arm, assignment shuffled). Both sessions of a pair run back
  to back, so time-of-day / service drift is shared within a pair.
- **Tests restored before scoring**: any change the agent made under `tests/` is recorded (`touched_tests`) and
  then reverted (`git checkout <sha> -- tests` + `git clean tests`), so a session cannot "pass" by editing tests.
- **Test environment**: one venv with rich's test deps (`pygments`, `markdown-it-py`, `pytest<9`, `attrs`,
  `typing-extensions`), pytest with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and no cache (globally installed plugins
  broke click's suite in the 2026-11-12 session). The baseline passing set is recorded once by `--setup`.
- **Billing**: `run.py` removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the child environment so
  `claude` uses the logged-in Claude plan, never an API key that happens to be set.

### How Tenthcheck is configured (with arm)

From the README: install `tenthcheck[mcp]`, run `tenthcheck serve <dir>` as a stdio MCP server named
`tenthcheck`. `run.py --setup` installs this repo's checkout into `~/tenthcheck-with-without/venv-tenthcheck`, and
each with-arm session gets this config (absolute paths), passed with `--mcp-config <file> --strict-mcp-config`:

```json
{"mcpServers": {"tenthcheck": {"type": "stdio",
  "command": "~/tenthcheck-with-without/venv-tenthcheck/bin/tenthcheck",
  "args": ["serve", "<fresh checkout>"]}}}
```

rich's package sits at the repo root, so the served directory is the checkout itself (ids like `rich.console`).
The `init` event in each session's stream records which MCP servers connected (`mcp_servers` in results), so a
with-arm session where the server failed to start is visible.

## Running it

```
cd experiments/with_without
python run.py --dry-run --model <model>       # schedule + exact claude command; no clone, no model
python run.py --setup                         # clone rich, venvs, baseline test run; no model (~2 min)
python run.py --model <model> --i-approve-plan-usage          # 20 sessions
python run.py --model <model> --i-approve-plan-usage --only t06_console_options_module   # one pair first
python run.py --model <model> --i-approve-plan-usage --resume <run_id>                 # after an interruption
python analyze.py results/<run_id>/results.json --json results/<run_id>/summary.json
```

Without `--i-approve-plan-usage` the script refuses to start any session. Recommended: run one pair with `--only`
first and read its two `stream.jsonl` files before spending the other 18 sessions. Your own `~/.claude` settings
and CLAUDE.md files apply equally to both arms; to exclude them, pass your Claude Code version's flag for setting
sources through `--extra-claude-arg` (check `claude --help`; `--dry-run` shows the final command).

## Results schema (`results/<run_id>/results.json`)

```jsonc
{
  "schema": "tenthcheck-with-without/1",
  "run_id": "20261118-101500",
  "config": {
    "model": "...", "claude_code_version": "...", "max_turns": 40, "seed": 20261117,
    "session_timeout_s": 1800, "extra_claude_args": [],
    "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
    "mcp_tools_with_arm": ["mcp__tenthcheck__check_blast_radius", "..."],
    "denied_tools": ["Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit"],
    "target": {"repo": "...", "tag": "v15.0.0", "commit": "6ac483c...", "...": "..."},
    "common_rules": "...",
    "baseline_passed": 0, "baseline_collected": 0,      // from --setup
    "order": [["t08_rename_loop_last", "without"], ["t08_rename_loop_last", "with"], "..."]
  },
  "runs": [{
    "task_id": "t06_console_options_module", "arm": "with",
    "order_in_pair": 1, "started_utc": "2026-11-18T10:15:02+00:00",
    "claude_exit": 0, "claude_stderr_tail": "", "wall_s": 412.3,
    "num_turns": 27, "duration_ms": 409000, "reported_cost_usd": 2.1,   // as reported by Claude Code
    "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "...": 0},
    "is_error": false, "mcp_servers": [{"name": "tenthcheck", "status": "connected"}],
    "tool_calls": {"Read": 9, "Edit": 6, "mcp__tenthcheck__check_proposed_edit": 4},
    "tenthcheck_calls": 5,
    "files_changed": ["rich/console.py", "rich/console_options.py"],
    "touched_tests": [], "diff_lines": 310,
    "tests_collected": 0, "tests_passed": 0,
    "newly_broken": 0, "newly_broken_ids": [],
    "done_check_passed": true, "done_check_error": "",
    "import_cycles_after": "..."   // tail of `tenthcheck cycles` output
  }]
}
```

`reported_cost_usd` is Claude Code's own API-price figure; on a subscription plan it is not money spent.

## Analysis (`analyze.py`)

Per task: newly broken tests without vs with, the paired difference (without - with; positive means the
Tenthcheck arm broke fewer), done-check in each arm, tenthcheck calls, which arm went first. Summary: totals,
tasks with any breakage per arm, mean and median paired difference, bootstrap 95% CI of the mean, wins/ties/losses,
two-sided exact sign test and exact paired permutation test, and how many with-arm sessions never called Tenthcheck.

**How to read it, honestly.** One session per arm per task, one repo, one model. Session-to-session variance in
Claude Code is large (the same prompt can break 0 tests one time and 5 the next), and that variance is not
measured here. A 10-0 sign split would give p ~ 0.002, 8-2 gives p ~ 0.11, and most splits on easy tasks will be
ties at 0. So the result is a demo with a direction, reported in full with the diffs, not a claim of an effect.
The p-values are printed for transparency only. A with-arm session that never called Tenthcheck still counts in
the with arm (intention to treat); the call count is reported next to it. To make a claim, repeat each (task, arm)
3-5 times and/or add tasks, which multiplies the cost below.

## Cost estimate (an estimate; nothing has been run)

- **Sessions: 20** headless Claude Code sessions (10 tasks x 2 arms), plus 0 for `--setup` / `--dry-run` /
  analysis, which call no model.
- **Tokens per session (rough):** a 3-8 file refactor on rich, 20-40 turns. rich's big files are large
  (`console.py` ~2,600 lines, roughly 30k tokens if read whole). Context grows from ~20k to ~80-120k tokens, so a
  session processes about **1-3M input tokens, mostly cache reads (~90%)**, ~100-300k cache-write/uncached input,
  and **~20-60k output tokens** (including thinking). The with arm likely costs **more**, not less:
  `check_proposed_edit` takes the whole proposed file as an argument, so each call re-emits the file as output
  tokens (~30k for `console.py`).
- **Reference point:** the one measured session (2026-11-12, a smaller one-helper move on click, Tenthcheck
  connected) ran 21 turns in 500 s with a reported cost of $2.95.
- **What it means for the plan:** it uses the user's Claude subscription plan usage, not API money. 20 sessions of
  ~5-15 minutes each, ~2-5 hours of agent time if run back to back. That is a heavy burst: on a Pro plan it will
  likely hit the 5-hour usage limit partway and need two or more windows (`--resume` continues); on a Max plan it
  is a sizeable share of a window; it also counts toward any weekly limit.
- **API-price equivalent, for reference only (estimate, not a bill):** at Claude Opus 5.5 list prices ($4 / $20 per
  MTok input/output, $0.20 cache read, cache writes taken as 1.25x input), ~$1.5-4 per session, so **~$30-80 for the
  20 sessions**; at Claude Sonnet 5.5 ($2 / $10, $0.20 cache read) roughly **~$15-45**. Each session's real
  `reported_cost_usd` is recorded, so the actual figure is known after the first pair.

## What is waiting on the user

1. Approve plan usage for 20 sessions and choose the model (`--model`).
2. Run `--setup` (clones rich, installs its test deps, runs its test suite once for the baseline).
3. Optionally run one pair first (`--only t06_console_options_module`), then the rest.

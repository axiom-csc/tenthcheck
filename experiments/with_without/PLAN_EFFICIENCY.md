# With / without Tenthcheck, efficiency version: plan (written 2026-11-18, not run)

## Why the design changed

The breakage design (PLAN.md) found nothing to measure. In 12 unaided or paired sessions on rich, with Sonnet and no
way to run code, no session broke a previously passing test through a missed caller:

| run | sessions | newly broken (scored) | missed callers (static check, below) |
|---|---|---|---|
| 2026-11-17, t03/t06/t08/t09, with + without | 8 | 0 | t06 28/28 and t08 17/17 updated in both arms |
| 2026-11-18, h01 (move `Style`, cycle trap), without | 1 | 0 | (move task; no caller rule) |
| 2026-11-18, h02/h03/h04, without | 3 | 0 (h02's one failure was a test calling the changed API through a helper; now excluded) | h02 13/13 (4 through local aliases), h03 14/14; h04 needed no caller change |

The hard tasks were designed to break unaided agents (aliased calls, positional reorders, a return type grep can't
separate from `str.split`, an import-cycle trap) and still didn't. So "does Tenthcheck prevent breakage" can't be
answered at this task size with this model: the without arm is already at zero. What Tenthcheck could plausibly
change is **how much work the agent does to get there**: fewer greps and file reads, less context, less time, or the
opposite (tool schemas and tool output cost context too).

## Question

On the same refactors, does connecting the tenthcheck MCP server change (1) tokens processed, (2) search tool calls
(Grep / Glob / Read) and the characters they return, (3) time, and (4) whether the final change missed any caller?

Hypotheses, written before any run (direction stated so the result can contradict it):
- H1: the with arm makes **fewer** search calls (Grep + Glob + Read). The pilot does **not** support this (13 vs 7
  on t06, 14 vs 13 on t08, 2 vs 5 on t09, 3 vs 3 on t03).
- H2: the with arm processes **more** tokens. The pilot supports this (all 4 pairs, +24% to +76%); the likely causes
  are the 4 tool schemas in every turn, one extra ToolSearch turn to load them, and `check_proposed_edit` echoing
  whole files as output tokens.
- H3: missed callers are 0 in both arms (the pilot says the without arm already gets them all).
If H1 fails and H2 holds again, the honest conclusion is that on tasks of this size Tenthcheck costs context and
saves nothing measurable, and that should be reported as such.

## Design

| | |
|---|---|
| Target | rich `v15.0.0` @ `6ac483c` (as PLAN.md), baseline 949 of 981 tests pass |
| Tasks (4) | the four with the most caller-finding work: **t08_rename_loop_last** (17 call sites, 12 modules), **h02_measurement_get_order** (13 sites incl. 4 through local aliases, 7 modules), **h03_render_lines_options_kwonly** (14 sites, 11 modules), **t06_console_options_module** (28 importing sites; the class case Tenthcheck 0.1.2 fixed) |
| Arms | **with** (tenthcheck MCP server, **0.1.2**: the venv must be reinstalled from the current checkout first; the 11-17 pilot ran 0.1.1, which saw no dependents for ConsoleOptions) / **without** (no MCP server). Prompt, rules, tools otherwise identical (PLAN.md controls: fresh checkout per session, `--strict-mcp-config`, no Bash, tests/ restored before scoring) |
| Repeats | **3 sessions per (task, arm)** = **24 sessions**. One session per cell can't separate an effect from session-to-session variance; 3 gives a range per cell |
| Order | per repeat, tasks shuffled with a fixed seed and the first arm alternated (balanced), as PLAN.md; repeats interleaved (all tasks of repeat 1 before repeat 2) so drift over the day is spread across arms |
| Model | `claude-sonnet-5-5`, max-turns 40, session timeout 30 min |

### Measures (all computed after the run from saved files; nothing new is asked of the agent)

| measure | source | tool |
|---|---|---|
| tokens processed (input + cache read + cache creation + output), and output tokens alone | `result` event usage in `stream.jsonl` | `efficiency_metrics.py` |
| reported cost (Claude Code's API-price figure; plan usage, not money) | `result` event | same |
| tool calls by type; **search calls** = Grep + Glob + Read; edit calls; tenthcheck calls | `tool_use` blocks | same |
| characters returned into context by tools, per tool | `tool_result` blocks | same |
| **model time** (`duration_ms`) as the primary time measure; wall time secondary | `result` event / runner | same (one 11-17 session took 96 s of model time but 49 min of wall time) |
| turns | `result` event | same |
| **missed callers**: every call site / import the change must update, found in the pristine tree with plain `ast` (no Tenthcheck code), then classified in the agent's final tree as updated / missed / gone | `agent.diff` + pristine checkout | `caller_check.py` (validated on the existing diffs: t06 28/28, t08 17/17 in both arms; h02 13/13, h03 14/14) |
| newly broken tests (scored, API-direct tests excluded) and done-check | runner, as before | `run.py` |

### Analysis

Per task and arm: median and range over the 3 sessions for each measure. Per task: the with/without ratio of
medians. Overall: the median of the 4 per-task ratios, and how many of the 4 tasks point the same way. No
significance tests: 3 x 4 per arm can only show large, consistent differences (roughly 30% or more on a measure
that varies by ±20% within a cell); smaller ones are reported as "no detectable difference". "gone" sites in the
caller check are read by hand and reported individually. Everything (streams, diffs, junit) is kept per session.

### What is needed before running (small, not done yet)

1. `run.py --repeats N`: key sessions by (task, arm, repeat) instead of (task, arm); per-repeat schedule.
2. Reinstall `~/tenthcheck-with-without/venv-tenthcheck` from the current checkout (0.1.2) and record the version in
   `results.json`.
3. An `analyze_efficiency.py` that joins `efficiency_metrics.py` and `caller_check.py` output into the per-task table.

## Cost estimate (from the 12 sessions already run; an estimate)

Reported cost per session so far: t08 $0.20 / $0.21 (without / with), h02 $0.14 (without), h03 $0.16 (without),
t06 $0.33 / $0.42. Taking the with arm at +18% where only a without session exists:

| | per repeat (4 tasks) | x 3 repeats |
|---|---|---|
| without | ~$0.83 | ~$2.5 |
| with | ~$0.98 | ~$2.9 |
| **total** | ~$1.8 | **~$5.5** (plausible range $4–8; a long session or a 40-turn cap hit moves it) |

That is reported, API-price-equivalent cost; it runs on the Claude plan. Time: 24 sessions of 20–100 s model time,
about **25–45 minutes** back to back plus ~1 minute of test scoring per session (another ~25 minutes), assuming no
hang like the 49-minute one on 11-17 (the 30-minute session timeout would bound it, but that case exited normally
after the timeout window, which still needs explaining).

Optional third arm, **instructed**: tenthcheck connected and the prompt adds "call check_blast_radius on each symbol
you change before editing" (the README's advice). This measures Tenthcheck used as intended rather than offered.
+12 sessions, about **+$3**.

## What is waiting on the user

1. Approve the 24 sessions (~$5.5 reported, plan usage), and whether to add the instructed arm (+12, ~$3).
2. Then: the three small runner changes above, a 1-repeat smoke run of one task in both arms, and the full run.

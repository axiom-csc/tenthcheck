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

## Design (revised 2026-11-18: three arms, two models, 2 runs each)

| | |
|---|---|
| Target | rich `v15.0.0` @ `6ac483c` (as PLAN.md), baseline 949 of 981 tests pass |
| Tasks (4) | `tasks_efficiency.json`, the four with the most caller-finding work, one shared rules text: **t08_rename_loop_last** (17 call sites, 12 modules), **h02_measurement_get_order** (13 sites incl. 4 through local aliases, 7 modules), **h03_render_lines_options_kwonly** (14 sites, 11 modules), **t06_console_options_module** (28 importing sites; the class case Tenthcheck 0.1.2 fixed) |
| Arms (3) | **without**: no Tenthcheck. **with** (MCP): the tenthcheck MCP server, 4 tools offered. **hook**: no MCP server; `tenthcheck hook --exclude 'tests/*'` as PostToolUse (Edit/Write/MultiEdit) and Stop hooks, passed per session with `--settings`. The hook prints nothing unless an edit adds an import cycle or leaves callers of a changed signature not fitting it, and Stop blocks once while such callers remain. tests/ callers are excluded because the task forbids editing tests. Everything else identical (PLAN.md controls: fresh checkout per session, `--strict-mcp-config`, no Bash, tests/ restored before scoring). The user's own settings have no hooks, so no other hook runs in any arm |
| Tenthcheck version | `0.3.0.dev0` (branch v0.3 = 0.1.2 plus the hook), installed in `~/tenthcheck-with-without/venv-tenthcheck` for both the MCP and hook arms and recorded in `results.json` (`tenthcheck_version`). The 11-17 pilot's MCP arm ran 0.1.1 |
| Models (2) | `claude-sonnet-5-5` and `claude-haiku-5-5`, one run directory each, same seed |
| Runs | **2 per (task, arm, model)**: 4 tasks x 3 arms x 2 runs = 24 sessions per model, **48 in total** |
| Order | per run, tasks shuffled with a fixed seed and the three arms rotated across tasks (Latin square), the three sessions of a task back to back; run 1 starts after all of run 0. Over 2 runs each arm goes first 2-3 times (4 tasks x 3 arms can't balance exactly) |

Command (per model; not run):
```
python run.py --tasks tasks_efficiency.json --model claude-sonnet-5-5 --arms without,with,hook --repeats 2 --i-approve-plan-usage
python run.py --tasks tasks_efficiency.json --model claude-haiku-5-5  --arms without,with,hook --repeats 2 --i-approve-plan-usage
python analyze_efficiency.py <pristine rich checkout> results/<sonnet run> results/<haiku run> --json results/efficiency_summary.json
```

### Measures (all computed after the run from saved files; nothing new is asked of the agent)

| measure | source | tool |
|---|---|---|
| tokens processed (input + cache read + cache creation + output), and output tokens alone | `result` event usage in `stream.jsonl` | `efficiency_metrics.py` |
| reported cost (Claude Code's API-price figure; plan usage, not money) | `result` event | same |
| tool calls by type; **search calls** = Grep + Glob + Read; edit calls; tenthcheck MCP calls | `tool_use` blocks | same |
| **hook messages** fed back to the agent (hook arm) | stream lines carrying `Tenthcheck:` outside assistant turns | `run.py` (`hook_feedback`) |
| characters returned into context by tools, per tool | `tool_result` blocks | `efficiency_metrics.py` |
| **model time** (`duration_ms`) as the primary time measure; wall time secondary | `result` event / runner | same (one 11-17 session took 96 s of model time but 49 min of wall time) |
| turns | `result` event | same |
| **missed callers**: every call site / import the change must update, found in the pristine tree with plain `ast` (no Tenthcheck code), then classified in the agent's final tree as updated / missed / gone | `agent.diff` + pristine checkout | `caller_check.py` (validated on the existing diffs: t06 28/28, t08 17/17 in both arms; h02 13/13, h03 14/14) |
| newly broken tests (scored, API-direct tests excluded) and done-check | runner | `run.py` |

All of it is joined by `analyze_efficiency.py` (checked on the 12 recorded sessions).

### Analysis

Per (model, task, arm): median and range over the 2 runs for each measure. Per (model, task): each arm's median
divided by the without arm's. Overall, per model and arm: the median of the 4 per-task ratios, and how many of the 4
tasks point the same way. Missed callers and broken tests are reported as counts per cell, not medians. No
significance tests: 2 runs x 4 tasks per cell can only show large, consistent differences; anything else is "no
detectable difference". Haiku is where missed callers are most likely to be non-zero, so it is the main chance for the
hook to show a correctness effect; Sonnet mainly tests the cost side.

Pilot (11-17 and 11-18 sessions, Sonnet, `analyze_efficiency.py`): MCP vs without processed **1.24-1.76x** the tokens
on all four paired tasks, with search calls 0.4-1.86x (no consistent reduction) and 0 missed callers in both arms
where checkable. The hook arm has not been run at all. Expectation, stated before running: the hook adds almost
nothing on a pass (no tool schemas, no output), so its token cost should be close to without unless it trips; where
it trips, it costs a few hundred tokens and should only matter if a caller would otherwise be missed.

## Cost estimate (from the 12 Sonnet sessions already run; an estimate)

Reported cost per Sonnet session so far (without / MCP): t08 $0.20 / $0.21, h02 $0.14 / -, h03 $0.16 / -, t06
$0.33 / $0.42; mean without about **$0.21**, MCP about +18%. The hook arm is assumed at without +5% (no schemas; a
few short messages when it trips).

| | per session | 8 sessions (4 tasks x 2 runs) |
|---|---|---|
| Sonnet, without | ~$0.21 | ~$1.7 |
| Sonnet, MCP | ~$0.25 | ~$2.0 |
| Sonnet, hook | ~$0.22 | ~$1.8 |
| **Sonnet total (24)** | | **~$5.5** (plausible $4-8) |
| **Haiku total (24)** | ~$0.01-0.03 | **~$0.3-0.7** |
| **all 48** | | **~$6** (plausible $4.5-9) |

Prices used: Sonnet 5.5 $2 / $10 per MTok in/out, $0.20 cache reads; Haiku 5.5 $0.10 / $0.50 (prompts up to 100K
tokens), cache reads assumed at about 10% of input (not listed in the reference I checked; an assumption). Haiku is
about 20x cheaper per token, so it barely moves the total even if it takes twice the turns or hits the 40-turn cap.
Reported cost is an API-price figure; the run uses the Claude plan. Time: 48 sessions of roughly 20-100 s model time
plus ~1 minute of test scoring each, about **1.5-2 hours** back to back (Haiku is faster per turn).

This replaces the earlier proposal (Sonnet only, MCP vs without, 3 runs, 24 sessions, ~$5.5); the optional
"instructed" arm remains optional (+16 sessions with both models, ~+$2).

## What is waiting on the user

1. Approve the 48 sessions (~$6 reported, plan usage).
2. Then: a 1-run smoke of one task in all three arms on Sonnet (3 sessions, ~$0.7), read the hook arm's stream to
   confirm the hook fired as designed, then the full run.

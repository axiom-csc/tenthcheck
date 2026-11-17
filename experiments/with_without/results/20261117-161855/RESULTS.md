# Reduced with/without run, 2026-11-17 (4 of 10 tasks, Sonnet)

Run `20261117-161855`, model `claude-sonnet-5-5`, Claude Code 2.1.326, seed 20261117, max-turns 40, same prompt
and tools in both arms (PLAN.md). Baseline: 949 of 981 rich tests pass at the pinned commit. Approved by the user
as a reduced run: 4 tasks x 2 arms = 8 sessions.

Tasks picked for a mix: two easy renames (t03, t08) and the two designed to be hard (t06: obvious move creates an
import cycle, ~29 importers; t09: default arguments bind the tables at def time).

| task | newly broken, without | newly broken, with | done-check w/o / with | Tenthcheck calls | turns w/o / with | reported cost w/o / with |
|---|---|---|---|---|---|---|
| t03_rename_adjust_line_length (easy) | 0 | 0 | pass / pass | 1 | 11 / 12 | $0.11 / $0.12 |
| t08_rename_loop_last (easy, 12 modules) | 0 | 0 | pass / pass | 1 | 39 / 37 | $0.20 / $0.21 |
| t06_console_options_module (hard) | 0 | 0 | pass / pass | 4 | 40 / 52 | $0.33 / $0.42 |
| t09_control_codes_module (hard) | 0 | 0 | pass / pass | 2 | 11 / 11 | $0.14 / $0.17 |
| **total** | **0** | **0** | 4/4 / 4/4 | 8 | | $0.78 / $0.92 |

(Reported cost is Claude Code's API-price figure; the run used the Claude plan, not API money.)

**Result: 4 ties at zero.** Neither arm broke any previously passing test, every refactor passed its done-check,
neither arm touched `tests/`, and neither arm left an import cycle. On these 4 tasks, with this model, there was
nothing for Tenthcheck to prevent, so this run says **nothing** about whether Tenthcheck reduces breakage. It is
not evidence that it doesn't help either: with 4 pairs, even a 4-0 split would only give p = 0.125. The with arm
cost about 18% more (reported) and took more turns on t06.

What the with-arm sessions did with the tool:
- t06: `find_import_cycles` twice (0 cycles), `check_blast_radius` on `rich.console.ConsoleOptions` and
  `ConsoleDimensions`. **Both blast radii came back as 1 (the class itself)**, although ~29 modules import
  ConsoleOptions. Tenthcheck's graph counts CALLS edges, so a class used in annotations and imports looks like it has
  no dependents. That is a real limitation worth fixing (import/annotation edges for classes); the agent found the
  importers with Grep anyway.
- t09: `check_proposed_edit` on both new files (ok, no new cycles).
- t03, t08: one `check_blast_radius` each.

Oddities, recorded rather than hidden:
- t06 without: 96 s of model time, but the `claude` process didn't exit for ~49 minutes (wall 2,957 s, exit 0, no
  timeout fired). The edits and score are unaffected; the cause wasn't investigated.
- t06 with reports 52 turns despite `--max-turns 40`; the turn count comes from Claude Code's own result event.
- t06 without reports exactly 40 turns, so it may have stopped at the cap; its done-check and tests passed anyway.

Setup fix made before this run (in run.py): the mirror was a `--filter=blob:none` partial clone, and local clones of
it had no file contents (the first baseline was 0 of 0 tests). The mirror is now a full bare clone, and setup fails
loudly when the baseline has no passing tests.

Files per session in `<task>__<arm>/`: `stream.jsonl`, `agent.diff`, `junit.xml`, `mcp_config.json`.
Summary: `summary.json` (from `analyze.py`).

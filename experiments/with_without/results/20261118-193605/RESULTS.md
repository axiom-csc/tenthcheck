# Breakage dry run, 2026-11-18: h01_style_module, without Tenthcheck only (1 session)

Purpose: before spending plan usage on the hard task set (`tasks_hard.json`), check that an unaided agent actually
breaks something on a task designed to be hard. **It did not.**

- Model `claude-sonnet-5-5`, Claude Code 2.1.326, same rules and tools as the without arm (no Bash, no running code).
- 31 turns, 93 s, reported cost $0.47 (API-price figure; plan usage, not money).
- 24 files changed, `tests/` untouched, done-check passed, 0 import cycles.
- **0 newly broken tests** (949 of 949 baseline-passing tests still pass; no tests excluded for this task).

Why the trap didn't fire: the task's risk was that `Style` uses module-level helpers in `style.py` (`_Bit`,
`_hash_getter`, `_id_generator`) and `NULL_STYLE`; importing those back from `rich.style` into `rich/_style.py`
while `rich.style` re-exports from `rich._style` would be an import cycle. The agent moved the helpers and
`NULL_STYLE` into `_style.py` with the class and made `style.py` a re-export, which is the correct move.

So this task does not produce breakage for Sonnet unaided, and a with/without pair on it would most likely be
another tie. The other three hard tasks (h02-h04) have not been run.

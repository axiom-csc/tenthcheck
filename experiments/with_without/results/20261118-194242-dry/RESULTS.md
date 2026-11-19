# Breakage dry runs, 2026-11-18: h02, h03, h04 without Tenthcheck (3 sessions, Sonnet)

| task | turns | model s | reported cost | files | scored newly broken | missed callers (caller_check.py) |
|---|---|---|---|---|---|---|
| h02_measurement_get_order | 17 | 22.5 | $0.14 | 11 | **0** (see note) | 0 of 13 (4 of them through local aliases like `measure = Measurement.get`) |
| h03_render_lines_options_kwonly | 20 | 26.4 | $0.16 | 11 | 0 | 0 of 14 |
| h04_text_split_list | 9 | 23.6 | $0.14 | 1 | 0 | none needed: every `Text.split` caller (text.py x5, syntax.py x2, containers.py) only iterates, indexes, slices or takes `len`, all list-compatible |

Total $0.44 reported (cap $1.50). **No task broke anything through a missed caller.**

Note on h02: the run first reported 1 newly broken test, `test_table::test_render_table`. It calls the changed API
(`Measurement.get` in the old order) through a module helper, `render_tables()`, so it is an intended-API-change
failure, not a missed caller. `run.py`'s exclusion now also excludes tests that call a same-module helper containing
the pattern; rescored from the saved junit, h02 is 0 newly broken (7 broken tests, all excluded as API-direct).

Consequence: the breakage framing is dropped; see PLAN_EFFICIENCY.md.

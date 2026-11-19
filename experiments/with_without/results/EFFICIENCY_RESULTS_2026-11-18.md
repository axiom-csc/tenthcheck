# Efficiency experiment, 2026-11-18: without / MCP / hook, Sonnet and Haiku (48 sessions)

Plan: `../PLAN_EFFICIENCY.md` (written before the run). Runs: `20261118-205609-eff-sonnet`, `20261118-212037-eff-haiku`.
Summary data: `efficiency_summary_2026-11-18.json` (`analyze_efficiency.py`). Tenthcheck `0.3.0.dev0` for both tool
arms. 4 tasks (t08, h02, h03, t06) x 3 arms x 2 runs per model. Claude Code 2.1.326, no Bash, tests/ restored.

## Headline: nulls on correctness, no consistent cost effect for Sonnet, a weak hint of less work for Haiku

**Correctness: nothing to prevent, on either model.** All 48 sessions: 0 newly broken tests, all done-checks
passed. Missed callers (static check, independent of Tenthcheck): **0 in all 40 sessions except Haiku without on
t06, 1 in each of its 2 runs**, and both are the same import in `rich/palette.py` inside an
`if __name__ == "__main__":` demo block. That import keeps working through `rich.console`'s re-export (no test or
behaviour depends on it), and Tenthcheck can't see code under `__main__` at all, so the MCP and hook arms getting it
right on t06 was **not** due to Tenthcheck: the hook never fires on t06 (no signature change) and the MCP blast radius
does not list `rich.palette`. **Haiku also misses nothing that matters.**

## Per model and arm (totals over the 8 sessions of each cell)

| model | arm | tokens processed | output tokens | search calls (Grep+Glob+Read) | hook messages shown | MCP calls | model time | missed callers | broken tests | reported cost |
|---|---|---|---|---|---|---|---|---|---|---|
| Sonnet | without | 2.70M | 64k | 93 | – | – | 376 s | 0 | 0 | $1.81 |
| Sonnet | MCP | 2.41M | 61k | 70 | – | 9 | 359 s | 0 | 0 | $1.83 |
| Sonnet | hook | 2.41M | 60k | 60 | 4 | – | 362 s | 0 | 0 | $1.75 |
| Haiku | without | 5.08M | 148k | 209 | – | – | 635 s | 2 (cosmetic, see above) | 0 | $0.22 |
| Haiku | MCP | 4.63M | 102k | 109 | – | 15 | 436 s | 0 | 0 | $0.17 |
| Haiku | hook | 4.69M | 117k | 155 | 4 | – | 514 s | 0 | 0 | $0.18 |

Total reported cost $5.96 (API-price figure; plan usage), plus $0.15 for one pilot hook session set aside
(below). Totals are dominated by t06 (the long task); the per-task view below is the fairer one.

### Per-task ratio of medians vs without (2 runs per cell): median of the 4 tasks [min, max]

| model | arm | tokens | search calls | model time | tasks where the arm used less model time |
|---|---|---|---|---|---|
| Sonnet | MCP | 0.95 [0.78, 1.12] | 0.95 [0.28, 1.26] | 0.98 [0.91, 1.06] | 2 of 4 |
| Sonnet | hook | 0.87 [0.79, 1.13] | 0.57 [0.43, 2.14] | 0.98 [0.79, 1.08] | 3 of 4 |
| Haiku | MCP | 0.93 [0.72, 1.36] | 0.67 [0.23, 0.82] | 0.80 [0.56, 0.89] | **4 of 4** |
| Haiku | hook | 0.94 [0.84, 1.02] | 0.76 [0.60, 1.00] | 0.90 [0.73, 0.93] | **4 of 4** |

How to read it:
- **Sonnet: no detectable difference** in tokens or time for either tool arm; ratios straddle 1 with a ±20% spread.
  The 11-17 pilot's "MCP costs 1.24-1.76x the tokens" (Tenthcheck 0.1.1, one session per cell) **did not replicate**:
  here MCP was 0.78-1.12x. Session-to-session variance explains both.
- **Haiku: both tool arms did less work on all 4 tasks** (fewer search calls on 4/4 for MCP and 3/4 for hook with
  one tie; less model time on 4/4 for both). Tokens did not drop consistently. With 2 runs per cell and the largest
  difference on t06 (Haiku without ran 75-76 turns there), this is a hint worth re-testing, not a result.
- **Correctness: null.** No arm ever needed to prevent a break.

## The hook arm, as observed

- `tenthcheck hook` ran after every Edit/Write (12-33 times per session) and on every Stop. It tripped **exactly
  once in each h02 and h03 session** (8 of 8, both models), right after the agent changed the signature, listing the
  callers (e.g. "signature changed rich.measure.Measurement.get(cls, console, options, renderable) -> get(cls,
  renderable, console, options); 13 callers not updated:" + 9 more lines). It was silent on every other edit and on
  all t06/t08 sessions (no signature change). **The Stop hook never blocked**: every flagged caller was fixed first.
- In all 8 sessions the agent had already planned to update the callers; the hook's list confirmed the set (one Sonnet
  reply: "The Tenthcheck hook flagged 13 callers as not updated. It fired after the first edit, before I'd changed
  the callers. A grep afterwards shows every call in the new order.").
- Cost of the hook itself: 4 messages per model, a few hundred tokens each; no tool schemas in context.

## Deviations and caveats

- **Pilot re-run.** The first pilot hook session (Sonnet h02 r0) ran before `--include-hook-events` was added: the
  hook fired (the agent's reply says so) but its output wasn't in the stream, so `hook_feedback` read 0. That session
  is set aside in `results.json` (`pilot_sessions_set_aside`, directory `PILOT_...`) and h02 r0 hook was re-run with
  hook events recorded. The pilot's without and MCP sessions (h02 r0) ran without the flag, which changes only what is
  recorded, not what the model sees.
- `num_turns` exceeds `--max-turns 40` in some sessions (up to 76): Claude Code's own count; no session was cut off
  (all reached a final reply and passed the done-check).
- Order: three-arm Latin square per run; over 2 runs each arm went first 2-3 times.
- Labels and analysis by the agent that built Tenthcheck; the missed-caller check uses plain `ast` and was validated
  on every earlier diff before this run.

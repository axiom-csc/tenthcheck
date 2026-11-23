# Invariant mining: negative result

**Outcome: across 15 real Python repos, the miners produced 0 unexplained true inconsistencies. Of the 18 findings graded after tuning, 3 were real but deliberate differences (one only probably) and 15 were false positives. Mining is therefore off by default (`tenthcheck scan --experimental-mining`).**

Sources: `results/scan_repos.md` (pre-registration and tuning log), `results/review_tune.md`, `results/review_heldout.md` (every finding, labelled), `results/scan_timing.md`.

## Method
- **Idea.** Count how many places follow a pattern and flag the places that don't (bugs as deviant behaviour). Thresholds: at least 5 places and at least 80% consistency; confidence is the Wilson lower bound.
- **Four miners.** route-guard (handlers in one router/class/verb group that lack the guard their siblings have), call-pair (opener without closer), guard-mutation (`x -= amt` without a comparison on `x`), sql-scope (UPDATE/DELETE with no tenant/user column, parsed with sqlglot).
- **Repos pinned before scanning** (commits in `results/scan_repos.md`).
  - Tune set (5): redash, zulip, celery, requests, saleor.
  - Held-out set 1 (5): superset, mealie, netbox, frappe, wagtail. Scanned once on the frozen tuned version.
  - Held-out set 2 (5): paperless-ngx, authentik, pretix, airflow, erpnext. Fresh repos for the v1.1 fixes made after set 1; scanned once, no tuning afterwards.
- **Labels.** Every finding was read in the source and labelled TRUE-INCONSISTENCY (differs with no visible reason), INTENTIONAL (real difference, deliberate), or FALSE-POSITIVE (not a real difference).
- Fully offline, no LLM in any verdict.

## Results
| Set | Version | Findings | True (unexplained) | Intentional | False positive |
|---|---|---|---|---|---|
| Tune (5 repos), before tuning | v1.0 first run | 48 (zulip) + 0 | 0 | 0 | 48 |
| Tune, after tuning | v1.0 tuned | 1 (redash) | 0 | 1 (probable; I did not run redash) | 0 |
| Held-out 1 | v1.0 frozen | 13 | 0 | 1 | 12 |
| Held-out 2 | v1.1 frozen | 4 | 0 | 1 | 3 |

- No genuine bug was found in any repo; nothing was written up for upstream and nothing was opened upstream.
- 11 of the 15 repos produced no findings at all under the final (v1.1) rules; wagtail's single v1.0 false positive is removed by v1.1.
- Only the route-guard miner ever fired. call-pair, guard-mutation and sql-scope produced exception sites on none of the 15 repos at default thresholds; they are tested on synthetic fixtures only.
- Recall was not measured. Counting INTENTIONAL as a correct detection, held-out precision was 1/13 and 1/4; counting only unexplained true inconsistencies it was 0/13 and 0/4. The samples are too small for a rate.
- The v1.1 fixes were made from held-out set 1's mistakes, so set 1 is no longer held out for v1.1; only set 2 is a clean test of v1.1.

## Why it failed
1. **Real codebases are full of intentional exceptions.** The "real" differences were all deliberate or probably so (redash `DestinationListResource.get` is the probable one): superset `download_xlsx` is login-free by design (a code comment says the job id is the credential), and erpnext `get_material_shortages` is read-only and already permission-checked by the framework. A deviation from the majority is evidence that someone chose differently, and the miner cannot see why.
2. **The same guarantee is written in many forms.** airflow's two `connections.py` handlers authorize inside the body with `is_authorized_connection(...)`, while their siblings use a `Depends(...)`. Equivalent protection, different syntax, flagged as missing.
3. **Names are a weak proxy for meaning.** A client factory called `get_..._admin_service_client` matched the guard-name regex. A guard family (`protect` / `protect_read`) was counted as two different guards.
4. **Framework knowledge decides what counts as an endpoint.** Ten of the 12 held-out-1 false positives were hook overrides or helpers in a class the miner treated as a group of handlers. Each framework needs its own rules, and each rule found after the fact is a sign of rules not yet found.
5. **Tuning on a handful of repos does not generalise.** The zulip flood (48 sites, all in `tests.py` files) was fixed in tuning, then a different class of false positive appeared in the next repo.
6. **The patterns it can state are too thin.** Many repos mined only 0-3 patterns, so the output is few findings with most of them wrong, which is the worst combination for a tool that must not cry wolf.

## What might work instead (untested)
- **Mine from the repo's own history.** Use commits that added a guard or a closer as evidence that the pattern matters, and flag only code that matches the fixed-bug shape. This ties a pattern to a human decision rather than to a majority.
- **Resolve equivalence before comparing.** Model "this handler is authorized" semantically (decorator, `Depends`, or a body call that reaches the auth manager) instead of matching names.
- **Per-framework adapters with declared endpoint rules**, written against the framework's documented routing, rather than a generic grouping heuristic.
- **Ask the author, not the codebase.** The Z3 path works because the invariant is written by the user; a short prompt-driven suggestion step could propose invariants for a human to accept, never enforcing anything until accepted.
- **Evaluate on seeded or historical bugs** (recall) before any precision claim: take known past fixes and check whether the miner would have flagged the pre-fix code.

## What stays in the tool
The code and tests remain behind `--experimental-mining`. Accepted `[[mined]]` entries in an existing `tenthcheck.toml` are still enforced by the hook and CI, because the user chose them. Nothing mines unless asked.

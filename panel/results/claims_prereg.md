# Pre-registration: claim blocker v2 (receipt citations), held-out evaluation

Written and committed BEFORE the claim blocker is changed. Hashes of every input are in `results/claims_prereg_hashes.txt`.

## Disclosure
`eval/run_claims_eval.py` was smoke-tested once before this commit; I saw only aggregate counts of the current (regex) blocker
(29 of 80 false claims rewritten, 1 of 30 non-claims, 6 of 10 citation attacks) and no per-item results. The data file was not edited after
that. I wrote the 80 texts knowing the regexes that exist in `src/tenthlook/panel/firewall/claims.py`, which flatters the old blocker if anything.

## Question
Does requiring action claims to cite a Panel receipt id (verified against the ledger), and rewriting replies that don't, catch
more reworded false claims than regex phrasing alone, without rewriting honest replies?

## Inputs (frozen)
- `eval/claims_heldout_v1.json` (made by `eval/make_claims_heldout.py`):
  - `false_claims`: 80 reworded false claims in 4 ledger scenarios (S0 empty 30, S1 refund held 20, S2 delete blocked 15, S3 a
    different action executed 15). Every one is false in its scenario. **Held out: I will not read per-item misses before the final run.**
  - `nonclaims` 30 (plans, offers, negations, info) in S1; `true_cited` 15 and `true_uncited` 15 true claims about a real executed
    $10 refund (S4); `citation_attacks` 10 false claims that cite a fabricated, blocked-action or other-conversation receipt.
- `eval/run_claims_eval.py`: builds the scenario ledgers with the real Firewall and runs `check_reply`. Receipt id = `"rcpt_" + first 12 hex of
  tenthlook.core.receipt.receipt_hash(receipt)`.
- The OLD result is kept: `results/eval/exploratory_claims.json` (40 claims, 25 non-claims; 20/40 caught, 1/25 false positive).

## Procedure
1. Run the current blocker (`--mode regex`) on the held-out set -> `results/claims/heldout_regex_before.json`. (BEFORE)
2. Implement receipts mode in `src/tenthlook/panel/firewall/claims.py`. Tune ONLY against the old 40/25 probe (`eval/exploratory_claims.py`, already seen,
   so not held-out evidence) and unit tests. Do not open the held-out per-item results.
3. Run the final blocker on the held-out set exactly once -> `results/claims/heldout_receipts_after.json`. (AFTER) Report misses as misses.
   If I change anything after seeing those results, it is a new version (v2 data set), and the first AFTER stays in the report.

## Metrics
false claims rewritten (of 80, by scenario); non-claims wrongly rewritten (of 30); true claims left alone: cited (of 15) and uncited
(of 15; in receipts mode uncited true claims are expected to be rewritten BY DESIGN, with a verified summary); citation attacks
rewritten (of 10).

## Hypotheses
- H1: receipts mode rewrites more of the 80 than regex mode (29). I do not predict 80/80: detection of "an action is asserted" is still lexical.
- H2: receipts mode rewrites all 10 citation attacks (citation checks are ledger lookups, not language).
- H3: non-claim false positives <= 2 of 30. H4: all 15 cited true claims pass.

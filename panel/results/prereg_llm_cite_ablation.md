# Pre-registration: do models cite receipt ids without being told to? (gpt-4.1-mini)

Written and committed BEFORE the run. Hashes: `results/prereg_llm_cite_ablation_hashes.txt`. Follows `results/prereg_llm_addendum.md` (same agent, prompt, tools, sandbox, scoring).

## Design
- Model `gpt-4.1-mini`, temperature 0.7, seeds 0-2 (3 samples), **with-Panel arm only**, tasks `eval/tasks_v2.jsonl` (T12 now lists payment ids), claim blocker = v2 (commit bcae1e9).
- Three tool-result variants (`--cite-mode`; the runner and `gateway.explain` gained this option, default unchanged):
  - `instruct` (control, as in the main run): result text shows the receipt id and tells the agent to cite it.
  - `id_only`: the result text shows `[Panel receipt rcpt_...]` and says nothing else.
  - `none`: the id is not in the text the model sees (it exists only in `_meta`).
- Same code and tasks for all three, so differences are due to the variant. Results are NOT comparable to the earlier main run (different blocker version and T12 task).
- Budget: this run must cost <= $2 more. Spend is logged to `results/llm/spend.jsonl` (assumed prices, 25% margin); the runner stops if total spend would exceed (current total + 2). Rows go to `results/llm_cite/`.

## Metrics
Among replies where >= 1 receipt was executed in the conversation: share that cite a valid receipt id of that conversation; share rewritten by the v2 receipts blocker. Plus the rewrite rate over all with-Panel replies, and
allowed actions completed (min(E,P), as before). `eval/llm_report.py` logic, applied to `results/llm_cite/`.

## Hypotheses
- H1: `instruct` cites >= 85% (the main run: 91%). H2: `id_only` cites less than `instruct`; I do not predict how much (guess: 30-70%). H3: `none` cites 0% by construction (the model never sees an id), so every true "done" claim is rewritten: this variant measures the cost of NOT telling the agent.
- H4: the share of honest (no-action) replies rewritten stays about the same across variants (it should not depend on the variant).
Contradictions are reported as such.

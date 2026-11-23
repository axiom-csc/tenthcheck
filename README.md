# Tenthlook

**Tenthlook checks AI before it acts (Tenthcheck) and while it acts (Panel).**

- **Tenthcheck** looks at what an AI coding agent's edit touches (the blast radius of the changed symbols) and checks numeric invariants you write with Z3, returning the exact counterexample when one breaks. It runs before the change lands: a CLI, an agent hook, an MCP server and a GitHub Action. Offline, no LLM.
- **Panel** sits between an agent (or a support bot) and the things it can do. Every tool call goes through a deterministic policy, out-of-band consent, an idempotency ledger and signed receipts, and "it's done" claims must cite a receipt. No model is in any verdict.
- **Receipts** (`tenthlook.core`) are the shared record: Ed25519-signed, hash-chained, verifiable offline with the `tenthlook` command. Both products write them.

Early, single-author, sandbox-only software. Every number in this README comes from a file in this repository (linked); read "Limits" before relying on any of them.

## Install

```bash
pip install tenthlook            # not on PyPI yet: from a checkout, pip install .
pip install "tenthlook[mcp]"    # adds the MCP SDK for `tenthcheck serve`
```
Python 3.11+. Three commands: `tenthcheck`, `usepanel`, and `tenthlook` (verify receipts). One package, three import paths: `tenthlook.tenthcheck`, `tenthlook.panel`, `tenthlook.core`.

---

## Tenthcheck: before the agent acts

```bash
cd your-repo && tenthcheck                 # your uncommitted diff: changed symbols, their dependents by hop, then any Z3 invariants in tenthcheck.toml
tenthcheck verify                         # Z3-check [[invariant]]s only
tenthcheck hook / tenthcheck serve        # as a Claude Code hook / an MCP server
```

**What it does** (details: [docs/tenthcheck.md](docs/tenthcheck.md)). (1) *Blast radius:* the diff becomes changed symbols (AST-hash diff against `HEAD`), then their dependents (callers, importers, subclasses, references), listed by hop depth. Calls it cannot resolve statically (`getattr`, dynamic dispatch) are not guessed. (2) *Z3 counterexamples:* for simple straight-line numeric functions it translates the function to Z3 and either finds a counterexample or finds none:

```toml
[[invariant]]
file = "bank.py"
function = "withdraw"
check = "final_balance >= 0"
assume = ["balance >= 0", "amount > 0", "fee >= 0"]
```

**Measured** (single runs on one macOS machine, Python 3.11):
- A one-line edit to starlette's `HTTPConnection.url_for`: 2 changed symbols, 469 dependents of `HTTPConnection`, 7 of `url_for` ([results/impact_timing.jsonl](results/impact_timing.jsonl)).
- Whole-repo scan: requests (20 files) 0.2 s, celery (208) 1.2 s, superset (1,243) 8.0 s, erpnext (2,551) 9.2 s, airflow (3,881) 18.7 s ([results/scan_timing.md](results/scan_timing.md)).
- `impact` after an edit, with the graph cache: requests 1.4 s cold / 0.20 s repeat / 0.54-0.62 s after a new edit; starlette 2.6 / 0.22 / 1.2 s; **airflow 258 s / 7.3 s / 113-128 s**. A changed file still re-parses the whole working-tree graph, so very large repos stay slow after an edit ([results/impact_timing_cached.jsonl](results/impact_timing_cached.jsonl)).
- **Precision of the optional mined invariants** (off by default): on five held-out repos run once, 1 of 13 findings was a real, documented exception and 12 were false positives; on five more, 1 of 4 was an intentional exception and 3 were false positives. No finding was an unexplained real inconsistency and no genuine bug was found; nothing was reported upstream. The samples are tiny ([results/scan_repos.md](results/scan_repos.md), [results/review_heldout.md](results/review_heldout.md), [results/mining_negative_result.md](results/mining_negative_result.md)).

**Does not do:** Python only; Z3 handles only simple numeric functions (no loops, calls, I/O, `//`, `%`, strings or containers); mined invariants are candidates, not proofs; it does not fix code; the GitHub Action has not been run on GitHub.

---

## Panel: while the agent acts

Two layers, both deterministic ([docs/panel.md](docs/panel.md) has the quickstart; [panel/FIREWALL.md](panel/FIREWALL.md) and [panel/GUARD_README.md](panel/GUARD_README.md) the detail):

| Layer | Checks | Between |
|---|---|---|
| **Action firewall** (`tenthlook.panel.firewall`) | what an agent *does*: tool calls to Stripe (test mode), Shopify dev stores, SQLite or any MCP server | agent and tools (an MCP proxy) |
| **Support guard** (`tenthlook.panel.guard`) | what a support bot *says and does* to a customer: promises vs. a written policy, actions only with consent | bot and customer (an OpenAI-compatible proxy) |

```bash
usepanel keygen                     # Panel will not start without a signing key (outside any git repo, mode 0600)
usepanel init --out policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data     # drafts a starter policy: REVIEW IT
usepanel proxy --policy policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data # point your MCP client at this
usepanel pending ; usepanel approve <id>                                                        # approve held actions out of band
```
Fail closed; consent never comes from the agent's own arguments; SQL is parsed to an AST (sqlglot); the same key never executes twice; only a successful upstream call is recorded as executed.

**Measured** (each evaluation was written down and hashed before it ran; the author wrote the policies, tasks and labels):
- **Scripted agent, 47 tasks / 55 calls** ([panel/results/report.md](panel/results/report.md)): without Panel 20 unsafe actions executed, 10 without consent, 3 duplicates; with Panel 0, 0, 0; allowed calls completed 19/19 in both. A scripted replay shows policy coverage, not model behaviour. Policy check latency: `validate` median 0.01 ms (Shopify/Stripe), 0.21 ms (SQLite); full `Firewall.handle` with ledger and signature median 0.36 ms, p95 0.61 ms ([panel/results/eval/latency.json](panel/results/eval/latency.json)).
- **Real models, with and without Panel** (gpt-4.1 and gpt-4.1-mini, 5 samples x 47 tasks; [panel/results/llm_report.md](panel/results/llm_report.md)): should-block actions executed in 67 and 57 of 115 task-runs without Panel, 5 and 11 with it (scoring artifacts I checked row by row); needs-consent actions run with no human: 39 and 30 of 45 without, **0 and 0** with.
- **Claim blocker** ("it's done" must cite a receipt id; [panel/results/claims_v2_report.md](panel/results/claims_v2_report.md)): on a new held-out set, 1 of 124 honest replies wrongly rewritten, 80 of 85 false claims caught, 12 of 12 citation attacks caught. **Models do not cite on their own**: gpt-4.1-mini cited a valid receipt in 37 of 43 replies when told to, in 0 of 43 when the id was visible but the instruction was not ([panel/results/llm_cite_report.md](panel/results/llm_cite_report.md)); Panel injects the instruction. Open defects: [panel/KNOWN_ISSUES.md](panel/KNOWN_ISSUES.md).
- **Support guard, fresh8** (gpt-5.6-luna, 20 conversations): with a plain prompt 0 of 10 allowed requests carried out and 6 of 60 replies made an unbacked claim; with Panel v6.5 9 of 10 carried out, 0 of 60 unbacked, 1 of 60 turns executed an action without consent ([panel/GUARD_README.md](panel/GUARD_README.md)).
- **Support guard v7** (consent by explicit confirmation: "To confirm: refund $24.00 on order #1042? Reply yes to proceed."; [panel/results/v7_report.md](panel/results/v7_report.md)): on the new held-out set fresh9, 11 of 11 allowed requests carried out and 0 actions without consent, at **one extra customer turn per carried-out request**. The live runs barely tested the yes/not-yes boundary (2 non-yes events on fresh9, none on fresh6/7/8); that is covered by 108 phrase cases in `panel/tests/test_v7.py`.

---

## Receipts (`tenthlook.core`)

```bash
tenthlook verify-receipt receipt.json --pubkey <base64 public key>
tenthlook verify-chain receipts.db --pubkey <base64> --expect-head <hash> --expect-count <n>
```
A receipt proves what was checked, by whom (the holder of the key you pinned) and that the record was not altered. It does **not** prove the action or code is safe, that the check was run honestly, or what time it was; removing the newest receipts leaves a shorter valid chain unless you record the head and count elsewhere ([docs/core.md](docs/core.md), [docs/receipts.md](docs/receipts.md)).

## Limits

- **Consent in Panel is protected by the OS user, not cryptography.** An agent with a shell as the same user can run `usepanel approve <id>` or edit the ledger. Run the agent as a different user or container, or approve from another device.
- Panel only guards what is routed through it; an agent with another path (shell, second MCP server, browser) bypasses it.
- The claim blocker is keyword-based and English-only and misses idioms; counts are not checked.
- Sandbox only: Stripe test keys only (the connector has run against a mock transport), Shopify development stores (connector run against fakes), local SQLite.
- Small, same-author evidence: one or two models, 20-conversation sets, labels made by an AI agent with second labels from another AI (no human), simulated customers.
- No formal verification anywhere: Z3 results are only as good as the model handed to Z3.

## Repository layout

```
src/tenthlook/          the package (one pyproject, one version)
  core/                 signed, hash-chained receipts (was the separate tenthlook-core)
  tenthcheck/           blast radius, Z3 invariants, hook, MCP server, CI
  panel/                firewall/ (engine, connectors, gateway, claim blocker) · guard/ (support guard v5-v7) · execute/ · shopify/
tests/                  tenthcheck tests; tests/core/ the receipt tests
panel/                  Panel's non-code material: tests/ eval/ results/ guard_data/ guard_results/ examples/ and its docs
docs/                   tenthcheck.md, panel.md, core.md, receipts.md
results/ measurements/ experiments/    Tenthcheck's measurements and experiments
action.yml              the Tenthcheck GitHub Action
```

## Development

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev,mcp]"
.venv/bin/python -m pytest -q      # tenthcheck + core + panel, one run (the real-MCP-server and openai tests skip if not installed)
```

MIT licensed ([LICENSE](LICENSE)).

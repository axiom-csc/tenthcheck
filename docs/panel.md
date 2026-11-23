# Panel: checks what an agent says and what it does

An agent can go wrong in two ways: it **does** something it should not (a refund over the limit, a `DELETE` without a `WHERE`, the same charge twice), or it **says** something that is not true ("I've refunded you" when nothing was refunded).
Panel is two layers that check both, with deterministic code and **no model in any verdict**.

| Layer | Checks | Sits between | Start here |
|---|---|---|---|
| **Action firewall** (`tenthlook.panel.firewall`) | what an agent *does*: tool calls to Stripe (test mode), Shopify dev stores, SQLite, or any MCP server | agent and tools (an MCP proxy) | [FIREWALL.md](../panel/FIREWALL.md) |
| **Support guard** (`tenthlook.panel.guard`) | what a support bot *says and does* to a customer: promises vs. a written policy, actions only with consent | bot and customer (an OpenAI-compatible proxy) | [GUARD_README.md](../panel/GUARD_README.md) |

Both are **early, single-author, sandbox-only** software. Read "Limits" before relying on any number below.

## How the firewall works
```
agent ──tool call──▶ Panel ──▶ policy check (code over a YAML policy and an append-only ledger) ──▶ allow / needs_consent / block
                       │            ├─ block: reasons go back to the agent; nothing runs
                       │            ├─ needs_consent: held until a human approves OUT OF BAND (usepanel approve, local UI)
                       │            └─ allow: claim the idempotency key ▶ execute once ▶ ledger ▶ signed receipt
                       └─ before a reply reaches a person: "it's done" claims must cite a receipt the ledger confirms, else the reply is rewritten
```
Fail closed (unknown tool, bad argument, engine error: block). Consent never comes from the agent's own arguments. SQL is parsed to an AST (sqlglot): single statement, allowlisted kinds, protected tables, row-count caps, a SQLite authorizer as a second layer. Receipts are Ed25519-signed and hash-chained ([tenthlook-core](https://pypi.org/project/tenthlook-core/) format).

## How the guard works
The support guard parses the bot's reply into promises and actions, checks them against a written store policy (refund window, amount limits, escalation threshold, discount cap) and the real order record, and lets a reply through only if every promise is backed.
**v7 (new) consent:** before any action runs, Panel sends a confirmation question it generates from the action ("To confirm: refund $24.00 on order #1042? Reply yes to proceed."), and only a short plain yes to *that* message counts; anything else is re-asked once, then handed to a person (`src/tenthlook/panel/guard/v7.py`, [results/v7_report.md](../panel/results/v7_report.md)). Earlier versions (v6.x) read the customer's words with phrase rules and are kept frozen.

## Install and quickstart
Requires Python 3.11+. `tenthlook-core` is a dependency; until it is on PyPI, install it from its source first.
```bash
pip install tenthlook-core panel-firewall        # or: pip install ./tenthlook-core ./panel-firewall
usepanel keygen                            # Panel will not start without a signing key (stored outside any git repo, mode 0600)
usepanel init --out policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data     # drafts a starter policy: REVIEW IT
usepanel proxy --policy policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data # point your MCP client at this command
usepanel pending ; usepanel approve <id>        # approve held actions (or: usepanel ui)
python -m tenthlook.panel.guard.proxy --upstream https://api.openai.com/v1 --policy my_policy.toml   # the support guard (see GUARD_README.md)
```
Example policies and MCP client config: [examples/](../panel/examples/). Tests: `pip install -e ".[dev]" && pytest` (the real-MCP-server tests skip if the servers are not installed).

## Real results
Every number below is from a file in this repo. Each evaluation was written down and hashed **before** it was run (`*prereg*` files and `FROZEN_*.sha256`). I wrote the policies, tasks, labels and code, so read these as engineering evidence, not independent validation.

**1. Firewall, scripted agent** (47 tasks / 55 calls, sandbox backends; [results/report.md](../panel/results/report.md)). A scripted replay, not a model: it shows policy coverage.

| | without Panel | with Panel |
|---|---|---|
| unsafe actions executed (23 should-block calls) | 20 | **0** |
| executed without consent (10 needs-consent calls) | 10 | **0** |
| duplicate executions | 3 | **0** |
| allowed calls completed | 19 / 19 | 19 / 19 |

Latency (Apple M4, loaded machine): policy `validate` median 0.01 ms (Shopify/Stripe), 0.21 ms (SQLite); full `Firewall.handle` with ledger write and signature median 0.36 ms, p95 0.61 ms ([results/eval/latency.json](../panel/results/eval/latency.json)).

**2. Firewall, real models** (gpt-4.1 and gpt-4.1-mini, 5 samples x 47 tasks, with and without Panel; [results/llm_report.md](../panel/results/llm_report.md)). Without Panel the models executed a should-block action in 67 (gpt-4.1) and 57 (mini) of 115 task-runs and a needs-consent action with no human in 39 and 30 of 45; with Panel, 5 and 11 and **0 and 0**. The 5 and 11 are scoring artifacts that I checked row by row (narrow `DELETE ... WHERE id = 7`-style statements the policy allows). Both models' replies were mostly honest about errors and blocks; the claim blocker had little real to catch and wrongly replaced 24 of 470 honest replies (5.1%) in v1.

**3. Claim blocker** ([results/claims_v2_report.md](../panel/results/claims_v2_report.md), [results/llm_cite_report.md](../panel/results/llm_cite_report.md)). Replies that say an action happened must cite a receipt id. On a new held-out set (v2): 1 of 124 honest replies wrongly rewritten, 80 of 85 false claims caught, 12 of 12 citation attacks caught. **Models do not cite on their own:** gpt-4.1-mini cited a valid receipt in 37 of 43 replies when the tool result told it to, and in 0 of 43 when the id was visible but the instruction was not. Panel injects the instruction into tool results. Open defects: [KNOWN_ISSUES.md](../panel/KNOWN_ISSUES.md).

**4. Support guard, fresh8** (gpt-5.6-luna, 20 conversations, 60 turns per condition, v6.5; [GUARD_README.md](../panel/GUARD_README.md), `guard_results/fresh8_compare/`):

| | plain prompt | Panel prompt, no guard | Panel v6.5 |
|---|---|---|---|
| allowed requests carried out | 0 / 10 | 10 / 10 | 9 / 10 |
| unbacked claims in sent replies | 6 / 60 | 0 / 60 | 0 / 60 |
| actions without consent | n/a (executes nothing) | 0 / 60 | 1 / 60 |

**5. Support guard v7, fresh9** (new held-out set, same model; [results/v7_report.md](../panel/results/v7_report.md)): 11 of 11 allowed requests carried out, 0 actions without consent, **one extra customer turn per carried-out request** (the yes). The live runs barely tested the yes/not-yes boundary (2 non-yes events on fresh9, none on fresh6/7/8); that boundary is tested by 108 phrase cases in `tests/test_v7.py`.

## Limits
- **Consent is protected by the OS user, not cryptography.** An agent with a shell as the same OS user can run `usepanel approve <id>` itself or edit the ledger. Stronger: run the agent as a separate OS user or container that cannot read the ledger/keys, or approve from another device. Not built in.
- **Only guards what is routed through it.** An agent with another path (shell, second MCP server, browser) bypasses Panel.
- **The claim blocker is keyword-based and English-only**; it misses idioms ("dealt with", "pushed it through") and does not check counts. See [KNOWN_ISSUES.md](../panel/KNOWN_ISSUES.md).
- **Sandbox only.** Stripe refuses non-`sk_test_` keys; Shopify is for development stores; the Stripe connector has only run against a mock transport, the Shopify connector against fakes (plus one recorded dev-store refund for the older executor).
- **Small, same-author evidence.** One or two models, 20-conversation sets, labels made by an AI agent (second labels from another AI, no human), customers simulated by scripts. "0 in 60" has an upper 95% bound of about 5%.
- **No formal verification.** A receipt proves what Panel checked and that the record was not altered, not that the action was a good idea.
- Not reviewed for production finance, healthcare or other regulated use.

## Repository layout
`src/tenthlook/panel/firewall/` firewall engine, connectors, gateway, receipts, claim blocker · `src/tenthlook/panel/guard/` support guard (v6.x frozen; v7) · `src/tenthlook/panel/execute/` executor and approval page · `eval/` and `results/` firewall evaluations (pre-registrations, hashes, raw rows) · `guard_data/`, `guard_results/` guard sets and runs (fictional store and orders) · `tests/`.

License: MIT (see LICENSE).

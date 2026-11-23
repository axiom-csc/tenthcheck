# Panel action firewall: quickstart

Panel sits between an AI agent and its tools. Every tool call goes **intercept → verify → consent → execute (once) → log → receipt**.
The allow / needs_consent / block decision is plain deterministic code reading your YAML policy and the ledger. No model is
consulted when deciding.

## Install

```bash
cd products/panel
python3.11 -m venv .venv && .venv/bin/pip install -e . pytest numpy
.venv/bin/usepanel keygen          # Ed25519 receipt key -> ~/.config/panel/receipt_ed25519.pem (mode 600, refused inside a git repo)
```
Panel **refuses to start without that key**. There is no default secret.

## Wrap an existing MCP server with no code changes

```bash
# 1. let Panel inspect the server and write a starter policy
.venv/bin/usepanel init --out policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data
# 2. REVIEW policy.yaml (it is a heuristic: reads allowed, writes need approval, destructive/unknown blocked, money caps = 0)
# 3. run the proxy; point your MCP client at this command instead of at the server
.venv/bin/usepanel proxy --policy policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data
```

**Claude Code / any MCP client**: use `examples/mcp.json` (stdio `command` + `args`), or
`claude mcp add fs-via-panel -- /ABS/.venv/bin/usepanel proxy --policy /ABS/policy.yaml -- npx -y @modelcontextprotocol/server-filesystem /srv/data`.

## Serve Panel's own connectors (SQLite, Stripe test mode, Shopify)

```bash
.venv/bin/usepanel serve --policy examples/policy.native.yaml     # tools: sql_query, stripe_refund, stripe_customer_credit
```

## Approving held actions (out-of-band only)

A call the policy marks `needs_consent` is **held**: it is not executed and the agent is told an approval id. Consent can only
come from a channel the agent's tool calls cannot reach:

```bash
.venv/bin/usepanel pending --policy policy.yaml
.venv/bin/usepanel approve <id> --policy policy.yaml --who alice     # or: usepanel deny <id>
.venv/bin/usepanel ui --policy policy.yaml                           # http://127.0.0.1:8765/?t=<token>, localhost only
```
After approval the agent repeats the same call and it runs **once**. Fields like `consent: true` or `_meta` in the agent's
call are ignored (tested). Who and when are recorded in the ledger.

**Limit:** `usepanel approve` and the UI token are protected by your OS user, not by cryptography. If the agent has a shell as
the same user it can run `usepanel approve` itself. Run agents that have a shell under a separate OS user or sandbox.

## Policy file

See `examples/` and the docstring at the top of `src/tenthlook/panel/firewall/policy.py` (rule keys: `allowed`, `risk`, `required_fields`,
`amount_field`, `max_amount`, `consent_over_amount`, `max_rows`, `consent_over_rows`, `protected`, `consent_if`, `block_if`,
`rate_limit`, `max_total_amount`, `sql_arg`, `path_args`, `allowed_roots`). Typos in rule keys are errors, not silent no-ops.
Unlisted tools are blocked. Modes: `dry_run` (nothing executes), `approval` (every non-read action needs a human), `auto`.

## Receipts

Each executed or blocked action gets a signed receipt in the ledger. `usepanel export-receipts --out r.json`, then
`usepanel verify-receipt one.json --pubkey ~/.config/panel/receipt_ed25519.pem.pub --policy policy.yaml`.
A receipt proves **what Panel checked and decided** and that the record **was not altered**. It is **not** a proof the action was safe.

## Idempotency

Key = the explicit `_meta["panel/idempotency_key"]` if the client sends one, else `sha256(session + tool + arguments)`.
The same key never executes twice; a repeat returns the recorded result. Only a successful upstream call is recorded as executed;
failed calls can be retried; a timeout (outcome unknown) is **not** retried automatically. Read tools (`risk: read`) are never deduplicated.
Consequence to know about: an *intentionally* repeated identical write in one session looks like a retry. Give such calls different
idempotency keys.

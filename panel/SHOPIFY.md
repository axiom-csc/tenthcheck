# Panel for Shopify

Panel's guard in front of a support bot, reading real orders from Shopify and carrying out approved refunds and
discount codes there. **Use a Shopify development store.** By default the executor refuses to touch any order that
isn't a Shopify test order.

Admin GraphQL API **2026-10** (latest stable on 2026-11-17, supported until Oct 2027), pinned in
`src/tenthlook/panel/shopify/__init__.py`.

## What it uses in Shopify (from the current docs)

| Need | Admin GraphQL | Scope |
|---|---|---|
| Read an order by its number | `orders(first: 1, query: "name:#1001")` (also tries `name:1001`) | `write_orders` (includes read) |
| Refund money | `refundCreate(input: RefundInput!) @idempotent(key: "<uuid>")`: one `REFUND` transaction against the order's successful `SALE`/`CAPTURE` transaction | `write_orders` |
| Discount code | `discountCodeBasicCreate(basicCodeDiscount: …)`: `customerGets.value.percentage` (0.10 = 10%), `context`, `usageLimit: 1`, `appliesOncePerCustomer: true` | `write_discounts` |

**Least privilege: the app needs exactly `write_orders` and `write_discounts`.** Not needed: `read_customers` (codes
aren't tied to a customer, and hand-offs don't include the customer's email), protected customer data, and
`read_all_orders`. Without `read_all_orders`, orders older than 60 days aren't visible, so Panel treats them as
unknown and commits to nothing.

Facts from the docs that shaped the code:
- `refundCreate` **requires** an idempotency key since 2026-04. Shopify keeps it 24 hours, so Panel also keeps its own
  never-twice ledger.
- `customerSelection` is deprecated; `context` replaces it.
- Admin-created custom apps with static tokens can't be created any more. Apps come from the Dev Dashboard, and the
  client ID and secret are exchanged for a 24-hour token (client credentials grant).

## Set up the dev store

1. In the Shopify **Dev Dashboard**: Stores → Create store → **Dev**.
2. Apps → Create app → "Start from Dev Dashboard". On the Versions page, set the scopes to
   `write_orders,write_discounts,read_products`, then **Release**. `read_products` is needed because the order lookup
   reads each line item's `variant { compareAtPrice }` (the policy's "on sale" fact), and ProductVariant requires
   `read_products`; without it the order read fails. Install the app on the dev store, which must be in the same organization.
3. In the dev store admin: Settings → Payments → activate the **Test payment gateway**. Place an order over $1 with
   card number `1`.
4. Put the credentials in a file only you can read (Panel refuses a group- or world-readable file):
   ```
   mkdir -p ~/.config/panel && touch ~/.config/src/tenthlook/panel/shopify.env && chmod 600 ~/.config/src/tenthlook/panel/shopify.env
   # then edit it:
   SHOPIFY_STORE=your-dev-store.myshopify.com
   SHOPIFY_CLIENT_ID=...
   SHOPIFY_CLIENT_SECRET=...
   PANEL_HANDOFF_WEBHOOK=https://...        # optional; or PANEL_SMTP_* (see src/tenthlook/panel/shopify/config.py)
   ```
   Environment variables of the same names override the file. No secret is printed, logged or stored by Panel.

## See a real test refund happen

```
python scripts/shopify_dev_refund_demo.py --order 1001 --amount 1.00
```
The script walks through these steps:
1. Shows which settings are set (names only). You type the store domain back to confirm it's the dev store.
2. Gets a token and prints the scopes granted. It warns if the app has more than Panel needs.
3. Reads the order and prints the facts Panel's policy uses. It stops unless the order is a **test** order.
4. Dry run: records the refund and calls nothing.
5. Approval mode: queues the refund and asks "Approve a REAL test refund of $1.00? [y/N]". Only `y` runs it, and it
   then re-reads the order to show the refunded amount.
6. Submits the same action again, to show it is not executed twice.

Without `PANEL_HANDOFF_WEBHOOK` or `PANEL_SMTP_*` set, the demo logs "hand-off … NOT delivered: no webhook or email
configured". That is expected: the hand-off is recorded but not sent, and the refund and discount steps don't depend on it.

Refunds go against the order's successful SALE, or for an authorize-then-capture order (the Bogus gateway does
this) its CAPTURE, as `parentId`. A failed refund stays in the ledger as `failed` under the same key, and a failed key is
never retried, so rerun with a different `--amount` (or a fresh `--db`) to try again.

Add `--discount` to also create one 10% single-use code, again only after a `y`. Open
`python -m tenthlook.panel.execute.approval_page --db <ledger> --policy guard_data/policy_fictional_store.toml` to see the
ledger.

## Run the proxy against Shopify

```
python -m tenthlook.panel.guard.proxy --upstream https://api.openai.com/v1 --policy my_policy.toml --shopify \
    --exec-mode approval --exec-db panel_actions.sqlite
python -m tenthlook.panel.execute.approval_page --db panel_actions.sqlite --policy my_policy.toml   # http://127.0.0.1:8790
```
Send `X-Panel-Conversation-Id: <your chat id>` (or `metadata.conversation_id`) with each request. Without it, Panel
uses a hash of the order and the first customer message.

### Execution modes (`--exec-mode`, or `[execution] mode` in the policy file)

| Mode | What happens to an approved action | What the bot tells the customer |
|---|---|---|
| `dry_run` (default) | recorded with what auto mode would have done; nothing runs | "I've sent … to our team for approval …" (never "I've issued …") |
| `approval` | queued; runs only when someone clicks **Approve** on the local page | "I've sent … to our team for approval …" |
| `auto` | runs at once if within the limits below; otherwise queued | "I've issued a refund of $X …" only if it really executed |

```toml
[execution]
mode = "auto"
auto_refund_max = 50            # refunds up to $50 run automatically; above that, approval
auto_discount_max_percent = 10
auto_handoff = true             # hand-offs are sent at once in every mode except dry_run
auto_exchange = false           # exchanges are manual: approved ones go to the team via the hand-off channel
```

**The confirmation always matches what actually happened.** The proxy words each confirmation from the executor's
result:
- **executed:** "I've issued a refund …", or for a discount "… Your code is PANEL-XXXX".
- **queued, or a dry run:** "I've sent … for approval".
- **failed or blocked:** "I wasn't able to complete that myself just now …". The team is told through the hand-off
  channel.

So Panel keeps its headline property here too: it never promises an action that didn't happen. (Shown with mocks in
`tests/test_executor.py`.)

### Only what the customer asked for (v6.3)

An action is executed only if the customer's latest message clearly asked for it, or said yes to the bot's offer of
it in the previous reply. A question ("what are my options?", "can I still return this?") gets an explanation and an
offer, never an action. The executor checks this from the transcript itself (`needs_consent`), independently of the
prompt and the guard. A request with no transcript is never executed. Details: GUARD_README.md, "v6.3".

### Never twice

- **The key:** each action's key is `sha256(conversation, order, action type, amount or percent)`.
- **The ledger:** `src/tenthlook/panel/execute/store.py` is SQLite. A key is inserted once, and only the caller that atomically moves
  it into `executing` runs it.
- **What the tests cover:** 20 concurrent submits run once, a restart keeps the ledger, and approving twice, repeating
  the same action in a conversation, or re-submitting after a failure each run nothing.
- **After a crash:** a key left in `executing` is not retried automatically. Its outcome is unknown, so check the order
  in Shopify. Shopify's own key makes a manual retry within 24 hours safe.

### Checks before anything runs

- **A fresh re-check:** the executor re-reads the order (not cached) and re-checks the action with the same
  deterministic checker the guard uses. A refund larger than what is still refundable, for example after an earlier
  partial refund, is **blocked**, not executed.
- **Test orders only:** orders that aren't test orders are refused unless `PANEL_ALLOW_LIVE_ORDERS=1` is set, which
  should be a deliberate choice.

### Hand-off

- **Channels:** a webhook (POST JSON, signed with HMAC-SHA256 in `X-Panel-Signature` if
  `PANEL_HANDOFF_WEBHOOK_SECRET` is set) or email (SMTP with STARTTLS).
- **The summary is deterministic:** order facts, the customer's first and last message, the actions taken or queued in
  this conversation, and the last 10 messages.
- **No channel configured:** the hand-off **fails** and the bot says it couldn't complete it. It never claims a person
  was told.

## What is verified and what isn't

- **Verified against mocked Shopify responses** (`tests/test_shopify.py`, `tests/test_executor.py`):
  - the request shapes, the token flow, the mode logic, idempotency, the re-check, hand-off signing, the approval
    page's CSRF and Host checks, and the demo script end to end.
- **Verified once against a real Shopify development store.** `scripts/shopify_dev_refund_demo.py` ran a real refund
  on a Shopify development store (store name and refund id withheld), and the idempotency check held
  when the script was run again (no second refund). This is one run, recorded here from the operator's report.
  - Not recorded from that run (check the script output before relying on them): whether the order-name filter needed
    `#`, the form of `context` for "all buyers", and the discount-code path. The refund path is the one confirmed.
- **Known limits:**
  - **Sale detection:** "on sale" comes from the variant's *current* `compareAtPrice`, not the price at purchase time.
  - **Exchanges:** these are manual work for the team.
  - **Discount codes:** codes aren't tied to a customer.
  - **Remembering earlier actions:** Panel finds them from the proxy's own confirmations in the conversation history,
    so a business that rewrites past assistant messages loses that, though the ledger still prevents doubles.

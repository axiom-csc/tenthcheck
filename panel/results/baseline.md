# Baseline (before any firewall work)

Branch `panel-action-firewall` @ 75824c3 (one prior commit: behavior-preserving executor refactor). Working tree clean; no pending work to commit.

- Machine: Darwin 25.4.0 (arm64 Mac), Python 3.11.18 (`.venv`, created for this work). System `python3` is 3.15.2 and lacks the deps.
- `python -m pytest -q`: **196 passed, 1 skipped** in 14.9 s. Skip: `tests/test_proxy.py:187` (optional `openai` package not installed).
  (An earlier run under the shared Homebrew python, which has openai, showed 197 passed; same suite.)
- Shopify demo, dry_run only (non-interactive script of steps 1-4 of `scripts/shopify_dev_refund_demo.py`; the approval step was NOT run, so no refund was made):
  token obtained (scopes read_products, write_discounts, write_orders); order #1001 read, `test: True`;
  submit of a $1.00 refund in dry_run recorded: "dry_run would queue for approval: refund $1.00 > auto limit $0.00".
  Nothing was sent to Shopify except the token request and the order read.
- Not run: the interactive approval/execute steps (they make a real test refund).

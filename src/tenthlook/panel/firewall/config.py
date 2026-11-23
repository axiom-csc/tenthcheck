"""Build a Firewall from a policy file. Connectors come from the policy's `connectors:` list.

connectors:
  - {type: sqlite,  path: ./internal.db}
  - {type: stripe}                                  # key from STRIPE_API_KEY or ~/.config/panel/stripe.env; must be sk_test_
  - {type: shopify, store_policy: guard_data/policy_fictional_store.toml}   # credentials from ~/.config/src/tenthlook/panel/shopify.env
  - {type: upstream, name: fs, command: [npx, -y, "@modelcontextprotocol/server-filesystem", /data]}   # used by `usepanel proxy`
"""
from __future__ import annotations

from pathlib import Path

from tenthlook.panel.firewall.engine import Firewall
from tenthlook.panel.firewall.ledger import Ledger
from tenthlook.panel.firewall.policy import Policy, load_policy
from tenthlook.panel.firewall.receipts import Signer
from tenthlook.panel.firewall.types import FirewallError


def ledger_path(policy: Policy, override: str | None) -> str:
    if override:
        return override
    base = Path(policy.path).resolve().parent if policy.path else Path.cwd()
    p = policy.raw.get("ledger")
    return str((base / p) if p else (base / "panel-ledger.sqlite"))


def native_connectors(policy: Policy) -> list:
    base = Path(policy.path).resolve().parent if policy.path else Path.cwd()
    out = []
    for c in policy.raw.get("connectors", []) or []:
        t = c.get("type")
        if t == "sqlite":
            from tenthlook.panel.firewall.connectors.sqlite_db import SqliteConnector
            out.append(SqliteConnector(str((base / c["path"]).resolve()), policy))
        elif t == "stripe":
            from tenthlook.panel.firewall.connectors.stripe import StripeConnector, load_key
            out.append(StripeConnector(load_key()))
        elif t == "shopify":
            from tenthlook.panel.execute.executor import ShopifyBackend
            from tenthlook.panel.shopify.client import ShopifyClient
            from tenthlook.panel.shopify.config import Config
            from tenthlook.panel.shopify.orders import ShopifyOrders
            from tenthlook.panel.guard.policy import load_policy as load_store
            from tenthlook.panel.firewall.connectors.shopify import ShopifyConnector
            client = ShopifyClient(Config())
            sp = c.get("store_policy")
            out.append(ShopifyConnector(ShopifyBackend(client), ShopifyOrders(client), load_store(base / sp) if sp else None))
        elif t == "upstream":
            continue
        else:
            raise FirewallError(f"unknown connector type {t!r}")
    return out


def build_firewall(policy_path: str, ledger: str | None = None, key: str | None = None, connectors: list | None = None,
                   mode: str | None = None, consent_wait: float | None = None) -> Firewall:
    pol = load_policy(policy_path)
    signer = Signer.load(key or (pol.raw.get("receipts") or {}).get("key_path"))   # refuses to start without a key
    conns = connectors if connectors is not None else native_connectors(pol)
    return Firewall(pol, Ledger(ledger_path(pol, ledger)), signer, conns, mode=mode,
                    consent_wait=consent_wait if consent_wait is not None else float(pol.raw.get("consent_wait_seconds", 0)))

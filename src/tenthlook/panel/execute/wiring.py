"""Build the Shopify-backed executor from config + policy (used by the proxy, the approval page and the demo)."""
from __future__ import annotations

from tenthlook.panel.execute.executor import Executor, Limits, ShopifyBackend
from tenthlook.panel.execute.handoff import notifier_from_config
from tenthlook.panel.execute.store import ActionStore
from tenthlook.panel.guard.policy import load_policy
from tenthlook.panel.shopify.client import ShopifyClient
from tenthlook.panel.shopify.config import Config
from tenthlook.panel.shopify.orders import ShopifyOrders


def build_executor(db: str, policy_path: str, mode: str | None = None, config: Config | None = None):
    config = config or Config()
    client = ShopifyClient(config)
    orders = ShopifyOrders(client)
    return Executor(ActionStore(db), ShopifyBackend(client), orders, load_policy(policy_path),
                    Limits.from_policy_file(policy_path), notifier_from_config(config), mode=mode,
                    allow_live_orders=config.allow_live_orders)

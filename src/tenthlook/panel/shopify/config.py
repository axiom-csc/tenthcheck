"""Secrets and settings, from the environment or a secret file only. Never logged, never echoed.

Lookup order for each name: the environment, then the secret file (PANEL_SECRETS_FILE, default
~/.config/src/tenthlook/panel/shopify.env), a KEY=VALUE file that must not be readable by group or others (chmod 600).

    SHOPIFY_STORE          yourstore.myshopify.com   (the dev store)
    SHOPIFY_CLIENT_ID      Dev Dashboard app client id       } exchanged for a 24-hour token (client credentials)
    SHOPIFY_CLIENT_SECRET  Dev Dashboard app client secret   }
    SHOPIFY_ADMIN_TOKEN    optional: a static token from an existing legacy custom app, instead of the two above
    PANEL_HANDOFF_WEBHOOK  optional: URL that receives hand-off summaries (POST, JSON)
    PANEL_HANDOFF_WEBHOOK_SECRET  optional: HMAC-SHA256 key; the signature goes in X-Panel-Signature
    PANEL_SMTP_HOST, PANEL_SMTP_PORT, PANEL_SMTP_USER, PANEL_SMTP_PASSWORD, PANEL_HANDOFF_EMAIL_FROM,
    PANEL_HANDOFF_EMAIL_TO  optional: hand-off by email (STARTTLS) instead of a webhook
    PANEL_ALLOW_LIVE_ORDERS  "1" lets the executor touch orders that are not Shopify test orders. Unset by default:
                             only test orders can be refunded or discounted.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_SECRETS = Path.home() / ".config/src/tenthlook/panel/shopify.env"


class ConfigError(RuntimeError):
    pass


class Secret(str):
    """A string that never shows its value in repr/str formatting of containers or logs."""

    def __repr__(self) -> str:
        return "Secret('<redacted>')"


def _read_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} is readable by group or others; run: chmod 600 {path}")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Config:
    SECRET_NAMES = ("SHOPIFY_CLIENT_SECRET", "SHOPIFY_ADMIN_TOKEN", "PANEL_HANDOFF_WEBHOOK_SECRET", "PANEL_SMTP_PASSWORD")

    def __init__(self, env: dict | None = None, secrets_file: str | Path | None = None):
        env = dict(os.environ if env is None else env)
        path = Path(secrets_file or env.get("PANEL_SECRETS_FILE") or DEFAULT_SECRETS).expanduser()
        self._values = {**_read_file(path), **{k: v for k, v in env.items() if k.startswith(("SHOPIFY_", "PANEL_"))}}
        self.secrets_file = path

    def get(self, name: str, default: str | None = None) -> str | None:
        v = self._values.get(name, default)
        return Secret(v) if (v is not None and name in self.SECRET_NAMES) else v

    def require(self, name: str) -> str:
        v = self.get(name)
        if not v:
            raise ConfigError(f"{name} is not set (environment or {self.secrets_file})")
        return v

    def present(self) -> list[str]:
        """Names that are set, for diagnostics. Values are never returned."""
        return sorted(k for k, v in self._values.items() if v)

    @property
    def store(self) -> str:
        s = self.require("SHOPIFY_STORE").strip().removeprefix("https://").rstrip("/")
        if not s.endswith(".myshopify.com"):
            raise ConfigError("SHOPIFY_STORE must be the store's myshopify.com domain, e.g. mystore.myshopify.com")
        return s

    @property
    def allow_live_orders(self) -> bool:
        return self.get("PANEL_ALLOW_LIVE_ORDERS") == "1"

    def __repr__(self) -> str:
        return f"Config(store={self._values.get('SHOPIFY_STORE')!r}, set={self.present()})"

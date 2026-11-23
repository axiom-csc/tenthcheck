"""Minimal Shopify Admin GraphQL client (stdlib only).

- Token: the client credentials grant (Dev Dashboard apps; POST /admin/oauth/access_token, 24-hour token, cached
  and refreshed 5 minutes before expiry), or a static legacy custom-app token if SHOPIFY_ADMIN_TOKEN is set.
- Requests: POST https://{store}/admin/api/{version}/graphql.json with X-Shopify-Access-Token.
- Throttling: GraphQL THROTTLED errors are retried with backoff (3 tries).
- Nothing here logs or returns a token. Errors carry the operation name and Shopify's messages only.
`transport(method, url, headers, body) -> (status, body_bytes)` is injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from tenthlook.panel.shopify import API_VERSION
from tenthlook.panel.shopify.config import Config

log = logging.getLogger("tenthlook.panel.shopify")


class ShopifyError(RuntimeError):
    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 30.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TokenProvider:
    def __init__(self, config: Config, transport=urllib_transport, clock=time.time):
        self.config, self.transport, self.clock = config, transport, clock
        self._token: str | None = None
        self._expires = 0.0
        self.scopes: list[str] = []

    def token(self) -> str:
        static = self.config.get("SHOPIFY_ADMIN_TOKEN")
        if static:
            return static
        if self._token and self.clock() < self._expires - 300:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": self.config.require("SHOPIFY_CLIENT_ID"),
                                       "client_secret": self.config.require("SHOPIFY_CLIENT_SECRET")}).encode()
        status, raw = self.transport("POST", f"https://{self.config.store}/admin/oauth/access_token",
                                     {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body)
        if status != 200:
            raise ShopifyError(f"token request failed: HTTP {status}")  # body not included: it may echo the client id
        data = json.loads(raw)
        self._token = data["access_token"]
        self._expires = self.clock() + float(data.get("expires_in", 86399))
        self.scopes = sorted(s for s in (data.get("scope") or "").split(",") if s)
        log.info("shopify token obtained (scopes: %s)", ",".join(self.scopes))
        return self._token


class ShopifyClient:
    def __init__(self, config: Config, tokens: TokenProvider | None = None, transport=urllib_transport,
                 version: str = API_VERSION, sleep=time.sleep):
        self.config, self.transport, self.version, self.sleep = config, transport, version, sleep
        self.tokens = tokens or TokenProvider(config, transport)

    @property
    def endpoint(self) -> str:
        return f"https://{self.config.store}/admin/api/{self.version}/graphql.json"

    def graphql(self, query: str, variables: dict | None = None, operation: str = "query") -> dict:
        for attempt in range(3):
            headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "X-Shopify-Access-Token": self.tokens.token()}
            status, raw = self.transport("POST", self.endpoint, headers,
                                         json.dumps({"query": query, "variables": variables or {}}).encode())
            if status == 401:
                raise ShopifyError(f"{operation}: unauthorized (check the app is installed and its scopes)")
            if status == 429 or status >= 500:
                self.sleep(2 ** attempt)
                continue
            data = json.loads(raw or b"{}")
            errs = data.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs if isinstance(e, dict)):
                self.sleep(2 ** attempt)
                continue
            if errs:
                raise ShopifyError(f"{operation}: " + "; ".join(str(e.get("message", e)) for e in errs), errs)
            log.info("shopify %s ok", operation)
            return data.get("data") or {}
        raise ShopifyError(f"{operation}: throttled or unavailable after 3 tries")

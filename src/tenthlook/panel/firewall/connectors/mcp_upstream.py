"""Connector for tools that live on an upstream MCP server. Panel does not need to know their schemas: the policy names
the tools it allows; everything else is blocked. Optional per-tool rule keys add structure-aware checks:

  sql_arg: query          the argument is SQL -> sqlguard (AST) checks + row count (needs policy `database:` to count)
  path_args: [path, ...]  arguments that are filesystem paths (str or list of str)
  allowed_roots: [/data]  every path must resolve (symlinks followed, `..` collapsed) inside one of these
  protected: {path: ["*/.env", "/data/secrets/*"]}   globs, applied to the RESOLVED path for path_args
"""
from __future__ import annotations

import fnmatch
import os
import sqlite3
import sys
from pathlib import Path

from tenthlook.panel.firewall import sqlguard
from tenthlook.panel.firewall.connector import Connector
from tenthlook.panel.firewall.policy import Policy
from tenthlook.panel.firewall.types import Action, ActionSchema, Result, canonical, sha256_hex

_CI = sys.platform in ("darwin", "win32")


def _fold(p: str) -> str:
    return p.lower() if _CI else p


def resolve_path(p: str) -> str | None:
    if not isinstance(p, str) or not p or "\x00" in p:
        return None
    if not os.path.isabs(os.path.expanduser(p)):
        return None
    return os.path.realpath(os.path.expanduser(p))


class McpUpstreamConnector(Connector):
    name = "mcp-upstream"

    def __init__(self, policy: Policy, call, prefix: str = "", known: set | None = None):
        """`call(tool, args, meta) -> dict` performs the real tools/call on the upstream (thread-safe, blocking) and
        returns the JSON-RPC result dict; it raises TimeoutError / ConnectionError for an unknown outcome."""
        self.policy, self._call, self.prefix, self.known = policy, call, prefix, known if known is not None else set()

    def describe_actions(self) -> list[ActionSchema]:
        return [ActionSchema(n, "upstream tool", {}, (), (self.policy.rule_for(n) or {}).get("risk", "write")) for n in self.known]

    def owns(self, tool: str) -> bool:
        if self.prefix and not tool.startswith(self.prefix):
            return False
        return tool in self.policy.actions or tool in self.known

    def _db_count(self, q: str) -> int:
        db = self.policy.raw.get("database")
        if not db:
            raise RuntimeError("no `database:` in the policy to count rows on")
        base = Path(self.policy.path).resolve().parent if self.policy.path else Path.cwd()
        con = sqlite3.connect(f"file:{(base / db).resolve()}?mode=ro", uri=True)
        try:
            return con.execute(q).fetchone()[0]
        finally:
            con.close()

    def facts(self, action: Action, policy: Policy) -> dict:
        rule = policy.rule_for(action.tool) or {}
        block: list[str] = []
        facts: dict = {}
        if rule.get("sql_arg"):
            a = sqlguard.analyze(action.args.get(rule["sql_arg"]), {**(policy.raw.get("sql") or {}), **(rule.get("sql") or {})},
                                 self._db_count)
            facts.update(a.facts())
            block += facts.pop("block_reasons")
        if rule.get("path_args"):
            roots = [r for r in (resolve_path(x) for x in rule.get("allowed_roots", [])) if r]
            globs = rule.get("protected") or {}
            for name in rule["path_args"]:
                if name not in action.args:
                    continue
                vals = action.args[name]
                vals = vals if isinstance(vals, list) else [vals]
                for v in vals:
                    rp = resolve_path(v)
                    if rp is None:
                        block.append(f"'{name}' must be an absolute path string (got {v!r})")
                        continue
                    if rule.get("allowed_roots") is not None and not any(
                            _fold(rp) == _fold(r) or _fold(rp).startswith(_fold(r).rstrip("/") + "/") for r in roots):
                        block.append(f"path {rp} is outside the allowed folders")
                    for g in globs.get(name, []) + globs.get("*", []):
                        if fnmatch.fnmatchcase(_fold(rp), _fold(str(g))):
                            block.append(f"path {rp} matches protected pattern '{g}'")
        facts["block_reasons"] = block + facts.get("block_reasons", [])
        return facts

    def execute(self, action: Action, mode: str) -> Result:
        if mode == "dry_run":
            return Result(True, None, {"would_call": action.tool, "args": action.args}, simulated=True)
        tool = action.tool[len(self.prefix):] if self.prefix and action.tool.startswith(self.prefix) else action.tool
        try:
            res = self._call(tool, action.args, action.meta)
        except (TimeoutError, ConnectionError, BrokenPipeError) as e:
            return Result(False, error=f"upstream did not answer ({type(e).__name__}); the outcome is unknown", unknown=True)
        if "error" in res:
            return Result(False, error=f"upstream error: {res['error'].get('message', res['error'])}"[:400])
        r = res.get("result")
        if not isinstance(r, dict):
            return Result(False, error="upstream returned a malformed result")
        if r.get("isError"):
            return Result(False, error="upstream reported a tool error: " + _text(r)[:400], output=r)
        return Result(True, "mcp:" + sha256_hex(canonical(r))[:16], r)


def _text(r: dict) -> str:
    return " ".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict) and c.get("type") == "text")

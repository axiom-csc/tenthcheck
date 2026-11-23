"""Connector: a local SQLite "internal database". One action, `sql.query`, guarded by sqlguard.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tenthlook.panel.firewall import sqlguard
from tenthlook.panel.firewall.connector import Connector
from tenthlook.panel.firewall.policy import LedgerState, Policy
from tenthlook.panel.firewall.types import Action, ActionSchema, Result, sha256_hex

MAX_OUTPUT_ROWS = 200


class SqliteConnector(Connector):
    name = "sqlite"

    def __init__(self, db_path: str | Path, policy: Policy | None = None):
        self.db_path, self.policy = str(db_path), policy

    def describe_actions(self) -> list[ActionSchema]:
        return [ActionSchema("sql.query", "Run ONE SQL statement (SELECT/INSERT/UPDATE/DELETE) on the internal database. "
                             "DROP/TRUNCATE/ALTER, multiple statements, and UPDATE/DELETE without a real WHERE are blocked.",
                             {"sql": {"type": "string", "description": "A single SQL statement"}}, ("sql",), "destructive",
                             ("deleted", "removed", "updated", "inserted", "dropped", "truncated", "altered"))]

    # ---------------------------------------------------------------- validation
    def _ro(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def _count(self, q: str) -> int:
        con = self._ro()
        try:
            return con.execute(q).fetchone()[0]
        finally:
            con.close()

    def _settings(self, policy: Policy, action: Action) -> dict:
        return {**(policy.raw.get("sql") or {}), **((policy.rule_for(action.tool) or {}).get("sql") or {})}

    def facts(self, action: Action, policy: Policy) -> dict:
        sql = action.args.get("sql")
        return sqlguard.analyze(sql, self._settings(policy, action), self._count).facts()

    # ---------------------------------------------------------------- execution
    def execute(self, action: Action, mode: str) -> Result:
        rule = (self.policy.rule_for(action.tool) or {}) if self.policy else {}
        settings = self._settings(self.policy, action) if self.policy else {}
        return self.run(action.args["sql"], mode, settings, rule.get("max_rows"))

    def run(self, sql: str, mode: str, settings: dict, max_rows: int | None) -> Result:
        cfg = {**sqlguard.DEFAULTS, **settings}
        prot = {t.lower() for t in cfg["protected_tables"]}
        con = sqlite3.connect(self.db_path, isolation_level=None)

        def authorizer(code, a1, a2, db, src):  # defence in depth, independent of the SQL parser
            deny = {sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
                    sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                    sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_CREATE_TRIGGER}
            if not cfg["allow_create"]:
                deny |= {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_VIEW,
                         sqlite3.SQLITE_CREATE_VTABLE}
            if code in deny:
                return sqlite3.SQLITE_DENY
            if code in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) \
                    and a1 and a1.lower() in prot:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        try:
            con.set_authorizer(authorizer)
            con.execute("BEGIN")
            try:
                cur = con.execute(sql)
                is_query = cur.description is not None
                rows = cur.fetchmany(MAX_OUTPUT_ROWS) if is_query else []
                affected = 0 if is_query else cur.rowcount
                if max_rows is not None and affected > max_rows:
                    con.execute("ROLLBACK")
                    return Result(False, error=f"rolled back: {affected} rows affected, cap is {max_rows}")
                con.execute("ROLLBACK" if mode == "dry_run" else "COMMIT")
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            return Result(False, error=f"{type(e).__name__}: {e}")
        finally:
            con.close()
        out = {"rows": [list(r) for r in rows], "columns": [d[0] for d in cur.description] if is_query else [],
               "rows_affected": affected}
        return Result(True, f"sqlite:{sha256_hex(sql)[:12]}:{affected}", out, simulated=(mode == "dry_run"))

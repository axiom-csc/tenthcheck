"""SQL safety analysis on the sqlglot AST (never regex on the SQL text).

Fail closed: anything that does not parse, parses to a statement type that is not on the allow-list (Command, Pragma,
Attach, Drop, TruncateTable, Alter, Create unless allowed, ...), is more than one statement, or touches a protected
table is blocked. UPDATE/DELETE need a WHERE that is not a constant-true (`WHERE 1`, `WHERE 1=1`, `WHERE TRUE`); the
number of rows they would touch is counted first on a read-only connection by running the same WHERE as a SELECT
COUNT(*) (this also catches `WHERE id=1 OR 1=1`). The executor additionally enforces the cap inside a transaction and
installs a SQLite authorizer, so a parser difference cannot turn into a destructive statement.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DEFAULTS = {"protected_tables": [], "allow_create": False, "allowed_statements": ["select", "insert", "update", "delete"]}
_KIND = {"Select": "select", "Union": "select", "Intersect": "select", "Except": "select", "Insert": "insert",
         "Update": "update", "Delete": "delete", "Create": "create", "Drop": "drop", "TruncateTable": "truncate",
         "Alter": "alter", "Pragma": "pragma", "Attach": "attach", "Detach": "detach", "Command": "command"}
_FORBIDDEN_NESTED = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create, exp.Command, exp.Pragma, exp.Attach)


@dataclass
class SqlAnalysis:
    kind: str | None = None
    tables: list[str] = field(default_factory=list)
    rows: int | None = None
    block: list[str] = field(default_factory=list)
    consent: list[str] = field(default_factory=list)

    def facts(self) -> dict:
        return {"rows": self.rows, "sql_kind": self.kind, "tables": self.tables,
                "block_reasons": self.block, "consent_reasons": self.consent}


def _arg(node, name: str):
    """sqlglot renamed some arg keys between versions (from -> from_, with -> with_); accept both so an upgrade can
    never silently turn a check into a no-op."""
    if node is None or not hasattr(node, "args"):
        return None
    return node.args.get(name) or node.args.get(name + "_")


def _set_with(sel: exp.Select, w) -> None:
    sel.set("with_" if "with_" in sel.arg_types else "with", w)


def _constant_true(where: exp.Expression) -> bool:
    """A WHERE with no column/subquery/parameter in it that SQLite itself evaluates to true."""
    cond = where.this
    if any(isinstance(n, (exp.Column, exp.Subquery, exp.Select, exp.Placeholder, exp.Parameter, exp.Anonymous, exp.Func))
           and not isinstance(n, (exp.Not, exp.And, exp.Or)) for n in cond.walk()):
        # functions might be non-deterministic (random()) -> not treated as constant; the row count decides.
        return False
    try:
        con = sqlite3.connect(":memory:")
        try:
            r = con.execute("SELECT " + cond.sql(dialect="sqlite")).fetchone()
        finally:
            con.close()
        return bool(r and r[0])
    except sqlite3.Error:
        return False


def parse_one_statement(sql: str) -> tuple[exp.Expression | None, list[str]]:
    if not isinstance(sql, str) or not sql.strip():
        return None, ["the SQL is empty or not a string"]
    try:
        parsed = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None and not isinstance(s, exp.Semicolon)]
    except (SqlglotError, RecursionError) as e:
        return None, [f"the SQL could not be parsed ({type(e).__name__}); blocked for safety"]
    if not parsed:
        return None, ["the SQL contains no statement"]
    if len(parsed) > 1:
        kinds = ", ".join(_KIND.get(type(s).__name__, type(s).__name__).upper() for s in parsed)
        return None, [f"multiple statements are not allowed ({kinds}); send one statement per call"]
    return parsed[0], []


def analyze(sql: str, settings: dict | None = None, count_rows=None) -> SqlAnalysis:
    """`count_rows(select_sql) -> int` runs a COUNT query read-only; None means rows cannot be determined."""
    cfg = {**DEFAULTS, **(settings or {})}
    out = SqlAnalysis()
    stmt, problems = parse_one_statement(sql)
    if problems:
        out.block += problems
        return out
    name = type(stmt).__name__
    kind = _KIND.get(name)
    out.kind = kind or name.lower()
    allowed = set(cfg["allowed_statements"]) | ({"create"} if cfg["allow_create"] else set())
    if kind not in allowed:
        why = {"drop": "DROP", "truncate": "TRUNCATE", "alter": "ALTER", "pragma": "PRAGMA", "attach": "ATTACH",
               "detach": "DETACH", "command": "this kind of command"}.get(kind, name.upper())
        out.block.append(f"{why} statements are not allowed")
        return out
    for n in stmt.walk():  # nested data-modifying / DDL nodes anywhere in the tree
        if n is not stmt and isinstance(n, _FORBIDDEN_NESTED):
            out.block.append(f"a nested {type(n).__name__.upper()} inside the statement is not allowed")
            return out
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    prot = {t.lower() for t in cfg["protected_tables"]}
    tables = [t for t in stmt.find_all(exp.Table) if t.name]
    out.tables = sorted({t.name.lower() for t in tables if t.name.lower() not in ctes})
    for t in out.tables:
        if t in prot:
            out.block.append(f"table '{t}' is protected")
        elif t.startswith("sqlite_") and kind != "select":
            out.block.append(f"internal table '{t}' cannot be modified")
    if any(t in ctes for t in prot):
        out.block.append("a CTE shadows a protected table name")
    if out.block:
        return out
    if kind in ("select",):
        out.rows = 0
        return out
    if kind == "create":
        out.rows = 0
        return out
    try:
        if kind in ("update", "delete"):
            where = stmt.args.get("where")
            if where is None:
                out.block.append(f"{kind.upper()} without a WHERE clause is not allowed")
                return out
            if _constant_true(where):
                out.block.append(f"{kind.upper()} with an always-true WHERE is the same as no WHERE; not allowed")
                return out
            if _arg(stmt, "from") or _arg(stmt.this, "joins") or _arg(stmt, "using"):
                out.block.append(f"{kind.upper()} with FROM/JOIN is not supported by the row counter; use a simple WHERE")
                return out
            target = stmt.this
            if not isinstance(target, exp.Table):
                out.block.append("the statement target could not be determined")
                return out
            sel = exp.select("COUNT(*)").from_(target.copy()).where(where.this.copy())
            if _arg(stmt, "with"):               # keep the statement's CTEs so the count sees the same rows
                _set_with(sel, _arg(stmt, "with").copy())
            out.rows = _count(count_rows, sel.sql(dialect="sqlite"), out)
        elif kind == "insert":
            src = stmt.args.get("expression")
            if isinstance(src, exp.Values):
                out.rows = len(src.expressions)
            elif src is not None:
                sel = exp.select("COUNT(*)").from_(exp.Subquery(this=src.copy()))
                if _arg(stmt, "with"):
                    _set_with(sel, _arg(stmt, "with").copy())
                out.rows = _count(count_rows, sel.sql(dialect="sqlite"), out)
            else:
                out.rows = 1
    except (SqlglotError, KeyError, AttributeError, RecursionError) as e:
        out.block.append(f"could not analyse the statement ({type(e).__name__}); blocked for safety")
    return out


def _count(count_rows, q: str, out: SqlAnalysis) -> int | None:
    if count_rows is None:
        out.block.append("no database is configured to count the affected rows")
        return None
    try:
        return int(count_rows(q))
    except Exception as e:
        out.block.append(f"could not count the affected rows ({type(e).__name__}); blocked for safety")
        return None

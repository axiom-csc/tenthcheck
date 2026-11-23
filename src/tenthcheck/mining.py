"""Invariant mining: find patterns a codebase mostly follows and report the exceptions.

Everything here is `ast` + counting. No LLM, no network. A finding is a *candidate*: "this place is
inconsistent with N others", never "this is a bug". When unsure the miners skip a site (neither
follower nor exception) rather than guess.

Four miners (Python only):
  route-guard     handlers sharing a router / class mostly carry a guard (decorator, Depends, or a guard call)
  call-pair       an opener call (open_x, acquire, begin ...) is mostly followed by its closer in the same function
  guard-mutation  `target -= amount` is mostly preceded/followed by a comparison mentioning `target`
  sql-scope       UPDATE/DELETE on a table mostly filter by tenant_id/user_id/... (SQL parsed with sqlglot)
"""
from __future__ import annotations

import ast
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from tenthcheck.parser import iter_py_files

DEFAULT_MIN_SUPPORT = 5
DEFAULT_MIN_CONSISTENCY = 0.80
_EXCLUDED_DIRS = {"tests", "test", "testing", "docs", "doc", "examples", "example", "benchmarks", "migrations",
                  "build", "dist", "node_modules", "site-packages"}
_VERBS = {"get", "post", "put", "delete", "patch"}
_ROUTE_METHODS = {"route", "get", "post", "put", "delete", "patch", "head", "options", "websocket", "api_route",
                  "trace", "on_event"}
# Words that make a decorator / call name a *guard* (access control, validation, rate limits).
GUARD_RE = re.compile(r"(auth|login|permission|perm_|admin|require|role|csrf|scope|protect|secure|verify|owner|"
                      r"access|allow|can_|throttle|rate_?limit|jwt|token|sudo|staff|superuser|ensure|check_|"
                      r"validate|sanitize|current_user|current_active)", re.I)
# Body calls must match the stricter set (access control only). `check_x`/`validate_x`/`ensure_x` are too generic.
STRONG_GUARD_RE = re.compile(r"(auth|login|permission|perm_|admin|require|role|csrf|scope|protect|owner|access|can_|"
                             r"jwt|sudo|staff|superuser|current_user|current_active)", re.I)
# Handlers that are public on purpose; never reported as exceptions.
PUBLIC_RE = re.compile(r"(login|logout|log_in|log_out|signin|sign_in|signup|sign_up|register|health|ping|status|"
                       r"index|home|static|callback|webhook|reset|forgot|oauth|public|favicon|robots|sitemap|"
                       r"docs|openapi|swagger|metrics|version|token|verify_email|confirm|activate|"
                       r"not_found|error|login_form)", re.I)
_PAIRS = {"open": ["close"], "acquire": ["release"], "lock": ["unlock", "release"], "start": ["stop", "close"],
          "begin": ["end", "commit", "rollback"], "connect": ["disconnect", "close"],
          "subscribe": ["unsubscribe"], "register": ["unregister"], "setup": ["teardown"],
          "attach": ["detach"], "mount": ["unmount"], "allocate": ["free", "deallocate", "release"],
          "enter": ["exit", "leave"], "startup": ["shutdown"]}
_NEUTRAL_FUNCS = re.compile(r"^(__enter__|__aenter__|__init__|setup|set_up|setUp|start|open|connect|acquire|begin|"
                            r"lock|register|attach|mount|subscribe|enter|startup|allocate|_?open_?\w*|_?start_?\w*|"
                            r"_?connect_?\w*|_?acquire_?\w*|_?begin_?\w*|_?setup_?\w*|get_?\w*|create_?\w*|new_?\w*|"
                            r"make_?\w*|build_?\w*)$")
SCOPE_COLS = {"tenant_id", "org_id", "organization_id", "organisation_id", "user_id", "account_id", "owner_id",
              "workspace_id", "company_id", "team_id", "customer_id", "project_id"}


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    func: str = ""

    def __str__(self) -> str:
        return f"{self.file}:{self.line}" + (f" {self.func}()" if self.func else "")


@dataclass
class Invariant:
    id: str
    kind: str
    pattern: str
    follow: int
    total: int
    followers: list[Site] = field(default_factory=list)
    exceptions: list[Site] = field(default_factory=list)

    @property
    def consistency(self) -> float:
        return self.follow / self.total if self.total else 0.0

    @property
    def confidence(self) -> str:
        lb = _wilson_lower(self.follow, self.total)
        return "high" if lb >= 0.70 else "medium" if lb >= 0.55 else "low"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "pattern": self.pattern, "follow": self.follow,
                "total": self.total, "confidence": self.confidence,
                "followers": [str(s) for s in self.followers[:5]], "exceptions": [str(s) for s in self.exceptions]}


@dataclass
class ScanResult:
    root: str
    files: int
    seconds: float
    invariants: list[Invariant]
    skipped: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[Invariant]:
        return [i for i in self.invariants if i.exceptions]


def _wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d


def dotted(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return None


def walk_no_nested(fn: ast.AST):
    """Yield nodes of fn's body without descending into nested functions, lambdas or classes."""
    stack = list(reversed(getattr(fn, "body", [])))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(n))))


def iter_functions(tree: ast.Module):
    """(function node, enclosing class name or None, in_nested_function) for every def."""
    def rec(body, cls, nested):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield n, cls, nested
                yield from rec(n.body, None, True)
            elif isinstance(n, ast.ClassDef):
                yield from rec(n.body, n.name, nested)
    yield from rec(tree.body, None, False)


@dataclass
class _Module:
    rel: str
    tree: ast.Module
    src: str


def _load(root: Path, include_tests: bool, exclude: list[str]) -> tuple[list[_Module], list[str]]:
    mods, skipped = [], []
    for p in iter_py_files(root):
        rel = p.relative_to(root).as_posix()
        parts = set(rel.split("/")[:-1])
        if not include_tests and (parts & _EXCLUDED_DIRS or p.name.startswith("test_") or p.name.endswith(("_test.py", "_tests.py"))
                                  or p.name in ("conftest.py", "tests.py", "test.py", "testing.py")):
            continue
        if any(rel.startswith(e) for e in exclude):
            continue
        try:
            src = p.read_text(encoding="utf-8")
            mods.append(_Module(rel, ast.parse(src), src))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            skipped.append(rel)
    return mods, skipped


# ---------------------------------------------------------------- route-guard

def _route_info(fn) -> tuple[str, str | None] | None:
    """(router name, path prefix segment) if fn carries a route decorator like @router.get('/admin/x')."""
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _ROUTE_METHODS:
            base = dotted(d.func.value)
            if not base:
                continue
            path = None
            if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                segs = [s for s in d.args[0].value.split("/") if s and not s.startswith(("{", "<", ":"))]
                path = segs[0] if segs else None
            return base, path
        if isinstance(d, ast.Attribute) and d.attr in _ROUTE_METHODS:  # @router.get without call: rare
            base = dotted(d.value)
            if base:
                return base, None
    return None


def _guards(fn, skip_route: bool) -> set[str]:
    g: set[str] = set()
    for d in fn.decorator_list:
        if skip_route and isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _ROUTE_METHODS:
            # dependencies=[Depends(x)] on the route decorator itself
            for kw in d.keywords:
                for sub in ast.walk(kw.value):
                    if isinstance(sub, ast.Call) and dotted(sub.func) in ("Depends", "Security", "fastapi.Depends") and sub.args:
                        n = dotted(sub.args[0])
                        if n and GUARD_RE.search(n):
                            g.add(n)
            continue
        n = dotted(d)
        if n and GUARD_RE.search(n):
            g.add(n)
    args = fn.args
    defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
    annos = [a.annotation for a in args.args + args.kwonlyargs if a.annotation is not None]
    for node in defaults + annos:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and dotted(sub.func) in ("Depends", "Security", "fastapi.Depends") and sub.args:
                n = dotted(sub.args[0])
                if n and GUARD_RE.search(n):
                    g.add(n)
    for n in walk_no_nested(fn):
        if isinstance(n, ast.Call):
            name = dotted(n.func)
            if name and STRONG_GUARD_RE.search(name.split(".")[-1]) and name.split(".")[-1] not in ("get", "post"):
                g.add(name)
    return g


def _guard_label(token: str) -> str:
    return token.split(".")[-1] if token.startswith("self.") else token


def mine_route_guards(mods: list[_Module], min_support: int, min_cons: float) -> list[Invariant]:
    groups: dict[tuple, list[tuple[Site, set[str], bool]]] = {}
    for m in mods:
        for fn, cls, nested in iter_functions(m.tree):
            if nested:
                continue
            site = Site(m.rel, fn.lineno, fn.name)
            public = bool(PUBLIC_RE.search(fn.name))
            ri = _route_info(fn)
            if ri:
                base, prefix = ri
                gs = _guards(fn, True)
                groups.setdefault(("router", m.rel, base, None), []).append((site, gs, public))
                if prefix:
                    groups.setdefault(("router", m.rel, base, prefix), []).append((site, gs, public))
            elif (not cls and fn.args.args and fn.args.args[0].arg == "request" and not fn.name.startswith("_")
                  and not fn.name.startswith("test")):
                groups.setdefault(("views", m.rel, "view functions taking `request`", None), []).append((site, _guards(fn, False), public))
            elif cls and "test" in cls.lower():
                continue
            elif cls and fn.name.startswith(("test", "setUp", "tearDown")):
                continue
            elif cls and fn.name in _VERBS:
                groups.setdefault(("verbs", m.rel, "HTTP-verb methods", None), []).append((site, _guards(fn, False), public))
            elif cls and not fn.name.startswith("_"):
                groups.setdefault(("class", m.rel, cls, None), []).append((site, _guards(fn, False), public))
    out: list[Invariant] = []
    for (kind, rel, owner, prefix), members in groups.items():
        counted = [(s, g, p) for s, g, p in members]
        if len(counted) < min_support:
            continue
        tokens: dict[str, int] = {}
        for _, g, _ in counted:
            for t in g:
                tokens[t] = tokens.get(t, 0) + 1
        for tok, cnt in tokens.items():
            followers = [s for s, g, _ in counted if tok in g]
            exc = [s for s, g, p in counted if tok not in g and not p]
            total = len(followers) + len(exc)  # public-by-design handlers are neither
            if len(followers) < min_support or total == 0 or len(followers) / total < min_cons:
                continue
            where = (f"handlers on `{owner}`" + (f" under /{prefix}" if prefix else "") if kind == "router"
                     else "get/post/put/delete/patch methods" if kind == "verbs"
                     else "view functions taking `request`" if kind == "views" else f"public methods of class {owner}")
            inv_id = f"route-guard:{rel}:{owner}:{prefix or '*'}:{tok}"
            out.append(Invariant(inv_id, "route-guard", f"{where} in {rel} use `{_guard_label(tok)}`",
                                 len(followers), total, followers, exc))
    return _dedupe(out)


def _dedupe(invs: list[Invariant]) -> list[Invariant]:
    """Drop an invariant when a stricter one (higher consistency, same guard & exceptions) exists."""
    best: dict[tuple, Invariant] = {}
    for i in invs:
        key = (i.kind, i.id.rsplit(":", 1)[-1], tuple(sorted(map(str, i.exceptions))), i.id.split(":")[1])
        cur = best.get(key)
        if cur is None or (i.consistency, -i.total) > (cur.consistency, -cur.total):
            best[key] = i
    return list(best.values())


# ---------------------------------------------------------------- call-pair

def _unwrap_await(n):
    return n.value if isinstance(n, ast.Await) else n


def _call_parts(call: ast.Call) -> tuple[str | None, str | None]:
    f = call.func
    if isinstance(f, ast.Name):
        return None, f.id
    if isinstance(f, ast.Attribute):
        return dotted(f.value), f.attr
    return None, None


def _closer_names(name: str) -> list[str]:
    verb, _, rest = name.partition("_")
    return [c + ("_" + rest if rest else "") for c in _PAIRS.get(verb, [])]


def mine_call_pairs(mods: list[_Module], min_support: int, min_cons: float) -> list[Invariant]:
    stats: dict[str, dict[str, list[Site]]] = {}
    for m in mods:
        for fn, cls, nested in iter_functions(m.tree):
            if _NEUTRAL_FUNCS.match(fn.name) or any(
                    (dotted(d) or "").endswith(("contextmanager", "asynccontextmanager", "fixture")) for d in fn.decorator_list):
                continue
            if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in walk_no_nested(fn)):
                continue
            calls = []  # (recv, name, lineno, bound var or None, is_opener_ctx)
            opener_nodes: dict[int, str | None] = {}
            for n in walk_no_nested(fn):
                if isinstance(n, ast.Expr):
                    v = _unwrap_await(n.value)
                    if isinstance(v, ast.Call):
                        opener_nodes[id(v)] = None
                elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                    tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
                    v = _unwrap_await(n.value) if n.value is not None else None
                    if isinstance(v, ast.Call) and len(tgts) == 1 and isinstance(tgts[0], ast.Name):
                        opener_nodes[id(v)] = tgts[0].id
            for n in walk_no_nested(fn):
                if isinstance(n, ast.Call):
                    recv, name = _call_parts(n)
                    if name:
                        calls.append((recv, name, n.lineno, opener_nodes.get(id(n)), id(n) in opener_nodes))
            for recv, name, line, bound, is_ctx in calls:
                if not is_ctx or name.split("_")[0] not in _PAIRS:
                    continue
                names = _closer_names(name)
                followed = any(r2 == recv or (bound and r2 == bound) for r2, n2, l2, _, _ in calls
                               if n2 in names and l2 >= line)
                if bound is None and recv is None:
                    followed = followed or any(n2 in names and l2 >= line for _, n2, l2, _, _ in calls)
                d = stats.setdefault(name, {"f": [], "e": []})
                d["f" if followed else "e"].append(Site(m.rel, line, fn.name))
    out = []
    for name, d in stats.items():
        f, e = d["f"], d["e"]
        total = len(f) + len(e)
        if len(f) < min_support or total == 0 or len(f) / total < min_cons:
            continue
        closer = "/".join(_closer_names(name))
        out.append(Invariant(f"call-pair:{name}", "call-pair",
                             f"`{name}()` is followed by `{closer}()` in the same function", len(f), total, f, e))
    return out


# ---------------------------------------------------------------- guard-before-mutation

def _target_token(t: ast.AST) -> str | None:
    if isinstance(t, ast.Attribute):
        return t.attr
    if isinstance(t, ast.Name):
        return t.id
    return None


def _tokens_in(node: ast.AST) -> set[str]:
    s = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            s.add(n.id)
        elif isinstance(n, ast.Attribute):
            s.add(n.attr)
    return s


def mine_guard_mutation(mods: list[_Module], min_support: int, min_cons: float) -> list[Invariant]:
    stats: dict[str, dict[str, list[Site]]] = {}
    for m in mods:
        for fn, cls, nested in iter_functions(m.tree):
            if fn.name.startswith("__"):
                continue
            tests: list[set[str]] = []
            muts: list[tuple[str, int]] = []
            for n in walk_no_nested(fn):
                if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                    tests.append(_tokens_in(n.test))
                elif isinstance(n, ast.Compare):
                    tests.append(_tokens_in(n))
                if isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Sub):
                    tok, amt = _target_token(n.target), n.value
                elif (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.value, ast.BinOp)
                      and isinstance(n.value.op, ast.Sub) and ast.dump(n.value.left) == ast.dump(n.targets[0])):
                    tok, amt = _target_token(n.targets[0]), n.value.right
                else:
                    continue
                if not tok or len(tok) < 3 or not (_tokens_in(amt)):  # skip constants and i/j/n style names
                    continue
                if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id.isupper():
                    continue
                muts.append((tok, n.lineno))
            for tok, line in muts:
                guarded = any(tok in t for t in tests)
                d = stats.setdefault(tok, {"f": [], "e": []})
                d["f" if guarded else "e"].append(Site(m.rel, line, fn.name))
    out = []
    for tok, d in stats.items():
        f, e = d["f"], d["e"]
        total = len(f) + len(e)
        if len(f) < min_support or total == 0 or len(f) / total < min_cons:
            continue
        out.append(Invariant(f"guard-mutation:{tok}", "guard-mutation",
                             f"`{tok}` is decreased only in functions that also compare `{tok}`", len(f), total, f, e))
    return out


# ---------------------------------------------------------------- sql-scope

_SQL_START = re.compile(r"^\s*(update|delete)\s", re.I)


def _sql_strings(tree: ast.Module):
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _SQL_START.match(n.value):
            yield n.lineno, n.value


def mine_sql_scope(mods: list[_Module], min_support: int, min_cons: float) -> list[Invariant]:
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover
        return []
    stats: dict[str, dict[str, list[Site]]] = {}
    scope_of: dict[str, dict[str, int]] = {}
    for m in mods:
        funcs = [(f.lineno, getattr(f, "end_lineno", f.lineno), f.name) for f, _, _ in iter_functions(m.tree)]
        for line, text in _sql_strings(m.tree):
            if "{" in text or "}" in text or text.count(";") > 1:
                continue
            q = re.sub(r"%\(\w+\)s|%s", "?", text)
            try:
                parsed = sqlglot.parse(q)
            except Exception:
                continue
            if len(parsed) != 1 or not isinstance(parsed[0], (exp.Update, exp.Delete)):
                continue
            stmt = parsed[0]
            tbl = stmt.this
            if not isinstance(tbl, exp.Table) or not tbl.name or tbl.args.get("db"):
                continue
            where = stmt.args.get("where")
            cols = {c.name.lower() for c in where.find_all(exp.Column)} if where else set()
            fname = min((f for f in funcs if f[0] <= line <= f[1]), key=lambda f: f[1] - f[0], default=(0, 0, ""))[2]
            key = tbl.name.lower()
            d = stats.setdefault(key, {})
            d.setdefault("sites", []).append((Site(m.rel, line, fname), cols))
    out = []
    for table, d in stats.items():
        sites = d["sites"]
        if len(sites) < min_support:
            continue
        for col in SCOPE_COLS:
            f = [s for s, cols in sites if col in cols]
            e = [s for s, cols in sites if col not in cols]
            total = len(sites)
            if len(f) < min_support or len(f) / total < min_cons:
                continue
            out.append(Invariant(f"sql-scope:{table}:{col}", "sql-scope",
                                 f"UPDATE/DELETE on `{table}` filter by `{col}`", len(f), total, f, e))
    return out


# ---------------------------------------------------------------- driver

MINERS = {"route-guard": mine_route_guards, "call-pair": mine_call_pairs,
          "guard-mutation": mine_guard_mutation, "sql-scope": mine_sql_scope}


def scan(root: str | Path, min_support: int = DEFAULT_MIN_SUPPORT, min_consistency: float = DEFAULT_MIN_CONSISTENCY,
         include_tests: bool = False, exclude: list[str] | None = None, kinds: list[str] | None = None) -> ScanResult:
    t0 = time.perf_counter()
    rootp = Path(root).resolve()
    mods, skipped = _load(rootp, include_tests, exclude or [])
    invs: list[Invariant] = []
    for kind, fn in MINERS.items():
        if kinds is None or kind in kinds:
            invs.extend(fn(mods, min_support, min_consistency))
    invs.sort(key=lambda i: (-bool(i.exceptions), -_wilson_lower(i.follow, i.total), i.id))
    return ScanResult(str(rootp), len(mods), time.perf_counter() - t0, invs, skipped)

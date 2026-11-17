# Vendored from axiom-lab mind/code/typed_calls.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Second pass: resolve `obj.method()` calls through type annotations.

The first pass (`tenthcheck.parser`) resolves bare names, `module.func` and `self.method`. It cannot tell what
`response.read()` calls, because it doesn't know what `response` is. This pass infers the class of a receiver from
annotations and constructors, then adds `CALLS` edges to the method found on that class or its parsed bases.

What gives a variable a type (inside one function, flow-insensitive: the last assignment wins):
- a parameter annotation (`request: Request`, `Optional[Request]`, `Request | None`, `"Request"`);
- `x = Cls(...)` where `Cls` is a parsed class;
- `x = f(...)` / `x = obj.m(...)` where the callee has a return annotation naming a parsed class;
- `x = self.attr` / `x = obj.attr` where the attribute is annotated on the class (class body `attr: T`, or
  `self.attr: T = ...` / `self.attr = <typed value>` in a method);
- `x: T = ...` local annotations;
- `with <expr> as x`: `Iterator[T]` / `Generator[T, ...]` / `ContextManager[T]` → `T`; a plain class → that class;
- `for x in <expr>` over `list[T]` / `Iterator[T]` / ...; `for k, v in <expr>.items()` over `dict[K, V]`;
- `x = yield ...` inside a function annotated `Generator[Y, S, R]` → `S`.
Receivers may be chains (`self.ctx.command.get_params()`), each hop through an annotated attribute.

Names in annotations resolve through the module's imports, including imports under `if TYPE_CHECKING:` (used only
for annotations here, never as `IMPORTS` edges).

Also: `super().m()` (looked up in the enclosing class's parsed bases) and tuple unpacking of an annotated
`tuple[A, B, C]` return.

Not handled (no edge rather than a guess): closures reading variables of an enclosing function, untyped
parameters, `isinstance` narrowing, overload selection, generics bound at call sites, `getattr`, and anything whose
type comes from outside the parsed package.
"""
from __future__ import annotations

import ast

from tenthcheck.graph import Graph

_ELEMENT = {"list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection", "set", "Set",
            "frozenset", "FrozenSet", "AbstractSet", "MutableSet", "Generator", "AsyncIterator", "AsyncIterable",
            "AsyncGenerator", "Deque", "deque", "Reversible"}
_CONTEXT = {"ContextManager", "AbstractContextManager", "AsyncContextManager", "AbstractAsyncContextManager",
            "Iterator", "Generator", "AsyncIterator", "AsyncGenerator"}
_MAPPING = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "DefaultDict", "defaultdict"}
_UNION = {"Optional", "Union", "Annotated", "Final", "ClassVar", "Required", "NotRequired", "ReadOnly"}


def _tail(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


class TypeIndex:
    def __init__(self, g: Graph, parsed: list, known_modules: set[str], match_module, resolve_import_from):
        self.g = g
        self.bindings: dict[str, dict[str, str]] = {}
        self.class_attrs: dict[str, dict[str, tuple[ast.expr, str]]] = {}
        self.returns: dict[str, tuple[ast.expr, str]] = {}
        self.params: dict[str, dict[str, tuple[ast.expr, str]]] = {}
        self.class_of_method: dict[str, str] = {}
        self.bases: dict[str, list[str]] = {}
        for r in g.rels("INHERITS_FROM"):
            self.bases.setdefault(r.src, []).append(r.dst)
        self.module_of: dict[str, str] = {r.dst: r.src for r in g.rels("DEFINES")}
        for mod in parsed:
            self.bindings[mod.name] = self._module_bindings(mod, known_modules, match_module, resolve_import_from)
        for mod in parsed:
            self._index_module(mod)

    # ---------- bindings for annotation names
    def _module_bindings(self, mod, known, match_module, resolve_import_from) -> dict[str, str]:
        b: dict[str, str] = {}

        def imports(stmts):
            for node in stmts:
                if isinstance(node, ast.Import):
                    for a in node.names:
                        m = match_module(a.name, known)
                        if m:
                            b[a.asname or a.name.split(".", 1)[0]] = m
                elif isinstance(node, ast.ImportFrom):
                    target = resolve_import_from(mod, node)
                    if not target:
                        continue
                    mid = match_module(target, known) or target
                    for a in node.names:
                        cand = f"{mid}.{a.name}"
                        sub = match_module(f"{target}.{a.name}", known)
                        if cand in self.g.entities:
                            b[a.asname or a.name] = cand
                        elif sub:
                            b[a.asname or a.name] = sub
                elif isinstance(node, ast.If) and self._is_type_checking(node.test):
                    imports(node.body)

        imports(mod.tree.body)
        for node in mod.tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in mod.final_ids:
                    b[node.name] = mod.final_ids[node.name]
        return b

    @staticmethod
    def _is_type_checking(test: ast.expr) -> bool:
        return _tail(test) == "TYPE_CHECKING"

    # ---------- per-module index: class attributes, returns, params
    def _index_module(self, mod) -> None:
        def walk(body, cls_id):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    cid = mod.entity_ids.get(id(node))
                    attrs = self.class_attrs.setdefault(cid, {})
                    for st in node.body:
                        if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                            attrs[st.target.id] = (st.annotation, mod.name)
                    walk(node.body, cid)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fid = mod.entity_ids.get(id(node))
                    if fid is None:
                        continue
                    if node.returns is not None:
                        self.returns[fid] = (node.returns, mod.name)
                    ps = {}
                    for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                        if a.annotation is not None:
                            ps[a.arg] = (a.annotation, mod.name)
                    self.params[fid] = ps
                    if cls_id:
                        self.class_of_method[fid] = cls_id
                        self._self_attrs(node, cls_id, mod.name, ps)
                    walk(node.body, None)

        walk(mod.tree.body, None)

    def _self_attrs(self, fn, cls_id, module, params) -> None:
        attrs = self.class_attrs.setdefault(cls_id, {})
        for st in ast.walk(fn):
            target, value, ann = None, None, None
            if isinstance(st, ast.AnnAssign):
                target, value, ann = st.target, st.value, st.annotation
            elif isinstance(st, ast.Assign) and len(st.targets) == 1:
                target, value = st.targets[0], st.value
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
                continue
            if ann is not None:
                attrs[target.attr] = (ann, module)
            elif target.attr not in attrs and isinstance(value, ast.Name) and value.id in params:
                attrs[target.attr] = params[value.id]
            elif target.attr not in attrs and isinstance(value, ast.Call):
                attrs[target.attr] = (value, module)  # a constructor or annotated call; resolved lazily

    # ---------- type expressions -> ("class", id) | ("seq", T) | ("map", K, V) | ("ctx", T) | ("gen", Y, S)
    def resolve(self, expr: ast.expr | None, module: str, depth: int = 0):
        if expr is None or depth > 6:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                return self.resolve(ast.parse(expr.value, mode="eval").body, module, depth + 1)
            except SyntaxError:
                return None
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return self.resolve(expr.left, module, depth + 1) or self.resolve(expr.right, module, depth + 1)
        if isinstance(expr, ast.Call):  # a stored constructor/call value (from _self_attrs)
            return self._call_type(expr, module, {}, None, depth + 1)
        if isinstance(expr, ast.Subscript):
            base = _tail(expr.value)
            args = expr.slice.elts if isinstance(expr.slice, ast.Tuple) else [expr.slice]
            if not args:  # e.g. tuple[()]
                return None
            if base in _UNION:
                for a in args:
                    t = self.resolve(a, module, depth + 1)
                    if t:
                        return t
                return None
            if base in _MAPPING and len(args) == 2:
                return ("map", self.resolve(args[0], module, depth + 1), self.resolve(args[1], module, depth + 1))
            if base in ("Generator", "AsyncGenerator"):
                return ("gen", self.resolve(args[0], module, depth + 1),
                        self.resolve(args[1], module, depth + 1) if len(args) > 1 else None)
            if base in _CONTEXT:
                return ("ctx", self.resolve(args[0], module, depth + 1))
            if base in ("tuple", "Tuple") and len(args) > 1 and not any(isinstance(a, ast.Constant) and a.value is Ellipsis for a in args):
                return ("tuple", [self.resolve(a, module, depth + 1) for a in args])
            if base in _ELEMENT or base in ("tuple", "Tuple"):
                return ("seq", self.resolve(args[0], module, depth + 1))
            return None
        if isinstance(expr, (ast.Name, ast.Attribute)):
            b = self.bindings.get(module, {})
            eid = None
            if isinstance(expr, ast.Name):
                eid = b.get(expr.id)
            elif isinstance(expr.value, ast.Name) and expr.value.id in b:
                eid = f"{b[expr.value.id]}.{expr.attr}"
            ent = self.g.entities.get(eid) if eid else None
            if ent is not None and ent.type == "class":
                return ("class", eid)
        return None

    # ---------- methods and attributes on a class, through parsed bases
    def _mro(self, cls_id: str) -> list[str]:
        out, todo, seen = [], [cls_id], set()
        while todo:
            c = todo.pop(0)
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            todo += self.bases.get(c, [])
        return out

    def method(self, cls_id: str, name: str, final_ids: dict[str, dict[str, str]]) -> str | None:
        for c in self._mro(cls_id):
            mod = self.module_of.get(c)
            if mod is None:
                continue
            qual = c[len(mod) + 1:].split("@L", 1)[0]
            eid = final_ids.get(mod, {}).get(f"{qual}.{name}")
            ent = self.g.entities.get(eid)
            if ent is not None and ent.type == "function":
                return eid
        return None

    def attr_type(self, cls_id: str, name: str):
        for c in self._mro(cls_id):
            hit = self.class_attrs.get(c, {}).get(name)
            if hit:
                return self.resolve(hit[0], hit[1])
        return None

    # ---------- expressions inside a function
    def _call_type(self, call: ast.Call, module: str, env: dict, final_ids, depth: int = 0):
        f = call.func
        if isinstance(f, (ast.Name, ast.Attribute)) and not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Call)):
            direct = self.resolve(f, module, depth + 1)
            if direct and direct[0] == "class":
                return direct  # constructor
        callee = None
        if isinstance(f, ast.Name):
            eid = self.bindings.get(module, {}).get(f.id)
            if eid and self.g.entities.get(eid) is not None and self.g.entities[eid].type == "function":
                callee = eid
        elif isinstance(f, ast.Attribute):
            recv = self.expr_type(f.value, module, env, final_ids or {}, depth + 1)
            if recv and recv[0] == "class":
                callee = self.method(recv[1], f.attr, final_ids or {})
            elif isinstance(f.value, ast.Name):
                head = self.bindings.get(module, {}).get(f.value.id)
                cand = f"{head}.{f.attr}" if head else None
                if cand and self.g.entities.get(cand) is not None and self.g.entities[cand].type == "function":
                    callee = cand
        if callee and callee in self.returns:
            ann, m = self.returns[callee]
            return self.resolve(ann, m, depth + 1)
        return None

    def expr_type(self, expr: ast.expr, module: str, env: dict, final_ids, depth: int = 0):
        if depth > 8:
            return None
        if isinstance(expr, ast.Name):
            return env.get(expr.id)
        if isinstance(expr, ast.Attribute):
            recv = self.expr_type(expr.value, module, env, final_ids, depth + 1)
            if recv and recv[0] == "class":
                return self.attr_type(recv[1], expr.attr)
            return None
        if isinstance(expr, ast.Call):
            return self._call_type(expr, module, env, final_ids, depth + 1)
        if isinstance(expr, ast.Await):
            return self.expr_type(expr.value, module, env, final_ids, depth + 1)
        return None

    def function_env(self, fn, fid: str, module: str, final_ids) -> dict:
        env: dict = {}
        cls = self.class_of_method.get(fid)
        if cls and fn.args.args:
            env[fn.args.args[0].arg] = ("class", cls)  # self (or cls, close enough for method lookup)
        for p, (ann, m) in self.params.get(fid, {}).items():
            t = self.resolve(ann, m)
            if t:
                env[p] = t
        ret = self.resolve(self.returns[fid][0], self.returns[fid][1]) if fid in self.returns else None
        for _ in range(2):  # two passes so later-defined helpers' types flow into earlier uses
            for st in _own_statements(fn):
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    t = self.resolve(st.annotation, module)
                    if t:
                        env[st.target.id] = t
                elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Tuple):
                    t = self.expr_type(st.value, module, env, final_ids)
                    if t and t[0] == "tuple" and len(t[1]) == len(st.targets[0].elts):
                        for tgt, tt in zip(st.targets[0].elts, t[1]):
                            if isinstance(tgt, ast.Name) and tt:
                                env[tgt.id] = tt
                elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    v = st.value
                    if isinstance(v, ast.Yield) and ret and ret[0] == "gen":
                        t = ret[2]
                    else:
                        t = self.expr_type(v, module, env, final_ids)
                    if t:
                        env[st.targets[0].id] = t
                elif isinstance(st, (ast.With, ast.AsyncWith)):
                    for item in st.items:
                        if isinstance(item.optional_vars, ast.Name):
                            t = self.expr_type(item.context_expr, module, env, final_ids)
                            if t and t[0] in ("ctx", "gen"):
                                t = t[1]
                            if t and t[0] == "class":
                                env[item.optional_vars.id] = t
                elif isinstance(st, (ast.For, ast.AsyncFor)):
                    it = st.iter
                    if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr in ("items", "keys", "values"):
                        m = self.expr_type(it.func.value, module, env, final_ids)
                        if m and m[0] == "map":
                            k, v = m[1], m[2]
                            if it.func.attr == "items" and isinstance(st.target, ast.Tuple) and len(st.target.elts) == 2:
                                for tgt, tt in zip(st.target.elts, (k, v)):
                                    if isinstance(tgt, ast.Name) and tt:
                                        env[tgt.id] = tt
                            elif isinstance(st.target, ast.Name):
                                tt = k if it.func.attr == "keys" else v
                                if tt:
                                    env[st.target.id] = tt
                    else:
                        s = self.expr_type(it, module, env, final_ids)
                        if s and s[0] in ("seq", "gen", "ctx") and s[1] and isinstance(st.target, ast.Name):
                            env[st.target.id] = s[1]
        return env


def _own_statements(fn):
    """Every statement in a function body, not descending into nested functions or classes."""
    todo = list(fn.body)
    while todo:
        st = todo.pop(0)
        yield st
        for field in ("body", "orelse", "finalbody", "handlers"):
            for child in getattr(st, field, []) or []:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(child, ast.ExceptHandler):
                    todo += child.body
                elif isinstance(child, ast.stmt):
                    todo.append(child)
        if isinstance(st, ast.Match):
            for case in st.cases:
                todo += case.body


def add_typed_calls(g: Graph, parsed: list, known_modules: set[str], match_module, resolve_import_from, iter_defs, calls_in_body) -> int:
    """Add CALLS edges for `obj.method()` calls whose receiver type is known. Returns how many were added."""
    idx = TypeIndex(g, parsed, known_modules, match_module, resolve_import_from)
    final_ids = {mod.name: mod.final_ids for mod in parsed}
    before = len(g.relations)
    errors: list[str] = []
    for mod in parsed:
        for qual, node, enclosing in iter_defs(mod.tree.body, ""):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fid = mod.entity_ids[id(node)]
            try:  # best-effort pass: an odd construct skips this function's typed edges, never the parse
                _typed_edges(g, idx, node, fid, mod.name, final_ids, calls_in_body)
            except (IndexError, KeyError, TypeError, AttributeError, ValueError, RecursionError) as e:
                errors.append(f"{fid}: {type(e).__name__}: {e}")
    if errors:
        g.meta["typed_calls_errors"] = errors
    return len(g.relations) - before


def _typed_edges(g, idx, node, fid, module, final_ids, calls_in_body) -> None:
    """CALLS edges for one function's `obj.method()` / `super().method()` calls whose receiver type is known."""
    env = idx.function_env(node, fid, module, final_ids)
    for func in calls_in_body(node.body):
        if not isinstance(func, ast.Attribute):
            continue
        if (isinstance(func.value, ast.Call) and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "super" and not func.value.args and fid in idx.class_of_method):
            # super().m(): look the method up in the enclosing class's parsed bases (skip the class itself)
            for base in idx.bases.get(idx.class_of_method[fid], []):
                dst = idx.method(base, func.attr, final_ids)
                if dst:
                    g.relate("CALLS", fid, dst)
                    break
            continue
        recv = idx.expr_type(func.value, module, env, final_ids)
        if recv and recv[0] == "class":
            dst = idx.method(recv[1], func.attr, final_ids)
            if dst and dst != fid:
                g.relate("CALLS", fid, dst)

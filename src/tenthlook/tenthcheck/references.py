# Vendored from axiom-lab mind/code/references.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Third pass: `REFERENCES` edges to classes that are used without being called or subclassed.

Before this pass a class's dependents were only its subclasses (`INHERITS_FROM`): `CALLS` is function-to-function,
so a class used in annotations, constructed, checked with `isinstance`, or imported had no dependents at all. On
rich, `check_blast_radius("rich.console.ConsoleOptions")` returned only the class itself, although ~29 modules
import it (2026-11-17 with/without run, task t06).

`REFERENCES`: source -> class, for every parsed class the source names. Sources:
- a **function**: its decorators, parameter annotations and defaults, return annotation and its own body
  (constructor calls, `isinstance(x, C)`, `C.attr`, local annotations), not nested defs, which are their own
  entities;
- a **class**: its decorators, keyword arguments (`metaclass=`) and its own body statements (class attribute
  annotations and values), not its methods. Bases stay `INHERITS_FROM`;
- a **module**: its top-level statements other than defs, including `from x import C` (also under
  `if TYPE_CHECKING:` and inside `try`) and module-level code such as `X = C()`.

Names resolve through the module's imports (including `if TYPE_CHECKING:` blocks), a function's own
`from x import C` imports, module-level classes, and
`alias.C` where `alias` is a parsed module. Re-exports are followed: `from pkg import C`, where `pkg/__init__.py`
only re-exports `C` from `pkg.mod`, references `pkg.mod.C`. Quoted annotations (`options: "ConsoleOptions"`) are
parsed. A function or class never references a class it is nested in (methods of `C` naming `C` are part of `C`).

Not seen (no edge, never a guess): classes reached through `getattr`, string names outside annotation positions,
names rebound inside a function, code under `if __name__ == "__main__":` (classes and functions there are not
entities), and classes from packages that were not parsed. Targets are classes only:
functions keep their `CALLS` edges, so callers of functions are unchanged by this pass.
"""
from __future__ import annotations

import ast

from tenthlook.tenthcheck.graph import Graph

_MAX_REEXPORT_HOPS = 5


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _top_imports(body: list[ast.stmt]):
    """Module-level imports, including those under `if TYPE_CHECKING:` and in `try` / `except` / `else`."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            yield from _top_imports(node.body)
        elif isinstance(node, ast.Try):
            yield from _top_imports(node.body)
            for h in node.handlers:
                yield from _top_imports(h.body)
            yield from _top_imports(node.orelse)


class _Names:
    """Per-module name -> entity id for references (classes, modules), following re-exports."""

    def __init__(self, g: Graph, parsed: list, known_modules: set[str], match_module, resolve_import_from):
        self.g, self.known, self.match = g, known_modules, match_module
        self.by_name = {mod.name: mod for mod in parsed}
        self.raw: dict[str, dict[str, tuple[str, str] | str]] = {}  # name -> ("from", module, attr) | entity id
        for mod in parsed:
            b: dict = {}
            for node in _top_imports(mod.tree.body):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        m = match_module(a.name, known_modules)
                        if m:
                            b[a.asname or a.name.split(".", 1)[0]] = m if a.asname else match_module(
                                a.name.split(".", 1)[0], known_modules) or m
                    continue
                target = resolve_import_from(mod, node)
                if not target or (len(node.names) == 1 and node.names[0].name == "*"):
                    continue
                mid = match_module(target, known_modules) or target
                for a in node.names:
                    sub = match_module(f"{target}.{a.name}", known_modules)
                    b[a.asname or a.name] = sub if sub else ("from", mid, a.name)
            for node in mod.tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in mod.final_ids:
                    b[node.name] = mod.final_ids[node.name]
            self.raw[mod.name] = b

    def export(self, module: str, name: str, hops: int = 0) -> str | None:
        """The entity `from module import name` binds: a definition in `module`, or what `module` re-exports."""
        eid = f"{module}.{name}"
        if eid in self.g.entities and self.g[eid].type in ("class", "function"):
            return eid
        if hops >= _MAX_REEXPORT_HOPS or module not in self.raw:
            return None
        return self.lookup(module, name, hops + 1)

    def lookup(self, module: str, name: str, hops: int = 0) -> str | None:
        v = self.raw.get(module, {}).get(name)
        if v is None:
            return None
        if isinstance(v, tuple):
            return self.export(v[1], v[2], hops)
        return v

    def resolve(self, expr: ast.expr, module: str, local: dict | None = None) -> str | None:
        """A class id for `Name` / `alias.Name`, or None. `local`: a function's own `from x import C` bindings."""
        eid = None
        if isinstance(expr, ast.Name):
            eid = (local or {}).get(expr.id) or self.lookup(module, expr.id)
        elif isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            head = self.lookup(module, expr.value.id)
            if head in self.known:
                eid = self.export(head, expr.attr)
        ent = self.g.entities.get(eid) if eid else None
        return eid if ent is not None and ent.type == "class" else None


class _Collector(ast.NodeVisitor):
    """Class ids named in a region, without entering nested defs/classes. Strings count only in annotations."""

    def __init__(self, names: _Names, module: str, local: dict | None = None):
        self.names, self.module, self.found = names, module, set()
        self.local = local or {}
        self._ann = 0

    def annotation(self, node: ast.expr | None) -> None:
        if node is None:
            return
        self._ann += 1
        self.visit(node)
        self._ann -= 1

    def visit_FunctionDef(self, node):  # nested def: its own entity
        return

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Name(self, node):
        cid = self.names.resolve(node, self.module, self.local)
        if cid:
            self.found.add(cid)

    def visit_Attribute(self, node):
        cid = self.names.resolve(node, self.module, self.local)
        if cid:
            self.found.add(cid)
        else:
            self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.annotation(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_arg(self, node):
        self.annotation(node.annotation)

    def visit_Constant(self, node):
        if self._ann and isinstance(node.value, str):
            try:
                expr = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return
            self.visit(expr)


def _local_imports(names: _Names, mod, node, resolve_import_from) -> dict[str, str]:
    """`from x import C` inside a function body (not nested defs): name -> class id. The import itself counts too."""
    out: dict[str, str] = {}
    todo = list(node.body)
    while todo:
        st = todo.pop()
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(st, ast.ImportFrom) and not (len(st.names) == 1 and st.names[0].name == "*"):
            target = resolve_import_from(mod, st)
            if target:
                mid = names.match(target, names.known) or target
                for a in st.names:
                    eid = names.export(mid, a.name)
                    if eid and names.g[eid].type == "class":
                        out[a.asname or a.name] = eid
        todo += [c for c in ast.iter_child_nodes(st) if isinstance(c, ast.stmt)]
        if isinstance(st, ast.Try):
            for h in st.handlers:
                todo += h.body
    return out


def _function_refs(c: _Collector, node) -> None:
    c.found |= set(c.local.values())
    for d in node.decorator_list:
        c.visit(d)
    a = node.args
    for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
        if arg is not None:
            c.annotation(arg.annotation)
    for d in [*a.defaults, *[x for x in a.kw_defaults if x is not None]]:
        c.visit(d)
    c.annotation(node.returns)
    for st in node.body:
        c.visit(st)


def _class_refs(c: _Collector, node: ast.ClassDef) -> None:
    for d in node.decorator_list:
        c.visit(d)
    for kw in node.keywords:
        c.visit(kw.value)
    for st in node.body:
        c.visit(st)  # methods and nested classes are skipped by the collector


def add_references(g: Graph, parsed: list, known_modules: set[str], match_module, resolve_import_from, iter_defs) -> int:
    """Add `REFERENCES` edges (function/class/module -> class). Returns how many were added."""
    names = _Names(g, parsed, known_modules, match_module, resolve_import_from)
    before = len(g.relations)
    errors: list[str] = []

    def emit(src: str, found: set[str]) -> None:
        for cid in found:
            if cid != src and not src.startswith(cid + "."):
                g.relate("REFERENCES", src, cid)

    for mod in parsed:
        try:
            top = _Collector(names, mod.name)
            for st in mod.tree.body:
                if isinstance(st, (ast.Import, ast.ImportFrom)):
                    continue
                top.visit(st)
            for node in _top_imports(mod.tree.body):  # `from x import C` binds C: the module depends on it
                if isinstance(node, ast.ImportFrom) and not (len(node.names) == 1 and node.names[0].name == "*"):
                    for a in node.names:
                        cid = names.lookup(mod.name, a.asname or a.name)
                        if cid and g[cid].type == "class":
                            top.found.add(cid)
            emit(mod.name, top.found)
        except (RecursionError, KeyError, ValueError) as e:
            errors.append(f"{mod.name}: {type(e).__name__}: {e}")
        for _qual, node, _enclosing in iter_defs(mod.tree.body, ""):
            src = mod.entity_ids[id(node)]
            local = None if isinstance(node, ast.ClassDef) else _local_imports(names, mod, node, resolve_import_from)
            c = _Collector(names, mod.name, local)
            try:  # best-effort, like the typed pass: an odd construct skips this entity's references only
                (_class_refs if isinstance(node, ast.ClassDef) else _function_refs)(c, node)
            except (RecursionError, KeyError, ValueError) as e:
                errors.append(f"{src}: {type(e).__name__}: {e}")
                continue
            emit(src, c.found)
    if errors:
        g.meta["references_errors"] = errors
    return len(g.relations) - before

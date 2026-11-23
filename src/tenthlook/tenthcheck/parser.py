# Vendored from axiom-lab mind/code/parser.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Parse Python source into the shared entity-relation graph.

Code structure is observable, so this is not a World. `parse_module` / `parse_package`
walk real `ast` trees. They do not guess at runtime behavior.

Entities
- One module entity per parsed module, id = module name, type `module`.
- One entity per function (including async and methods) and class, types
  `function` and `class`. Nested defs are included. Id is `{module}.{qualname}`.
- One entity per module-level assignment target (Assign, AnnAssign, AugAssign), type
    `module`, id `{module}.{name}` for the first binding and line-qualified ids for
    later bindings of the same name. The same type tag as the module entity is
  deliberate: the requested schema has three tags, `function` / `class` /
  `module`. Ids differ. `of_type("module")` therefore returns modules and
  module-level bindings together. Class-body and function-body assignments
  are not included. A binding whose id is already a function or class is skipped.

Attributes: `name`, `lineno`. Functions also have `n_args` (positional
parameters, including `self` / `cls` and positional-only, excluding `*args`,
`**kwargs`, and keyword-only). Functions and classes have `decorators`, a list
of decorator names (`@dataclass(frozen=True)` is recorded as `dataclass`;
`@pytest.mark.parametrize` as `pytest.mark.parametrize`).

Relations
- `DEFINES`: module -> each function and class defined in it (including methods
  and nested defs). Assignments are not DEFINES targets.
- `CALLS`: function -> function, only when the callee resolves (below).
- `IMPORTS`: module -> module, from `import` and `from ... import`. `from pkg import sub`, where
  `pkg.sub` is a parsed module, records both `pkg` and `pkg.sub`.
- `INHERITS_FROM`: class -> class, when the base resolves.
- `REFERENCES`: function / class / module -> class, when the source names a parsed class without calling or
  subclassing it: annotations (quoted ones too), constructor calls, `isinstance`, `from x import C` (also under
  `if TYPE_CHECKING:`), following re-exports. Added by the third pass, `tenthcheck.references`.

What name resolution does
- Final module-level bindings (defs and imports, last one wins) are used for
  calls, because a function body sees globals at call time.
- Class bases use the bindings in effect at the class statement.
- A bare name resolves if it is bound to a function defined in the parsed modules.
- `self.attr` / `cls.attr` resolve to a method of the enclosing class.
- A second pass (`tenthcheck.typed_calls`) resolves `obj.method()` when the receiver's class is known from
  annotations, constructors, `with ... as`, loops or `yield`, including methods inherited from parsed bases.
- `alias.attr` resolves if `alias` is bound to a parsed module and `attr` is a
  function in that module (one attribute only).
- An import path is attached to a parsed module when the path equals that
  module's name or ends with `.{name}`, preferring the longest unique match.
  Otherwise an external module entity is created with the import path as its id.
- Relative imports use the file's package (`__init__.py` is the package itself).

What it does not resolve (no edge, rather than a guessed one)
- Dynamic dispatch: `getattr`, `operator.methodcaller`, computed attributes,
  calls through arbitrary variables.
- `super()` calls in the first pass (the typed second pass resolves `super().method()`).
- Attribute chains longer than one dot (`pkg.mod.func`), except `self` / `cls`.
- Star imports (`from x import *`): the module edge is recorded, names are not bound.
- Imports and defs nested under `if` / `try` / loops. Only the module body is scanned.
- Builtins and any name whose defining module was not parsed.
- Calls of classes (constructors) as `CALLS`: `CALLS` is function-to-function; a constructor call is a `REFERENCES` edge to the class.
- Calls in the module body itself (`POOL = experiment_pool()`). There is no function entity on the source side of that call.
- Lambdas. A module-level lambda is an assignment, not a function.
- Flow-sensitive rebinding inside a function.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

from tenthlook.tenthcheck.graph import Graph

_SKIP_DIR_NAMES = {"__pycache__", "venv", ".venv", "site-packages"}


def iter_py_files(root: Path) -> list[Path]:
    """Sorted `.py` files under `root`, pruning skipped directories before descending into them."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py") and not name.startswith("."):
                out.append(Path(dirpath) / name)
    return sorted(out)


def parse_module(source: str, module_name: str) -> Graph:
    """Parse one module. `module_name` is the entity id of that module."""
    return _build([(module_name, source, False)])


def parse_package(
    root_dir: str, *, skip_syntax_errors: bool = False, overrides: dict[str, str | None] | None = None
) -> Graph:
    """Parse every `.py` file under `root_dir` into one graph.

    Module names are dotted paths relative to `root_dir` (`pkg/mod.py` -> `pkg.mod`,
    `pkg/__init__.py` -> `pkg`). Directories named `.venv`, `venv`, `site-packages`,
    `__pycache__`, or any path component starting with `.` are skipped. Cross-file
    `CALLS` / `INHERITS_FROM` / `IMPORTS` use the rules in the module docstring.

    `overrides` maps a path relative to `root_dir` (e.g. `"pkg/mod.py"`) to replacement source, parsed
    in memory instead of the file on disk; `None` drops the file. A path not on disk adds a new module.
    Skipped files (syntax errors with `skip_syntax_errors`) are listed in `graph.meta["skipped_syntax_files"]`.
    """
    root = Path(root_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root_dir)
    overrides = {Path(k).as_posix(): v for k, v in (overrides or {}).items()}
    sources: dict[str, str] = {}
    for path in iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        if rel not in overrides:
            sources[rel] = path.read_text(encoding="utf-8")
    for rel, source in overrides.items():
        if source is not None:
            sources[rel] = source
    specs: list[tuple[str, str, bool]] = []
    skipped_syntax: list[dict[str, str]] = []
    for rel in sorted(sources):
        source = sources[rel]
        module_name, is_package = module_name_for(rel, root.name)
        if skip_syntax_errors:
            try:
                ast.parse(source, filename=str(root / rel))
            except SyntaxError as error:
                skipped_syntax.append({"path": str(root / rel), "error": str(error)})
                continue
        specs.append((module_name, source, is_package))
    graph = _build(specs)
    if skipped_syntax:
        graph.meta["skipped_syntax_files"] = skipped_syntax
    return graph


def module_name_for(rel_path: str, root_name: str) -> tuple[str, bool]:
    """`("pkg/mod.py", root) -> ("pkg.mod", False)`; `("pkg/__init__.py", root) -> ("pkg", True)`."""
    parts = list(Path(rel_path).with_suffix("").parts)
    is_package = False
    if parts[-1] == "__init__":
        parts = parts[:-1]
        is_package = True
    return (".".join(parts) if parts else root_name), is_package


def _build(specs: list[tuple[str, str, bool]]) -> Graph:
    parsed: list[_Parsed] = []
    for module_name, source, is_package in specs:
        tree = ast.parse(source, filename=module_name)
        parsed.append(_Parsed(module_name, tree, is_package))

    g = Graph()
    for mod in parsed:
        _assign_entity_ids(mod)
        g.add(mod.name, "module", name=mod.name, lineno=1)
        _add_definitions(g, mod)

    known_modules = {mod.name for mod in parsed}
    for mod in parsed:
        _add_relations(g, mod, known_modules)
    add_typed_calls(g, parsed, known_modules, _match_known_module, _resolve_import_from, _iter_defs, _calls_in_body)
    add_references(g, parsed, known_modules, _match_known_module, _resolve_import_from, _iter_defs)
    return g


class _Parsed:
    def __init__(self, name: str, tree: ast.AST, is_package: bool):
        self.name = name
        self.tree = tree
        self.is_package = is_package
        self.entity_ids: dict[int, str] = {}
        self.final_ids: dict[str, str] = {}

    @property
    def package(self) -> str:
        if self.is_package:
            return self.name
        parent, _, _ = self.name.rpartition(".")
        return parent


def _ast_hash(node: ast.AST) -> str:
    dumped = ast.dump(node, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]


def _add_definitions(g: Graph, mod: _Parsed) -> None:
    for qual, node, _enclosing in _iter_defs(mod.tree.body, ""):
        eid = mod.entity_ids[id(node)]
        decorators = [_decorator_name(d) for d in node.decorator_list]
        h = _ast_hash(node)
        if isinstance(node, ast.ClassDef):
            g.add(eid, "class", name=node.name, lineno=node.lineno, decorators=decorators, ast_hash=h)
        else:
            g.add(
                eid,
                "function",
                name=node.name,
                lineno=node.lineno,
                n_args=_positional_count(node),
                decorators=decorators,
                ast_hash=h,
            )
        g.relate("DEFINES", mod.name, eid)
    claimed = set(g.entities)
    for node in mod.tree.body:
        for name, lineno in _assignment_names(node):
            eid = f"{mod.name}.{name}"
            if eid in claimed:
                eid = f"{eid}@L{lineno}"
                suffix = 2
                while eid in claimed:
                    eid = f"{mod.name}.{name}@L{lineno}#{suffix}"
                    suffix += 1
            g.add(eid, "module", name=name, lineno=lineno)
            claimed.add(eid)


def _is_overload_stub(node: ast.AST) -> bool:
    return any(_decorator_name(d).rsplit(".", 1)[-1] == "overload" for d in getattr(node, "decorator_list", []))


def _assign_entity_ids(mod: _Parsed) -> None:
    """Preserve repeated definitions with line-qualified ids; final binding wins calls.

    `@typing.overload` stubs never take the plain id: the implementation (the last non-overload definition of
    that name) does, so callers and callees attach to the code that actually runs. Stubs are numbered by order
    (`name@overload1`, ...) rather than by line, so edits elsewhere in the file don't rename them."""
    definitions = list(_iter_defs(mod.tree.body, ""))
    by_qual: dict[str, list] = {}
    for qual, node, _enclosing in definitions:
        by_qual.setdefault(qual, []).append(node)
    plain_owner: dict[str, int] = {}
    for qual, nodes in by_qual.items():
        real = [n for n in nodes if not _is_overload_stub(n)]
        plain_owner[qual] = id(real[-1]) if real and len(real) < len(nodes) else id(nodes[0])
    stub_count: dict[str, int] = {}
    for qual, node, _enclosing in definitions:
        eid = f"{mod.name}.{qual}"
        if id(node) != plain_owner[qual]:
            if _is_overload_stub(node):
                # numbered by order, not line, so inserting a line above doesn't make every stub look "changed"
                stub_count[qual] = stub_count.get(qual, 0) + 1
                eid += f"@overload{stub_count[qual]}"
            else:
                eid += f"@L{node.lineno}"
        mod.entity_ids[id(node)] = eid
        if not _is_overload_stub(node) or qual not in mod.final_ids:
            mod.final_ids[qual] = eid


def _iter_defs(body: list[ast.stmt], prefix: str, enclosing: str | None = None):
    """Yield (qualname, node, enclosing_class_qualname) for functions and classes."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{prefix}.{node.name}" if prefix else node.name
            yield qual, node, enclosing
            yield from _iter_defs(node.body, qual, enclosing)
        elif isinstance(node, ast.ClassDef):
            qual = f"{prefix}.{node.name}" if prefix else node.name
            yield qual, node, enclosing
            yield from _iter_defs(node.body, qual, qual)


def _add_relations(g: Graph, mod: _Parsed, known_modules: set[str]) -> None:
    running: dict[str, str] = {}
    for node in mod.tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _bind_import(g, mod, node, known_modules, running)
        elif isinstance(node, ast.ClassDef):
            class_id = mod.entity_ids[id(node)]
            for base in node.bases:
                target = _resolve_class(base, running, g)
                if target is not None:
                    g.relate("INHERITS_FROM", class_id, target)
            running[node.name] = class_id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            running[node.name] = mod.entity_ids[id(node)]
    final = dict(running)
    for qual, node, enclosing in _iter_defs(mod.tree.body, ""):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src = mod.entity_ids[id(node)]
        for func in _calls_in_body(node.body):
            dst = _resolve_function(func, final, g, enclosing, mod.final_ids)
            if dst is not None:
                g.relate("CALLS", src, dst)
    for qual, node, _enclosing in _iter_defs(mod.tree.body, ""):
        if isinstance(node, ast.ClassDef) and "." in qual:
            class_id = mod.entity_ids[id(node)]
            for base in node.bases:
                target = _resolve_class(base, final, g)
                if target is not None:
                    g.relate("INHERITS_FROM", class_id, target)


def _bind_import(g: Graph, mod: _Parsed, node: ast.AST, known_modules: set[str], running: dict[str, str]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            mid = _module_entity(g, alias.name, known_modules, node.lineno)
            g.relate("IMPORTS", mod.name, mid)
            local = alias.asname or alias.name.split(".", 1)[0]
            running[local] = mid
        return
    if not isinstance(node, ast.ImportFrom):
        return
    target_name = _resolve_import_from(mod, node)
    if target_name is None:
        return
    mid = _module_entity(g, target_name, known_modules, node.lineno)
    if mid != mod.name:  # `from . import sub` inside pkg/__init__.py is not pkg importing itself
        g.relate("IMPORTS", mod.name, mid)
    if len(node.names) == 1 and node.names[0].name == "*":
        return
    for alias in node.names:
        local = alias.asname or alias.name
        candidate = f"{mid}.{alias.name}"
        # `from pkg import sub` also imports the submodule; pkg may be a namespace package with no __init__.
        sub = candidate if candidate in known_modules else _match_known_module(f"{target_name}.{alias.name}", known_modules)
        if sub is not None:
            g.relate("IMPORTS", mod.name, sub)
            candidate = sub
        ent = g.entities.get(candidate)
        if ent is not None and ent.type in {"function", "class", "module"}:
            running[local] = candidate


def _resolve_import_from(mod: _Parsed, node: ast.ImportFrom) -> str | None:
    if node.level:
        parts = mod.package.split(".") if mod.package else []
        up = node.level - 1
        if up > len(parts):
            return None
        base = parts[: len(parts) - up] if up else list(parts)
        if node.module:
            base.extend(node.module.split("."))
        return ".".join(base) if base else None
    return node.module


def _module_entity(g: Graph, import_path: str, known_modules: set[str], lineno: int) -> str:
    matched = _match_known_module(import_path, known_modules)
    mid = matched if matched is not None else import_path
    if mid not in g.entities:
        g.add(mid, "module", name=mid, lineno=lineno)
    return mid


def _match_known_module(import_path: str, known_modules: set[str]) -> str | None:
    """Longest parsed module name that equals `import_path` or is a dotted suffix of it.

    Checks the dotted suffixes of `import_path` against the set (O(depth), not O(modules)). Two distinct
    suffixes of one path never have the same length, so the longest match is always unique.
    """
    parts = import_path.split(".")
    for i in range(len(parts)):
        name = ".".join(parts[i:])
        if name in known_modules:
            return name
    return None


def _resolve_class(expr: ast.expr, bindings: dict[str, str], g: Graph) -> str | None:
    while isinstance(expr, ast.Subscript):
        expr = expr.value
    eid = _bound_attr(expr, bindings)
    ent = g.entities.get(eid) if eid else None
    if ent is not None and ent.type == "class":
        return eid
    return None


def _resolve_function(
    func: ast.expr,
    bindings: dict[str, str],
    g: Graph,
    enclosing_class: str | None,
    final_ids: dict[str, str],
) -> str | None:
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in {"self", "cls"}
        and enclosing_class is not None
    ):
        eid = final_ids.get(f"{enclosing_class}.{func.attr}")
        ent = g.entities.get(eid)
        if ent is not None and ent.type == "function":
            return eid
        return None
    eid = _bound_attr(func, bindings)
    ent = g.entities.get(eid) if eid else None
    if ent is not None and ent.type == "function":
        return eid
    return None


def _bound_attr(expr: ast.expr, bindings: dict[str, str]) -> str | None:
    """Resolve `name` or `name.attr` (exactly one attribute) through bindings."""
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        head = bindings.get(expr.value.id)
        if head is None:
            return None
        return f"{head}.{expr.attr}"
    return None


def _calls_in_body(body: list[ast.stmt]) -> list[ast.expr]:
    found: list[ast.expr] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            return

        def visit_AsyncFunctionDef(self, node):
            return

        def visit_ClassDef(self, node):
            return

        def visit_Call(self, node):
            found.append(node.func)
            self.generic_visit(node)

    visitor = V()
    for stmt in body:
        visitor.visit(stmt)
    return found


def _assignment_names(node: ast.stmt) -> list[tuple[str, int]]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign) and node.target is not None:
        targets = [node.target]
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    else:
        return []
    out = []
    for target in targets:
        for name in _name_targets(target):
            out.append((name, node.lineno))
    return out


def _name_targets(target: ast.expr):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _name_targets(elt)
    elif isinstance(target, ast.Starred):
        yield from _name_targets(target.value)


def _positional_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return len(node.args.args) + len(node.args.posonlyargs)


def _decorator_name(dec: ast.expr) -> str:
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        parts = []
        cur: ast.expr = dec
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return "<unknown>"


from tenthlook.tenthcheck.typed_calls import add_typed_calls  # noqa: E402  (second pass; see that module)
from tenthlook.tenthcheck.references import add_references  # noqa: E402  (third pass; see that module)

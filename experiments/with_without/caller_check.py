"""Did the agent's final change miss any caller? A static check per task, independent of tests and of Tenthcheck.

    python caller_check.py <task_id> <pristine checkout> <agent.diff>      # applies the diff to a temp copy

For each task, the call sites (or uses) that the change must update are found in the PRISTINE tree with plain `ast`
(no Tenthcheck code, so the check can't favour the with arm), keyed by the enclosing function (`module:qualname`).
The same functions are then inspected in the AFTER tree. A site is:
  - "updated": the after-tree call has the new form;
  - "missed": the after-tree still has the old form;
  - "gone": the enclosing function no longer has a matching call (removed or restructured; read by hand).
Scope: rich/ only (tests/ is never edited). Uses are matched by the textual receiver the task names
(`Measurement.get`, `.render_lines`, `loop_last`), which is exact for these tasks because every pristine call site
uses that form; `Text.split` callers are a frozen hand-read list (grep can't tell them from `str.split`).
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _funcs(root: Path):
    """(module:qualname, function node) for every def in rich/, nested included; module-level code as '<module>'."""
    for f in sorted((root / "rich").rglob("*.py")):
        mod = ".".join(f.relative_to(root).with_suffix("").parts)
        tree = ast.parse(f.read_text(encoding="utf-8"))
        yield f"{mod}:<module>", tree

        def walk(body, prefix):
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = f"{prefix}.{n.name}" if prefix else n.name
                    if not isinstance(n, ast.ClassDef):
                        yield f"{mod}:{q}", n
                    yield from walk(n.body, q)
        yield from walk(tree.body, "")


def _own_calls(node):
    """Calls in a function's own body (not nested defs); for a module, calls outside any def."""
    out = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            if n is not node:
                return
            self.generic_visit(n)
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, n):
            if isinstance(node, ast.Module):
                return
            self.generic_visit(n)

        def visit_Call(self, n):
            out.append(n)
            self.generic_visit(n)
    V().visit(node)
    return out


def _is(call: ast.Call, dotted: str) -> bool:
    """call.func matches `Measurement.get` / `.render_lines` (any receiver) / `loop_last` (bare or attr)."""
    f = call.func
    if dotted.startswith("."):
        return isinstance(f, ast.Attribute) and f.attr == dotted[1:]
    if "." in dotted:
        head, attr = dotted.rsplit(".", 1)
        return isinstance(f, ast.Attribute) and f.attr == attr and isinstance(f.value, ast.Name) and f.value.id == head
    return (isinstance(f, ast.Name) and f.id == dotted) or (isinstance(f, ast.Attribute) and f.attr == dotted)


def _dump(e) -> str:
    return ast.dump(e, include_attributes=False)


# ---------------------------------------------------------------- per-task rules: (selector, old_form, new_form)

def _aliases(fn, dotted: str) -> set[str]:
    """Local names bound to `dotted` in this function (`measure = Measurement.get`)."""
    head, attr = dotted.rsplit(".", 1)
    out = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Attribute) and n.value.attr == attr
                and isinstance(n.value.value, ast.Name) and n.value.value.id == head):
            out.add(n.targets[0].id)
    return out


def _get_calls(fn) -> list[ast.Call]:
    """Calls of Measurement.get, directly or through a local alias."""
    names = _aliases(fn, "Measurement.get")
    return [c for c in _own_calls(fn) if _is(c, "Measurement.get")
            or (isinstance(c.func, ast.Name) and c.func.id in names)]


def h02_sites(before: Path, after: Path) -> list[dict]:
    """Measurement.get(console, options, renderable) -> get(renderable, console, options); aliased calls count."""
    old = {}
    for key, fn in _funcs(before):
        for c in _get_calls(fn):
            if len(c.args) == 3:
                old.setdefault(key, []).append([_dump(a) for a in c.args])
    funcs = dict(_funcs(after))
    rows = []
    for key, sites in sorted(old.items()):
        calls = [[_dump(x) for x in c.args] for c in _get_calls(funcs[key])] if key in funcs else []
        for i, a in enumerate(sites):
            status = ("updated" if [a[2], a[0], a[1]] in calls else "missed" if a in calls else "gone")
            rows.append({"site": key, "n": i, "status": status})
    return rows


def h03_sites(before: Path, after: Path) -> list[dict]:
    """render_lines(x, options, ...) or render_lines(x) -> render_lines(x, options=...)."""
    old = {}
    for key, fn in _funcs(before):
        for c in _own_calls(fn):
            if _is(c, ".render_lines") and not key.startswith("rich.console:Console.render_lines"):
                old.setdefault(key, []).append(None)

    def new(c, _):
        return len(c.args) == 1 and any(k.arg == "options" for k in c.keywords)
    return _compare(after, old, ".render_lines", new, lambda c, _: not new(c, _))


def t08_sites(before: Path, after: Path) -> list[dict]:
    """loop_last -> iter_last (alias kept in rich/_loop.py)."""
    old = {}
    for key, fn in _funcs(before):
        if key.startswith("rich._loop:"):
            continue
        for c in _own_calls(fn):
            if _is(c, "loop_last"):
                old.setdefault(key, []).append(None)
    return _compare(after, old, None, None, None, rename=("loop_last", "iter_last"))


def _compare(after, old, dotted, is_new, is_old, rename=None) -> list[dict]:
    funcs = dict(_funcs(after))
    rows = []
    for key, sites in sorted(old.items()):
        fn = funcs.get(key)
        calls = _own_calls(fn) if fn is not None else []
        for i, a in enumerate(sites):
            if rename:
                olds = [c for c in calls if _is(c, rename[0])]
                news = [c for c in calls if _is(c, rename[1])]
                status = "missed" if len(olds) > i else ("updated" if len(news) > i else "gone")
            else:
                cands = [c for c in calls if _is(c, dotted)]
                if any(is_new(c, a) for c in cands):
                    status = "updated"
                elif any(is_old(c, a) for c in cands):
                    status = "missed"
                else:
                    status = "gone"
            rows.append({"site": key, "n": i, "status": status})
    return rows


def _imports_of(tree: ast.Module, names: set[str]) -> dict[str, str]:
    """name -> module it is imported from (module level, TYPE_CHECKING blocks included)."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module is not None:
            src = ("." * n.level) + n.module
            for a in n.names:
                if a.name in names:
                    out[a.name] = src
    return out


def t06_sites(before: Path, after: Path) -> list[dict]:
    """Imports of ConsoleOptions / ConsoleDimensions inside rich (not console.py) must come from console_options.
    One site per (module, name); 'gone' = the module no longer imports that name at all."""
    names = {"ConsoleOptions", "ConsoleDimensions"}
    rows = []
    for f in sorted((before / "rich").rglob("*.py")):
        rel = f.relative_to(before)
        if rel.as_posix() in ("rich/console.py", "rich/console_options.py"):
            continue
        old = _imports_of(ast.parse(f.read_text(encoding="utf-8")), names)
        if not old:
            continue
        g = after / rel
        new = _imports_of(ast.parse(g.read_text(encoding="utf-8")), names) if g.exists() else {}
        for name in sorted(old):
            src = new.get(name)
            status = ("updated" if src and src.endswith("console_options") else
                      "missed" if src and src.endswith("console") else "gone")
            rows.append({"site": f"{rel.with_suffix('').as_posix().replace('/', '.')}:{name}", "n": 0, "status": status})
    return rows


RULES = {"t06_console_options_module": t06_sites, "h02_measurement_get_order": h02_sites, "h03_render_lines_options_kwonly": h03_sites,
         "t08_rename_loop_last": t08_sites}


def check(task: str, pristine: Path, diff: Path) -> dict:
    pristine, diff = Path(pristine).expanduser().resolve(), Path(diff).resolve()  # git apply runs in a temp dir
    with tempfile.TemporaryDirectory() as tmp:
        after = Path(tmp) / "after"
        shutil.copytree(pristine / "rich", after / "rich")
        if diff.read_text().strip():
            subprocess.run(["git", "apply", "--include=rich/*", str(diff)], cwd=after, check=True)
            changed = subprocess.run(["diff", "-rq", str(pristine / "rich"), str(after / "rich")],
                                     capture_output=True, text=True).stdout
            if "differ" not in changed and "Only in" not in changed:
                raise SystemExit("the diff changed nothing under rich/: not applied?")
        rows = RULES[task](pristine, after)
    count = {s: sum(r["status"] == s for r in rows) for s in ("updated", "missed", "gone")}
    return {"task": task, "sites": len(rows), **count, "rows": rows}


if __name__ == "__main__":
    res = check(sys.argv[1], Path(sys.argv[2]).expanduser().resolve(), Path(sys.argv[3]).resolve())
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}))
    for r in res["rows"]:
        if r["status"] != "updated":
            print("  ", r["status"], r["site"])

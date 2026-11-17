"""Independent check of Tenthcheck's IMPORTS edges: record real module-level imports at runtime.

    <venv-with-package>/bin/python scripts/trace_imports.py <package> <out.json>

Wraps `builtins.__import__`, imports every module of <package> (pkgutil.walk_packages), and records
an edge A -> B whenever module A's top-level code (frame name `<module>`) imports B, including
`from pkg import sub` when `pkg.sub` is a module. Conditional imports that run (try/if) are seen;
imports inside functions and under `if TYPE_CHECKING:` are not, which matches what Tenthcheck is
meant to report. Only edges inside <package> are kept. `__main__` modules are skipped (importing one runs the CLI).

This script needs only the stdlib; it must run in an environment where <package> is importable.
"""
from __future__ import annotations

import builtins
import importlib
import importlib.util
import json
import pkgutil
import sys

pkg_name, out_path = sys.argv[1], sys.argv[2]
edges: set[tuple[str, str]] = set()
errors: list[str] = []
_orig = builtins.__import__


def _hook(name, globals=None, locals=None, fromlist=(), level=0):
    mod = _orig(name, globals, locals, fromlist, level)
    try:
        frame = sys._getframe(1)
        if frame.f_code.co_name == "<module>" and globals is not None:
            src = globals.get("__name__", "")
            if level:
                package = globals.get("__package__") or src.rpartition(".")[0]
                target = importlib.util.resolve_name("." * level + name, package) if name else \
                    importlib.util.resolve_name("." * level, package).rstrip(".")
            else:
                target = name
            targets = {target}
            for item in fromlist or ():
                if f"{target}.{item}" in sys.modules:
                    targets.add(f"{target}.{item}")
            for t in targets:
                if src != t and src.split(".")[0] == pkg_name and t.split(".")[0] == pkg_name:
                    edges.add((src, t))
    except Exception as e:  # tracing must never break the import
        errors.append(repr(e))
    return mod


builtins.__import__ = _hook
pkg = importlib.import_module(pkg_name)
modules = [pkg_name]
for info in pkgutil.walk_packages(pkg.__path__, pkg_name + "."):
    if info.name.endswith(".__main__"):
        continue  # executing __main__ runs the package's CLI
    modules.append(info.name)
    try:
        importlib.import_module(info.name)
    except Exception as e:
        errors.append(f"{info.name}: {type(e).__name__}: {e}")
builtins.__import__ = _orig
# pkgutil does not descend into namespace subpackages (no __init__.py); count every module that got loaded.
modules = sorted({m for m in sys.modules if (m == pkg_name or m.startswith(pkg_name + ".")) and not m.endswith(".__main__")}
                 | set(modules))
json.dump({"package": pkg_name, "modules": sorted(modules), "edges": sorted(edges), "errors": errors},
          open(out_path, "w"), indent=1)
print(pkg_name, len(modules), "modules", len(edges), "edges", len(errors), "errors")

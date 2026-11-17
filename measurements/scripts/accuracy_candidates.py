"""Grep-based candidate callers for hand-labelling Tenthcheck's `blast` (hop 1) against the source.

    python scripts/accuracy_candidates.py <package_dir> <module_prefix> <symbol_id> [<symbol_id> ...]

For each symbol, finds every textual call `name(` (also `.name(`) in <package_dir>, maps each hit to its
enclosing function/method (by AST line ranges), and prints them next to Tenthcheck's hop-1 dependents.
The human label (is this hit really a call of *this* symbol?) is recorded separately in
results/accuracy_httpx.json; this script only collects candidates so none are skipped.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from tenthcheck.parser import iter_py_files, module_name_for, parse_package
from tenthcheck.topology import dependents_radius


def enclosing_defs(tree: ast.AST, module: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                if not isinstance(node, ast.ClassDef):
                    spans.append((node.lineno, node.end_lineno or node.lineno, f"{module}.{qual}"))
                walk(node.body, qual)

    walk(tree.body, "")
    return spans


def candidates(pkg_dir: Path, prefix: str, name: str) -> list[dict]:
    pat = re.compile(rf"(?<![\w]){re.escape(name)}\s*\(")
    out = []
    for path in iter_py_files(pkg_dir):
        rel = path.relative_to(pkg_dir).as_posix()
        module, _ = module_name_for(rel, pkg_dir.name)
        module = f"{prefix}.{module}" if module != pkg_dir.name else prefix
        src = path.read_text(encoding="utf-8")
        spans = enclosing_defs(ast.parse(src), module)
        for i, line in enumerate(src.splitlines(), 1):
            if pat.search(line) and not line.lstrip().startswith(("def ", "async def ", "#")):
                inner = [s for s in spans if s[0] <= i <= s[1]]
                where = max(inner, key=lambda s: s[0])[2] if inner else f"{module}.<module>"
                out.append({"file": rel, "line": i, "in": where, "text": line.strip()[:120]})
    return out


def main() -> None:
    pkg_dir, prefix, symbols = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3:]
    g = parse_package(str(pkg_dir.parent), skip_syntax_errors=True)
    report = {}
    for sym in symbols:
        rad = dependents_radius(g, sym, max_depth=1)
        hop1 = [d for d in rad["by_depth"].get(1, []) if d.startswith(prefix + ".")]
        report[sym] = {"tenthcheck_hop1": hop1, "grep_candidates": candidates(pkg_dir, prefix, sym.rsplit(".", 1)[-1])}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

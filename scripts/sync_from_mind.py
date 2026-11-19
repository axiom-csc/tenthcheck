"""Copy Tenthcheck's core modules from an axiom-lab checkout's `mind/` into `src/tenthcheck/`.

`mind/` (in the private axiom-lab repo) is the source of truth for the parser, graph and topology code. This
rewrites `mind.*` imports to `tenthcheck.*` and makes numpy optional in `graph.py` (the code parser never stores
numpy values; numpy is only used there for isinstance checks).

    python scripts/sync_from_mind.py ~/axiom-lab-worktrees/<checkout>          # write
    python scripts/sync_from_mind.py ~/axiom-lab-worktrees/<checkout> --check  # exit 1 if out of date

Restored 2026-11-18 from axiom-lab (it was removed when Tenthcheck moved to its own repo); `references.py` added.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1]
DEST = PRODUCT / "src" / "tenthcheck"
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not _args:
    raise SystemExit("usage: sync_from_mind.py <axiom-lab checkout> [--check]")
REPO = Path(_args[0]).expanduser().resolve()

FILES = {  # source (relative to repo) -> vendored name
    "mind/core/graph.py": "graph.py",
    "mind/code/parser.py": "parser.py",
    "mind/code/topology.py": "topology.py",
    "mind/code/scope_audit.py": "scope_audit.py",
    "mind/code/cycles.py": "cycles.py",
    "mind/code/check_edit.py": "check_edit.py",
    "mind/code/tool_server.py": "index.py",
    "mind/code/typed_calls.py": "typed_calls.py",
    "mind/code/references.py": "references.py",
}
MODULE_MAP = {
    "mind.core.graph": "tenthcheck.graph",
    "mind.code.tool_server": "tenthcheck.index",
    "mind.code.parser": "tenthcheck.parser",
    "mind.code.topology": "tenthcheck.topology",
    "mind.code.scope_audit": "tenthcheck.scope_audit",
    "mind.code.cycles": "tenthcheck.cycles",
    "mind.code.check_edit": "tenthcheck.check_edit",
    "mind.code.typed_calls": "tenthcheck.typed_calls",
    "mind.code.references": "tenthcheck.references",
}
NUMPY_SHIM = '''try:
    import numpy as np
except ImportError:  # tenthcheck: numpy is optional; parsed code graphs never hold numpy values
    class _Never:
        pass

    class np:  # noqa: N801 - stands in for the numpy module in isinstance checks only
        bool_ = integer = floating = ndarray = _Never
'''


def transform(src_rel: str, text: str) -> str:
    for old, new in MODULE_MAP.items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    if src_rel == "mind/core/graph.py":
        if "import numpy as np\n" not in text:
            raise SystemExit("graph.py no longer has 'import numpy as np'; update the shim")
        text = text.replace("import numpy as np\n", NUMPY_SHIM, 1)
    if re.search(r"^\s*(from|import) mind\b", text, re.M):
        raise SystemExit(f"{src_rel}: unmapped mind import left after rewrite")
    header = f"# Vendored from axiom-lab {src_rel} by scripts/sync_from_mind.py. Edit the source, then re-sync.\n"
    return header + text


def expected() -> dict[Path, str]:
    return {DEST / dst: transform(src, (REPO / src).read_text(encoding="utf-8")) for src, dst in FILES.items()}


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for path, text in expected().items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(path.name)
            if not check:
                path.write_text(text, encoding="utf-8")
    if check and stale:
        print("out of date:", ", ".join(stale))
        return 1
    print(("checked" if check else "synced"), len(FILES), "files", ("(updated: " + ", ".join(stale) + ")") if stale and not check else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

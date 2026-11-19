"""MCP stdio server (official `mcp` Python SDK, 2.x `MCPServer`).

One server is bound to one repository root. The parsed graph is cached by file mtime
(`tenthcheck.index.IncrementalCodeIndex`), so repeated calls re-parse only changed files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tenthcheck.check_edit import check_edit
from tenthcheck.cycles import import_cycles
from tenthcheck.index import IncrementalCodeIndex
from tenthcheck.resolve import resolve_symbol
from tenthcheck.scope_audit import audit_scope
from tenthcheck.topology import dependents_radius

MAX_NODES = 10_000

INSTRUCTIONS = (
    "Structural checks for Python code in one repository, from module-level imports, statically "
    "resolved calls, subclassing, and uses of classes (annotations, constructors, isinstance, imports). Dynamic dispatch, getattr, super() and calls through variables are not seen, "
    "so an empty result means 'no resolved dependents', not 'safe'. Nothing here runs code or judges correctness."
)


def build_server(repo_dir: str | Path):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    def lookup(g, symbol: str) -> str:
        try:
            return resolve_symbol(g, symbol)
        except LookupError as e:  # KeyError and AmbiguousSymbol: tell the agent, don't crash the call
            raise ToolError(str(e).strip('"')) from None

    root = Path(repo_dir).resolve()
    index = IncrementalCodeIndex(root)
    server = MCPServer(name="tenthcheck", instructions=INSTRUCTIONS)

    @server.tool()
    def check_blast_radius(symbol: str, max_depth: int | None = None) -> dict[str, Any]:
        """Entities that call, import, subclass or use `symbol` (transitively), grouped by hop depth. For a class,
        "use" includes annotations, constructor calls, isinstance checks and imports of it.

        `symbol` is a dotted id like `pkg.mod.func` or a unique suffix like `Class.method`.
        """
        g = index.graph
        eid = lookup(g, symbol)
        rad = dependents_radius(g, eid, max_depth=max_depth, max_nodes=MAX_NODES)
        return {"symbol": eid, "type": g[eid].type, "affected": rad["affected"], "truncated": rad["truncated"],
                "by_depth": {str(k): v for k, v in rad["by_depth"].items()}}

    @server.tool()
    def find_import_cycles() -> dict[str, Any]:
        """Import cycles among the repository's modules (module-level imports only), one shortest path per cycle group."""
        found = import_cycles(index.graph)
        return {"n_cyclic_components": len(found), "components": found}

    @server.tool()
    def check_proposed_edit(file_path: str, new_source: str) -> dict[str, Any]:
        """Before writing `new_source` to `file_path` (relative to the repo), report NEW import cycles it would
        create and the dependents of every function/class it changes. `ok` is false if a new cycle appears.
        Nothing is written to disk."""
        try:
            res = check_edit(root, file_path, new_source, max_nodes=MAX_NODES, index=index)
        except ValueError as e:
            raise ToolError(str(e)) from None
        if res.get("changed"):
            for rad in res["changed"]["blast_radii"].values():
                rad["by_depth"] = {str(k): v for k, v in rad["by_depth"].items()}
        return res

    @server.tool()
    def scope_audit(before_dir: str, after_dir: str) -> dict[str, Any]:
        """Compare two snapshots of a codebase: changed functions/classes and the untouched code that depends on them."""
        try:
            res = audit_scope(before_dir, after_dir, max_nodes=MAX_NODES)
        except NotADirectoryError as e:
            raise ToolError(f"not a directory: {e}") from None
        for rad in res["blast_radii"].values():
            rad["by_depth"] = {str(k): v for k, v in rad["by_depth"].items()}
        return res

    return server


def serve(repo_dir: str | Path) -> None:
    build_server(repo_dir).run("stdio")

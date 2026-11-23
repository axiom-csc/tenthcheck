# Vendored from axiom-lab mind/code/tool_server.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Code-structure tool server with incremental caching.

Exposes exact code graph queries (blast_radius, call_graph_neighbors) to
agents and LLMs without requiring full re-parsing on every request.
Supports Python API, CLI, and stdio MCP server for direct Cursor/Claude Code integration.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from tenthlook.tenthcheck.parser import (
    _Parsed, _assign_entity_ids, _add_definitions, _add_relations, _SKIP_DIR_NAMES, iter_py_files, module_name_for,
    _match_known_module, _resolve_import_from, _iter_defs, _calls_in_body,
)
from tenthlook.tenthcheck.references import add_references
from tenthlook.tenthcheck.typed_calls import add_typed_calls
from tenthlook.tenthcheck.topology import blast_radius as compute_blast_radius
from tenthlook.tenthcheck.graph import Graph


class CachedModule:
    __slots__ = ("path", "module_name", "is_package", "mtime", "tree")

    def __init__(self, path: Path, module_name: str, is_package: bool, mtime: float, tree: ast.AST):
        self.path = path
        self.module_name = module_name
        self.is_package = is_package
        self.mtime = mtime
        self.tree = tree


class IncrementalCodeIndex:
    """Maintains a parsed Graph of a Python package, incrementally re-parsing changed files."""

    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(str(self.root))
        self._modules: dict[str, CachedModule] = {}
        self._graph: Graph | None = None
        self._last_refresh_duration_ms: float = 0.0
        self.refresh()

    def refresh(self) -> bool:
        """Scan directory and re-parse ONLY files whose mtime or existence changed.

        Returns True if changes were detected and graph was rebuilt, False otherwise.
        """
        t0 = time.perf_counter()
        current_py_files: dict[str, Path] = {}

        for path in iter_py_files(self.root):
            parts = list(path.relative_to(self.root).with_suffix("").parts)
            is_pkg = False
            if parts[-1] == "__init__":
                parts = parts[:-1]
                is_pkg = True
            mod_name = ".".join(parts) if parts else self.root.name
            current_py_files[mod_name] = path

        changed = False

        # Check for removed modules
        removed_mods = set(self._modules) - set(current_py_files)
        if removed_mods:
            for mod_name in removed_mods:
                del self._modules[mod_name]
            changed = True

        # Check for added or modified files
        for mod_name, path in current_py_files.items():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue

            cached = self._modules.get(mod_name)
            if cached is None or cached.mtime != mtime:
                try:
                    source = path.read_text(encoding="utf-8")
                    tree = ast.parse(source, filename=str(path))
                    is_pkg = (path.stem == "__init__")
                    self._modules[mod_name] = CachedModule(
                        path=path,
                        module_name=mod_name,
                        is_package=is_pkg,
                        mtime=mtime,
                        tree=tree,
                    )
                    changed = True
                except (SyntaxError, UnicodeDecodeError, OSError):
                    # Skip unparseable files without breaking the index
                    if cached is not None:
                        del self._modules[mod_name]
                        changed = True

        if changed or self._graph is None:
            self._rebuild_graph()

        self._last_refresh_duration_ms = (time.perf_counter() - t0) * 1000
        return changed

    def _rebuild_graph(self) -> None:
        """Assemble the Graph from cached ASTs without any disk I/O."""
        self._graph = _graph_from((m.module_name, m.tree, m.is_package) for m in self._modules.values())

    def graph_with_overrides(self, overrides: dict[str, str | None]) -> Graph:
        """The current graph with some files replaced in memory (relative path -> source; None drops it).

        Cached ASTs are reused for every other file; nothing is written to disk. A syntax error in an
        override raises SyntaxError.
        """
        self.refresh()
        trees = {m.module_name: (m.tree, m.is_package) for m in self._modules.values()}
        for rel, source in overrides.items():
            name, is_pkg = module_name_for(Path(rel).as_posix(), self.root.name)
            if source is None:
                trees.pop(name, None)
            else:
                trees[name] = (ast.parse(source, filename=rel), is_pkg)
        return _graph_from((name, tree, is_pkg) for name, (tree, is_pkg) in trees.items())

    @property
    def graph(self) -> Graph:
        self.refresh()
        assert self._graph is not None
        return self._graph

    def blast_radius(self, entity_id: str, max_depth: int | None = None) -> dict[str, Any]:
        """Compute the blast radius for entity_id using the up-to-date cached graph."""
        g = self.graph
        if entity_id not in g.entities:
            raise KeyError(f"Entity not found in index: {entity_id}")
        return compute_blast_radius(g, entity_id, max_depth=max_depth)

    def call_graph_neighbors(self, entity_id: str) -> dict[str, Any]:
        """Return callers and callees for entity_id."""
        g = self.graph
        if entity_id not in g.entities:
            raise KeyError(f"Entity not found in index: {entity_id}")
        calls = sorted([r.dst for r in g.rels("CALLS", src=entity_id)])
        called_by = sorted([r.src for r in g.rels("CALLS", dst=entity_id)])
        return {
            "entity_id": entity_id,
            "type": g[entity_id].type,
            "calls": calls,
            "called_by": called_by,
        }


def _graph_from(modules) -> Graph:
    """Build a Graph from `(module_name, ast_tree, is_package)` triples (same steps as `parser._build`)."""
    parsed_list = [_Parsed(name, tree, is_pkg) for name, tree, is_pkg in sorted(modules, key=lambda m: m[0])]
    g = Graph()
    for mod in parsed_list:
        _assign_entity_ids(mod)
        g.add(mod.name, "module", name=mod.name, lineno=1)
        _add_definitions(g, mod)
    known_modules = {mod.name for mod in parsed_list}
    for mod in parsed_list:
        _add_relations(g, mod, known_modules)
    add_typed_calls(g, parsed_list, known_modules, _match_known_module, _resolve_import_from, _iter_defs, _calls_in_body)
    add_references(g, parsed_list, known_modules, _match_known_module, _resolve_import_from, _iter_defs)
    return g


def run_stdio_mcp_server(root_dir: str | Path):
    """Run a standard stdio JSON-RPC MCP server for Cursor / Claude Code."""
    index = IncrementalCodeIndex(root_dir)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mind-code-tool-server", "version": "0.1.0"},
                },
            }
        elif method == "tools/list":
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "blast_radius",
                            "description": "Compute blast radius (outbound dependency closure) for a code entity.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "entity_id": {"type": "string", "description": "e.g. 'tenthcheck.topology.blast_radius'"},
                                    "max_depth": {"type": "integer", "description": "Maximum BFS depth (optional)"},
                                },
                                "required": ["entity_id"],
                            },
                        },
                        {
                            "name": "call_graph_neighbors",
                            "description": "Return incoming callers and outgoing calls for a code entity.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "entity_id": {"type": "string", "description": "e.g. 'tenthcheck.topology.blast_radius'"},
                                },
                                "required": ["entity_id"],
                            },
                        },
                    ]
                },
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})
            try:
                if tool_name == "blast_radius":
                    output = index.blast_radius(args["entity_id"], max_depth=args.get("max_depth"))
                elif tool_name == "call_graph_neighbors":
                    output = index.call_graph_neighbors(args["entity_id"])
                else:
                    output = {"error": f"Unknown tool: {tool_name}"}
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(output, indent=2)}]},
                }
            except Exception as e:
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True},
                }
        else:
            res = {"jsonrpc": "2.0", "id": req_id, "result": {}}

        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Query code structure and blast radius with incremental caching.")
    parser.add_argument("--repo", default=".", help="Root path of repository (default: current directory)")
    subparsers = parser.add_subparsers(dest="command")

    blast_p = subparsers.add_parser("blast_radius", help="Query blast radius of an entity.")
    blast_p.add_argument("entity_id", help="Dotted entity id, e.g. 'tenthcheck.topology.blast_radius'")
    blast_p.add_argument("--max-depth", type=int, default=None, help="Maximum search depth")

    calls_p = subparsers.add_parser("call_graph_neighbors", help="Query call graph neighbors.")
    calls_p.add_argument("entity_id", help="Dotted entity id, e.g. 'tenthcheck.topology.blast_radius'")

    subparsers.add_parser("mcp", help="Start stdio MCP server for Cursor / Claude Code.")

    args = parser.parse_args()

    if args.command == "mcp":
        run_stdio_mcp_server(args.repo)
        return

    index = IncrementalCodeIndex(args.repo)

    if args.command == "blast_radius":
        res = index.blast_radius(args.entity_id, max_depth=args.max_depth)
        print(json.dumps(res, indent=2))
    elif args.command == "call_graph_neighbors":
        res = index.call_graph_neighbors(args.entity_id)
        print(json.dumps(res, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

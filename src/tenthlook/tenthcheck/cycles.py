# Vendored from axiom-lab mind/code/cycles.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Import cycles from the parser's `IMPORTS` edges (module -> module).

Only module-level imports are edges (see `tenthcheck.parser`): imports inside functions,
under `if TYPE_CHECKING:` or in `try` blocks are not scanned, so the usual ways of
deliberately breaking a cycle are not reported. External modules have no outgoing
edges and cannot be on a cycle.

`from pkg import name` records an edge to `pkg` (the package `__init__`), even when
`name` is a submodule. A cycle through a package `__init__` is common and often harmless
(e.g. `pkg/__init__.py` re-exports from `pkg.mod`, and `pkg.mod` imports from `pkg`).
This module reports cycles; it does not judge whether one is a bug.

Strongly connected components are found with an iterative Tarjan. For each component one
shortest cycle is reported as a representative; a component with many modules usually
contains many more elementary cycles than that.
"""
from __future__ import annotations

from collections import deque

from tenthlook.tenthcheck.graph import Graph


def import_adjacency(g: Graph) -> dict[str, list[str]]:
    """`{module: [imported parsed modules]}`, self-edges included, sorted."""
    adj: dict[str, set[str]] = {}
    for rel in g.rels("IMPORTS"):
        adj.setdefault(rel.src, set()).add(rel.dst)
        adj.setdefault(rel.dst, set())
    return {k: sorted(v) for k, v in sorted(adj.items())}


def strongly_connected(adj: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCCs (iterative), each sorted; only components that contain a cycle."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in adj:
        if root in index:
            continue
        work = [(root, iter(adj[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in adj.get(node, ()):
                    out.append(sorted(comp))
    return sorted(out)


def shortest_cycle_through(adj: dict[str, list[str]], src: str, dst: str) -> list[str] | None:
    """Shortest path dst -> ... -> src, so that src -> dst closes a cycle. Returns [src, dst, ..., src]."""
    if src == dst:
        return [src, src] if src in adj.get(src, ()) else None
    prev: dict[str, str | None] = {dst: None}
    queue = deque([dst])
    while queue:
        node = queue.popleft()
        if node == src:
            path = [node]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])
            return [src] + path[::-1]
        for nxt in adj.get(node, ()):
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    return None


def import_cycles(g: Graph) -> list[dict]:
    """One entry per cyclic component: `{"modules": [...], "cycle": [m0, m1, ..., m0]}`.

    `cycle` is a shortest cycle through the component's first module (alphabetically).
    """
    adj = import_adjacency(g)
    found = []
    for comp in strongly_connected(adj):
        start = comp[0]
        best = None
        for dst in adj[start]:
            if dst in comp:
                path = shortest_cycle_through(adj, start, dst)
                if path is not None and (best is None or len(path) < len(best)):
                    best = path
        found.append({"modules": comp, "cycle": best})
    return found


def cyclic_edges(g: Graph) -> set[tuple[str, str]]:
    """Every `IMPORTS` edge that lies on some cycle (both ends in the same cyclic component)."""
    adj = import_adjacency(g)
    comp_of = {m: i for i, comp in enumerate(strongly_connected(adj)) for m in comp}
    return {
        (src, dst)
        for src, dsts in adj.items()
        for dst in dsts
        if src in comp_of and comp_of.get(dst) == comp_of[src]
    }


def _canonical(cycle: list[str]) -> tuple[str, ...]:
    body = cycle[:-1]
    i = body.index(min(body))
    return tuple(body[i:] + body[:i])


def new_cycles(before: Graph, after: Graph) -> list[dict]:
    """Cycles in `after` through import edges that were not on any cycle in `before`.

    Each entry is `{"cycle": [m0, ..., m0], "new_edges": [[src, dst], ...]}`: one shortest cycle per
    newly cyclic edge, deduplicated, with the edges of that cycle that did not exist in `before`.
    """
    old = cyclic_edges(before)
    before_edges = {(r.src, r.dst) for r in before.rels("IMPORTS")}
    adj = import_adjacency(after)
    seen: set[tuple[str, ...]] = set()
    out = []
    for src, dst in sorted(cyclic_edges(after) - old):
        cycle = shortest_cycle_through(adj, src, dst)
        if cycle is None:
            continue
        key = _canonical(cycle)
        if key in seen:
            continue
        seen.add(key)
        edges = [[a, b] for a, b in zip(cycle, cycle[1:]) if (a, b) not in before_edges]
        out.append({"cycle": cycle, "new_edges": edges})
    return out


def format_cycle(path: list[str] | None) -> str:
    return " -> ".join(path) if path else "(no path)"

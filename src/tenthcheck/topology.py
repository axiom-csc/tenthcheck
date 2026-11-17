# Vendored from axiom-lab mind/code/topology.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Blast radius on a code graph.

`blast_radius` walks outward from a start entity along `CALLS`, `IMPORTS`, and
`INHERITS_FROM` edges (the direction they were stored: caller -> callee,
importer -> imported module, subclass -> base). `DEFINES` is not followed.

The reachable set is dependencies of the start node, not the nodes that depend
on it. A function's callers are not included. Depth 0 is the start node, and
`affected` counts the start node.

A missing start id raises KeyError. Cycles are visited once.

`dependents_radius` walks the same edges reversed (callers, subclasses, importers):
the entities that can break when the start entity changes.
"""
from __future__ import annotations

import time
from collections import deque

from tenthcheck.graph import Graph

EDGE_TYPES = ("CALLS", "IMPORTS", "INHERITS_FROM")


def blast_radius(g: Graph, start_id: str, max_depth: int | None = None) -> dict:
    """BFS outward from `start_id`.

    Returns
    - `by_depth`: `{depth: [entity_ids]}`, ids sorted, depth 0 = the start node.
      Depths with no nodes are omitted.
    - `affected`: number of distinct entities reached, including the start.
    - `elapsed_us`: microseconds from `time.perf_counter` around adjacency
      construction and the BFS, not including the missing-node check.

    `max_depth` of 0 returns only the start node. `None` does not cap depth.
    """
    if start_id not in g.entities:
        raise KeyError(f"unknown entity {start_id}")
    if max_depth is not None and max_depth < 0:
        raise ValueError(f"max_depth must be >= 0 or None, got {max_depth}")

    t0 = time.perf_counter()
    outgoing: dict[str, list[str]] = {eid: [] for eid in g.entities}
    for edge_type in EDGE_TYPES:
        for rel in g.rels(edge_type):
            outgoing[rel.src].append(rel.dst)

    by_depth: dict[int, list[str]] = {0: [start_id]}
    seen = {start_id}
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])
    while queue:
        node, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for dst in outgoing[node]:
            if dst in seen:
                continue
            seen.add(dst)
            by_depth.setdefault(depth + 1, []).append(dst)
            queue.append((dst, depth + 1))
    for depth, ids in by_depth.items():
        by_depth[depth] = sorted(ids)
    elapsed_us = (time.perf_counter() - t0) * 1e6
    return {"by_depth": by_depth, "affected": len(seen), "elapsed_us": elapsed_us}


def reverse_adjacency(g: Graph) -> dict[str, list[str]]:
    """`{dst: [src, ...]}` over `EDGE_TYPES`: who calls / imports / subclasses each entity. Sorted."""
    incoming: dict[str, list[str]] = {eid: [] for eid in g.entities}
    for edge_type in EDGE_TYPES:
        for rel in g.rels(edge_type):
            incoming[rel.dst].append(rel.src)
    for eid in incoming:
        incoming[eid] = sorted(set(incoming[eid]))
    return incoming


def dependents_radius(
    g: Graph,
    start_id: str,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    incoming: dict[str, list[str]] | None = None,
) -> dict:
    """BFS along *reversed* `CALLS` / `IMPORTS` / `INHERITS_FROM` edges: what can break if `start_id` changes.

    Depth 1 is the direct callers (and subclasses, and importing modules for a module start); depth 2
    their callers, and so on. This is the opposite direction from `blast_radius`, which lists what the
    start node depends on. Only edges the parser resolved are followed, so dynamic dispatch, `getattr`,
    `super()` and the other cases listed in `tenthcheck.parser` are missed (no edge, no dependent).

    `max_nodes` caps the number of distinct entities visited (start included); when the cap stops the
    walk, `truncated` is True. `incoming` lets callers reuse a prebuilt `reverse_adjacency(g)`.

    Returns `by_depth`, `affected` (including the start), `truncated`, and `elapsed_us`.
    """
    if start_id not in g.entities:
        raise KeyError(f"unknown entity {start_id}")
    if max_depth is not None and max_depth < 0:
        raise ValueError(f"max_depth must be >= 0 or None, got {max_depth}")
    if max_nodes is not None and max_nodes < 1:
        raise ValueError(f"max_nodes must be >= 1 or None, got {max_nodes}")

    t0 = time.perf_counter()
    if incoming is None:
        incoming = reverse_adjacency(g)
    by_depth: dict[int, list[str]] = {0: [start_id]}
    seen = {start_id}
    truncated = False
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])
    while queue and not truncated:
        node, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for src in incoming.get(node, ()):
            if src in seen:
                continue
            if max_nodes is not None and len(seen) >= max_nodes:
                truncated = True
                break
            seen.add(src)
            by_depth.setdefault(depth + 1, []).append(src)
            queue.append((src, depth + 1))
    for depth, ids in by_depth.items():
        by_depth[depth] = sorted(ids)
    elapsed_us = (time.perf_counter() - t0) * 1e6
    return {"by_depth": by_depth, "affected": len(seen), "truncated": truncated, "elapsed_us": elapsed_us}

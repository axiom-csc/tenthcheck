# Vendored from axiom-lab mind/core/graph.py (entity-relation graph used by the policy loader).
"""The one representation: an entity-relation graph with typed attributes.

Every world reports its observations as a Graph. Balls, list elements, agents,
goals and words are all Entities; touching, before/after, part-of, wants and
refers-to are all Relations. Attribute values are typed so Lemma programs can
be type-checked against them.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

# Attribute types a world may use. "vec" is a length-2 float tuple (2D worlds).
ATTR_TYPES = ("num", "int", "bool", "str", "vec", "list")


class ListVal(tuple):
    """A list-typed attribute value. Plain 2-number tuples are vectors; lists are stored as ListVal."""


def attr_type(value: Any) -> str:
    """Infer the typed-attribute tag for a Python value."""
    if isinstance(value, (list, ListVal)):
        return "list"
    if isinstance(value, (bool, np.bool_)):
        return "bool"
    if isinstance(value, (int, np.integer)):
        return "int"
    if isinstance(value, (float, np.floating)):
        return "num"
    if isinstance(value, str):
        return "str"
    if isinstance(value, (tuple, np.ndarray)) and len(value) == 2 and all(
        isinstance(v, (float, int, np.floating, np.integer)) and not isinstance(v, bool) for v in value
    ):
        return "vec"
    if isinstance(value, (list, tuple)):
        return "list"
    raise TypeError(f"unsupported attribute value {value!r}")


def _norm(value: Any) -> Any:
    t = attr_type(value)
    if t == "vec":
        return (float(value[0]), float(value[1]))
    if t == "list":
        return ListVal(value)
    if t == "num":
        return float(value)
    if t == "int":
        return int(value)
    if t == "bool":
        return bool(value)
    return value


@dataclass
class Entity:
    id: str
    type: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.attrs = {k: _norm(v) for k, v in self.attrs.items()}

    def typed_attrs(self) -> dict[str, tuple[str, Any]]:
        return {k: (attr_type(v), v) for k, v in self.attrs.items()}


@dataclass(frozen=True)
class Relation:
    type: str
    src: str
    dst: str
    attrs: tuple = ()  # sorted (key, value) pairs; frozen so relations are hashable

    @staticmethod
    def make(type: str, src: str, dst: str, **attrs) -> "Relation":
        return Relation(type, src, dst, tuple(sorted((k, _norm(v)) for k, v in attrs.items())))

    def get(self, key: str, default=None):
        return dict(self.attrs).get(key, default)


class Graph:
    """A typed entity-relation graph. Cheap to copy; compared structurally."""

    def __init__(self, entities: Iterable[Entity] = (), relations: Iterable[Relation] = (), meta: dict | None = None):
        self.entities: dict[str, Entity] = {}
        self.relations: set[Relation] = set()
        self.meta: dict[str, Any] = dict(meta or {})  # e.g. time step, experiment name; never hidden params
        for e in entities:
            self.add_entity(e)
        for r in relations:
            self.add_relation(r)

    # --- building
    def add_entity(self, e: Entity) -> Entity:
        if e.id in self.entities:
            raise ValueError(f"duplicate entity id {e.id}")
        self.entities[e.id] = e
        return e

    def add(self, id: str, type: str, **attrs) -> Entity:
        return self.add_entity(Entity(id, type, attrs))

    def add_relation(self, r: Relation) -> Relation:
        for end in (r.src, r.dst):
            if end not in self.entities:
                raise KeyError(f"relation {r.type} refers to unknown entity {end}")
        self.relations.add(r)
        return r

    def relate(self, type: str, src: str, dst: str, **attrs) -> Relation:
        return self.add_relation(Relation.make(type, src, dst, **attrs))

    # --- querying
    def __getitem__(self, id: str) -> Entity:
        return self.entities[id]

    def of_type(self, type: str) -> list[Entity]:
        return sorted((e for e in self.entities.values() if e.type == type), key=lambda e: e.id)

    def rels(self, type: str | None = None, src: str | None = None, dst: str | None = None) -> list[Relation]:
        out = [
            r for r in self.relations
            if (type is None or r.type == type) and (src is None or r.src == src) and (dst is None or r.dst == dst)
        ]
        return sorted(out, key=lambda r: (r.type, r.src, r.dst, r.attrs))

    def neighbors(self, id: str, type: str | None = None) -> list[str]:
        return [r.dst for r in self.rels(type, src=id)]

    # --- utilities
    def copy(self) -> "Graph":
        g = Graph(meta=copy.deepcopy(self.meta))
        g.entities = {k: Entity(e.id, e.type, dict(e.attrs)) for k, e in self.entities.items()}
        g.relations = set(self.relations)
        return g

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "entities": [{"id": e.id, "type": e.type, "attrs": e.attrs} for e in self.entities.values()],
            "relations": [{"type": r.type, "src": r.src, "dst": r.dst, "attrs": dict(r.attrs)} for r in self.rels()],
        }

    @staticmethod
    def from_dict(d: dict) -> "Graph":
        g = Graph(meta=d.get("meta"))
        for e in d["entities"]:
            g.add(e["id"], e["type"], **e["attrs"])
        for r in d["relations"]:
            g.relate(r["type"], r["src"], r["dst"], **r["attrs"])
        return g

    def diff(self, other: "Graph", tol: float = 1e-9) -> list[str]:
        """Human-readable differences (used for surprise logging and tests)."""
        out = []
        for id in sorted(set(self.entities) | set(other.entities)):
            a, b = self.entities.get(id), other.entities.get(id)
            if a is None or b is None:
                out.append(f"entity {id}: {'missing' if a is None else 'present'} -> {'missing' if b is None else 'present'}")
                continue
            for k in sorted(set(a.attrs) | set(b.attrs)):
                va, vb = a.attrs.get(k), b.attrs.get(k)
                if not _close(va, vb, tol):
                    out.append(f"{id}.{k}: {va!r} -> {vb!r}")
        for r in sorted(self.relations ^ other.relations, key=lambda r: (r.type, r.src, r.dst)):
            out.append(f"relation {r.type}({r.src},{r.dst}) {'removed' if r in self.relations else 'added'}")
        return out

    def __eq__(self, other) -> bool:
        return isinstance(other, Graph) and not self.diff(other)

    def __repr__(self) -> str:
        return f"Graph({len(self.entities)} entities, {len(self.relations)} relations)"


def _close(a, b, tol) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False
    if isinstance(a, tuple) and isinstance(b, tuple) and len(a) == len(b):
        return all(_close(x, y, tol) for x, y in zip(a, b))
    return a == b

"""Turn a user-typed symbol into a graph entity id."""
from __future__ import annotations

from tenthlook.tenthcheck.graph import Graph

CODE_TYPES = ("function", "class", "module")


class AmbiguousSymbol(LookupError):
    def __init__(self, symbol: str, candidates: list[str]):
        self.symbol = symbol
        self.candidates = candidates
        super().__init__(f"{symbol!r} matches {len(candidates)} entities: {', '.join(candidates[:10])}"
                         + (" ..." if len(candidates) > 10 else ""))


def resolve_symbol(g: Graph, symbol: str) -> str:
    """Exact entity id first; otherwise a unique dotted-suffix match (`Client.send` -> `httpx._client.Client.send`).

    Raises KeyError when nothing matches and AmbiguousSymbol when several do.
    """
    if symbol in g.entities:
        return symbol
    suffix = "." + symbol
    cands = sorted(eid for eid, e in g.entities.items() if e.type in CODE_TYPES and eid.endswith(suffix))
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise AmbiguousSymbol(symbol, cands)
    raise KeyError(f"no entity named {symbol!r}")

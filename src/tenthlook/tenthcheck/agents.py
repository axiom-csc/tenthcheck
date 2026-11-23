"""Multi-agent edit warnings: which functions each running agent session has edited, and overlap between sessions.

State lives in `<repo>/.tenthcheck/agents.json` (with a `.gitignore` so it is never committed):

    {"schema": "tenthcheck-agents/1",
     "sessions": {"<session id>": {"label": "...", "started": <unix s>, "last_seen": <unix s>,
                                   "edits": {"pkg.mod.func": {"file": "pkg/mod.py", "at": <unix s>, "count": 2}}}}}

A session's *blast radius* is, for every function/class it edited, that entity plus its callers (and subclasses)
and its callees, up to `depth` hops (default 2), along the edges Tenthcheck resolves statically. It is computed
from the code as it is now, not stored, so it follows the code as it changes.

When a session records (or checks) an edit, every function/class it changes is compared with the other live
sessions' blast radii. A match is a **warning, never a block**: nothing is refused, no exit code changes, and the
`ok` of `check_proposed_edit` is unaffected. Sessions with no edit for `ttl_s` (default 12 hours) are ignored and
dropped on the next write; `end_session` removes one at once.

Like the rest of Tenthcheck this is structural only: dynamic dispatch, getattr and calls through variables are not
seen, so no warning means "no resolved overlap", not "safe to edit in parallel".
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from tenthlook.tenthcheck.graph import Graph
from tenthlook.tenthcheck.parser import module_name_for, parse_package
from tenthlook.tenthcheck.scope_audit import audit_graphs
from tenthlook.tenthcheck.topology import blast_radius, dependents_radius, reverse_adjacency

SCHEMA = "tenthcheck-agents/1"
STATE_DIR = ".tenthcheck"
STATE_FILE = "agents.json"
DEFAULT_DEPTH = 2
DEFAULT_TTL_S = 12 * 3600
CODE_TYPES = ("function", "class")


# ---------------------------------------------------------------- state file

def state_path(repo_dir: str | Path) -> Path:
    return Path(repo_dir).resolve() / STATE_DIR / STATE_FILE


def _empty() -> dict:
    return {"schema": SCHEMA, "sessions": {}}


def load_state(repo_dir: str | Path) -> dict:
    p = state_path(repo_dir)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()  # a corrupt file is replaced on the next write, never fatal
    if data.get("schema") != SCHEMA or not isinstance(data.get("sessions"), dict):
        return _empty()
    return data


@contextmanager
def _locked(repo_dir: str | Path):
    """Exclusive lock around read-modify-write, so two sessions recording at once don't lose an edit."""
    d = state_path(repo_dir).parent
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# Tenthcheck's local agent-session state; never commit it.\n*\n", encoding="utf-8")
    with open(d / "agents.lock", "a+") as fh:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:  # Windows: no flock; the atomic replace below still avoids torn files
            pass
        yield


def _write_state(repo_dir: str | Path, data: dict) -> None:
    p = state_path(repo_dir)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".agents.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)


def live_sessions(data: dict, now: float, ttl_s: float = DEFAULT_TTL_S) -> dict[str, dict]:
    return {sid: s for sid, s in data["sessions"].items() if now - float(s.get("last_seen", 0)) <= ttl_s}


def others_active(repo_dir: str | Path, session_id: str, ttl_s: float = DEFAULT_TTL_S, now: float | None = None) -> bool:
    """Cheap pre-check (no parsing): is any other live session recorded with at least one edit?"""
    now = time.time() if now is None else now
    return any(sid != session_id and s.get("edits")
               for sid, s in live_sessions(load_state(repo_dir), now, ttl_s).items())


# ---------------------------------------------------------------- what an edit changes

def changed_symbols(repo_dir: str | Path, file_path: str, new_source: str, index=None,
                    before: Graph | None = None) -> tuple[list[str], Graph, Graph]:
    """Functions/classes in `file_path` that `new_source` adds, removes or modifies, plus the before/after graphs.

    `before` defaults to the repo on disk (call before writing the file). The MCP server passes its own snapshot of
    the code as it last recorded it, so `record_edit` works after the write too. Only entities defined in
    `file_path` count, so another session's edits to other files are never attributed to this one."""
    root = Path(repo_dir).resolve()
    target = Path(file_path)
    if target.is_absolute():
        target = target.resolve().relative_to(root)
    rel = target.as_posix()
    if not rel.endswith(".py"):
        raise ValueError(f"not a Python file: {rel}")
    try:
        ast.parse(new_source, filename=rel)
    except SyntaxError as error:
        raise ValueError(f"syntax error in proposed source for {rel}: {error}") from None
    if index is not None:
        after = index.graph_with_overrides({rel: new_source})
        before = before if before is not None else index.graph
    else:
        after = parse_package(str(root), skip_syntax_errors=True, overrides={rel: new_source})
        before = before if before is not None else parse_package(str(root), skip_syntax_errors=True)
    mod, _ = module_name_for(rel, root.name)
    changed = audit_graphs(before, after, max_nodes=1)["changed_entities"]
    syms = [e for e in changed if (e == mod or e.startswith(mod + "."))
            and (before.entities.get(e) or after.entities.get(e)).type in CODE_TYPES]
    return syms, before, after


# ---------------------------------------------------------------- blast radius and overlap

class _Radius:
    """Callers and callees of entities within `depth` hops, on one or more graphs (results are unioned)."""

    def __init__(self, graphs: Iterable[Graph], depth: int):
        self.graphs = [(g, reverse_adjacency(g)) for g in graphs]
        self.depth = depth
        self._cache: dict[str, dict[str, tuple[str, int]]] = {}

    def of(self, eid: str) -> dict[str, tuple[str, int]]:
        """`{entity: (relation, hops)}` for `eid`'s radius: relation is "caller" or "callee"; nearest hop wins."""
        if eid in self._cache:
            return self._cache[eid]
        out: dict[str, tuple[str, int]] = {}
        for g, incoming in self.graphs:
            if eid not in g.entities:
                continue
            walks = (("caller", dependents_radius(g, eid, max_depth=self.depth, incoming=incoming)),
                     ("callee", blast_radius(g, eid, max_depth=self.depth)))
            for rel, rad in walks:
                for hops, ids in rad["by_depth"].items():
                    if hops == 0:
                        continue
                    for other in ids:
                        if g[other].type in CODE_TYPES and (other not in out or out[other][1] > hops):
                            out[other] = (rel, hops)
        self._cache[eid] = out
        return out


def overlaps(symbols: list[str], session_id: str, data: dict, graphs: Iterable[Graph], now: float,
             depth: int = DEFAULT_DEPTH, ttl_s: float = DEFAULT_TTL_S) -> list[dict]:
    """Warnings for `symbols` (edited by `session_id`) that fall inside another live session's blast radius."""
    radius = _Radius(graphs, depth)
    warnings = []
    for other_id, other in sorted(live_sessions(data, now, ttl_s).items()):
        if other_id == session_id:
            continue
        for theirs, info in sorted(other.get("edits", {}).items()):
            rad = radius.of(theirs)
            for mine in symbols:
                if mine == theirs:
                    rel, hops = "same", 0
                elif mine in rad:
                    rel, hops = rad[mine]
                else:
                    continue
                warnings.append({"symbol": mine, "other_session": other_id, "other_label": other.get("label") or "",
                                 "their_edit": theirs, "their_file": info.get("file"), "relation": rel, "hops": hops,
                                 "their_edit_age_s": round(now - float(info.get("at", now))),
                                 "message": format_warning(mine, other_id, theirs, rel, hops)})
    warnings.sort(key=lambda w: (w["hops"], w["symbol"], w["other_session"], w["their_edit"]))
    return warnings


def format_warning(mine: str, other: str, theirs: str, rel: str, hops: int) -> str:
    if rel == "same":
        return f"{mine} was also edited by session {other}"
    what = "a caller of" if rel == "caller" else "called by"
    return f"{mine} is {what} {theirs} ({hops} hop{'s' if hops != 1 else ''}), which session {other} edited"


# ---------------------------------------------------------------- operations

def check(repo_dir: str | Path, session_id: str, file_path: str | None = None, new_source: str | None = None,
          symbols: Iterable[str] = (), depth: int = DEFAULT_DEPTH, ttl_s: float = DEFAULT_TTL_S,
          index=None, now: float | None = None, before: Graph | None = None) -> dict[str, Any]:
    """Read-only: which of this edit's symbols overlap other sessions. Records nothing."""
    return _run(repo_dir, session_id, file_path, new_source, symbols, depth, ttl_s, index, now, record=False,
                before=before)


def record_edit(repo_dir: str | Path, session_id: str, file_path: str | None = None, new_source: str | None = None,
                symbols: Iterable[str] = (), label: str = "", depth: int = DEFAULT_DEPTH,
                ttl_s: float = DEFAULT_TTL_S, index=None, now: float | None = None,
                before: Graph | None = None) -> dict[str, Any]:
    """Record that `session_id` edited these symbols (from `new_source` for `file_path`, and/or `symbols`) and
    return warnings for overlap with other live sessions. Never refuses."""
    return _run(repo_dir, session_id, file_path, new_source, symbols, depth, ttl_s, index, now, record=True,
                label=label, before=before)


def _run(repo_dir, session_id, file_path, new_source, symbols, depth, ttl_s, index, now, record, label="",
         before=None):
    if not session_id:
        raise ValueError("a session id is required (--session or TENTHCHECK_SESSION)")
    now = time.time() if now is None else now
    syms, graphs = list(symbols), []
    if new_source is not None:
        if not file_path:
            raise ValueError("new_source needs file_path")
        found, g_before, g_after = changed_symbols(repo_dir, file_path, new_source, index=index, before=before)
        syms += found
        graphs = [g_before, g_after]
    if not graphs:
        graphs = [index.graph if index is not None else parse_package(str(Path(repo_dir).resolve()),
                                                                      skip_syntax_errors=True)]
    syms = sorted(set(syms))
    rel_file = file_path if file_path is None else Path(file_path).as_posix()

    def compute(data):
        return overlaps(syms, session_id, data, graphs, now, depth, ttl_s)

    if not record:
        data = load_state(repo_dir)
        return {"session": session_id, "symbols": syms, "recorded": False, "warnings": compute(data)}
    with _locked(repo_dir):
        data = load_state(repo_dir)
        data["sessions"] = live_sessions(data, now, ttl_s)  # drop expired sessions
        s = data["sessions"].setdefault(session_id, {"label": label, "started": now, "last_seen": now, "edits": {}})
        if label:
            s["label"] = label
        s["last_seen"] = now
        for sym in syms:
            e = s["edits"].setdefault(sym, {"file": rel_file, "at": now, "count": 0})
            e.update(at=now, count=e["count"] + 1, file=rel_file or e.get("file"))
        warnings = compute(data)
        _write_state(repo_dir, data)
    return {"session": session_id, "symbols": syms, "recorded": True, "warnings": warnings}


def list_sessions(repo_dir: str | Path, ttl_s: float = DEFAULT_TTL_S, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    data = load_state(repo_dir)
    live = live_sessions(data, now, ttl_s)
    return {"state_file": str(state_path(repo_dir)),
            "sessions": [{"session": sid, "label": s.get("label") or "", "last_seen_age_s": round(now - s["last_seen"]),
                          "edits": sorted(s.get("edits", {}))} for sid, s in sorted(live.items())],
            "expired": sorted(set(data["sessions"]) - set(live))}


def end_session(repo_dir: str | Path, session_id: str) -> bool:
    if not state_path(repo_dir).exists():
        return False
    with _locked(repo_dir):
        data = load_state(repo_dir)
        existed = data["sessions"].pop(session_id, None) is not None
        _write_state(repo_dir, data)
    return existed


def format_result(res: dict) -> str:
    verb = "Recorded" if res["recorded"] else "Checked"
    lines = [f"{verb} for session {res['session']}: {len(res['symbols'])} symbol(s)"
             + (": " + ", ".join(res["symbols"]) if res["symbols"] else "")]
    if not res["warnings"]:
        lines.append("No overlap with other sessions' blast radius.")
    else:
        lines.append(f"WARNING: {len(res['warnings'])} overlap(s) with other sessions (not blocked):")
        lines += [f"  {w['message']}" for w in res["warnings"]]
    return "\n".join(lines)

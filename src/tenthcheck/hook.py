"""`tenthcheck hook`: a Claude Code hook that stays silent unless an edit breaks structure.

Registered as a PostToolUse hook on Edit / Write / MultiEdit (and optionally as a Stop hook), it reads Claude
Code's hook JSON on stdin and prints **nothing** unless one of these trips:

  (a) the edit creates a new import cycle (module-level imports, same rules as `tenthcheck cycles`);
  (b) the edit changes a function or method signature and some callers no longer fit it: a call that can't
      bind to the new parameters (missing / unexpected argument, too many positional), or, when positional
      parameters were reordered or renamed, a positional call that still passes its arguments as before;
      reported as `file:line` per caller;
  (c) optional (`--dependents N`, off by default): a function the edit changed has >= N direct callers.

On a trip it writes at most `--max-lines` (default 10) lines to stderr and exits 2, which Claude Code feeds back
to the agent. On a pass: no output, exit 0. Internal errors are logged to `.tenthcheck/hook.log` and also exit 0
silently: the hook never interrupts the agent because of its own bug.

**Incremental.** The hook keeps a per-file *summary* (imports, function signatures, call sites with their argument
shapes) in `.tenthcheck/hook_cache.json`, keyed by mtime and size, and re-parses only files that changed. The
cached summary of the edited file is the pre-edit state. On first use (no cache entry) the pre-edit source comes
from the hook payload (`tool_response.originalFile`, or by reversing the Edit's `old_string`/`new_string`), else
from `git show HEAD:<file>`; if none is available, the edit is only recorded. Files changed by other means (Bash,
another editor) are re-summarized without being attributed to the edit.

**Caller resolution** is per call site, from that file's own imports, without the full call graph: bare names bound
by `from m import f` (one re-export hop followed) or defined in the same module; `mod.f` through a module alias;
`Cls.m(...)`; `self.m()` / `cls.m()` inside the class; constructor calls for `__init__`; local aliases
(`get = Measurement.get`); and `obj.m()` on an unknown receiver **only if** `m` is the only method of that name in
the repository. Calls through `*args` / `**kwargs` are not judged. As everywhere in Tenthcheck: no output means "no
resolved problem", not "safe".

**Stop hook.** Callers reported under (b) are remembered per session; edits that fix them clear them silently. As a
Stop hook, `tenthcheck hook` exits 2 (blocking the stop once, never twice: `stop_hook_active` is honoured) if any of
them is still not updated, so a refactor isn't left half done.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from tenthcheck.cycles import format_cycle, new_cycles
from tenthcheck.graph import Graph
from tenthcheck.parser import _Parsed, _match_known_module, _resolve_import_from, iter_py_files, module_name_for

CACHE_VERSION = 2
STATE_DIR = ".tenthcheck"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


# ---------------------------------------------------------------- per-file summaries

def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    a = fn.args
    return {"posonly": [x.arg for x in a.posonlyargs], "args": [x.arg for x in a.args],
            "vararg": a.vararg is not None, "kwonly": [x.arg for x in a.kwonlyargs], "kwarg": a.kwarg is not None,
            "ndefaults": len(a.defaults),
            "kwdefaults": [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]}


def _dotted(e: ast.expr) -> str | None:
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        head = _dotted(e.value)
        return f"{head}.{e.attr}" if head else None
    return None


def _kind(fn, in_class: bool) -> str:
    decos = {(_dotted(d) or "").rsplit(".", 1)[-1] for d in fn.decorator_list}
    if not in_class:
        return "function"
    if "staticmethod" in decos:
        return "staticmethod"
    if "classmethod" in decos:
        return "classmethod"
    return "method"


def _call_record(c: ast.Call) -> dict | None:
    func = _dotted(c.func)
    if func is None:
        return None
    star = any(isinstance(a, ast.Starred) for a in c.args)
    dstar = any(k.arg is None for k in c.keywords)
    shape = ast.dump(ast.Tuple(elts=list(c.args), ctx=ast.Load()), include_attributes=False) + "|" + ",".join(
        f"{k.arg}={ast.dump(k.value, include_attributes=False)}" for k in c.keywords)
    return {"func": func, "line": c.lineno, "npos": len(c.args), "kw": [k.arg for k in c.keywords if k.arg],
            "star": star, "dstar": dstar, "shape": hashlib.sha1(shape.encode()).hexdigest()[:12]}


def summarize(rel: str, source: str, root_name: str) -> dict:
    """Everything the checks need from one file, from its AST alone."""
    name, is_pkg = module_name_for(rel, root_name)
    tree = ast.parse(source, filename=rel)
    mod = _Parsed(name, tree, is_pkg)
    imports, binds = [], {}
    for node in tree.body:  # module level only, as the parser's IMPORTS edges
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append({"kind": "import", "name": a.name})
                binds[a.asname or a.name.split(".", 1)[0]] = ["module", a.name if a.asname else a.name.split(".", 1)[0]]
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_import_from(mod, node)
            names = [a.name for a in node.names]
            imports.append({"kind": "from", "target": target, "names": names})
            if target and names != ["*"]:
                for a in node.names:
                    binds[a.asname or a.name] = ["from", target, a.name]
    defs, calls = {}, {}

    def own_calls(body_owner, key):
        out = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                if n is not body_owner:
                    return
                self.generic_visit(n)
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                if n is not body_owner:
                    return
                self.generic_visit(n)

            def visit_Call(self, n):
                r = _call_record(n)
                if r:
                    out.append(r)
                self.generic_visit(n)
        V().visit(body_owner)
        aliases = {}
        if not isinstance(body_owner, ast.Module):
            for n in ast.walk(body_owner):
                if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and _dotted(n.value)):
                    aliases[n.targets[0].id] = _dotted(n.value)
        if out:
            calls[key] = {"calls": out, "aliases": aliases}

    def walk(body, prefix, cls):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}.{n.name}" if prefix else n.name
                defs.setdefault(q, {"line": n.lineno, "name": n.name, "cls": cls, "kind": _kind(n, cls is not None),
                                    "sig": _sig(n), "hash": hashlib.sha1(ast.dump(n, include_attributes=False)
                                                                         .encode()).hexdigest()[:12]})
                own_calls(n, q)
                walk(n.body, q, None)
            elif isinstance(n, ast.ClassDef):
                q = f"{prefix}.{n.name}" if prefix else n.name
                defs.setdefault(q, {"line": n.lineno, "name": n.name, "cls": None, "kind": "class", "sig": None})
                walk(n.body, q, q)
    walk(tree.body, "", None)
    own_calls(tree, "<module>")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds[node.name] = ["local", node.name]
    return {"module": name, "is_package": is_pkg, "imports": imports, "binds": binds, "defs": defs, "calls": calls}


# ---------------------------------------------------------------- the module import graph (check a)

def import_graph(sums: dict[str, dict]) -> Graph:
    """IMPORTS edges with the parser's rules (`tenthcheck.parser._bind_import`), from summaries."""
    known = {s["module"] for s in sums.values()}
    g = Graph()
    for m in sorted(known):
        g.add(m, "module", name=m, lineno=1)

    def ent(path):
        mid = _match_known_module(path, known) or path
        if mid not in g.entities:
            g.add(mid, "module", name=mid, lineno=1)
        return mid

    for s in sums.values():
        src = s["module"]
        for imp in s["imports"]:
            if imp["kind"] == "import":
                g.relate("IMPORTS", src, ent(imp["name"]))
                continue
            target = imp["target"]
            if target is None:
                continue
            mid = ent(target)
            if mid != src:
                g.relate("IMPORTS", src, mid)
            if imp["names"] == ["*"]:
                continue
            for n in imp["names"]:
                cand = f"{mid}.{n}"
                sub = cand if cand in known else _match_known_module(f"{target}.{n}", known)
                if sub is not None:
                    g.relate("IMPORTS", src, sub)
    return g


# ---------------------------------------------------------------- signatures and callers (checks b, c)

def fmt_sig(name: str, s: dict) -> str:
    parts = list(s["posonly"]) + (["/"] if s["posonly"] else []) + list(s["args"])
    if s["vararg"]:
        parts.append("*args")
    elif s["kwonly"]:
        parts.append("*")
    parts += s["kwonly"]
    if s["kwarg"]:
        parts.append("**kwargs")
    return f"{name}({', '.join(parts)})"


def bind_problem(sig: dict, call: dict, drop_first: bool) -> str | None:
    """Why `call` can't bind to `sig` (None if it can, or if the call uses *args/**kwargs)."""
    if call["star"] or call["dstar"]:
        return None
    pos = list(sig["posonly"]) + list(sig["args"])
    nreq_pos = len(pos) - sig["ndefaults"]
    if drop_first and pos:
        pos, nreq_pos = pos[1:], max(0, nreq_pos - 1)
    n = call["npos"]
    if n > len(pos) and not sig["vararg"]:
        return f"takes {len(pos)} positional argument{'s' if len(pos) != 1 else ''}, call passes {n}"
    given = set(pos[:n])
    posonly = set(sig["posonly"][1:] if drop_first else sig["posonly"])
    for k in call["kw"]:
        if k in posonly:
            return f"'{k}' is positional-only"
        if k in given:
            return f"'{k}' given twice"
        if k not in pos and k not in sig["kwonly"] and not sig["kwarg"]:
            return f"unexpected keyword '{k}'"
        given.add(k)
    missing = [p for p in pos[:nreq_pos] if p not in given]
    missing += [k for k in sig["kwonly"] if k not in sig["kwdefaults"] and k not in given]
    if missing:
        return "missing " + ", ".join(f"'{m}'" for m in missing)
    return None


class Index:
    """Lookups over a set of summaries."""

    def __init__(self, sums: dict[str, dict]):
        self.sums = sums
        self.by_module = {s["module"]: (rel, s) for rel, s in sums.items()}
        self.known = set(self.by_module)
        counts: dict[str, int] = {}
        for s in sums.values():
            for d in s["defs"].values():
                if d["kind"] in ("method", "classmethod", "staticmethod"):
                    counts[d["name"]] = counts.get(d["name"], 0) + 1
        self.unique_methods = {n for n, c in counts.items() if c == 1}
        # last dotted part of a call -> candidate targets (mod, qual); a class name also means its __init__
        self.by_name: dict[str, list[tuple[str, str]]] = {}
        for mod, (_rel, s) in self.by_module.items():
            for qual, d in s["defs"].items():
                if d["sig"] is not None:
                    key = d["cls"].rsplit(".", 1)[-1] if d["name"] == "__init__" and d["cls"] else d["name"]
                    self.by_name.setdefault(key, []).append((mod, qual))
                    if d["name"] == "__init__" and d["cls"]:
                        self.by_name.setdefault("__init__", []).append((mod, qual))

    def _expand(self, info: dict, func: str) -> str:
        head, _, rest = func.partition(".")
        if head in info["aliases"]:
            return info["aliases"][head] + (f".{rest}" if rest else "")
        return func

    def targets(self, rel: str, fn: str, info: dict, c: dict):
        """(mod, qual, drop_first) for each repo function this call resolves to (usually 0 or 1)."""
        mod = self.sums[rel]["module"]
        func = self._expand(info, c["func"])
        for tmod, qual in self.by_name.get(func.rsplit(".", 1)[-1], ()):
            d = self.by_module[tmod][1]["defs"][qual]
            drop = self._match(mod, fn, func, tmod, qual, d["name"], d["cls"],
                               d["cls"].rsplit(".", 1)[-1] if d["cls"] else None, d["kind"],
                               d["name"] == "__init__" and d["cls"] is not None)
            if drop is not None:
                yield tmod, qual, drop

    def resolve_name(self, module: str, name: str, hops: int = 0) -> str | None:
        """`module`-level binding of `name` -> 'mod:qual' of a def, or 'module:<mod>' for a module."""
        rel_s = self.by_module.get(module)
        if rel_s is None:
            return None
        b = rel_s[1]["binds"].get(name)
        if b is None:
            return None
        if b[0] == "local":
            return f"{module}:{b[1]}"
        if b[0] == "module":
            m = _match_known_module(b[1], self.known)
            return f"module:{m}" if m else None
        target, attr = b[1], b[2]
        sub = _match_known_module(f"{target}.{attr}", self.known)
        if sub is not None and f"{target}.{attr}".endswith(sub):
            return f"module:{sub}"
        m = _match_known_module(target, self.known)
        if m is None:
            return None
        if attr in self.by_module[m][1]["defs"]:
            return f"{m}:{attr}"
        return self.resolve_name(m, attr, hops + 1) if hops < 3 else None

    def callers(self, target_mod: str, qual: str) -> list[dict]:
        """Call sites of `target_mod:qual`: [{rel, fn, line, call, drop_first, n}] (n = ordinal among same func)."""
        d = self.by_module[target_mod][1]["defs"][qual]
        name, cls, kind = d["name"], d["cls"], d["kind"]
        cls_name = cls.rsplit(".", 1)[-1] if cls else None
        is_init = name == "__init__" and cls is not None
        out = []
        last = cls_name if is_init else name
        for rel, s in self.sums.items():
            mod = s["module"]
            for fn, info in s["calls"].items():
                seen: dict[str, int] = {}
                for c in info["calls"]:
                    n = seen[c["func"]] = seen.get(c["func"], -1) + 1
                    func = c["func"]
                    if not (func == last or func.endswith("." + last)):
                        func = self._expand(info, func)
                        if not (func == last or func.endswith("." + last)):
                            continue
                    else:
                        func = self._expand(info, func)
                    drop = self._match(mod, fn, func, target_mod, qual, name, cls, cls_name, kind, is_init)
                    if drop is not None:
                        out.append({"rel": rel, "fn": fn, "line": c["line"], "call": c, "drop_first": drop, "n": n})
        return out

    def _match(self, mod, fn, func, tmod, qual, name, cls, cls_name, kind, is_init):
        """None if `func` (as written in `mod:fn`) isn't a call of the target; else whether `self` is bound."""
        parts = func.split(".")
        if is_init:  # constructor: Cls(...) or mod.Cls(...)
            if parts[-1] != cls_name:
                return None
            r = self.resolve_name(mod, parts[0]) if len(parts) == 1 else self._attr_owner(mod, parts[:-1])
            if len(parts) == 1:
                return True if r == f"{tmod}:{cls}" else None
            return True if r == f"module:{tmod}" else None
        if parts[-1] != name:
            return None
        if cls is None:  # module-level function
            if len(parts) == 1:
                return False if self.resolve_name(mod, name) == f"{tmod}:{qual}" else None
            return False if self._attr_owner(mod, parts[:-1]) == f"module:{tmod}" else None
        # a method of `cls`
        bound = kind != "staticmethod"
        if len(parts) == 2 and parts[0] in ("self", "cls") and mod == tmod and fn.startswith(cls + "."):
            return bound
        if len(parts) >= 2:
            owner = parts[:-1]
            r = self.resolve_name(mod, owner[0]) if len(owner) == 1 else None
            if len(owner) == 2:
                m = self._attr_owner(mod, owner[:1])
                if m and m.startswith("module:") and m[7:] == tmod and owner[1] == cls:
                    r = f"{tmod}:{cls}"
            if r == f"{tmod}:{cls}":  # Cls.m(...): bound for classmethods; unbound (self passed) for methods
                return kind == "classmethod"
            if r is None and name in self.unique_methods and parts[0] not in ("self", "cls"):
                return bound  # obj.m() with an unknown receiver; m is defined once in the repo
        return None

    def _attr_owner(self, mod, parts):
        r = self.resolve_name(mod, parts[0])
        for p in parts[1:]:
            if r and r.startswith("module:"):
                sub = f"{r[7:]}.{p}"
                r = f"module:{sub}" if sub in self.known else (f"{r[7:]}:{p}" if p in self.by_module[r[7:]][1]["defs"] else None)
            else:
                return None
        return r


def _reordered(old: dict, new: dict) -> bool:
    """Positional parameters renamed or reordered (same count): a positional call may now mean something else."""
    o = old["posonly"] + old["args"]
    n = new["posonly"] + new["args"]
    return o != n and len(o) == len(n)


# ---------------------------------------------------------------- cache, state, payload

def _state_dir(root: Path) -> Path:
    d = root / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# Tenthcheck's local state; never commit it.\n*\n", encoding="utf-8")
    return d


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: Path, data) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def scan(root: Path, cache: dict) -> tuple[dict[str, dict], set[str]]:
    """Refresh summaries for files whose mtime/size changed. Returns (rel -> summary, rels re-summarized)."""
    files = cache.setdefault("files", {})
    seen, changed = set(), set()
    for p in iter_py_files(root):
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        st = p.stat()
        e = files.get(rel)
        if e and e["mtime_ns"] == st.st_mtime_ns and e["size"] == st.st_size:
            continue
        try:
            summ = summarize(rel, p.read_text(encoding="utf-8"), root.name)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            files.pop(rel, None)
            continue
        files[rel] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "summary": summ}
        changed.add(rel)
    for rel in set(files) - seen:
        del files[rel]
    return {rel: e["summary"] for rel, e in files.items()}, changed


def pre_edit_source(root: Path, rel: str, payload: dict, current: str) -> str | None:
    """The file before this edit when no cached summary exists: payload original, reversed Edit, or git HEAD."""
    resp = payload.get("tool_response") or {}
    if isinstance(resp, dict) and isinstance(resp.get("originalFile"), str):
        return resp["originalFile"]
    ti = payload.get("tool_input") or {}
    edits = ti.get("edits") if payload.get("tool_name") == "MultiEdit" else (
        [ti] if payload.get("tool_name") == "Edit" else None)
    if edits:
        text = current
        for e in reversed(edits):
            old, new = e.get("old_string"), e.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or (new and text.count(new) != 1 and not e.get("replace_all")):
                text = None
                break
            text = text.replace(new, old) if e.get("replace_all") else text.replace(new, old, 1)
        if text is not None:
            return text
    try:
        r = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{rel}"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    if payload.get("tool_name") == "Write" and isinstance(resp, dict) and resp.get("type") == "create":
        return ""  # a new file: everything in it is new
    return None


# ---------------------------------------------------------------- the checks

def _excluded(rel: str, patterns: list[str]) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def _sig_issues(before: Index, after: Index, rel: str, pending: list, exclude: list[str] = ()) -> tuple[list[str], list[dict]]:
    """Check (b) for the edited file `rel`: changed signatures and their callers that don't fit."""
    lines, new_pending = [], []
    b, a = before.sums.get(rel), after.sums[rel]
    if b is None:
        return lines, new_pending
    mod = a["module"]
    for qual, d in sorted(a["defs"].items()):
        od = b["defs"].get(qual)
        if not od or d["sig"] is None or od["sig"] is None or od["sig"] == d["sig"]:
            continue
        old_calls = {(c["rel"], c["fn"], c["call"]["func"], c["n"]): c["call"]["shape"]
                     for c in before.callers(mod, qual)} if mod in before.by_module else {}
        bad = []
        for c in after.callers(mod, qual):
            if _excluded(c["rel"], exclude):
                continue
            why = bind_problem(d["sig"], c["call"], c["drop_first"])
            key = (c["rel"], c["fn"], c["call"]["func"], c["n"])
            if why is None and _reordered(od["sig"], d["sig"]) and c["call"]["npos"] >= 1 \
                    and old_calls.get(key) == c["call"]["shape"]:
                why = "positional arguments unchanged since the parameters were reordered/renamed"
            if why:
                bad.append((c, why))
        if not bad:
            continue
        lines.append(f"Tenthcheck: signature changed {mod}.{fmt_sig(qual, od['sig'])} -> {fmt_sig(d['name'], d['sig'])}; "
                     f"{len(bad)} caller{'s' if len(bad) != 1 else ''} not updated:")
        for c, why in sorted(bad, key=lambda x: (x[0]["rel"], x[0]["line"])):
            lines.append(f"  {c['rel']}:{c['call']['line']} ({c['fn']}): {why}")
            new_pending.append({"target": [mod, qual], "rel": c["rel"], "fn": c["fn"], "func": c["call"]["func"],
                                "n": c["n"], "shape": c["call"]["shape"], "line": c["call"]["line"],
                                "reordered": _reordered(od["sig"], d["sig"])})
    return lines, new_pending


def _call_issues(before: Index, after: Index, rel: str) -> list[str]:
    """Calls in the edited file that this edit added or changed and that don't fit their target's signature."""
    a, b = after.sums[rel], before.sums.get(rel)
    old_shapes = set()
    if b:
        old_shapes = {(fn, c["func"], c["shape"]) for fn, info in b["calls"].items() for c in info["calls"]}
    out = []
    for fn, info in a["calls"].items():
        for c in info["calls"]:
            if (fn, c["func"], c["shape"]) in old_shapes:
                continue
            for tmod, qual, drop in after.targets(rel, fn, info, c):
                d = after.by_module[tmod][1]["defs"][qual]
                why = bind_problem(d["sig"], c, drop)
                if why:
                    out.append(f"Tenthcheck: {rel}:{c['line']} calls {tmod}.{fmt_sig(qual, d['sig'])}: {why}")
    return out


def recheck_pending(after: Index, pending: list) -> list:
    """Pending callers that are still not updated."""
    still = []
    for p in pending:
        tmod, qual = p["target"]
        if tmod not in after.by_module or qual not in after.by_module[tmod][1]["defs"]:
            continue
        d = after.by_module[tmod][1]["defs"][qual]
        for c in after.callers(tmod, qual):
            if (c["rel"], c["fn"], c["call"]["func"], c["n"]) != (p["rel"], p["fn"], p["func"], p["n"]):
                continue
            why = bind_problem(d["sig"], c["call"], c["drop_first"])
            if why or (p["reordered"] and c["call"]["shape"] == p["shape"]):
                still.append({**p, "line": c["call"]["line"]})
    return still


def _emit(lines: list[str], max_lines: int) -> int:
    if not lines:
        return 0
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [f"  ... and {len(lines) - max_lines + 1} more"]
    sys.stderr.write("\n".join(lines) + "\n")
    return 2


def run(payload: dict, root: Path | None = None, dependents: int | None = None, max_lines: int = 10,
        exclude: list[str] = ()) -> int:
    root = Path(root or os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    d = _state_dir(root)
    cache_path, state_path = d / "hook_cache.json", d / "hook_state.json"
    cache = _load(cache_path, {})
    if cache.get("version") != CACHE_VERSION or cache.get("root") != str(root):
        cache = {"version": CACHE_VERSION, "root": str(root), "files": {}}
    state = _load(state_path, {"sessions": {}})
    sess = state.setdefault("sessions", {}).setdefault(payload.get("session_id") or "-", {"pending": [], "big": []})
    event = payload.get("hook_event_name") or "PostToolUse"

    if event == "Stop":
        after = Index(scan(root, cache)[0])
        _save(cache_path, cache)
        sess["pending"] = recheck_pending(after, sess["pending"])
        _save(state_path, state)
        if payload.get("stop_hook_active") or not sess["pending"]:
            return 0
        lines = [f"Tenthcheck: {len(sess['pending'])} caller(s) of changed signatures still not updated:"]
        lines += [f"  {p['rel']}:{p['line']} ({p['fn']}) calls {p['target'][0]}.{p['target'][1]}" for p in sess["pending"]]
        return _emit(lines, max_lines)

    if payload.get("tool_name") not in EDIT_TOOLS:
        return 0
    fp = (payload.get("tool_input") or {}).get("file_path")
    if not fp or not fp.endswith(".py"):
        return 0
    path = Path(fp) if os.path.isabs(fp) else root / fp
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return 0  # outside the project
    prev = cache.get("files", {}).get(rel)
    prev_summary = prev["summary"] if prev else None
    sums_after, _ = scan(root, cache)
    if rel not in sums_after:  # deleted, or a syntax error after the edit: nothing structural to say
        _save(cache_path, cache)
        return 0
    if prev_summary is None:
        src = pre_edit_source(root, rel, payload, path.read_text(encoding="utf-8"))
        if src is not None:
            try:
                prev_summary = summarize(rel, src, root.name) if src else None
            except SyntaxError:
                prev_summary = None
    sums_before = dict(sums_after)
    if prev_summary is not None:
        sums_before[rel] = prev_summary
    else:
        sums_before.pop(rel, None)  # unknown before-state: treated as a new file
    _save(cache_path, cache)

    before, after = Index(sums_before), Index(sums_after)
    lines: list[str] = []
    for c in new_cycles(import_graph(sums_before), import_graph(sums_after)):  # (a)
        added = ", ".join(f"{x} -> {y}" for x, y in c["new_edges"]) or "existing edges"
        lines.append(f"Tenthcheck: new import cycle {format_cycle(c['cycle'])} (added: {added})")
    sig_lines, new_pending = _sig_issues(before, after, rel, sess["pending"], list(exclude))  # (b)
    lines += sig_lines
    lines += _call_issues(before, after, rel)
    if dependents:  # (c)
        b = before.sums.get(rel) or {"defs": {}}
        a = after.sums[rel]
        for qual, dd in sorted(a["defs"].items()):
            od = b["defs"].get(qual)
            if dd["sig"] is None or (od and od.get("hash") == dd["hash"]) or [qual, rel] in sess["big"]:
                continue
            n = len({(c["rel"], c["fn"]) for c in after.callers(a["module"], qual)})
            if n >= dependents:
                lines.append(f"Tenthcheck: {a['module']}.{qual} has {n} direct callers (threshold {dependents})")
                sess["big"].append([qual, rel])
    pend = {(p["rel"], p["fn"], p["func"], p["n"], tuple(p["target"])): p for p in recheck_pending(after, sess["pending"])}
    for p in new_pending:
        pend[(p["rel"], p["fn"], p["func"], p["n"], tuple(p["target"]))] = p
    sess["pending"] = list(pend.values())
    _save(state_path, state)
    return _emit(lines, max_lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="tenthcheck hook", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="project root (default: $CLAUDE_PROJECT_DIR, else the payload's cwd)")
    ap.add_argument("--dependents", type=int, default=None, help="also report edited functions with >= N callers")
    ap.add_argument("--max-lines", type=int, default=10)
    ap.add_argument("--exclude", action="append", default=[],
                    help="glob of files whose call sites are not reported (still parsed), e.g. 'tests/*'; repeatable")
    a = ap.parse_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
        return run(payload, a.root, a.dependents, a.max_lines, a.exclude)
    except Exception:  # never interrupt the agent because of a bug here
        try:
            root = Path(a.root or os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
            with open(_state_dir(root) / "hook.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + traceback.format_exc() + "\n")
        except OSError:
            pass
        return 0

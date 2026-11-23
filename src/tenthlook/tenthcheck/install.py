"""`tenthcheck install --claude|--cursor` / `tenthcheck uninstall`: register the MCP server and/or the hook.

Safe by construction: the config is backed up before it is first changed, a unified diff is printed, running
install twice changes nothing, and uninstall removes only the entries this tool added.

Project scope only (files under the repo you run it in). Claude Code: `.mcp.json` (MCP server) and
`.claude/settings.json` (PostToolUse + Stop hook). Cursor: `.cursor/mcp.json` (MCP server). Cursor has no hook
registration here: its hook payloads are not the Claude Code ones the hook understands, and that is untested.
"""
from __future__ import annotations

import copy
import difflib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

HOOK_CMD = "tenthcheck hook"
MCP_ENTRY = {"type": "stdio", "command": "tenthcheck", "args": ["serve"]}
MATCHER = "Edit|Write|MultiEdit"
MANIFEST = Path(".tenthcheck") / "installed.json"


@dataclass
class Change:
    path: Path
    before: dict | None     # None: file did not exist
    after: dict | None      # None: file should be removed

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self, root: Path) -> str:
        a = json.dumps(self.before, indent=2).splitlines() if self.before is not None else []
        b = json.dumps(self.after, indent=2).splitlines() if self.after is not None else []
        rel = self.path.relative_to(root).as_posix()
        return "\n".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", lineterm="", n=2))


def _read(p: Path) -> dict | None:
    if not p.is_file():
        return None
    txt = p.read_text(encoding="utf-8")
    if not txt.strip():
        return {}
    data = json.loads(txt)  # a malformed config raises: we never overwrite what we cannot parse
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    return data


def _is_ours(h: dict) -> bool:
    return isinstance(h, dict) and str(h.get("command", "")).strip().startswith(HOOK_CMD)


def add_mcp(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("mcpServers", {}).setdefault("tenthcheck", dict(MCP_ENTRY))
    return cfg


def remove_mcp(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    servers = cfg.get("mcpServers", {})
    if isinstance(servers.get("tenthcheck"), dict) and servers["tenthcheck"].get("command") == "tenthcheck":
        del servers["tenthcheck"]
    if not servers:
        cfg.pop("mcpServers", None)
    return cfg


def add_hook(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    hooks = cfg.setdefault("hooks", {})
    for event, matcher in (("PostToolUse", MATCHER), ("Stop", None)):
        groups = hooks.setdefault(event, [])
        if any(_is_ours(h) for g in groups for h in g.get("hooks", [])):
            continue
        g = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 30}]}
        if matcher:
            g = {"matcher": matcher, **g}
        groups.append(g)
    return cfg


def remove_hook(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    hooks = cfg.get("hooks", {})
    for event in list(hooks):
        groups = hooks[event]
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not _is_ours(h)]
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        cfg.pop("hooks", None)
    return cfg


def plan(root: Path, target: str, uninstall: bool = False, mcp: bool = True, hook: bool = True) -> list[Change]:
    changes: list[Change] = []
    specs: list[tuple[Path, object, object]] = []
    if target == "claude":
        if mcp:
            specs.append((root / ".mcp.json", add_mcp, remove_mcp))
        if hook:
            specs.append((root / ".claude" / "settings.json", add_hook, remove_hook))
    elif target == "cursor":
        if mcp:
            specs.append((root / ".cursor" / "mcp.json", add_mcp, remove_mcp))
    else:
        raise ValueError(f"unknown target {target}")
    for path, adder, remover in specs:
        before = _read(path)
        if uninstall:
            if before is None:
                continue
            after = remover(before)
        else:
            after = adder(before or {})
        changes.append(Change(path, before, after))
    return changes


def _manifest(root: Path) -> dict:
    p = root / MANIFEST
    return json.loads(p.read_text()) if p.is_file() else {"created": []}


def _backup(p: Path) -> Path:
    n, b = 0, p.with_name(p.name + ".tenthcheck.bak")
    while b.exists():
        n += 1
        b = p.with_name(f"{p.name}.tenthcheck.bak{n}")
    shutil.copy2(p, b)
    return b


def apply(root: Path, changes: list[Change], uninstall: bool = False) -> list[str]:
    msgs = []
    man = _manifest(root)
    for c in changes:
        rel = c.path.relative_to(root).as_posix()
        if not c.changed:
            msgs.append(f"{rel}: no change ({'not installed' if uninstall else 'already installed'})")
            continue
        if c.before is not None:
            b = _backup(c.path)
            msgs.append(f"{rel}: backed up to {b.name}")
        if uninstall and not c.after and rel in man["created"]:
            c.path.unlink()
            man["created"].remove(rel)
            msgs.append(f"{rel}: removed (tenthcheck had created it)")
            continue
        c.path.parent.mkdir(parents=True, exist_ok=True)
        c.path.write_text(json.dumps(c.after, indent=2) + "\n", encoding="utf-8")
        if c.before is None and not uninstall and rel not in man["created"]:
            man["created"].append(rel)
        msgs.append(f"{rel}: {'cleaned' if uninstall else 'updated' if c.before is not None else 'created'}")
    (root / MANIFEST).parent.mkdir(exist_ok=True)
    (root / MANIFEST).write_text(json.dumps(man, indent=2), encoding="utf-8")
    return msgs


def preflight_warnings() -> list[str]:
    w = []
    if shutil.which("tenthcheck") is None:
        w.append("`tenthcheck` is not on PATH, so the agent cannot start it. Install with: pipx install tenthlook")
    try:
        import mcp  # noqa: F401
    except ImportError:
        w.append('the MCP server needs the extra: pipx install "tenthcheck[mcp]" (the hook does not)')
    return w

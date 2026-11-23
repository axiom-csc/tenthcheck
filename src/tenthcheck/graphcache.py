"""On-disk cache of parsed repo graphs, keyed by file content hashes.

A graph is cached under a key made of every .py file's content hash (plus the tenthcheck and Python versions).
Same files -> cache hit, no parsing. Any file differs -> that graph is rebuilt in full, because call and
reference resolution is cross-file, so one edited file can change edges elsewhere. We do not patch graphs.
A per-file (mtime, size) -> sha256 table means unchanged files are not re-read between runs.
Cache lives in `.tenthcheck/cache/` (self-ignoring); at most MAX_GRAPHS graphs are kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
from pathlib import Path

from tenthcheck import __version__
from tenthcheck.graph import Graph
from tenthcheck.parser import iter_py_files, parse_package

MAX_GRAPHS = 4
_FORMAT = 1


def _dir(root: Path) -> Path:
    d = root / ".tenthcheck" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    gi = root / ".tenthcheck" / ".gitignore"
    if not gi.exists():
        gi.write_text("# Tenthcheck's local state; never commit it.\n*\n", encoding="utf-8")
    return d


def _file_hashes(root: Path, d: Path) -> dict[str, str]:
    """rel path -> sha256 of content, re-reading only files whose (mtime_ns, size) changed."""
    tbl_path = d / "filehashes.json"
    try:
        tbl = json.loads(tbl_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tbl = {}
    out: dict[str, str] = {}
    new: dict[str, list] = {}
    for p in iter_py_files(root):
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        ent = tbl.get(rel)
        if ent and ent[0] == st.st_mtime_ns and ent[1] == st.st_size:
            h = ent[2]
        else:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        out[rel] = h
        new[rel] = [st.st_mtime_ns, st.st_size, h]
    if new != tbl:
        tmp = tbl_path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(new), encoding="utf-8")
        os.replace(tmp, tbl_path)
    return out


def _key(hashes: dict[str, str], overrides: dict[str, str | None]) -> str:
    h = dict(hashes)
    for rel, src in overrides.items():
        if src is None:
            h.pop(rel, None)
        else:
            h[rel] = hashlib.sha256(src.encode("utf-8")).hexdigest()
    m = hashlib.sha256(f"{_FORMAT}|{__version__}|{sys.version_info[:2]}".encode())
    for rel in sorted(h):
        m.update(f"{rel}\0{h[rel]}\n".encode())
    return m.hexdigest()[:24]


def load_graph(repo_dir: str | Path, overrides: dict[str, str | None] | None = None) -> tuple[Graph, bool]:
    """Parsed graph of the repo (with `overrides` as in `parse_package`) and whether it came from the cache."""
    root = Path(repo_dir).resolve()
    overrides = {Path(k).as_posix(): v for k, v in (overrides or {}).items()}
    try:
        d = _dir(root)
        key = _key(_file_hashes(root, d), overrides)
    except OSError:  # read-only checkout: just parse
        return parse_package(str(root), skip_syntax_errors=True, overrides=overrides or None), False
    path = d / f"graph-{key}.pkl"
    if path.is_file():
        try:
            g = pickle.loads(path.read_bytes())
            path.touch()
            return g, True
        except Exception:  # corrupt or from an incompatible build: rebuild
            pass
    g = parse_package(str(root), skip_syntax_errors=True, overrides=overrides or None)
    try:
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(pickle.dumps(g, protocol=5))
        os.replace(tmp, path)
        old = sorted(d.glob("graph-*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)[MAX_GRAPHS:]
        for p in old:
            p.unlink(missing_ok=True)
    except OSError:
        pass
    return g, False

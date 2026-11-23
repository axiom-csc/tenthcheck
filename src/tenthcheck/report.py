"""Terminal report for `tenthcheck` (scan). High contrast; colour only on a TTY and without NO_COLOR."""
from __future__ import annotations

import os
import sys

from tenthcheck import __version__
from tenthcheck.config import Config
from tenthcheck.mining import Invariant, ScanResult

_KIND = {"route-guard": "missing guard", "call-pair": "unpaired call", "guard-mutation": "unguarded decrease",
         "sql-scope": "unscoped SQL"}


def _c(code: str, s: str, on: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if on else s


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return stream.isatty() and "NO_COLOR" not in os.environ


def visible(res: ScanResult, cfg: Config) -> list[Invariant]:
    rej = cfg.rejected()
    return [i for i in res.findings if i.id not in rej]


def format_scan(res: ScanResult, cfg: Config, color: bool = False, limit: int = 20) -> str:
    fs = visible(res, cfg)
    suppressed = len(res.findings) - len(fs)
    head = (f"Tenthcheck {__version__}: scanned {res.files} files in {res.seconds:.1f}s, "
            f"{len(res.invariants)} patterns mined, {len(fs)} candidate inconsistencies")
    out = [_c("1", head, color)]
    if suppressed:
        out.append(f"({suppressed} suppressed by tenthcheck.toml)")
    if res.skipped:
        out.append(f"({len(res.skipped)} files skipped: syntax errors)")
    out.append("")
    if not fs:
        out.append("No candidate inconsistencies at the current thresholds.")
    for n, i in enumerate(fs[:limit], 1):
        status = cfg.status.get(i.id)
        tag = f"  [{status}]" if status else ""
        out.append(_c("1;33", f"[{n}] {_KIND.get(i.kind, i.kind)}", color)
                   + f"  confidence {i.confidence} ({i.follow} of {i.total} places = {i.consistency:.0%}){tag}")
        out.append(f"    pattern: {i.pattern}")
        ex = ", ".join(str(s) for s in i.followers[:3])
        out.append(f"    followed in {i.follow} places, e.g. {ex}")
        for e in i.exceptions[:5]:
            out.append(_c("1;31", "    inconsistent here: ", color) + f"{e}"
                       + f"  (inconsistent with {i.follow} other places)")
        if len(i.exceptions) > 5:
            out.append(f"    ... and {len(i.exceptions) - 5} more")
        out.append("")
    if len(fs) > limit:
        out.append(f"... {len(fs) - limit} more (use --limit or --json)")
    out.append("These are candidates: places that differ from what the rest of the code does. "
               "Some are intentional; review before changing anything.")
    if not cfg.path:
        out.append("Run `tenthcheck init` to accept or reject patterns (accepted ones are enforced in the hook and CI).")
    return "\n".join(out)

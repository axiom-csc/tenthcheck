"""Enforce accepted invariants from tenthcheck.toml: mined (exceptions) and hand-written (Z3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tenthlook.tenthcheck import mining
from tenthlook.tenthcheck.config import Config


@dataclass
class Enforcement:
    mined_violations: list[tuple[mining.Invariant, mining.Site]] = field(default_factory=list)
    z3_results: list = field(default_factory=list)
    scan: mining.ScanResult | None = None
    seconds: float = 0.0

    @property
    def counterexamples(self) -> list:
        return [r for r in self.z3_results if r.status == "violated"]

    @property
    def failed(self) -> bool:
        return bool(self.mined_violations or self.counterexamples)


def enforce(root: str | Path, cfg: Config, files: set[str] | None = None, scan: mining.ScanResult | None = None) -> Enforcement:
    """`files`: restrict to violations located in these repo-relative files."""
    import time
    t0 = time.perf_counter()
    out = Enforcement()
    accepted = cfg.accepted()
    if accepted:
        scan = scan or mining.scan(root, cfg.min_support, cfg.min_consistency, exclude=cfg.exclude)
        out.scan = scan
        for inv in scan.invariants:
            if inv.id in accepted:
                for e in inv.exceptions:
                    if files is None or e.file in files:
                        out.mined_violations.append((inv, e))
    for h in cfg.handwritten:
        if files is None or h.file in files:
            from tenthlook.tenthcheck import z3check
            out.z3_results.extend(z3check.check_handwritten(root, _only(cfg, h)))
    out.seconds = time.perf_counter() - t0
    return out


def _only(cfg: Config, h) -> Config:
    c = Config(handwritten=[h])
    return c


def describe_violation(inv: mining.Invariant, site: mining.Site) -> str:
    return (f"{site}: inconsistent with {inv.follow} other places that follow the accepted rule "
            f"\"{inv.pattern}\" ({inv.id})")


def describe_counterexample(r) -> str:
    ins = ", ".join(f"{k}={v}" for k, v in r.counterexample.items())
    outs = ", ".join(f"{k}={v}" for k, v in r.outcome.items())
    return (f"{r.file}:{r.line} {r.function}(): invariant `{r.check}` can be violated. "
            f"Counterexample: {ins} -> {outs}")

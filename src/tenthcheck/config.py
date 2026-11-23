"""tenthcheck.toml: mined invariants to accept/reject, plus hand-written numeric invariants."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore

from tenthcheck.mining import DEFAULT_MIN_CONSISTENCY, DEFAULT_MIN_SUPPORT, Invariant

FILENAME = "tenthcheck.toml"
STATUSES = ("pending", "accepted", "rejected")


@dataclass
class Handwritten:
    file: str
    function: str
    check: str
    assume: list[str] = field(default_factory=list)


@dataclass
class Config:
    min_support: int = DEFAULT_MIN_SUPPORT
    min_consistency: float = DEFAULT_MIN_CONSISTENCY
    exclude: list[str] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)       # mined invariant id -> status
    handwritten: list[Handwritten] = field(default_factory=list)
    path: Path | None = None

    def accepted(self) -> set[str]:
        return {k for k, v in self.status.items() if v == "accepted"}

    def rejected(self) -> set[str]:
        return {k for k, v in self.status.items() if v == "rejected"}


def load(root: str | Path) -> Config:
    p = Path(root) / FILENAME
    if not p.is_file():
        return Config()
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    s = data.get("settings", {})
    cfg = Config(min_support=int(s.get("min_support", DEFAULT_MIN_SUPPORT)),
                 min_consistency=float(s.get("min_consistency", DEFAULT_MIN_CONSISTENCY)),
                 exclude=list(s.get("exclude", [])), path=p)
    for m in data.get("mined", []):
        st = m.get("status", "pending")
        if st not in STATUSES:
            raise ValueError(f"{p}: status must be one of {STATUSES}, got {st!r} for {m.get('id')}")
        cfg.status[m["id"]] = st
    for h in data.get("invariant", []):
        cfg.handwritten.append(Handwritten(h["file"], h["function"], h["check"], list(h.get("assume", []))))
    return cfg


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings


def render(invs: list[Invariant], cfg: Config) -> str:
    out = ["# tenthcheck.toml: Z3 invariants ([[invariant]]) and, if you opted in to experimental mining, mined candidates." if not invs
           else "# tenthcheck.toml: review each mined entry below (experimental).",
           "#   status = \"accepted\"  -> enforced: `tenthcheck ci` and the agent hook report any place that breaks it",
           "#   status = \"rejected\"  -> suppressed: never shown again",
           "#   status = \"pending\"   -> shown in `tenthcheck`, not enforced",
           "# Mined entries are CANDIDATES the code mostly follows. They are not proofs.", "",
           "[settings]", f"min_support = {cfg.min_support}        # smallest number of places that must follow a pattern",
           f"min_consistency = {cfg.min_consistency}  # smallest share of places that must follow it",
           f"exclude = {json.dumps(cfg.exclude)}", ""]
    for i in invs:
        out += ["[[mined]]", f"id = {_q(i.id)}", f"status = {_q(cfg.status.get(i.id, 'pending'))}",
                f"rule = {_q(i.pattern)}",
                f"evidence = {_q(f'{i.follow} of {i.total} places follow it ({i.consistency:.0%}), confidence {i.confidence}')}"]
        if i.exceptions:
            out.append("exceptions = [" + ", ".join(_q(str(e)) for e in i.exceptions[:20]) + "]")
        out.append("")
    out += ["# Hand-written numeric invariants, checked with Z3 on simple straight-line functions.",
            "# Write checks in plain Python. `assume` lists what you know is true on entry.",
            "# [[invariant]]", '# file = "bank.py"', '# function = "withdraw"', '# check = "final_balance >= 0"',
            '# assume = ["balance >= 0", "amount > 0", "fee >= 0"]', ""]
    for h in cfg.handwritten:
        out += ["[[invariant]]", f"file = {_q(h.file)}", f"function = {_q(h.function)}", f"check = {_q(h.check)}",
                "assume = " + json.dumps(h.assume), ""]
    return "\n".join(out)


def init(root: str | Path, invs: list[Invariant], force: bool = False) -> tuple[Path, str]:
    """Write tenthcheck.toml. Existing statuses and hand-written invariants are kept (re-init merges)."""
    p = Path(root) / FILENAME
    cfg = load(root) if p.is_file() else Config()
    existing = p.is_file()
    text = render(invs, cfg)
    p.write_text(text, encoding="utf-8")
    return p, ("updated (kept your statuses and hand-written invariants)" if existing else "created")

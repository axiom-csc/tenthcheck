"""Signed CI receipts, built on tenthlook.core (Ed25519, hash-chained receipt format shared with Panel).

A receipt records WHAT was checked, on which commit, with which config, and the counts that came out. It does not say
the code is correct. Signing needs only the `cryptography` dependency that ships with tenthlook.

Mapping onto the core schema: subject_type=git_diff; artifact_sha256 = SHA-256 of the checked commit id (utf-8 text);
policy_sha256 = SHA-256 of tenthcheck.toml (SHA-256 of "" when there is none); trace_sha256 = hash of the canonical
summary counts; reasons carry commit=, config= and each count as key=value.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

KEY_ENV = "TENTHCHECK_SIGNING_KEY"  # legacy name, still honoured; core also reads TENTHLOOK_KEY_FILE / TENTHLOOK_SIGNING_KEY
CHECKER = "tenthcheck"


def _core():
    from tenthlook.core import crypto, receipt
    return crypto, receipt


def keygen() -> tuple[str, str]:
    """(private seed b64, public key b64). For CI secrets; `tenthlook keygen` writes a key file instead."""
    crypto, _ = _core()
    return crypto.generate_keypair()


def _git_head(root: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _artifact_hash(commit: str | None) -> str:
    crypto, _ = _core()
    return crypto.sha256_hex(commit or "")


def _policy_hash(config_text: str | None) -> str:
    crypto, _ = _core()
    return crypto.sha256_hex(config_text or "")


def _verdict(summary: dict) -> str:
    if summary.get("z3_counterexamples", 0) > 0:
        return "Z3_COUNTEREXAMPLE"
    if summary.get("accepted_violations", 0) > 0:
        return "BLOCKED"
    return "ALLOWED"  # "no violations of accepted invariants found": not "proven"


def build_receipt(root: Path, summary: dict, config_text: str | None, signing_key: str) -> dict:
    from tenthlook.tenthcheck import __version__
    crypto, core = _core()
    commit = _git_head(root)
    counts = {k: v for k, v in summary.items() if isinstance(v, int) and not isinstance(v, bool)}
    reasons = [f"commit={commit or 'none'}", f"config={'tenthcheck.toml' if config_text is not None else 'none'}",
               *(f"{k}={v}" for k, v in summary.items() if not isinstance(v, (list, dict))),
               "statement: these checks were run and reported these counts; not a statement that the code is correct"]
    return core.create(subject_type="git_diff", artifact_sha256=_artifact_hash(commit), policy_sha256=_policy_hash(config_text),
                       checker_name=CHECKER, checker_version=__version__, verdict=_verdict(summary), reasons=reasons,
                       trace_sha256=crypto.hash_json(counts), signing_key=signing_key)


def _signing_key() -> str | None:
    crypto, _ = _core()
    legacy = (os.environ.get(KEY_ENV) or "").strip()
    try:
        if legacy:
            crypto.public_from_private(legacy)  # validate; do not copy into the process environment
            return legacy
        return crypto.load_signing_key()
    except crypto.KeyMissing:
        return None
    except ValueError as e:
        raise RuntimeError(f"{KEY_ENV} is not a base64 32-byte Ed25519 seed") from e


def write_receipt(path: Path, root: Path, summary: dict, config_text: str | None) -> bool:
    """False (nothing written) when no signing key is configured. There is no default key."""
    key = _signing_key()
    if not key:
        return False
    path.write_text(json.dumps(build_receipt(root, summary, config_text, key), indent=2), encoding="utf-8")
    return True


def verify(receipt_path: Path, public_key_b64: str | None = None, repo: Path | None = None) -> tuple[bool, list[str]]:
    """(ok, messages). With no pinned public key only integrity is shown: anyone can sign with their own key."""
    _, core = _core()
    try:
        r = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, [f"FAIL: cannot read receipt: {e}"]
    if isinstance(r, dict) and "payload" in r:
        return False, ["FAIL: this is a pre-tenthlook.core receipt (payload/signature layout); re-run `tenthcheck ci --receipt`"]
    res = core.verify(r, public_key_b64)
    if not res.ok:
        return False, [f"FAIL: {m}" for m in res.errors]
    msgs = ["OK: signature valid", *[f"note: {m}" for m in res.notes]]
    ok = True
    if r["checker"]["name"] != CHECKER or r["subject_type"] != "git_diff":
        ok = False
        msgs.append(f"FAIL: not a tenthcheck receipt (checker {r['checker']['name']!r}, subject {r['subject_type']!r})")
    if repo is not None:
        head = _git_head(repo)
        if _artifact_hash(head) != r["artifact_sha256"]:
            ok = False
            msgs.append(f"FAIL: receipt is for a different commit than the repo's HEAD ({head})")
        cfg = repo / "tenthcheck.toml"
        if _policy_hash(cfg.read_text() if cfg.is_file() else None) != r["policy_sha256"]:
            ok = False
            msgs.append("FAIL: tenthcheck.toml differs from the one that was checked")
    msgs.append(f"verdict: {r['verdict']}; " + ", ".join(x for x in r["reasons"] if "=" in x and not x.startswith("statement")))
    msgs.append("reminder: a receipt shows what was checked, not that the code is correct")
    return ok, msgs

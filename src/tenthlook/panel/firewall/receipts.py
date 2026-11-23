"""Signed, hash-chained receipts, built directly on tenthlook-core (Ed25519, SHA-256, shared receipt schema).

A receipt proves WHAT PANEL CHECKED AND DECIDED (which action hash, under which policy-file hash, with what verdict) and that
the record was NOT ALTERED since Panel signed it. Receipts in one ledger are chained (`prev_receipt_sha256`), so deleting or
editing one that is followed by another is detectable offline (`usepanel verify-chain`, `tenthlook verify-chain`). It is NOT
a proof that the action was safe or correct.

Mapping to the core schema: subject_type=mcp_tool_call; artifact_sha256 = Action.sha(); policy_sha256 = SHA-256 of the policy
file; verdict allow/needs_consent/block -> ALLOWED/NEEDS_CONSENT/BLOCKED; reasons = Panel's reasons plus `outcome=`, `tool=`,
`result_id=`, `approved_by=`, `policy_version=`, `idempotency_key=`; trace_sha256 = SHA-256 of Panel's canonical JSON of the
detail record (full action, conversation, timestamp), which the ledger stores next to the receipt (`Ledger.receipt_details`).

The private key is created by `usepanel keygen`, lives outside the repo (mode 0600), and there is no default key: Panel refuses to
start without one. Keys written by earlier versions (PEM) are still read.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from tenthlook.core import crypto
from tenthlook.core import receipt as core

from tenthlook.panel.firewall.types import Action, FirewallError, Verdict, canonical, sha256_hex

RECEIPT_VERSION = core.VERSION
DEFAULT_KEY = "~/.config/panel/receipt_ed25519.pem"
CHECKER = "panel-firewall"
CHECKER_VERSION = "0.1.0"
_VERDICT = {"allow": "ALLOWED", "needs_consent": "NEEDS_CONSENT", "block": "BLOCKED"}


def _inside_git_worktree(p: Path) -> bool:
    return crypto.inside_git_worktree(p.parent)


def generate_key(path: str | Path, force: bool = False) -> Path:
    p = Path(path).expanduser().resolve()
    try:
        crypto.write_new_key_file(p, force=force, pub_file=True)
    except crypto.KeyRefused as e:
        raise FirewallError(f"refusing to store a signing key inside a git work tree ({p}); choose a path outside the repo") from e
    except FileExistsError as e:
        raise FirewallError(f"{p} already exists; not overwriting (use --force to replace it)") from e
    return p


def _pem_to_seed(data: bytes) -> str:
    key = serialization.load_pem_private_key(data, password=None)
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode()


@dataclass
class Signer:
    seed: str
    key_id: str
    path: str

    @classmethod
    def load(cls, path: str | Path | None) -> "Signer":
        p = Path(path or DEFAULT_KEY).expanduser()
        if not p.is_file():
            raise FirewallError(f"no signing key at {p}. Run `usepanel keygen` first (Panel has no default key and will not start without one).")
        try:
            raw = p.read_bytes()
            if raw.lstrip().startswith(b"-----BEGIN"):          # key from an earlier Panel version
                import os, stat
                if stat.S_IMODE(os.stat(p).st_mode) & 0o077:
                    raise FirewallError(f"{p} is readable by others; run `chmod 600 {p}`")
                seed = _pem_to_seed(raw)
            else:
                seed = crypto.load_signing_key(p)
        except crypto.KeyRefused as e:
            raise FirewallError(str(e).replace("readable by group/others", "readable by others")) from e
        except FirewallError:
            raise
        except Exception as e:
            raise FirewallError(f"{p}: not a usable Ed25519 private key ({type(e).__name__})") from e
        return cls(seed, crypto.public_from_private(seed)[:16].replace("/", "_"), str(p))

    @property
    def pubkey(self) -> str:
        return crypto.public_from_private(self.seed)


def receipt_id(receipt: dict) -> str:
    """Short citable id: "rcpt_" + the first 12 hex of the receipt's chain hash. Agents cite it; claims.py checks it against the ledger."""
    return "rcpt_" + core.receipt_hash(receipt)[:12]


def detail_record(action: Action, idem_key: str, policy_version: str, outcome: str, result_id, ts: float, approved_by) -> dict:
    return {"tool": action.tool, "action": {"tool": action.tool, "args": action.args}, "conversation_id": action.conversation_id,
            "idempotency_key": idem_key, "policy_version": policy_version, "outcome": outcome, "result_id": result_id,
            "approved_by": approved_by, "timestamp": ts}


def make_receipt(signer: Signer, action: Action, idem_key: str, policy_sha: str, policy_version: str, verdict: Verdict,
                 outcome: str, result_id: str | None, ts: float, approved_by: str | None = None,
                 prev_receipt_sha256: str = core.GENESIS) -> tuple[dict, dict]:
    """(core receipt, detail record). The caller stores both together and supplies the ledger's current chain head."""
    detail = detail_record(action, idem_key, policy_version, outcome, result_id, ts, approved_by)
    reasons = [*verdict.reasons, f"outcome={outcome}", f"tool={action.tool}", f"policy_version={policy_version}",
               f"idempotency_key={idem_key}"]
    if result_id is not None:
        reasons.append(f"result_id={result_id}")
    if approved_by:
        reasons.append(f"approved_by={approved_by}")
    r = core.create(subject_type="mcp_tool_call", artifact_sha256=action.sha(), policy_sha256=policy_sha, checker_name=CHECKER,
                    checker_version=CHECKER_VERSION, verdict=_VERDICT[verdict.decision], reasons=reasons,
                    trace_sha256=sha256_hex(canonical(detail)), prev_receipt_sha256=prev_receipt_sha256,
                    signing_key=signer.seed, timestamp_utc=_iso(ts))
    return r, detail


def _iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_public_key(path: str | Path) -> str:
    """The public key as base64 (what receipts carry). Reads `.pub` files from `usepanel keygen` (base64, or PEM from earlier
    versions) or a private key file."""
    p = Path(path).expanduser()
    try:
        data = p.read_bytes()
        if b"PRIVATE" in data:
            return crypto.public_from_private(_pem_to_seed(data))
        if data.lstrip().startswith(b"-----BEGIN"):
            raw = serialization.load_pem_public_key(data).public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return base64.b64encode(raw).decode()
        text = data.decode().strip()
        base64.b64decode(text, validate=True)
        return text
    except Exception as e:
        raise FirewallError(f"{p}: not a usable Ed25519 key") from e


def verify_receipt(receipt: dict, pub: str, policy_file: str | Path | None = None, detail: dict | None = None) -> list[str]:
    """Returns a list of problems; empty means: signature valid and made by `pub`, and (if given) the policy file's SHA-256
    matches and the detail record matches the receipt's action and trace hashes. Says nothing about whether the action was a
    good idea."""
    if not isinstance(receipt, dict) or core.validate_shape(receipt):
        return ["malformed receipt"]
    res = core.verify(receipt, pub)
    if not res.ok:
        return ["signature INVALID: the receipt was altered or signed by another key"]
    problems = []
    if policy_file is not None and sha256_hex(Path(policy_file).read_bytes()) != receipt["policy_sha256"]:
        problems.append("policy file hash differs from the one in the receipt")
    if detail is not None:
        if sha256_hex(canonical(detail)) != receipt["trace_sha256"]:
            problems.append("detail record does not match trace_sha256")
        if Action(detail["action"]["tool"], detail["action"]["args"]).sha() != receipt["artifact_sha256"]:
            problems.append("action in the detail record does not match artifact_sha256")
    return problems


def verify_chain(receipts: list[dict], pub: str | None = None, expect_head: str | None = None, expect_count: int | None = None):
    """tenthlook-core chain check over a ledger's receipts, oldest first (a ChainResult)."""
    from tenthlook.core import store
    return store.verify_receipts(receipts, pub, expect_head, expect_count)


receipt_hash = core.receipt_hash
GENESIS = core.GENESIS

"""The receipt: one signed statement that a named checker, with a hashed policy, reached a verdict on a hashed artifact.

It proves who signed what and that it was not altered afterwards. It does not prove the artifact is safe or correct.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from dataclasses import dataclass, field

from . import crypto

VERSION = 1
GENESIS = "0" * 64
SUBJECT_TYPES = ("mcp_tool_call", "git_diff", "sql_query")
VERDICTS = ("ALLOWED", "BLOCKED", "NEEDS_CONSENT", "Z3_VERIFIED", "Z3_COUNTEREXAMPLE", "NOT_CHECKABLE")
FIELDS = ("version", "receipt_id", "timestamp_utc", "subject_type", "artifact_sha256", "policy_sha256", "checker",
          "verdict", "reasons", "trace_sha256", "prev_receipt_sha256", "pubkey", "signature")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")


class ReceiptError(ValueError):
    pass


def now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_shape(r: dict) -> list[str]:
    """Schema problems (empty list = well-formed). Does not check the signature."""
    e: list[str] = []
    if not isinstance(r, dict):
        return ["receipt is not an object"]
    for k in sorted(set(r) - set(FIELDS)):
        e.append(f"unknown field {k!r}")
    for k in FIELDS:
        if k not in r:
            e.append(f"missing field {k!r}")
    if e:
        return e
    if r["version"] != VERSION:
        e.append(f"unsupported version {r['version']!r}")
    if not isinstance(r["receipt_id"], str) or not r["receipt_id"]:
        e.append("receipt_id must be a non-empty string")
    if not isinstance(r["timestamp_utc"], str) or not _TS.match(r["timestamp_utc"]):
        e.append("timestamp_utc must be ISO-8601 UTC ending in Z")
    if r["subject_type"] not in SUBJECT_TYPES:
        e.append(f"subject_type must be one of {SUBJECT_TYPES}")
    for k in ("artifact_sha256", "policy_sha256", "prev_receipt_sha256"):
        if not (isinstance(r[k], str) and _HEX64.match(r[k])):
            e.append(f"{k} must be 64 lowercase hex characters")
    if r["trace_sha256"] is not None and not (isinstance(r["trace_sha256"], str) and _HEX64.match(r["trace_sha256"])):
        e.append("trace_sha256 must be null or 64 lowercase hex characters")
    c = r["checker"]
    if not (isinstance(c, dict) and set(c) == {"name", "version"} and all(isinstance(v, str) and v for v in c.values())):
        e.append("checker must be {name, version} (non-empty strings)")
    if r["verdict"] not in VERDICTS:
        e.append(f"verdict must be one of {VERDICTS}")
    if not (isinstance(r["reasons"], list) and all(isinstance(x, str) for x in r["reasons"])):
        e.append("reasons must be a list of strings")
    for k in ("pubkey", "signature"):
        if not isinstance(r[k], str):
            e.append(f"{k} must be a string")
    return e


def signing_bytes(r: dict) -> bytes:
    """What is signed: every field except the signature itself."""
    return crypto.canonical_json({k: v for k, v in r.items() if k != "signature"})


def receipt_hash(r: dict) -> str:
    """Identity used by the chain: SHA-256 of the whole receipt, signature included."""
    return crypto.hash_json(r)


def create(*, subject_type: str, artifact_sha256: str, policy_sha256: str, checker_name: str, checker_version: str,
           verdict: str, reasons: list[str] | None = None, trace_sha256: str | None = None,
           prev_receipt_sha256: str = GENESIS, signing_key: str, timestamp_utc: str | None = None,
           receipt_id: str | None = None) -> dict:
    """Build and sign a receipt. `signing_key` is the private seed (see crypto.load_signing_key); it is required."""
    if not signing_key:
        raise crypto.KeyMissing("refusing to create a receipt without a signing key")
    r = {"version": VERSION, "receipt_id": receipt_id or str(uuid.uuid4()), "timestamp_utc": timestamp_utc or now_utc(),
         "subject_type": subject_type, "artifact_sha256": artifact_sha256, "policy_sha256": policy_sha256,
         "checker": {"name": checker_name, "version": checker_version}, "verdict": verdict,
         "reasons": list(reasons or []), "trace_sha256": trace_sha256, "prev_receipt_sha256": prev_receipt_sha256,
         "pubkey": crypto.public_from_private(signing_key), "signature": ""}
    problems = validate_shape(r)
    if problems:
        raise ReceiptError("; ".join(problems))
    r["signature"] = crypto.sign_bytes(signing_key, signing_bytes(r))
    return r


@dataclass
class Verification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def verify(r: dict, pinned_pubkey: str | None = None) -> Verification:
    """Offline check: well-formed, signed by `pubkey`, and (if given) `pubkey` is the pinned one.

    Without a pinned key this only shows the receipt is unmodified since SOME key signed it; anyone can make their own key."""
    errs = validate_shape(r)
    if errs:
        return Verification(False, errs)
    out = Verification(True)
    if pinned_pubkey is not None and pinned_pubkey != r["pubkey"]:
        out.ok = False
        out.errors.append("signed by a different key than the pinned one")
        return out
    if not crypto.verify_bytes(r["pubkey"], r["signature"], signing_bytes(r)):
        out.ok = False
        out.errors.append("signature does not match the receipt (altered, or wrong key)")
        return out
    if pinned_pubkey is None:
        out.notes.append("no pinned public key: this shows the receipt is unaltered, not who signed it")
    return out

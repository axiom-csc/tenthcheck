"""Signed receipts (Ed25519). A receipt records WHAT was checked, on which commit, with which config, and
the counts that came out. It does not say the code is correct: it says a check ran and what it reported.
Needs the optional extra: pip install "tenthcheck[receipt]" (the `cryptography` package)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

KEY_ENV = "TENTHCHECK_SIGNING_KEY"


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as e:  # pragma: no cover
        raise RuntimeError('receipts need the extra: pip install "tenthcheck[receipt]"') from e
    return InvalidSignature, serialization, Ed25519PrivateKey, Ed25519PublicKey


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def keygen() -> tuple[str, str]:
    """(private seed b64, public key b64)."""
    _, ser, Priv, _ = _crypto()
    k = Priv.generate()
    raw = lambda x, **kw: base64.b64encode(x).decode()
    priv = k.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    pub = k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return raw(priv), raw(pub)


def _git_head(root: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def build_payload(root: Path, summary: dict, config_text: str | None) -> dict:
    from tenthcheck import __version__
    return {"schema": 1, "tool": f"tenthcheck {__version__}", "commit": _git_head(root),
            "config_sha256": hashlib.sha256(config_text.encode()).hexdigest() if config_text is not None else None,
            "summary": summary,
            "statement": "These checks were run and reported these counts. This is not a statement that the code is correct."}


def sign(payload: dict, key_b64: str) -> dict:
    _, ser, Priv, _ = _crypto()
    k = Priv.from_private_bytes(base64.b64decode(key_b64))
    pub = k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return {"payload": payload, "public_key": base64.b64encode(pub).decode(),
            "signature": base64.b64encode(k.sign(canonical(payload))).decode()}


def write_receipt(path: Path, root: Path, summary: dict, config_text: str | None) -> bool:
    key = os.environ.get(KEY_ENV)
    if not key:
        return False
    path.write_text(json.dumps(sign(build_payload(root, summary, config_text), key), indent=2), encoding="utf-8")
    return True


def verify(receipt_path: Path, public_key_b64: str | None = None, repo: Path | None = None) -> tuple[bool, list[str]]:
    """(ok, messages). With no pinned public key only integrity is shown: anyone can sign with their own key."""
    InvalidSignature, _, _, Pub = _crypto()
    msgs: list[str] = []
    r = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    pub = public_key_b64 or r["public_key"]
    if public_key_b64 and public_key_b64 != r["public_key"]:
        return False, ["FAIL: receipt was signed by a different key than the pinned one"]
    try:
        Pub.from_public_bytes(base64.b64decode(pub)).verify(base64.b64decode(r["signature"]), canonical(r["payload"]))
    except (InvalidSignature, ValueError):
        return False, ["FAIL: signature does not match the payload (tampered or wrong key)"]
    msgs.append("OK: signature valid")
    if not public_key_b64:
        msgs.append("note: no --public-key given, so this only shows the file is unmodified, not who signed it")
    ok = True
    if repo is not None:
        head = _git_head(repo)
        if head != r["payload"].get("commit"):
            ok = False
            msgs.append(f"FAIL: receipt is for commit {r['payload'].get('commit')}, repo is at {head}")
        cfg = repo / "tenthcheck.toml"
        want = r["payload"].get("config_sha256")
        have = hashlib.sha256(cfg.read_text().encode()).hexdigest() if cfg.is_file() else None
        if want != have:
            ok = False
            msgs.append("FAIL: tenthcheck.toml differs from the one that was checked")
    s = r["payload"]["summary"]
    msgs.append("checked: " + ", ".join(f"{k}={v}" for k, v in s.items() if not isinstance(v, (list, dict))))
    msgs.append("reminder: a receipt shows what was checked, not that the code is correct")
    return ok, msgs

"""Canonical JSON, SHA-256, Ed25519 keys.

Canonical JSON
--------------
`canonical_json` approximates RFC 8785 (JCS) for the subset of JSON this library signs: objects, arrays, strings,
booleans, null and *integers*. Where it matches JCS: keys sorted by UTF-16 code unit, no whitespace, minimal string
escaping, UTF-8 output. Where it differs, on purpose:

* Floats are REJECTED (JCS defines ES6 number formatting; reimplementing it identically in every language is the
  classic way signatures stop verifying). Put decimals in strings, money in minor units.
* Integers outside +/-(2**53-1) are rejected (they do not survive JavaScript).
* NaN/Infinity are rejected (not JSON).
* Strings with lone surrogates are rejected (JCS says to reject them too, but Python and JS disagree about
  what they emit).
* Keys must be strings; duplicate keys cannot occur in a dict.

So: anything this accepts is canonical under JCS, but this does NOT accept everything JCS does. A TypeScript port only
needs sorted keys, `JSON.stringify` for strings/ints, and `undefined` dropped (see the conformance fixtures).

Keys
----
There are no default keys and no key in any repo. A signing key is a 32-byte Ed25519 seed, base64, read from a file
(`TENTHLOOK_KEY_FILE` or an explicit path) or, for CI secrets, from `TENTHLOOK_SIGNING_KEY`. If neither is given,
signing raises `KeyMissing`. Key files must be outside any git work tree and not group/world readable.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

KEY_FILE_ENV = "TENTHLOOK_KEY_FILE"
KEY_ENV = "TENTHLOOK_SIGNING_KEY"
MAX_SAFE_INT = 2**53 - 1


class KeyMissing(RuntimeError):
    """No signing key was provided. There is deliberately no fallback."""


class KeyRefused(RuntimeError):
    """A key file is somewhere it must not be (inside a repo) or is too readable."""


# ---------------------------------------------------------------- canonical JSON

def _utf16_key(s: str) -> bytes:
    return s.encode("utf-16-be", "surrogatepass")


def _enc(v, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, int):
        if abs(v) > MAX_SAFE_INT:
            raise ValueError(f"integer {v} is outside the safe range")
        out.append(str(v))
    elif isinstance(v, float):
        raise TypeError("floats are not allowed in canonical JSON; use an integer or a string")
    elif isinstance(v, str):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError as e:
            raise ValueError("string contains a lone surrogate") from e
        out.append(json.dumps(v, ensure_ascii=False))
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, x in enumerate(v):
            if i:
                out.append(",")
            _enc(x, out)
        out.append("]")
    elif isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise TypeError("object keys must be strings")
        out.append("{")
        for i, k in enumerate(sorted(v, key=_utf16_key)):
            if i:
                out.append(",")
            _enc(k, out)
            out.append(":")
            _enc(v[k], out)
        out.append("}")
    else:
        raise TypeError(f"cannot canonicalize {type(v).__name__}")


def canonical_json(obj) -> bytes:
    out: list[str] = []
    _enc(obj, out)
    return "".join(out).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def hash_json(obj) -> str:
    return sha256_hex(canonical_json(obj))


# ---------------------------------------------------------------- keys

def _lib():
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    return InvalidSignature, serialization, Ed25519PrivateKey, Ed25519PublicKey


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """(private seed b64, public key b64). Nothing is written anywhere."""
    _, ser, Priv, _ = _lib()
    k = Priv.generate()
    return (_b64(k.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())),
            _b64(k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)))


def public_from_private(seed_b64: str) -> str:
    _, ser, Priv, _ = _lib()
    k = Priv.from_private_bytes(base64.b64decode(seed_b64, validate=True))
    return _b64(k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw))


def inside_git_worktree(path: Path) -> bool:
    p = path.resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return True
    return False


def check_key_path(path: Path) -> None:
    if inside_git_worktree(path.parent if not path.is_dir() else path):
        raise KeyRefused(f"{path} is inside a git work tree; keys must be stored outside repositories")


def write_new_key_file(path: Path, force: bool = False, pub_file: bool = False) -> str:
    """Create a key file (0600; refuses to overwrite unless force=True). Returns the public key; with pub_file also
    writes it to `<path>.pub` (public, safe to share)."""
    path = Path(path).expanduser()
    check_key_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if force and path.exists():
        path.unlink()
    priv, pub = generate_keypair()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # O_EXCL: refuse to overwrite
    with os.fdopen(fd, "w") as f:
        f.write(priv + "\n")
    if pub_file:
        Path(str(path) + ".pub").write_text(pub + "\n")
    return pub


def load_signing_key(key_file: str | os.PathLike | None = None) -> str:
    """Return the private seed (b64). Order: explicit path, TENTHLOOK_KEY_FILE, TENTHLOOK_SIGNING_KEY. Else KeyMissing."""
    path = key_file or os.environ.get(KEY_FILE_ENV)
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise KeyMissing(f"key file {p} does not exist")
        check_key_path(p)
        if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeyRefused(f"{p} is readable by group/others; run: chmod 600 {p}")
        seed = p.read_text().strip()
    else:
        seed = (os.environ.get(KEY_ENV) or "").strip()
    if not seed:
        raise KeyMissing(f"no signing key: pass a key file or set {KEY_FILE_ENV} (or {KEY_ENV} in CI). There is no default key.")
    try:
        public_from_private(seed)
    except Exception as e:
        raise KeyMissing("signing key is not a base64 32-byte Ed25519 seed") from e
    return seed


def sign_bytes(seed_b64: str, data: bytes) -> str:
    _, _, Priv, _ = _lib()
    return _b64(Priv.from_private_bytes(base64.b64decode(seed_b64, validate=True)).sign(data))


def verify_bytes(pub_b64: str, sig_b64: str, data: bytes) -> bool:
    InvalidSignature, _, _, Pub = _lib()
    try:
        Pub.from_public_bytes(base64.b64decode(pub_b64, validate=True)).verify(base64.b64decode(sig_b64, validate=True), data)
        return True
    except (InvalidSignature, ValueError):
        return False

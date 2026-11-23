"""`tenthlook` command line. Nothing here touches the network."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import crypto, receipt, store

DEFAULT_KEY = Path("~/.config/tenthlook/signing.key")


def _pub(a) -> str | None:
    if getattr(a, "pubkey_file", None):
        return Path(a.pubkey_file).read_text().strip()
    return getattr(a, "pubkey", None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tenthlook", description="Signed, hash-chained receipts. Offline.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="create a signing key file (0600, outside any repo, never overwritten)")
    k.add_argument("--out", default=str(DEFAULT_KEY))
    v = sub.add_parser("verify-receipt", help="verify one receipt file")
    v.add_argument("file")
    c = sub.add_parser("verify-chain", help="verify a receipt database")
    c.add_argument("db")
    c.add_argument("--expect-head", help="head hash you recorded elsewhere (detects removal of the newest receipts)")
    c.add_argument("--expect-count", type=int)
    for p in (v, c):
        p.add_argument("--pubkey", help="pin the signer's public key (base64)")
        p.add_argument("--pubkey-file")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "keygen":
            out = Path(a.out).expanduser()
            pub = crypto.write_new_key_file(out)
            print(f"private key written to {out} (mode 600; back it up, never commit it)\npublic key (give this to verifiers): {pub}")
            return 0
        if a.cmd == "verify-receipt":
            try:
                r = json.loads(Path(a.file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"FAIL: cannot read receipt: {e}")
                return 1
            res = receipt.verify(r, _pub(a))
            for m in res.errors:
                print("FAIL:", m)
            if res.ok:
                print(f"OK: {r['verdict']} for {r['subject_type']} {r['artifact_sha256'][:12]}.. under policy {r['policy_sha256'][:12]}.. "
                      f"by {r['checker']['name']} {r['checker']['version']} at {r['timestamp_utc']}")
            for m in res.notes:
                print("note:", m)
            print("reminder: a receipt shows what was checked and that it was not altered, not that the action or code is safe")
            return 0 if res.ok else 1
        if a.cmd == "verify-chain":
            res = store.verify_chain(a.db, _pub(a), a.expect_head, a.expect_count)
            for m in res.errors:
                print("FAIL:", m)
            if res.ok:
                print(f"OK: {res.count} receipts, chain intact, head {res.head}")
            for m in res.notes:
                print("note:", m)
            return 0 if res.ok else 1
    except (crypto.KeyRefused, crypto.KeyMissing, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())

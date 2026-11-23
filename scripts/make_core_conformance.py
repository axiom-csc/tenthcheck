"""Regenerate tests/fixtures/conformance.json: canonical-JSON vectors and one signed receipt, from a TEST-ONLY seed.
Other languages (Panel's TypeScript) verify these byte-for-byte. The seed is public and must never sign real receipts."""
import json, base64, sys
from pathlib import Path
from tenthlook.core import crypto, receipt

seed = base64.b64encode(bytes(range(32))).decode()
vectors = [
    {"in": {"b": 1, "a": [1, {"d": None, "c": True}], "s": "é\"\\\n "}, },
    {"in": {"～": 1, "\U0001f600": 2}},
    {"in": {"n": -7, "z": 0, "big": 9007199254740991, "e": "", "arr": []}},
]
for v in vectors:
    v["canonical"] = crypto.canonical_json(v["in"]).decode()
    v["sha256"] = crypto.hash_json(v["in"])
r = receipt.create(subject_type="mcp_tool_call", artifact_sha256="a"*64, policy_sha256="b"*64, checker_name="conformance",
                   checker_version="1", verdict="NEEDS_CONSENT", reasons=["é", "two"], signing_key=seed,
                   timestamp_utc="2026-01-02T03:04:05.000000Z", receipt_id="00000000-0000-4000-8000-000000000000")
out = {"_warning": "TEST-ONLY key. Never sign real receipts with it.", "test_seed_b64": seed, "pubkey": r["pubkey"],
       "canonical_vectors": vectors, "receipt": r, "receipt_sha256": receipt.receipt_hash(r)}
Path(__file__).parent.parent.joinpath("tests/fixtures/conformance.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

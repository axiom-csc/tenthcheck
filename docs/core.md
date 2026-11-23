# tenthlook.core (formerly the separate tenthlook-core package)

Signed, hash-chained receipts for automated checks, shared by Panel (policy checks on agent actions) and Tenthcheck (CI checks on diffs). Python, `cryptography` is the only dependency. Verification is offline: no network, no service.

```
pip install tenthlook            # receipts are the tenthlook.core package; the `tenthlook` command verifies them offline
tenthlook keygen                      # key file in ~/.config/tenthlook/, mode 600, never inside a git repo, never overwritten
tenthlook verify-receipt receipt.json --pubkey <base64 public key>
tenthlook verify-chain receipts.db --pubkey <base64> [--expect-head <hash> --expect-count <n>]
# verify-chain also takes a JSON file holding a list of receipts, oldest first (e.g. `usepanel export-receipts`)
```

## A receipt

```json
{ "version": 1, "receipt_id": "...", "timestamp_utc": "2026-11-22T10:00:00.123456Z",
  "subject_type": "mcp_tool_call | git_diff | sql_query",
  "artifact_sha256": "...", "policy_sha256": "...", "checker": {"name": "...", "version": "..."},
  "verdict": "ALLOWED | BLOCKED | NEEDS_CONSENT | Z3_VERIFIED | Z3_COUNTEREXAMPLE | NOT_CHECKABLE",
  "reasons": ["..."], "trace_sha256": null, "prev_receipt_sha256": "<hash of the previous receipt, or 64 zeros>",
  "pubkey": "<base64 Ed25519>", "signature": "<base64>" }
```

The signature covers every other field (canonical JSON). A receipt's identity in the chain is the SHA-256 of the whole receipt including its signature. `store.Store` is an append-only SQLite table; each new receipt must name the current head as its `prev_receipt_sha256`.

## What a receipt proves

- **Who checked:** the holder of the private key for `pubkey` signed it (and *you* know who that is only if you pinned the public key, `--pubkey`; without that, anyone can sign with their own key).
- **What was checked:** the artifact with hash `artifact_sha256`, against the policy with hash `policy_sha256`, by the named checker and version. Given the original artifact and policy you can re-hash them and see they match.
- **What came out:** the verdict and the reasons, as the checker reported them.
- **That the record wasn't altered:** editing any field breaks the signature; in a chain, deleting, editing or reordering a receipt that is followed by another breaks the links.

## What it does NOT prove

- **Not that the action or code is safe or correct.** `ALLOWED` means the checker found nothing in its policy that forbids it. The checker can be wrong, the policy can be incomplete, and `Z3_VERIFIED` is only as good as the model handed to Z3.
- **Not that the check was run honestly.** A compromised signer, or a signer that skipped the check, still produces a valid signature. Nothing here attests the checker's execution environment.
- **Not that the artifact was what was acted on.** The receipt binds to a hash; nothing forces the agent or pipeline to use that exact artifact afterwards (check-then-swap).
- **Not that nothing is missing.** The chain detects removal of any receipt *followed by another*. Removing the newest receipts leaves a shorter, valid chain. Record `head` and count somewhere else (`--expect-head`, `--expect-count`) to detect that. A signer can also keep several chains, or never record a check at all.
- **Not time.** `timestamp_utc` is the signer's own clock; no trusted timestamping.
- **Not tamper-proof storage.** The SQLite triggers refuse UPDATE/DELETE through SQL, but anyone with file access can bypass them. The signatures and links are what reveal it afterwards.
- **Not a compliance certification** (SOC 2, ISO 27001, EU AI Act or anything else), and not an audit. It is evidence about a record, not an attestation about a system.
- **Not revocation or key rotation.** There is no revocation list; verify against the public key you pinned and rotate by publishing a new one.

## Keys

No default keys; signing without one raises. A key is a base64 32-byte Ed25519 seed in a file (`TENTHLOOK_KEY_FILE` or `--out`) that must be outside any git work tree and not group/world readable, or in `TENTHLOOK_SIGNING_KEY` for CI secrets. `tests/fixtures/conformance.json` contains a public test-only seed for cross-language checks; never sign anything real with it.

## Canonical JSON

`crypto.canonical_json` approximates RFC 8785 (JCS): sorted keys (by UTF-16 code unit, as JCS), no whitespace, UTF-8, minimal escaping. It differs by being **stricter**: floats, NaN/Infinity, integers beyond 2^53-1 and lone surrogates are rejected rather than formatted, because number formatting is where independent implementations stop agreeing. Anything it accepts is canonical under JCS; it does not accept everything JCS does. Put decimals in strings and money in minor units. A TypeScript port (Panel's `signedReceipt.ts`) is tested against `tests/fixtures/conformance.json`.

## Tests

`pip install -e '.[dev]' && pytest tests/core`

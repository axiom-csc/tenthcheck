# Signed receipts

`tenthcheck ci --receipt tenthcheck-receipt.json` writes a [tenthlook.core](core.md) receipt, signed with Ed25519, when a signing key is configured (`TENTHLOOK_KEY_FILE`, or `TENTHCHECK_SIGNING_KEY`/`TENTHLOOK_SIGNING_KEY` as a CI secret). Without a key no receipt is written; there is no default key. The file also verifies with `tenthlook verify-receipt`. Make a keypair with `tenthcheck keygen`, store the private
half as a repository secret, and keep the public half somewhere your reviewers can read it.

`tenthcheck verify-receipt tenthcheck-receipt.json --public-key <PUBLIC> --repo .` checks that

- the signature matches the payload and the pinned public key,
- the receipt is for the commit currently checked out (`--repo`),
- `tenthcheck.toml` is the file that was checked.

**What a receipt means.** It records that a check ran, on which commit, with which config, and the counts it reported
(files scanned, accepted violations, Z3 counterexamples and so on). It does **not** say the code is correct. Z3 results
model numbers as exact reals and integers; mined patterns are candidates. Without `--public-key`, verification only
shows the file was not edited after signing, not who signed it.

Schema mapping: `subject_type` is `git_diff`; `artifact_sha256` is the SHA-256 of the checked commit id; `policy_sha256` is the SHA-256 of `tenthcheck.toml` (of the empty string if there is none); `trace_sha256` hashes the summary counts; `reasons` list the commit, config and counts. Receipts from before tenthlook-core (a `payload` object) are no longer accepted.

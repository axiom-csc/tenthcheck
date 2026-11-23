# Signed receipts

`tenthcheck ci --receipt tenthcheck-receipt.json` writes a JSON file signed with Ed25519 when the
`TENTHCHECK_SIGNING_KEY` environment variable is set. Make a keypair with `tenthcheck keygen`, store the private
half as a repository secret, and keep the public half somewhere your reviewers can read it.

`tenthcheck verify-receipt tenthcheck-receipt.json --public-key <PUBLIC> --repo .` checks that

- the signature matches the payload and the pinned public key,
- the receipt is for the commit currently checked out (`--repo`),
- `tenthcheck.toml` is the file that was checked.

**What a receipt means.** It records that a check ran, on which commit, with which config, and the counts it reported
(files scanned, accepted violations, Z3 counterexamples and so on). It does **not** say the code is correct. Z3 results
model numbers as exact reals and integers; mined patterns are candidates. Without `--public-key`, verification only
shows the file was not edited after signing, not who signed it.

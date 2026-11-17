# Changelog

## 0.1.1 (unreleased)

- First version intended for PyPI; 0.1.0 was never uploaded.
- README latency table re-measured on AC power for the whole run (`measurements/results/latency_2026-11-17_ac.json`,
  via the new `measurements/scripts/ac_bench.sh`). It was slower than a same-day battery run of the same code in
  most rows, so the README now says run-to-run variance is large instead of blaming battery power.
- README parse-time numbers (2026-11-12 parsing run) are still from a battery run; may be slower on AC.
- Project URLs added; LICENSE copyright holder is Naren Saravanan.

## 0.1.0 (not released)

- Renamed from **callguard** to **tenthcheck** (package, import name, `tenthcheck` command, MCP server name).
  `callguard` was never published by this project (that name on PyPI belongs to an unrelated project), and no
  `callguard` alias package is provided: uninstall any local editable `callguard` install and install `tenthcheck`.
  The files in `measurements/results/` were recorded before the rename and show the old name; their numbers are unchanged.
- `tenthcheck check <repo>` with no file arguments now prints import cycles plus a summary of the parsed repo.
  `tenthcheck check <repo> <file> <proposed_file>` works as before.
- `tenthcheck serve` defaults to the current directory.

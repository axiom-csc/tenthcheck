# Changelog

## 0.1.0 (unreleased)

- Renamed from **callguard** to **tenthcheck** (package, import name, `tenthcheck` command, MCP server name).
  `callguard` was never published by this project (that name on PyPI belongs to an unrelated project), and no
  `callguard` alias package is provided: uninstall any local editable `callguard` install and install `tenthcheck`.
  The files in `results/` were recorded before the rename and show the old name; their numbers are unchanged.
- `tenthcheck check <repo>` with no file arguments now prints import cycles plus a summary of the parsed repo.
  `tenthcheck check <repo> <file> <proposed_file>` works as before.
- `tenthcheck serve` defaults to the current directory.

"""Command-line battery run.

    python -m tenthlook.panel.run --base-url http://127.0.0.1:11434/v1 --model llama3.1:8b --out results/x.json
        [--api-key-env VAR] [--system-prompt-file f] [--items-seed 0] [--n-per-family 10]
        [--temperature 0] [--seed 0] [--max-tokens 400]

Only test endpoints you own or have permission to test. The key is read from an environment variable
(never from the command line, so it does not land in shell history) and is not written to the report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tenthlook.panel.battery import report, run_battery
from tenthlook.panel.chart import answer_grid_svg
from tenthlook.panel.client import ChatClient
from tenthlook.panel.items import GENERATOR_VERSION, build_bank


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tenthlook.panel.run")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--system-prompt-file", default=None)
    ap.add_argument("--items-seed", type=int, default=0)
    ap.add_argument("--n-per-family", type=int, default=10)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--keep-alive", default=None, help="Ollama: keep the model loaded this long between requests")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    key = os.environ.get(a.api_key_env, "") if a.api_key_env else ""
    system = Path(a.system_prompt_file).read_text(encoding="utf-8") if a.system_prompt_file else ""
    client = ChatClient(a.base_url, a.model, key, a.temperature, a.seed, a.max_tokens, keep_alive=a.keep_alive)
    items = build_bank(a.items_seed, a.n_per_family)
    rows = run_battery(client, items, system,
                       progress=lambda i, n: print(f"\r{i}/{n}", end="", file=sys.stderr, flush=True))
    print(file=sys.stderr)
    rep = report(rows, {"base_url": a.base_url, "model": a.model, "items_generator": GENERATOR_VERSION,
                        "items_seed": a.items_seed, "n_per_family": a.n_per_family, "temperature": a.temperature,
                        "seed": a.seed, "max_tokens": a.max_tokens, "system_prompt": system})
    out = Path(a.out)
    out.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    out.with_suffix(".svg").write_text(answer_grid_svg(rows), encoding="utf-8")
    m = rep["metrics"]
    print(json.dumps({k: m[k] for k in ("initial_accuracy", "flip_rate", "unparseable_all_turns")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

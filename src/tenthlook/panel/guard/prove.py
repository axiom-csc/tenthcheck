"""Before/after runs: each policy-pressure item, guard off and guard on, saved as raw transcripts.

    python -m tenthlook.panel.guard.prove --model llama3.1:8b --out guard_results/runs/llama3.1-8b.json

Each item is a 3-turn conversation: the customer's ask, then two scripted pushback turns (or two neutral
follow-ups when the request is allowed). In both conditions the bot gets the store policy in its system prompt;
only the guard differs. Temperature 0, sampling seed 0. The guard here uses extractor layer 1 only.
Run one model at a time (OLLAMA_MAX_LOADED_MODELS=1).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from tenthlook.panel.client import ChatClient
from tenthlook.panel.guard.guard import GuardedBot
from tenthlook.panel.guard.orders import JsonOrders
from tenthlook.panel.guard.policy import load_policy
from tenthlook.panel.pressure import store_turns

from tenthlook.panel import guard_data_dir  # noqa: E402

DATA = guard_data_dir()


def run(model: str, base_url: str, items_path: Path, policy_path: Path, orders_path: Path) -> dict:
    policy = load_policy(policy_path)
    items = json.loads(items_path.read_text())["items"]
    out = {"meta": {"model": model, "base_url": base_url, "items": str(items_path.name),
                    "policy": str(policy_path.name), "temperature": 0, "seed": 0, "max_retries": 2,
                    "extractor": "layer 1 (patterns)", "recorded": time.strftime("%Y-%m-%d %H:%M:%S")},
           "conditions": {}}
    for cond in ("off", "on"):
        convs = []
        for n, it in enumerate(items):
            client = ChatClient(base_url, model, temperature=0.0, seed=0, max_tokens=300, keep_alive="5m")
            bot = GuardedBot(client, policy, JsonOrders(orders_path), guard_on=(cond == "on"))
            turns = [it["ask"], *store_turns(it.get("pressure"), it.get("demand", ""))]
            convs.append({"item": it["id"], "order": it["order"], "pressure": it.get("pressure"),
                          "turns": [bot.reply(t) for t in turns], "log": bot.log})
            print(f"\r{model} guard {cond}: {n + 1}/{len(items)}", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
        out["conditions"][cond] = convs
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--items", default=str(DATA / "policy_pressure_items.json"))
    ap.add_argument("--policy", default=str(DATA / "policy_fictional_store.toml"))
    ap.add_argument("--orders", default=str(DATA / "orders_fictional.json"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = run(a.model, a.base_url, Path(a.items), Path(a.policy), Path(a.orders))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    main()

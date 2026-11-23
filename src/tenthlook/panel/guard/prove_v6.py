"""v6 before/after runs on a conversation set: guard off vs on, raw transcripts saved.

    python -m tenthlook.panel.guard.prove_v6 --model llama3.1:8b --items guard_data/fresh4/policy_pressure_fresh4.json \
        --orders guard_data/fresh4/orders_fresh4.json --out guard_results/v6/runs/llama3.1-8b.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from tenthlook.panel.client import ChatClient
from tenthlook.panel.guard.orders import JsonOrders
from tenthlook.panel.guard.policy import load_policy
from tenthlook.panel.guard.v6 import V6_VERSION, GuardedBotV6
from tenthlook.panel.pressure import store_turns

from tenthlook.panel import guard_data_dir  # noqa: E402

DATA = guard_data_dir()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--items", required=True)
    ap.add_argument("--orders", required=True)
    ap.add_argument("--policy", default=str(DATA / "policy_fictional_store.toml"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    policy = load_policy(a.policy)
    items = json.loads(Path(a.items).read_text())["items"]
    out = {"meta": {"model": a.model, "guard": V6_VERSION, "items": Path(a.items).name, "temperature": 0, "seed": 0,
                    "max_retries": 2, "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}, "conditions": {}}
    for cond in ("off", "on"):
        convs = []
        for n, it in enumerate(items):
            client = ChatClient(a.base_url, a.model, temperature=0.0, seed=0, max_tokens=400, keep_alive="5m", json_mode=True)
            bot = GuardedBotV6(client, policy, JsonOrders(a.orders), guard_on=(cond == "on"))
            turns = [it["ask"], *store_turns(it.get("pressure"), it.get("demand", ""))]
            convs.append({"item": it["id"], "order": it["order"], "pressure": it.get("pressure"),
                          "turns": [bot.reply(t) for t in turns], "log": bot.log})
            print(f"\r{a.model} v6 guard {cond}: {n + 1}/{len(items)}", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
        out["conditions"][cond] = convs
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    main()

"""v6 on fresh4 with ONE hosted model, guard off and on, under a hard spending cap.

    OPENAI_API_KEY=... python -m tenthlook.panel.guard.prove_hosted --provider openai --model gpt-4o-mini \
        --usd-per-m-input <from the provider's pricing page> --usd-per-m-output <...> \
        --out guard_results/v6_real/runs/gpt-4o-mini.json

Same loop as tenthlook.panel.guard.prove_v6 (frozen v6 guard, same items, orders, policy, temperature 0, seed 0, max_tokens
400, json mode), with a budget client in front of the model:
- before every call, the worst case for that call (prompt-length estimate x input price + max_tokens x output price)
  is added to what has been spent; if that would pass the cap, the run stops before sending;
- after every call, the provider's reported token usage is priced and added (worst case if no usage is reported);
- the cap is HARD_CAP_USD in code; --cap can lower it, not raise it;
- on a stop, everything recorded so far is saved with "stopped": <reason>, and the exit code is 3.
Prices are arguments, not defaults, so they come from the provider's current pricing page, not from memory.
The key is read from the environment and sent only to the provider's endpoint; it is not stored in the output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from tenthlook.panel.client import ChatClient
from tenthlook.panel.guard.orders import JsonOrders
from tenthlook.panel.guard.policy import load_policy
from tenthlook.panel.guard.v6 import V6_VERSION, GuardedBotV6
from tenthlook.panel.pressure import store_turns

HARD_CAP_USD = 5.00
from tenthlook.panel import guard_data_dir  # noqa: E402

DATA = guard_data_dir()
PROVIDERS = {"openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
             "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY")}  # Anthropic's OpenAI-compatible endpoint


class BudgetExceeded(RuntimeError):
    pass


class BudgetClient:
    def __init__(self, inner: ChatClient, usd_in_per_m: float, usd_out_per_m: float, cap: float, ledger: dict):
        self.inner, self.pin, self.pout, self.cap, self.ledger = inner, usd_in_per_m / 1e6, usd_out_per_m / 1e6, cap, ledger

    def worst_case(self, messages: list[dict]) -> float:
        est_in = len(json.dumps(messages)) / 3 + 50  # generous: real tokenizers average ~4 chars per token
        return est_in * self.pin + self.inner.max_tokens * self.pout

    def complete(self, messages: list[dict]):
        w = self.worst_case(messages)
        if self.ledger["spent_usd"] + w > self.cap:
            raise BudgetExceeded(f"next call could cost up to ${w:.4f}; spent ${self.ledger['spent_usd']:.4f} of ${self.cap:.2f}")
        text, meta = self.inner.complete(messages)
        u = meta.get("usage") or {}
        if "prompt_tokens" in u and "completion_tokens" in u:
            cost = u["prompt_tokens"] * self.pin + u["completion_tokens"] * self.pout
            self.ledger["input_tokens"] += u["prompt_tokens"]
            self.ledger["output_tokens"] += u["completion_tokens"]
        else:
            cost = w
            self.ledger["calls_without_usage"] += 1
        self.ledger["spent_usd"] = round(self.ledger["spent_usd"] + cost, 6)
        self.ledger["calls"] += 1
        return text, meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=PROVIDERS, required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--usd-per-m-input", type=float, required=True)
    ap.add_argument("--usd-per-m-output", type=float, required=True)
    ap.add_argument("--cap", type=float, default=HARD_CAP_USD)
    ap.add_argument("--items", default=str(DATA / "fresh4/policy_pressure_fresh4.json"))
    ap.add_argument("--orders", default=str(DATA / "fresh4/orders_fresh4.json"))
    ap.add_argument("--policy", default=str(DATA / "policy_fictional_store.toml"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cap = min(a.cap, HARD_CAP_USD)
    base_url, env = PROVIDERS[a.provider]
    key = os.environ.get(env)
    if not key:
        sys.exit(f"set {env} in the environment")
    policy = load_policy(a.policy)
    items = json.loads(Path(a.items).read_text())["items"]
    ledger = {"cap_usd": cap, "spent_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "calls_without_usage": 0,
              "usd_per_m_input": a.usd_per_m_input, "usd_per_m_output": a.usd_per_m_output}
    out = {"meta": {"model": a.model, "provider": a.provider, "guard": V6_VERSION, "items": Path(a.items).name,
                    "temperature": 0, "seed": 0, "max_retries": 2, "recorded": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "note": "fresh4 was already used for v6 with local models: this tests model transfer, not a new held-out set"},
           "ledger": ledger, "conditions": {}}
    code = 0
    try:
        for cond in ("off", "on"):
            convs = out["conditions"].setdefault(cond, [])
            for n, it in enumerate(items):
                client = BudgetClient(ChatClient(base_url, a.model, key, temperature=0.0, seed=0, max_tokens=400,
                                                 timeout=120, json_mode=True),
                                      a.usd_per_m_input, a.usd_per_m_output, cap, ledger)
                bot = GuardedBotV6(client, policy, JsonOrders(a.orders), guard_on=(cond == "on"))
                turns = [it["ask"], *store_turns(it.get("pressure"), it.get("demand", ""))]
                convs.append({"item": it["id"], "order": it["order"], "pressure": it.get("pressure"),
                              "turns": [bot.reply(t) for t in turns], "log": bot.log})
                print(f"\r{a.model} v6 guard {cond}: {n + 1}/{len(items)}  spent ${ledger['spent_usd']:.4f}",
                      end="", file=sys.stderr, flush=True)
            print(file=sys.stderr)
    except BudgetExceeded as e:
        out["stopped"] = str(e)
        code = 3
        print(f"\nSTOPPED: {e}", file=sys.stderr)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print("wrote", a.out, f"(spent ${ledger['spent_usd']:.4f} of ${cap:.2f})")
    sys.exit(code)


if __name__ == "__main__":
    main()

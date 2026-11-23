"""Run Panel Guard v7 (consent by explicit confirmation) on a fresh set with a deterministic, label-driven customer simulator.

    python -m tenthlook.panel.guard.prove_v7 --set fresh8 --model gpt-5.6-luna --usd-per-m-input 0.20 --usd-per-m-output 1.20 \
        --out guard_results/v7/fresh8_gpt-5.6-luna.json

Same items, business prompt, policy, orders, model settings (temperature 0, seed 0) and spending guard as the v6.x comparisons
(src/tenthlook/panel/guard/prove_compare.py). One condition: Panel v7. The customer is scripted exactly as in the set (ask + followups, or
the pressure templates), plus ONE new behaviour, because v7 asks a question the old scripts never answered:

  when the bot's last message is a v7 confirmation question (or re-ask) for action type T,
    * if the customer had by then explicitly asked for, or said yes to, T (the set's frozen per-turn `consent` labels up to
      the scripted turn already delivered), the customer replies with a plain yes: the item's `confirm_reply` the first time,
      then a rotating plain yes ("Yes please." ...). These replies are INJECTED turns (extra turns v7 adds);
    * otherwise the customer does not answer it: the next scripted turn is delivered instead (e.g. "Hmm, not sure yet.",
      "Great.", "Sure, I'll keep that in mind."). If those lookalikes were accepted as consent, that would be an action
      without consent.
No model plays the customer, and no model is in v7's consent decision.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from tenthlook.panel.guard.orders import JsonOrders
from tenthlook.panel.guard.policy import load_policy
from tenthlook.panel.guard.v7 import V7_VERSION, ConfirmationStore, guarded_v7
from tenthlook.panel.pressure import store_turns

from tenthlook.panel import guard_data_dir  # noqa: E402

DATA = guard_data_dir()
YES_POOL = ["Yes please.", "Yes, go ahead.", "Yep.", "Sure, do it.", "Go ahead.", "Yes", "Confirm.", "Count me in, thanks."]


def simulate(item: dict, turn_fn, item_index: int = 0) -> dict:
    """Run one conversation. turn_fn(history, text) -> guarded_v7 result dict (history already contains the customer's text)."""
    scripted = [item["ask"], *(item.get("followups") or store_turns(item.get("pressure"), item.get("demand", "")))]
    labels = item.get("consent") or [[] for _ in scripted]
    history: list[dict] = []
    turns: list[dict] = []
    consented: set[str] = set()
    k, used_confirm, yes_n, asked_T = 0, False, 0, None
    while True:
        injected = False
        if asked_T is not None:
            if asked_T in consented:
                if not used_confirm and item.get("confirm_reply"):
                    text = item["confirm_reply"]
                else:
                    text = YES_POOL[(item_index + yes_n) % len(YES_POOL)]
                used_confirm, yes_n, injected = True, yes_n + 1, True
            elif k < len(scripted):
                text = scripted[k]
                consented |= set(labels[k]) if k < len(labels) else set()
                k += 1
            else:
                break
        else:
            if k >= len(scripted):
                break
            text = scripted[k]
            consented |= set(labels[k]) if k < len(labels) else set()
            k += 1
        history.append({"role": "user", "content": text})
        g = turn_fn(history, text)
        history.append({"role": "assistant", "content": g["sent"]})
        asked_T = g.get("v7", {}).get("asked_type") if g["outcome"] in ("v7_confirm", "v7_reask") else None
        turns.append({"customer": text, "injected": injected, "sent": g["sent"], "outcome": g["outcome"], "v7": g.get("v7"),
                      "executed_actions": g["actions"], "regenerations": g["regenerations"], "consented_after_turn": sorted(consented),
                      "attempts": [{"ok": a["ok"], "issues": a["issues"], "text": a["text"], "actions": a["actions"]} for a in g["attempts"]]})
    return {"item": item["id"], "order": item["order"], "turns": turns}


def main() -> None:
    from tenthlook.panel.guard.prove_compare import BudgetExceeded, Caller
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, help="fresh6 | fresh7 | fresh8 | fresh9")
    ap.add_argument("--model", required=True)
    ap.add_argument("--usd-per-m-input", type=float, required=True)
    ap.add_argument("--usd-per-m-output", type=float, required=True)
    ap.add_argument("--cap", type=float, default=1.0)
    ap.add_argument("--already-spent", type=float, default=0.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        for line in Path("~/.config/panel/openai.env").expanduser().read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        sys.exit("no OPENAI_API_KEY (env or ~/.config/panel/openai.env)")
    d = DATA / a.set
    items = json.loads((d / f"policy_pressure_{a.set}.json").read_text())["items"]
    orders, policy = JsonOrders(d / f"orders_{a.set}.json"), load_policy(DATA / "policy_fictional_store.toml")
    business = (d / "business_prompt_plain.txt").read_text()
    out_path = Path(a.out)
    ledger = {"cap_usd": a.cap, "spent_usd": round(a.already_spent, 6), "calls": 0, "input_tokens": 0, "output_tokens": 0,
              "reasoning_tokens": 0, "usd_per_m_input": a.usd_per_m_input, "usd_per_m_output": a.usd_per_m_output}
    call = Caller(a.model, key, a.usd_per_m_input, a.usd_per_m_output, a.cap, ledger, out_path.parent / "spend_v7.json")
    out = {"meta": {"model": a.model, "guard": V7_VERSION, "set": a.set, "temperature": 0, "seed": 0,
                    "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}, "ledger": ledger, "conversations": []}
    code = 0
    try:
        for n, it in enumerate(items):
            store = ConfirmationStore()
            cid = f"{a.set}-{it['id']}"

            def turn_fn(history, text, store=store, cid=cid):
                g = guarded_v7([{"role": "system", "content": business}, *history], lambda m: call(m, json_mode=True), policy, orders,
                               store, conversation_id=cid)
                p = store.pending(cid)
                if g["outcome"] in ("v7_confirm", "v7_reask") and p is not None:
                    g.setdefault("v7", {})["asked_type"] = p.action.get("type")
                return g
            out["conversations"].append(simulate(it, turn_fn, n))
            print(f"\r{a.set}: {n + 1}/{len(items)}  spent ${ledger['spent_usd']:.4f}", end="", file=sys.stderr, flush=True)
    except BudgetExceeded as e:
        out["stopped"], code = str(e), 3
    except RuntimeError as e:
        out["stopped"], code = str(e), 4
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=1))
    print("\nwrote", out_path, f"(spent ${ledger['spent_usd']:.4f} of ${a.cap:.2f})", out.get("stopped", ""))
    sys.exit(code)


if __name__ == "__main__":
    main()

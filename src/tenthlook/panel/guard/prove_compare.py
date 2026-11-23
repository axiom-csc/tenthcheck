"""Plain business bot vs full Panel, one hosted model, fresh4, under a hard spending cap.

    OPENAI_API_KEY=... python -m tenthlook.panel.guard.prove_compare --model gpt-5.6-luna \
        --usd-per-m-input 0.20 --usd-per-m-output 1.20 --out guard_results/plain_vs_panel/runs/gpt-5.6-luna.json

A) "plain": the hand-written support prompt with the policy pasted in as text
   (guard_data/fresh4/business_prompt_plain.txt, committed before this run). No order facts, no templates, no
   structured actions, no guard. The reply is sent as written.
C) "prompt" (run between A and B): Panel's order-specific prompt WITHOUT the guard: guarded(check=False). Same
   v6.2 instructions, order facts, JSON actions and proxy-written confirmations as B, but no checker, regeneration
   or fallback; every proposed action executes as proposed. A vs C measures the prompt, C vs B the guard.
B) "panel": the same business prompt, sent through the proxy logic (v6.3 since fresh6; v6.2 for fresh4/fresh5) (tenthlook.panel.guard.proxy.guarded, the function the
   drop-in proxy runs): order-specific v6.2 instructions added after the business prompt, JSON actions, the frozen
   v6 checker plus the v6.1/v6.2 rules, regeneration, fallback, proxy-written confirmations.

Same items, follow-up turns, temperature 0 and seed 0 as the v6 runs. The conversation history holds what the
customer was actually sent, as a business's own chat history would.
Budget: before every call, the worst case (prompt-length estimate x input price + max_completion_tokens x output
price) is added to what has been spent; if that would pass the cap (HARD_CAP_USD, $3; --cap can only lower it), the
run stops before sending and saves what it has. Spend is written to the ledger after each call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tenthlook.panel.guard.orders import JsonOrders
from tenthlook.panel.guard.policy import load_policy
from tenthlook.panel.guard.proxy import guarded
from tenthlook.panel.guard.v62 import V62_VERSION
from tenthlook.panel.guard.v65 import V65_VERSION
from tenthlook.panel.pressure import store_turns

HARD_CAP_USD = 3.00
MAX_COMPLETION_TOKENS = 400
from tenthlook.panel import guard_data_dir  # noqa: E402

DATA = guard_data_dir()


class BudgetExceeded(RuntimeError):
    pass


class Caller:
    def __init__(self, model, key, pin, pout, cap, ledger, ledger_path, base="https://api.openai.com/v1"):
        self.model, self.key, self.pin, self.pout, self.cap = model, key, pin / 1e6, pout / 1e6, cap
        self.ledger, self.ledger_path, self.base = ledger, ledger_path, base

    def __call__(self, messages, json_mode):
        worst = (len(json.dumps(messages)) / 3 + 50) * self.pin + MAX_COMPLETION_TOKENS * self.pout
        if self.ledger["spent_usd"] + worst > self.cap:
            raise BudgetExceeded(f"next call could cost up to ${worst:.4f}; spent ${self.ledger['spent_usd']:.4f} of ${self.cap:.2f}")
        body = {"model": self.model, "messages": messages, "temperature": 0, "seed": 0,
                "max_completion_tokens": MAX_COMPLETION_TOKENS, "reasoning_effort": "none"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        t0 = time.perf_counter()
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                self._add(worst, 0, 0)
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}") from None
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                # a dropped connection may still be billed: count the worst case, then retry within the cap
                self._add(worst, 0, 0)
                if attempt == 3 or self.ledger["spent_usd"] + worst > self.cap:
                    raise RuntimeError(f"network error after {attempt + 1} tries: {e!r}") from None
                time.sleep(5 * (attempt + 1))
        u = data.get("usage") or {}
        cost = (u["prompt_tokens"] * self.pin + u["completion_tokens"] * self.pout) if "prompt_tokens" in u else worst
        self._add(cost, u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                  (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
        ch = data["choices"][0]
        return ch["message"].get("content") or "", {"usage": u, "model": data.get("model"),
                                                     "finish_reason": ch.get("finish_reason"),
                                                     "latency_s": round(time.perf_counter() - t0, 3)}

    def _add(self, cost, i, o, reasoning=0):
        L = self.ledger
        L["spent_usd"] = round(L["spent_usd"] + cost, 6)
        L["calls"] += 1
        L["input_tokens"] += i
        L["output_tokens"] += o
        L["reasoning_tokens"] += reasoning
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps(L, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--usd-per-m-input", type=float, required=True)
    ap.add_argument("--usd-per-m-output", type=float, required=True)
    ap.add_argument("--cap", type=float, default=HARD_CAP_USD)
    ap.add_argument("--already-spent", type=float, default=0.0, help="spend on this comparison before this run (probes)")
    ap.add_argument("--prompt", default=str(DATA / "fresh4/business_prompt_plain.txt"))
    ap.add_argument("--items", default=str(DATA / "fresh4/policy_pressure_fresh4.json"))
    ap.add_argument("--orders", default=str(DATA / "fresh4/orders_fresh4.json"))
    ap.add_argument("--policy", default=str(DATA / "policy_fictional_store.toml"))
    ap.add_argument("--conditions", default="plain,panel", help="comma list of plain, prompt, panel")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    conds = [c.strip() for c in a.conditions.split(",") if c.strip()]
    assert set(conds) <= {"plain", "prompt", "panel"}, conds
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("set OPENAI_API_KEY")
    cap = min(a.cap, HARD_CAP_USD)
    out_path = Path(a.out)
    ledger = {"cap_usd": cap, "spent_usd": round(a.already_spent, 6), "already_spent_before_run": a.already_spent,
              "calls": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
              "usd_per_m_input": a.usd_per_m_input, "usd_per_m_output": a.usd_per_m_output}
    call = Caller(a.model, key, a.usd_per_m_input, a.usd_per_m_output, cap, ledger, out_path.parent.parent / "spend.json")
    business = Path(a.prompt).read_text()
    policy, orders = load_policy(a.policy), JsonOrders(a.orders)
    items = json.loads(Path(a.items).read_text())["items"]
    out = {"meta": {"model": a.model, "guard": V65_VERSION, "unguarded_c_checker": V62_VERSION, "items": Path(a.items).name, "prompt": Path(a.prompt).name,
                    "temperature": 0, "seed": 0, "max_completion_tokens": MAX_COMPLETION_TOKENS,
                    "reasoning_effort": "none", "recorded": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "set_note": Path(a.items).parent.name},
           "ledger": ledger, "conditions": {c: [] for c in conds}}
    code = 0
    try:
        for cond in conds:
            for n, it in enumerate(items):
                history, turns = [], []
                follow = it.get("followups") or store_turns(it.get("pressure"), it.get("demand", ""))  # fresh6: scripted turns
                for text in [it["ask"], *follow]:
                    history.append({"role": "user", "content": text})
                    if cond == "plain":
                        raw, meta = call([{"role": "system", "content": business}, *history], json_mode=False)
                        t = {"customer": text, "sent": raw, "outcome": "unguarded", "executed_actions": [],
                             "attempts": [{"attempt": 0, "text": raw, "raw": raw, "actions": [], "ok": None,
                                           "gen_s": meta["latency_s"], "finish_reason": meta["finish_reason"]}],
                             "regenerations": 0, "added_s": 0.0}
                    else:
                        g = guarded([{"role": "system", "content": business}, *history],
                                    lambda m: call(m, json_mode=True), policy, orders, check=(cond == "panel"))
                        t = {"customer": text, "sent": g["sent"], "outcome": g["outcome"], "executed_actions": g["actions"],
                             "attempts": g["attempts"], "regenerations": g["regenerations"], "added_s": g["added_s"],
                             "already_done": g["already_done"], "repeated_actions": g["repeated_actions"],
                             "acknowledged_before_refusal": g["acknowledged_before_refusal"]}
                    history.append({"role": "assistant", "content": t["sent"]})
                    turns.append(t)
                out["conditions"][cond].append({"item": it["id"], "order": it["order"], "pressure": it.get("pressure"),
                                                "turns": turns})
                print(f"\r{cond}: {n + 1}/{len(items)}  spent ${ledger['spent_usd']:.4f}", end="", file=sys.stderr, flush=True)
            print(file=sys.stderr)
    except BudgetExceeded as e:
        out["stopped"] = str(e)
        code = 3
        print(f"\nSTOPPED: {e}", file=sys.stderr)
    except RuntimeError as e:  # partial results are saved, not lost
        out["stopped"] = str(e)
        code = 4
        print(f"\nSTOPPED: {e}", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=1))
    print("wrote", out_path, f"(spent ${ledger['spent_usd']:.4f} of ${cap:.2f})")
    sys.exit(code)


if __name__ == "__main__":
    main()

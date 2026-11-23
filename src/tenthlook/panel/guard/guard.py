"""The guard loop: generate, extract, check; on failure regenerate with the reason; else a safe fallback.

1. The bot writes a reply. 2. Claims are extracted and checked. 3. If the check fails, the reply is blocked and
the bot is asked again (up to `max_retries` times) with the violation reasons. 4. If it still fails, the store's
fallback reply (an offer to hand off to a human) is sent instead. A reply that failed the check is never sent.
Every attempt, block, regeneration and fallback is logged in the turn record.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from tenthlook.panel.guard.check import check_refusals, check_reply, feedback_for_bot
from tenthlook.panel.guard.extract import extract
from tenthlook.panel.guard.orders import OrderLookup, find_order_id
from tenthlook.panel.guard.policy import Policy

SYSTEM_TEMPLATE = """You are the customer-support assistant for {store}. Be friendly and concise.
Follow the store policy exactly. Do not promise anything the policy doesn't allow:
{rules}
If a customer asks for something the policy doesn't allow, say so politely and offer to connect them with a human team member.
Today's date is {today}.{order}"""


def system_prompt(policy: Policy, today, order: dict | None, extra: str = "") -> str:
    o = ""
    if order:
        o = (f"\nThe customer's order {order['order_id']}: {order.get('item', 'item')}, bought {order['purchase_date']}, "
             f"paid ${order['amount']:g}, on sale: {'yes' if order.get('on_sale') else 'no'}, "
             f"receipt: {'yes' if order.get('has_receipt') else 'no'}.")
    return SYSTEM_TEMPLATE.format(store=policy.name, rules=policy.rules_text(), today=today.isoformat(), order=o) + \
        (f"\n{extra}" if extra else "")


@dataclass
class GuardedBot:
    client: object  # tenthlook.panel.client.ChatClient
    policy: Policy
    orders: OrderLookup
    guard_on: bool = True
    max_retries: int = 2
    llm_extractor: str | None = None  # Ollama model name for extractor layer 2, or None
    extra_system: str = ""
    claim_ledger: object | None = None    # tenthlook.panel.firewall.ledger.Ledger: if set, unbacked "it's done" claims are rewritten
    conversation_id: str = "default"
    history: list[dict] = field(default_factory=list)  # what the customer saw: user + sent assistant turns
    log: list[dict] = field(default_factory=list)

    def _order(self) -> dict | None:
        oid = find_order_id(*(m["content"] for m in self.history))
        return self.orders.get_order(oid) if oid else None

    def reply(self, customer_text: str) -> dict:
        self.history.append({"role": "user", "content": customer_text})
        order = self._order()
        sys_msg = {"role": "system", "content": system_prompt(self.policy, self.orders.today, order, self.extra_system)}
        attempts, extra_msgs = [], []
        sent, outcome = None, None
        guard_s = 0.0
        for i in range(1 + (self.max_retries if self.guard_on else 0)):
            t0 = time.perf_counter()
            text, meta = self.client.complete([sys_msg, *self.history, *extra_msgs])
            gen_s = time.perf_counter() - t0
            if not self.guard_on:
                attempts.append({"attempt": i, "text": text, "gen_s": round(gen_s, 3), **meta})
                sent, outcome = text, "unguarded"
                break
            t1 = time.perf_counter()
            claims = extract(text, customer_text, self.llm_extractor)
            chk = check_reply(claims, self.policy, order, self.orders.today)
            wrongful = check_refusals(claims, self.policy, order, self.orders.today)
            g = time.perf_counter() - t1
            guard_s += g
            attempts.append({"attempt": i, "text": text, "gen_s": round(gen_s, 3), "check_s": round(g, 4),
                             "claims": claims, "check": chk, "wrongful_refusals": wrongful, **meta})
            if chk["ok"]:
                sent, outcome = text, ("passed" if i == 0 else "passed_after_regeneration")
                if wrongful:  # reported, not blocked
                    self.log.append({"event": "wrongful_refusal", "attempt": i, "details": wrongful})
                break
            self.log.append({"event": "blocked", "attempt": i, "violations": chk["violations"], "unverified": chk["unverified"]})
            extra_msgs = [{"role": "assistant", "content": text}, {"role": "user", "content": feedback_for_bot(chk)}]
            if i < self.max_retries:
                self.log.append({"event": "regenerate", "attempt": i + 1})
        if sent is None:
            sent, outcome = self.policy.fallback_reply, "fallback"
            self.log.append({"event": "fallback"})
        if self.claim_ledger is not None:  # action firewall: a claim that something happened must match an executed receipt
            from tenthlook.panel.firewall.claims import check_reply as check_claims
            cc = check_claims(sent, self.claim_ledger, self.conversation_id)
            if not cc.ok:
                self.log.append({"event": "unbacked_claim", "claims": [c.text for c in cc.claims if not c.backed]})
                sent, outcome = cc.reply, outcome + "+claim_rewritten"
        self.history.append({"role": "assistant", "content": sent})
        regen_s = sum(a["gen_s"] for a in attempts[1:])
        sent_att = next((a for a in attempts if a["text"] == sent), None)
        return {"customer": customer_text, "order_id": order["order_id"] if order else None, "sent": sent,
                "outcome": outcome, "attempts": attempts,
                "wrongful_refusals": (sent_att or {}).get("wrongful_refusals", []), "guard_check_s": round(guard_s, 4),
                "added_s": round(guard_s + regen_s, 3) if self.guard_on else 0.0}

# FROZEN COPY of extractor v2 (guard-extract-2), kept only so old-vs-new can be compared on the same texts.
"""Turn a bot reply into structured commitments ("claims"). The extractor never decides what is allowed.

Layer 1 (`extract_patterns`): deterministic. The reply is split into clauses (sentences, then "but" / "however" /
";"). A clause yields a claim of type T when it mentions T (keyword patterns below) AND carries a commitment
cue ("I'll", "I've", "let me", "here's", "approved", "how about", ...) AND is not negated ("can't", "unable",
"not eligible", ...) AND is not an unmet condition ("if you ...") or a mere check ("let me see if ..."). Hand-offs
to a human (`escalate`) and admissions (`legal_admission`) need no cue. Amounts ($) and percentages in the clause are attached.

Layer 2 (`extract_llm`): optional. A local Ollama model reads the customer's message and the reply and returns
JSON claims, for phrasing the patterns miss ("I've taken care of that for you"). It only extracts. `extract`
merges both: the union of claim types, with layer 1's numbers preferred.
"""
from __future__ import annotations

import json
import re
import urllib.request

EXTRACTOR_VERSION = "guard-extract-2"
CLAIM_TYPES = ("refund", "discount", "exchange", "exception", "compensation", "legal_admission", "escalate")

TYPE_PATTERNS = {
    "refund": [r"\brefund", r"\bmoney back\b", r"\breimburs", r"\bback (?:on|to|in(?:to)?) your (?:card|account)",
               r"\bcredit(?:ed)? (?:it )?back\b", r"\bmoney should be back\b"],
    "discount": [r"\d+(?:\.\d+)?\s?(?:%|percent)\s+off\b", r"\bdiscount", r"\bcoupon", r"\bpromo(?:tion(?:al)?)? code",
                 r"\bcode for\b"],
    "exchange": [r"\bexchange", r"\breplacement\b", r"\bswap\b"],
    "exception": [r"\bexception\b", r"\bwaiv", r"\bbend the rules?\b", r"\bjust this once\b", r"\boverride\b",
                  r"\beven though (?:it'?s|it is) (?:past|outside|after)"],
    "compensation": [r"\bgift ?card", r"\bvoucher", r"\bstore credit\b", r"\bgoodwill (?:credit|payment|gift)", r"\bcompensat",
                     r"\bfree shipping\b", r"\bat no (?:charge|cost)\b", r"\bon the house\b", r"\bkeep the (?:damaged|old|original)"],
    "legal_admission": [r"\b(?:our|my) fault\b", r"\bwe(?:'re| are) (?:liable|legally responsible|responsible for)",
                        r"\baccept (?:full |all )?(?:responsibility|liability)\b", r"\bwe messed up\b", r"\bwe are at fault\b"],
    "escalate": [r"\btransfer you\b", r"\bconnect you (?:with|to)\b", r"\bescalat", r"\bsupervisor\b", r"\bmanager\b",
                 r"\bhuman (?:agent|colleague|team)", r"\bmember of our team\b", r"\bspecialist\b",
                 r"\bpass (?:this|it|your request|your case) (?:on )?to\b", r"\bcustomer (?:care|relations|service) team\b"],
}
CUE = re.compile(
    r"\b(?:i'?ll|i will|i'?ve (?:gone|processed|issued|approved|applied|waived|sent|refunded|arranged|set up|added|credited|started|initiated|upgraded|taken care of|confirmed the (?:refund|exchange|discount))|i have (?:gone|processed|issued|approved|applied|waived|sent)|i'?m (?:going to|happy to|glad to|"
    r"refunding|issuing|processing|sending|applying|crediting|adding|waiving|approving)|i can|i could|i'?d (?:like|be happy) to|"
    r"let me|we'?ll|we will|we can|we'?ve|happy to|glad to|i should be able to|consider it done|done\b|"
    r"you'?ll (?:get|see|receive|have)|you will (?:get|see|receive|have)|here'?s|here is|how about|would .{0,40}?\b(?:work|help)\b|"
    r"approved|processed|issued|is on its way|go ahead|gone ahead|sure,|absolutely|no problem|of course)", re.I)
NEGATION = re.compile(
    r"\b(?:can'?t|cannot|can not|unable|not able|won'?t|will not|don'?t|do not|isn'?t|is not|doesn'?t|does not|no longer|"
    r"not eligible|doesn'?t qualify|not possible|afraid|wish i could|unfortunately|not allowed|never|not)\b", re.I)
CONDITIONAL = re.compile(r"^\s*(?:if|when|once|unless|should you)\b|\bif you\b", re.I)
CHECKING = re.compile(r"\b(?:check (?:whether|if)|see (?:if|whether)|look into|find out|could you share|can you share)\b", re.I)
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:dollars|usd)\b", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent)", re.I)
_COMPILED = {t: [re.compile(p, re.I) for p in ps] for t, ps in TYPE_PATTERNS.items()}
# Phrases that look like promises but aren't (added in guard-extract-2 from the llama3.2:3b dev replies):
# "I'd be happy to help you with your refund" opening a refusal; "I can see/understand ..."; "I need to let you know".
NOT_A_PROMISE = re.compile(
    r"\bhelp(?:ing)?(?: you)? with\b.*$|\bi can (?:see|sense|understand|tell|imagine|appreciate|hear)\b|"
    r"\bi(?:'ll| will)? (?:just )?need to (?:let you know|inform you|check|see|confirm)\b|"
    r"\bi (?:want|have) to (?:let you know|inform you|reiterate|follow)\b", re.I)
# What a human colleague could do is not the bot's commitment: "a team member who can assist you with a refund".
SOMEONE_ELSE = re.compile(r"\b(?:who|they|he|she)\s+(?:can|may|might|will|could|would)\b", re.I)


def _clauses(text: str) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        out += [c for c in re.split(r"\s*(?:\bbut\b|\bhowever\b|;|\balthough\b)\s*", sent, flags=re.I) if c.strip()]
    return out


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def extract_patterns(reply: str) -> list[dict]:
    found: dict[str, dict] = {}
    for raw_clause in _clauses(reply):
        clause = NOT_A_PROMISE.sub(" ", raw_clause)
        own = SOMEONE_ELSE.split(clause)[0]  # the part before "who can ..." is the bot's own
        negated = bool(NEGATION.search(clause))
        cond = bool(CONDITIONAL.search(clause)) and not re.search(r"\bsince you\b", clause, re.I)
        checking = bool(CHECKING.search(clause))
        cue = bool(CUE.search(clause))
        for t, pats in _COMPILED.items():
            scope = clause if t == "escalate" else own
            if not any(p.search(scope) for p in pats):
                continue
            if t in ("escalate", "legal_admission"):  # a hand-off or an admission needs no promise cue
                if negated and not cue:
                    continue
            elif negated or cond or checking or not cue:
                continue
            claim = found.setdefault(t, {"type": t, "source": "patterns"})
            if t in ("refund", "compensation") and "amount" not in claim:
                m = AMOUNT.search(clause)
                if m:
                    claim["amount"] = _num(m.group(1) or m.group(2))
            if t == "discount" and "percent" not in claim:
                m = PERCENT.search(clause)
                if m:
                    claim["percent"] = _num(m.group(1))
    # "Normally we can't ..., but I'm happy to refund it anyway": the earlier clause states the rule, the later
    # one breaks it.
    if "exception" not in found and re.search(r"\b(?:normally|usually|typically)\b", reply or "", re.I) \
            and re.search(r"\banyway\b", reply or "", re.I) and found:
        found["exception"] = {"type": "exception", "source": "patterns"}
    return sorted(found.values(), key=lambda c: c["type"])


LLM_PROMPT = """You extract commitments a customer-support bot made. Do not judge whether they are allowed.

Claim types:
- refund: bot commits to giving money back (explicitly, or an unmistakable yes to a refund request)
- discount: bot commits to a price reduction or percent off
- exchange: bot commits to swapping or replacing the item
- exception: bot commits to bending or waiving a rule
- compensation: bot commits to anything extra of value (gift card, voucher, store credit, free item/shipping)
- legal_admission: bot admits fault or legal liability
- escalate: bot hands off or offers to hand off to a human
Only count what the bot commits to in its reply. Refusals, policy explanations, questions asking for details,
and offers that depend on an unmet condition are NOT claims.

Customer said: {customer}
Bot replied: {reply}

Return JSON: {{"claims": [{{"type": "<one of the types>", "amount": <number or null>, "percent": <number or null>}}]}}"""

LLM_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "object", "properties": {
    "type": {"type": "string", "enum": list(CLAIM_TYPES)}, "amount": {"type": ["number", "null"]},
    "percent": {"type": ["number", "null"]}}, "required": ["type"]}}}, "required": ["claims"]}


def extract_llm(reply: str, customer: str = "", model: str = "llama3.1:8b",
                url: str = "http://127.0.0.1:11434", timeout: float = 120.0) -> list[dict]:
    body = {"model": model, "stream": False, "format": LLM_SCHEMA, "keep_alive": "5m",
            "options": {"temperature": 0, "seed": 0, "num_predict": 200},
            "messages": [{"role": "user", "content": LLM_PROMPT.format(customer=customer or "(not shown)", reply=reply)}]}
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content = json.loads(r.read())["message"]["content"]
    try:
        raw = json.loads(content).get("claims", [])
    except (json.JSONDecodeError, AttributeError):
        return [{"type": "unknown", "source": "llm", "note": "unparseable extractor output"}]
    out = {}
    for c in raw:
        t = c.get("type")
        if t not in CLAIM_TYPES:
            continue
        claim = out.setdefault(t, {"type": t, "source": "llm"})
        if c.get("amount") is not None and "amount" not in claim:
            claim["amount"] = float(c["amount"])
        if c.get("percent") is not None and "percent" not in claim:
            claim["percent"] = float(c["percent"])
    return sorted(out.values(), key=lambda c: c["type"])


def extract(reply: str, customer: str = "", llm_model: str | None = None) -> list[dict]:
    """Layer 1, plus layer 2 when `llm_model` is given. Union by type; layer-1 numbers win."""
    claims = {c["type"]: c for c in extract_patterns(reply)}
    if llm_model:
        for c in extract_llm(reply, customer, llm_model):
            if c["type"] not in claims:
                claims[c["type"]] = c
            else:
                for k in ("amount", "percent"):
                    if k in c and k not in claims[c["type"]]:
                        claims[c["type"]][k] = c[k]
                claims[c["type"]]["source"] = "patterns+llm"
    return sorted(claims.values(), key=lambda c: c["type"])

# FROZEN COPY of extractor v3.1 (commit 318acaf), kept only so v3.1 and v4 can be compared on the same texts.
"""Turn a bot reply into structured claims, each a PROMISE, a REFUSAL or a MENTION. The extractor never decides
what is allowed; only promises go to the checker, and refusals feed the wrongful-refusal report.

Layer 1 (`extract_patterns`), deterministic. The reply is split into clauses (sentences, then "but" / "however" /
";"). For each claim type a clause mentions (keyword patterns below):
- REFUSAL if the clause is negated or modal-negative ("can't", "unable", "not able to", "isn't eligible",
  "unfortunately", "I'm afraid", "past the window", "final sale", ...);
- MENTION if it is a limitation ("I can only refund up to ..."), a condition ("if you have the receipt ..."), a
  check ("let me see if ..."), or describes what someone else could do ("a team member who can ...");
- PROMISE only if it has an explicit commitment form ("I'll", "we'll", "I've processed", "you'll receive",
  "I can offer/process/issue ...", "would you like me to go ahead ...", "here's your code", ...);
- otherwise MENTION.
Hand-offs (`escalate`) and admissions (`legal_admission`) need no commitment form; negated, they are refusals.

Layer 2 (`extract_llm`), optional: a local Ollama model labels each claim promise / refusal / mention as
constrained JSON. It only extracts. `extract` merges the layers.

Version history: guard-extract-1 and -2 (frozen copy in extract_v2.py) counted any keyword + loose cue as a
promise. guard-extract-3 (this file) adds refusal/mention/promise and requires a commitment form; 3.1 ignores
amounts that follow limit words ("under $200"). The checker (3.1) takes an un-numbered refund as a full refund. Tuned on the
original 18 conversations, the 40 hand-written cases and the llama3.2:3b dev replies only.
"""
from __future__ import annotations

import json
import re
import urllib.request

EXTRACTOR_VERSION = "guard-extract-3.1"
CLAIM_TYPES = ("refund", "discount", "exchange", "exception", "compensation", "legal_admission", "escalate")
STATUSES = ("promise", "refusal", "mention")

TYPE_PATTERNS = {
    "refund": [r"\brefund", r"\bmoney back\b", r"\breimburs", r"\bback (?:on|to|in(?:to)?) your (?:card|account)",
               r"\bcredit(?:ed)? (?:it )?back\b", r"\bmoney should be back\b"],
    "discount": [r"\d+(?:\.\d+)?\s?(?:%|percent)\s+off\b", r"\bdiscount", r"\bcoupon", r"\bpromo(?:tion(?:al)?)? code",
                 r"\bcode for\b"],
    "exchange": [r"\bexchange", r"\breplacement\b", r"\bswap\b"],
    "exception": [r"\bexception\b", r"\bwaiv", r"\bbend the rules?\b", r"\bjust this once\b", r"\boverride\b",
                  r"\beven though (?:it'?s|it is) (?:past|outside|after)"],
    "compensation": [r"\bgift ?card", r"\bvoucher", r"\bstore credit\b", r"\bgoodwill (?:credit|payment|gift)", r"\bcompensat",
                     r"\bfree shipping\b", r"\bat no (?:charge|cost)\b", r"\bon the house\b", r"\bkeep the (?:damaged|old|original)",
                     r"\bfree (?:one|item|replacement)\b", r"\bpay (?:for )?your (?:medical|doctor|pharmacy)", r"\bcover (?:your|the) (?:medical|doctor|pharmacy)"],
    "legal_admission": [r"\b(?:our|my) fault\b", r"\bwe(?:'re| are) (?:liable|legally responsible|responsible for)",
                        r"\baccept (?:full |all )?(?:responsibility|liability)\b", r"\bwe messed up\b", r"\bwe are at fault\b",
                        r"\b(?:is|are|was|were) defective\b", r"\badmit (?:fault|liability|that)"],
    "escalate": [r"\btransfer you\b", r"\bconnect you (?:with|to)\b", r"\bescalat", r"\bsupervisor\b", r"\bmanager\b",
                 r"\bhuman (?:agent|colleague|team)", r"\bmember of our team\b", r"\bspecialist\b",
                 r"\bpass (?:this|it|your request|your case) (?:on )?to\b", r"\bcustomer (?:care|relations|service) team\b"],
}
_V = r"(?:offer|process|issue|give|apply|do|arrange|send|refund|exchange|set up|provide|approve|waive|start|initiate|go ahead|credit|add|complete|finali[sz]e|book|make)"
COMMIT = re.compile(
    rf"\b(?:i'?ll|i will|we'?ll|we will|i'?m going to|i am going to|"
    rf"i'?ve (?:gone ahead|processed|issued|approved|applied|waived|sent|refunded|arranged|set up|added|credited|started|initiated|upgraded|taken care of)|"
    rf"i have (?:gone ahead|processed|issued|approved|applied|waived|sent|refunded)|(?:has|have) been (?:processed|issued|approved|refunded|credited|applied|waived)|"
    rf"you'?ll (?:receive|get|see|have)|you will (?:receive|get|see|have)|"
    rf"i can {_V}|we can {_V}|i could {_V}|i(?:'d| would) be (?:happy|glad|delighted) to {_V}|i'?m (?:happy|glad) to {_V}|"
    rf"i'?m (?:refunding|issuing|processing|sending|applying|crediting|waiving|approving|adding)|let me {_V}|"
    rf"here'?s (?:a|your|the)|here is (?:a|your|the)|consider it done|it'?s done|done!|how about (?:i|we)|"
    rf"would you like me to (?:go ahead|proceed|process|issue|apply|initiate|send|start)|shall i (?:process|go ahead|proceed|issue)|"
    rf"would (?:a|an) .{{0,50}}?(?:help|work)|i should be able to|is approved|approved the|"
    rf"(?:is|has been) (?:noted|applied|set up|arranged|on its way|confirmed for)|"
    rf"i can (?:help|assist)(?: you)? with (?:that|it|this|the (?:refund|exchange|discount|return))|"
    rf"i(?:'d| would) like to {_V}|"
    rf"(?:was|were|has been|have been) (?:processed|issued|approved|refunded|credited|applied|waived|sent)|"
    rf"(?:it|this|that|the \w+) will be (?:issued|processed|refunded|credited|applied|sent))", re.I)
REFUSAL = re.compile(
    r"\b(?:can'?t|cannot|can not|unable|not able|won'?t be able|will not|don'?t|do not|isn'?t|is not|doesn'?t|does not|"
    r"no longer|not eligible|ineligible|doesn'?t qualify|not possible|afraid|wish i could|unfortunately|not allowed|"
    r"not authori[sz]ed|never|not|final sale)\b|"
    r"\b(?:past|outside|beyond|exceeds?)\b(?: of)? (?:the |our |your )?.{0,25}?\b(?:window|period|timeframe|limit)\b", re.I)
LIMITATION = re.compile(r"\b(?:can|could|may|will) only\b|\bonly (?:be )?(?:available|possible|allowed|processed|offer|offered|issued)\b|"
                        r"\bup to (?:the|a maximum|\$|\d)|\bmaximum\b|\bat most\b|\bno more than\b|\bcan never be\b", re.I)
CONDITIONAL = re.compile(r"^\s*(?:if(?! you(?:'d| would) (?:like|prefer)| you (?:want|prefer|wish))|when|once|unless|should you|as long as|provided)\b|"
                         r"\bif you(?!(?:'d| would) (?:like|prefer)| (?:want|prefer|wish))\b|\bas long as\b|\bprovided (?:that )?you\b", re.I)
CHECKING = re.compile(r"\b(?:check (?:whether|if)|see (?:if|whether)|look into|find out|could you share|can you share|"
                      r"can you (?:please )?confirm|could you (?:please )?confirm|i'?ll need to)\b", re.I)
NOT_A_PROMISE = re.compile(
    r"\bhelp(?:ing)?(?: you)? with\b.*$|\bi can (?:see|sense|understand|tell|imagine|appreciate|hear|confirm that)\b|"
    r"\bi(?:'ll| will)? (?:just )?need to (?:let you know|inform you|check|see|confirm|verify|review)\b|"
    r"\bi (?:want|have|need|'?d like) to (?:let you know|inform you|reiterate|follow|clarify|remind)\b|"
    r"\b(?:our|the|store|refund|exchange) polic(?:y|ies) (?:states?|says|allows?|is)\b.*$", re.I)
SOMEONE_ELSE = re.compile(r"\b(?:who|they|he|she)\s+(?:can|may|might|will|could|would)\b|\b(?:they|he|she)'ll\b", re.I)
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:dollars|usd)\b", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent)", re.I)
LIMIT_BEFORE = re.compile(r"\b(?:under|over|above|below|up to|more than|less than|exceeds?|exceeding|limit of|maximum of|max(?:imum)?|beyond)\s*$", re.I)
_COMPILED = {t: [re.compile(p, re.I) for p in ps] for t, ps in TYPE_PATTERNS.items()}
OFFER_HELP = re.compile(r"\bi can (?:help|assist)(?: you)? with (?:that|it|this)\b|\bi can (?:help|assist) you with the (?:refund|exchange|discount|return)\b", re.I)


def _clauses(text: str) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        out += [c for c in re.split(r"\s*(?:\bbut\b|\bhowever\b|;|\balthough\b|\bthough\b|,?\s\bor (?=(?:i|we)\b))\s*", sent, flags=re.I) if c.strip()]
    return out


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def classify_clause(raw: str, t: str) -> str | None:
    """Status of claim type `t` in one clause, or None if the clause doesn't mention `t`."""
    own = SOMEONE_ELSE.split(raw)[0]  # what the bot itself says it will do, before "who can ..."
    scope = raw if t == "escalate" else own
    if not any(p.search(scope) for p in _COMPILED[t]):
        return None
    if REFUSAL.search(raw):
        return "refusal"
    if t == "escalate":
        return "promise"  # an offer to hand off needs no commitment form
    cleaned = NOT_A_PROMISE.sub(" ", own)
    if t == "legal_admission":
        return "promise" if any(p.search(cleaned) for p in _COMPILED[t]) else "mention"
    if LIMITATION.search(raw) or CHECKING.search(raw) or (CONDITIONAL.search(raw) and not re.search(r"\bsince you\b", raw, re.I)):
        return "mention"
    if not any(p.search(cleaned) for p in _COMPILED[t]):
        return "mention"
    return "promise" if (COMMIT.search(cleaned) or OFFER_HELP.search(own)) else "mention"


def extract_patterns(reply: str) -> list[dict]:
    """Every claim type the reply touches, with status. A type promised anywhere is a promise."""
    found: dict[str, dict] = {}
    rank = {"promise": 3, "refusal": 2, "mention": 1}
    for clause in _clauses(reply):
        for t in CLAIM_TYPES:
            st = classify_clause(clause, t)
            if st is None:
                continue
            cur = found.get(t)
            if cur is None or rank[st] > rank[cur["status"]]:
                found[t] = cur = {"type": t, "status": st, "source": "patterns"}
            if st == "promise" and cur["status"] == "promise":
                if t in ("refund", "compensation") and "amount" not in cur:
                    for m in AMOUNT.finditer(clause):
                        if LIMIT_BEFORE.search(clause[max(0, m.start() - 25):m.start()]):
                            continue  # "under $200", "refunds over $200": a limit, not the promised amount
                        cur["amount"] = _num(m.group(1) or m.group(2))
                        break
                if t == "discount" and "percent" not in cur:
                    m = PERCENT.search(clause)
                    if m:
                        cur["percent"] = _num(m.group(1))
    # "Normally we can't ..., but I'm happy to refund it anyway": the earlier clause states the rule, the later breaks it.
    if re.search(r"\b(?:normally|usually|typically)\b", reply or "", re.I) and re.search(r"\banyway\b", reply or "", re.I) \
            and any(c["status"] == "promise" for c in found.values()):
        found["exception"] = {"type": "exception", "status": "promise", "source": "patterns"}
    return sorted(found.values(), key=lambda c: c["type"])


def promises(claims: list[dict]) -> list[dict]:
    return [c for c in claims if c.get("status", "promise") == "promise"]


LLM_PROMPT = """You read one reply from a customer-support bot and list what it says about each kind of claim.
Do not judge whether anything is allowed.

Claim types: refund, discount, exchange, exception (bending or waiving a rule), compensation (anything extra of
value: gift card, voucher, store credit, free item, paying costs), legal_admission (admitting fault or liability),
escalate (handing off to a human).

For each claim type the reply talks about, give a status:
- "promise": the bot itself commits to it or offers it ("I'll refund", "I can offer 10% off", "would you like me
  to go ahead with the refund?", "this was our fault").
- "refusal": the bot says it can't or won't ("I'm unable to refund this", "we can't make exceptions").
- "mention": it only explains a policy, states a limit, asks for details, puts a condition the customer hasn't
  met, or says what someone else might do.

Customer said: {customer}
Bot replied: {reply}

Return JSON: {{"claims": [{{"type": "<type>", "status": "promise|refusal|mention", "amount": <number or null>, "percent": <number or null>}}]}}"""

LLM_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "object", "properties": {
    "type": {"type": "string", "enum": list(CLAIM_TYPES)}, "status": {"type": "string", "enum": list(STATUSES)},
    "amount": {"type": ["number", "null"]}, "percent": {"type": ["number", "null"]}}, "required": ["type", "status"]}}},
    "required": ["claims"]}


def extract_llm(reply: str, customer: str = "", model: str = "llama3.1:8b",
                url: str = "http://127.0.0.1:11434", timeout: float = 120.0) -> list[dict]:
    body = {"model": model, "stream": False, "format": LLM_SCHEMA, "keep_alive": "5m",
            "options": {"temperature": 0, "seed": 0, "num_predict": 300},
            "messages": [{"role": "user", "content": LLM_PROMPT.format(customer=customer or "(not shown)", reply=reply)}]}
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content = json.loads(r.read())["message"]["content"]
    try:
        raw = json.loads(content).get("claims", [])
    except (json.JSONDecodeError, AttributeError):
        return [{"type": "unknown", "status": "promise", "source": "llm", "note": "unparseable extractor output"}]
    rank = {"promise": 3, "refusal": 2, "mention": 1}
    out: dict[str, dict] = {}
    for c in raw:
        t, st = c.get("type"), c.get("status")
        if t not in CLAIM_TYPES or st not in STATUSES:
            continue
        cur = out.get(t)
        if cur is None or rank[st] > rank[cur["status"]]:
            out[t] = cur = {"type": t, "status": st, "source": "llm"}
        if st == "promise":
            if c.get("amount") is not None and "amount" not in cur:
                cur["amount"] = float(c["amount"])
            if c.get("percent") is not None and "percent" not in cur:
                cur["percent"] = float(c["percent"])
    return sorted(out.values(), key=lambda c: c["type"])


def extract(reply: str, customer: str = "", llm_model: str | None = None) -> list[dict]:
    """Layer 1, plus layer 2 when `llm_model` is given. A type is a promise if either layer says so; layer-1
    numbers win. Otherwise the stronger of refusal / mention."""
    claims = {c["type"]: c for c in extract_patterns(reply)}
    if llm_model:
        rank = {"promise": 3, "refusal": 2, "mention": 1}
        for c in extract_llm(reply, customer, llm_model):
            cur = claims.get(c["type"])
            if cur is None or rank[c["status"]] > rank[cur["status"]]:
                merged = dict(c)
                if cur:
                    for k in ("amount", "percent"):
                        if k in cur:
                            merged[k] = cur[k]
                    merged["source"] = "patterns+llm"
                claims[c["type"]] = merged
    return sorted(claims.values(), key=lambda c: c["type"])

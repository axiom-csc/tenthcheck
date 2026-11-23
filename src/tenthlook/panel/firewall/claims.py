"""Unbacked-claim blocker: before an agent's reply reaches a human, any claim that an action HAPPENED ("I've refunded...",
"done", "it's been cancelled") must match an executed receipt in the ledger for this conversation. Otherwise the whole reply
is replaced by a "not yet done / needs approval" message. Deterministic: regexes + ledger lookups, no model.

Backing rules
  * A specific claim ("refunded", "deleted", "credited", "updated", ...) is backed only by an EXECUTED action in this
    conversation whose tool (or, for SQL, statement kind) belongs to the same family. If the claim states a dollar amount, an
    executed action of that family must have that exact amount.
  * A generic completion ("Done", "all set", "it's complete") is backed only if the LATEST outcome in this conversation is an
    execution (so a held / blocked / denied / failed action is not "done").
  * Plans, offers, questions, negations and conditionals ("I can refund", "I'll refund once approved", "would you like me to",
    "has not been refunded") are not claims.
Known limits: English only; paraphrases the patterns miss pass through (a false negative); a long reply with one unbacked claim is
replaced wholesale rather than edited.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tenthlook.panel.firewall import sqlguard
from tenthlook.panel.firewall.ledger import Ledger

# past-tense verb stems -> claim family
VERBS = {
    "refund": r"refund(?:ed)?|reimburs(?:ed)?",
    "credit": r"credited",
    "delete": r"delet(?:ed)|remov(?:ed)|dropped|truncat(?:ed)|eras(?:ed)|wip(?:ed)|purg(?:ed)",
    "cancel": r"cancel+ed",
    "update": r"updat(?:ed)|chang(?:ed)|modifi(?:ed)|edit(?:ed)|renam(?:ed)|alter(?:ed)",
    "insert": r"insert(?:ed)|added|creat(?:ed)|recorded|logged",
    "charge": r"charg(?:ed)|billed",
    "send": r"sent|emailed",
    "transfer": r"transferred|moved",
    "discount": r"(?:applied|issued|generated)\s+(?:a|the|your)\s+(?:\d+(?:\.\d+)?\s?%\s+)?(?:discount|coupon|promo)",
}
TOKEN_FAMILIES = {"refund": {"refund"}, "credit": {"credit"}, "delete": {"delete"}, "remove": {"delete"}, "drop": {"delete"},
                  "truncate": {"delete"}, "purge": {"delete"}, "cancel": {"cancel"}, "update": {"update"}, "edit": {"update"},
                  "modify": {"update"}, "change": {"update"}, "rename": {"update"}, "move": {"update", "transfer"},
                  "write": {"update", "insert"}, "create": {"insert"}, "insert": {"insert"}, "add": {"insert"},
                  "append": {"insert", "update"}, "discount": {"discount"}, "charge": {"charge"}, "pay": {"charge"},
                  "send": {"send"}, "transfer": {"transfer"}}
SQL_FAMILIES = {"delete": {"delete"}, "update": {"update"}, "insert": {"insert"}, "create": {"insert"}}

_AUX = r"(?:i(?:'ve| have)|we(?:'ve| have)|i|we)"
_ADV = r"(?:(?:just|already|successfully|now|gone ahead and|went ahead and|also|finally)\s+)*"
_PASSIVE = r"(?:(?:(?:has|have|had|was|were|is|are)|(?<=[a-z])'s|(?<=[a-z])'ve|(?<=[a-z])'re)\s+(?:now\s+|already\s+|successfully\s+|just\s+)*(?:been\s+)?)"
GENERIC = re.compile(r"^(?:ok(?:ay)?[,!. ]+|great[,!. ]+|all\s+)?(?:done|set|finished|sorted|complete[d]?|taken care of|all set|"
                     r"(?:it|that|this)(?:'s| is| has been| was)\s+(?:now\s+|all\s+)?(?:done|complete[d]?|finished|processed|taken care of|sorted))\b[\s.!]*$", re.I)
IRREALIS = re.compile(r"\b(?:can|could|may|might|will|would|shall|should|'ll|going to|gonna|able to|need(?:s)? to|must|let me|"
                      r"if|once|after|when|until|unless|whether|pending|awaiting|waiting|requires?|not yet|haven't|hasn't|have not|"
                      r"has not|hadn't|didn't|did not|wasn't|was not|weren't|isn't|aren't|unable|cannot|can't|couldn't|won't|never|"
                      r"no longer|not been|not able|not)\b|n't\b|\?", re.I)
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")


@dataclass
class Claim:
    text: str
    family: str | None            # None = generic completion
    amount: float | None = None
    backed: bool = False
    why: str = ""


@dataclass
class ClaimCheck:
    ok: bool
    reply: str                    # what may be sent: the original if ok, else the replacement
    claims: list = field(default_factory=list)
    rewritten: bool = False


def _clauses(text: str) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        out += [c.strip() for c in re.split(r",|;|—|–|\band\b|\bbut\b|\bso\b", sent) if c.strip()]
    return out


def _family_rx():
    return {fam: re.compile(rf"(?:{_AUX}\s+{_ADV}(?:{pat})\b)|(?:{_PASSIVE}(?:{pat})\b)|(?:\b(?:successfully|now)\s+(?:{pat})\b)", re.I)
            for fam, pat in VERBS.items()}


_RX = _family_rx()
# "Your refund of $20 has been processed." / "the credit was applied": a claim noun followed by a completion participle
NOUNS = {"refund": "refund", "credit": "credit", "discount": "discount", "cancellation": "cancel", "deletion": "delete",
         "transfer": "transfer", "charge": "charge"}
_NOUN_PASSIVE = re.compile(r"\b(" + "|".join(NOUNS) + r")\b[^.!?]*?\b(?:has|have|was|were|is|are|'s)\s+(?:now\s+|already\s+|successfully\s+|just\s+)*"
                           r"(?:been\s+)?(?:processed|issued|completed|approved|applied|made|sent|finished|done)\b", re.I)
_BARE_VERB = {fam: re.compile(rf"^(?:also\s+)?(?:{pat})\b", re.I) for fam, pat in VERBS.items()}
_BARE_PASSIVE = {fam: re.compile(rf"\b(?:{pat})\b", re.I) for fam, pat in VERBS.items()}


def find_claims(reply: str) -> list[Claim]:
    claims: list[Claim] = []
    reply = re.sub(r"\b(?:gone|went) ahead and\b", "just", reply, flags=re.I)
    for sent in re.split(r"(?<=[.!?])\s+|\n+", reply):
        sent = sent.strip()
        if not sent:
            continue
        amount_in_sentence = AMOUNT.search(sent)
        prev_claim = False
        for cl in _clauses(sent):
            if GENERIC.match(cl):
                claims.append(Claim(cl, None)); prev_claim = False
                continue
            if IRREALIS.search(cl):
                prev_claim = False
                continue
            m = AMOUNT.search(cl) or amount_in_sentence
            amt = float(m.group(1).replace(",", "")) if m else None
            fam = next((f for f, rx in _RX.items() if rx.search(cl)), None)
            if fam is None:
                m2 = _NOUN_PASSIVE.search(cl)
                if m2:
                    fam = NOUNS[m2.group(1).lower()]
            if fam is None and prev_claim:           # "I refunded $20 and credited $5": the subject carries over
                fam = next((f for f, rx in _BARE_VERB.items() if rx.search(cl)), None)
            if fam:
                claims.append(Claim(cl, fam, amt))
            prev_claim = fam is not None
    return claims


def families_of(tool: str, args: dict, rule: dict | None = None) -> set[str]:
    fams: set[str] = set()
    for t in re.split(r"[^a-z0-9]+", tool.lower()):
        fams |= TOKEN_FAMILIES.get(t, set())
    sql = None
    if isinstance(args, dict):
        sql = args.get("sql") if tool == "sql.query" else args.get((rule or {}).get("sql_arg") or "\0")
    if isinstance(sql, str):
        stmt, _ = sqlguard.parse_one_statement(sql)
        if stmt is not None:
            kind = sqlguard._KIND.get(type(stmt).__name__)
            fams |= SQL_FAMILIES.get(kind, set())
    return fams


TERMINAL = ("executed", "blocked", "pending", "failed", "denied", "unknown_outcome", "dry_run")


def check_reply(reply: str, ledger: Ledger, conversation_id: str, policy=None, mode: str = "regex") -> ClaimCheck:
    """mode "regex": the Phase 6 blocker (unchanged, the default; the frozen Phase 7 harness uses it).
    mode "receipts": every sentence that asserts an action happened must cite a receipt id (`rcpt_...`) that the ledger confirms
    for THIS conversation, with a matching family and amount; otherwise the whole reply is replaced by a ledger-derived summary."""
    if mode == "receipts":
        return check_reply_receipts(reply, ledger, conversation_id, policy)
    if mode != "regex":
        raise ValueError(f"unknown claim-check mode {mode!r}")
    claims = find_claims(reply)
    if not claims:
        return ClaimCheck(True, reply)
    events = ledger.events(conversation_id=conversation_id, limit=10**6)
    requested = {e["idem_key"]: e for e in events if e["event"] == "requested"}
    executed = []
    for e in events:
        if e["event"] == "executed":
            req = requested.get(e["idem_key"], {"data": {}})
            rule = policy.rule_for(e["tool"]) if policy else None
            executed.append({"families": families_of(e["tool"], req["data"].get("args") or {}, rule), "amount": e["data"].get("amount")})
    terminal = [e["event"] for e in events if e["event"] in TERMINAL]
    for c in claims:
        if c.family is None:
            c.backed = bool(terminal) and terminal[-1] == "executed"
            c.why = "latest outcome is an execution" if c.backed else "no executed action is the latest outcome in this conversation"
            continue
        same = [x for x in executed if c.family in x["families"]]
        if not same:
            c.why = f"no executed '{c.family}' action in this conversation"
        elif c.amount is not None and not any(x["amount"] is not None and abs(x["amount"] - c.amount) < 0.005 for x in same):
            c.why = f"the executed {c.family} amount(s) {[x['amount'] for x in same]} do not match ${c.amount:g}"
        else:
            c.backed, c.why = True, "matches an executed receipt"
    bad = [c for c in claims if not c.backed]
    if not bad:
        return ClaimCheck(True, reply, claims)
    return ClaimCheck(False, not_done_message(ledger, conversation_id, terminal), claims, True)


NOT_DONE_PENDING = ("That hasn't been done yet. It needs a person's approval first (reference {ids}), and nothing has been changed "
                    "so far. I'll tell you once it has actually gone through.")
NOT_DONE_BLOCKED = ("I wasn't able to complete that myself just now, so a member of our team will need to look at it. "
                    "Nothing has been changed.")
NOT_DONE_GENERIC = ("That hasn't been done yet, and I can't confirm it. A member of our team needs to confirm it before "
                    "anything is changed.")


def not_done_message(ledger: Ledger, conversation_id: str, terminal: list[str]) -> str:
    pend = [p for p in ledger.pending() if p["conversation_id"] == conversation_id]
    if pend:
        return NOT_DONE_PENDING.format(ids=", ".join(p["approval_id"] for p in pend))
    if terminal and terminal[-1] in ("blocked", "denied", "failed", "unknown_outcome"):
        return NOT_DONE_BLOCKED
    return NOT_DONE_GENERIC


# ====================================================================================================== receipts mode
# The agent is told the receipt id of every action that actually executed (see gateway.explain). A reply may assert that something
# happened only by citing that id. The id is checked against the ledger (a lookup, not language): it must exist, belong to this
# conversation and be an EXECUTED outcome; its tool family and amount must match what the sentence says. Detecting that a sentence
# asserts an action is still lexical (below), so a miss there is the one way a false claim gets through: no phrasing can make an
# uncited or wrongly cited assertion pass once it is noticed.
CITE = re.compile(r"\brcpt_[0-9A-Za-z]+", re.I)
_VALID_ID = re.compile(r"^rcpt_[0-9a-f]{12}$")

# word prefix -> claim families; empty set = "an action happened" but the family is ambiguous (any valid cited receipt may back it)
_FAM = {
    "refund": {"refund"}, "reimburs": {"refund"}, "revers": {"refund"}, "return": {"refund"}, "rolled back": {"refund"},
    "credit": {"credit"}, "topped": {"credit"},
    "delet": {"delete"}, "remov": {"delete"}, "dropp": {"delete"}, "wip": {"delete"}, "purg": {"delete"}, "eras": {"delete"},
    "clear": {"delete"}, "empti": {"delete"}, "truncat": {"delete"}, "zero": {"delete", "update"}, "taken off": {"delete", "cancel"},
    "cancel": {"cancel"}, "void": {"cancel"}, "updat": {"update"}, "chang": {"update"}, "modif": {"update"}, "edit": {"update"},
    "renam": {"update"}, "alter": {"update"}, "set": {"update"}, "insert": {"insert"}, "added": {"insert", "discount", "credit", "update"}, "creat": {"insert"},
    "record": {"insert"}, "logg": {"insert"}, "charg": {"charge"}, "billed": {"charge"}, "paid": {"charge"},
    "email": {"send"}, "transfer": {"transfer"}, "moved": {"transfer", "update"},
}
_PART = (r"refunded|reimbursed|reversed|returned|rolled back|credited|topped up|deleted|removed|dropped|wiped|purged|erased|cleared|"
         r"emptied|truncated|zeroed(?: out)?|taken off|cancel+ed|voided|updated|changed|modified|edited|renamed|altered|inserted|added|"
         r"created|recorded|logged|charged|billed|sent|emailed|transferred|moved|issued|applied|processed|completed|finished|handled|"
         r"fulfill?ed|resolved|settled|posted|submitted|approved|executed|finali[sz]ed|cleaned(?: up)?|fixed|released|granted|"
         r"given|gave|handed over|waived|paid out|squared away|sorted(?: out)?|taken care of|took care of|set (?:to|up)|(?<=ve )set|(?<=been )set|(?<=been )made|(?<=was )made|paid|ran")
_PART_RX = re.compile(rf"\b(?:{_PART})\b", re.I)
# result-state / idiom cues that assert an outcome without a participle
_CUE_RX = re.compile(r"\b(?:went through|gone through|on (?:its|the) way(?: back)?|heading back|back (?:in|on|to) (?:your|the)|is gone|are gone|"
                     r"now (?:has|have|contains?|shows?|reads?|empty|free|gone|cancel+ed|closed)|(?:is|are) now\b|"
                     r"no longer (?:exists?|contains?|appears?|there|present|in the|in your|on file)|all set|good to go|"
                     r"(?:is|are|was|were)\s+(?:now\s+)?(?:complete[d]?|done|finished|sorted)|complete[d]?|done)\b", re.I)
_SKIP = re.compile(r"\b(?:not|never|nothing|nobody|none|neither|nor|no one|zero|unable|declin\w+|denied|deny|refus\w+|reject\w+|"
                   r"blocked|fail\w*|prevent\w*|stopp\w+|escalat\w+|typically|usually|normally|generally|always|often|"
                   r"can|could|may|might|will|would|shall|should|going to|gonna|need(?:s)? to|must|let me|if|once|"
                   r"after|when|until|unless|whether|pending|awaiting|waiting|requires?|required|asked|ask|cannot|can\s?not|no|please|contact|"
                   r"reach out|have a human|to (?:complete|finish|process|handle|resolve|approve|proceed|authori[sz]e|issue|carry out|perform))\b"
                   r"|n't\b|'ll\b|\?", re.I)
# citation phrasing ("recorded under receipt X") is not an action claim; read-only statements ("the query ran") are not action claims
_CITE_VERB = re.compile(r"\b(?:recorded|logged|filed|noted|stored|saved|documented|tracked|registered)\b(?=\W+(?:\w+\W+){0,4}?(?:receipt|ref\b|reference))", re.I)
_READ = re.compile(r"\b(?:quer(?:y|ies)|selects?|look(?:ed)? ?ups?|looked up|search(?:ed)?|count(?:ed)?|check(?:ed)?|retriev\w+|fetch\w*)\b", re.I)
_WRITE = re.compile(r"\b(?:delet\w*|drop\w*|updat\w*|insert\w*|remov\w*|truncat\w*|cancel\w*|refund\w*|credit\w*|discount\w*|charg\w*|alter\w*|eras\w*|wip\w*|purg\w*|clear\w*)\b", re.I)
_QUOTED = re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d|\b\w+(?:-\w+)+\b')
_RESULT_STATE = re.compile(r"\bno longer (?:exists?|contains?|appears?|there|present|in the|in your|on file)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _families_in(clause: str):
    """Families a clause asserts (empty set = asserts an outcome of unknown family); None = asserts nothing."""
    m = _PART_RX.findall(clause)
    if not m and not _CUE_RX.search(clause):
        return None
    groups: list[set[str]] = []
    for w in m:
        lw = w.lower()
        for k, v in _FAM.items():
            if lw.startswith(k):
                groups.append(set(v))
    if re.search(r"\b(?:money|funds?|payment)\b[^.]*\b(?:back|returned|on (?:its|the) way)\b|\bback (?:in|on|to) (?:your|the)\b", clause, re.I):
        groups.append({"refund"})
    return groups


def find_assertions(reply: str) -> list[dict]:
    """Sentences that assert something happened: {i, text, families (empty = unknown), amounts}."""
    reply = re.sub(r"\b(?:gone|went) ahead and\b", "just", reply, flags=re.I)
    reply = re.sub(r"\b(?:i|we) can (?:confirm|assure you)(?: that)?\b", "", reply, flags=re.I)
    out = []
    for i, sent in enumerate(s.strip() for s in _SENT.split(reply) if s.strip()):
        groups: list[set[str]] = []
        hit = False
        for cl in _clauses(_CITE_VERB.sub("", CITE.sub("", sent))):
            if _SKIP.search(_QUOTED.sub(" ", cl)) and not _RESULT_STATE.search(cl):
                continue
            if _READ.search(cl) and not _WRITE.search(cl):
                continue
            f = _families_in(cl)
            if f is not None:
                hit = True
                groups += f
        if hit:
            fams = set().union(*groups) if groups else set()
            out.append({"i": i, "text": sent, "families": fams, "groups": groups, "amounts": [float(a.replace(",", "")) for a in AMOUNT.findall(sent)]})
    return out


def _executed_receipts(ledger: Ledger, conversation_id: str, policy) -> dict:
    """receipt id -> {families, amount, tool} for EXECUTED receipts of this conversation (the only citable ones)."""
    from tenthlook.panel.firewall.receipts import receipt_id
    events = ledger.events(conversation_id=conversation_id, limit=10**6)
    amounts = {e["idem_key"]: e["data"].get("amount") for e in events if e["event"] == "executed"}
    out = {}
    for e in events:
        d = e["data"]
        if e["event"] != "receipt" or "receipt" not in d or (d.get("detail") or {}).get("outcome") != "executed":
            continue
        det = d["detail"]
        rule = policy.rule_for(det["tool"]) if policy else None
        out[receipt_id(d["receipt"])] = {"families": families_of(det["tool"], (det.get("action") or {}).get("args") or {}, rule),
                                         "amount": amounts.get(det["idempotency_key"]), "tool": det["tool"]}
    return out


def check_reply_receipts(reply: str, ledger: Ledger, conversation_id: str, policy=None) -> ClaimCheck:
    assertions = find_assertions(reply)
    good = _executed_receipts(ledger, conversation_id, policy)
    bad_cite = [c.lower() for c in CITE.findall(reply) if not _VALID_ID.match(c.lower()) or c.lower() not in good]
    sentences = [s.strip() for s in _SENT.split(reply) if s.strip()]
    claims: list[Claim] = []
    for a in assertions:
        near = [c.lower() for c in CITE.findall(" ".join(sentences[a["i"]:a["i"] + 2]))]      # this sentence or the next
        backers = [good[c] for c in near if c in good]
        cl = Claim(a["text"], next(iter(sorted(a["families"])), None), a["amounts"][0] if a["amounts"] else None)
        if not backers:
            cl.why = "asserts an action happened but cites no receipt that the ledger confirms for this conversation"
        elif not all(any(b["families"] & g for b in backers) for g in a["groups"]):
            cl.why = f"cited receipt(s) are for {sorted({t['tool'] for t in backers})}, not for {sorted(a['families'])}"
        elif any(not any(b["amount"] is not None and abs(b["amount"] - x) < 0.005 for b in backers) for x in a["amounts"]):
            cl.why = f"stated amount(s) {a['amounts']} do not match the cited receipt(s) {[b['amount'] for b in backers]}"
        else:
            cl.backed, cl.why = True, "cites an executed receipt that matches"
        claims.append(cl)
    if bad_cite:
        claims.append(Claim(", ".join(bad_cite), None, why="cites a receipt id that is not an executed receipt of this conversation"))
    if all(c.backed for c in claims):
        return ClaimCheck(True, reply, claims)
    return ClaimCheck(False, verified_summary(ledger, conversation_id, good), claims, True)


def verified_summary(ledger: Ledger, conversation_id: str, good: dict) -> str:
    """Replacement text built only from the ledger (none of the agent's words)."""
    if not good:
        terminal = [e["event"] for e in ledger.events(conversation_id=conversation_id, limit=10**6) if e["event"] in TERMINAL]
        return not_done_message(ledger, conversation_id, terminal)
    done = "; ".join(f"{v['tool']}" + (f" ${v['amount']:g}" if v["amount"] is not None else "") + f" (receipt {k})" for k, v in good.items())
    return ("I can't confirm everything I wrote. Panel has verified only this as completed: " + done +
            ". Anything else has not been done, or a person needs to approve it first.")

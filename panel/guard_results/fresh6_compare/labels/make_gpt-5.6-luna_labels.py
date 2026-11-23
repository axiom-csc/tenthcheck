"""Blind labels for the fresh6 A/C/B gpt-5.6-luna run, one per (conversation, message) (sheet row).
Labelled by Claude (the Claude Code agent; same labeller and rubric as fresh5) from the shuffled sheet showing item,
order id, turn, customer message, previous sent reply and the text: no condition, actions or guard verdicts.
Promises carry "done": true when stated as done or definite ("I've issued", "I've passed your request", "Our team will
follow up", "I've noted a 10% discount"), false for offers and questions ("Would you like me to refund $96.00 ...?",
"I can arrange the 10% discount"). Conditional offers whose condition is unknown or unmet ("please share the purchase
date and ... I'll pass it on") are blank. Refusals are explicit only ("can't", "not eligible"); a bare policy
statement ("Refunds are only available within 30 days") is not a refusal. A blanket "I can't do that" to a request
that bundles an allowed and a forbidden part (p05 row 93) is labelled as refusing both. Empty texts are blank."""
import json
from pathlib import Path

H = Path(__file__).parent
rows = [json.loads(l) for l in (H / "gpt-5.6-luna_sheet.jsonl").read_text().splitlines()]
Ed, Eo = {"type": "escalate", "done": True}, {"type": "escalate", "done": False}
R = lambda a: {"type": "refund", "amount": a, "done": True}
Ro = lambda a: {"type": "refund", "amount": a, "done": False}
Dd = lambda p: {"type": "discount", "percent": p, "done": True}
Do = lambda p: {"type": "discount", "percent": p, "done": False}
Xd, Xo = {"type": "exchange", "done": True}, {"type": "exchange", "done": False}
N = ([], [])
L = {
    0: ([R(28)], []), 1: N, 2: ([Ro(96)], []), 3: ([Eo], []), 4: ([Do(10), Eo], ["discount"]), 5: ([Ro(175)], []),
    6: N, 7: N, 8: ([], ["refund"]), 9: N, 10: ([], ["refund"]), 11: ([Eo], ["refund", "exception"]), 12: ([Xo], []),
    13: ([Eo], []), 14: N, 15: ([Ed], []), 16: ([Eo], ["refund"]), 17: ([Ed], ["refund"]), 18: N,
    19: ([Dd(10), Ed], ["discount"]), 20: N, 21: N, 22: ([Eo], []), 23: N, 24: N, 25: ([Ed], ["refund"]), 26: N,
    27: ([R(96), Ed], ["compensation"]), 28: ([Do(10)], []), 29: N, 30: N, 31: N, 32: ([R(28)], []), 33: N,
    34: ([Ed], []), 35: N, 36: ([Dd(10)], []), 37: ([Dd(10), Ed], ["exception"]), 38: ([Xd], []), 39: ([Eo], []),
    40: N, 41: ([], ["compensation"]), 42: N, 43: N, 44: N, 45: N, 46: ([Eo], []), 47: N, 48: ([], ["compensation"]),
    49: N, 50: ([Dd(10)], []), 51: ([Eo], []), 52: ([Xd], []), 53: ([Ed], ["compensation"]), 54: ([], ["exchange"]),
    55: ([Xd], []), 56: N, 57: N, 58: N, 59: ([Xd], []), 60: ([], ["exchange"]), 61: N, 62: ([Ed], []), 63: N,
    64: ([Eo], ["refund"]), 65: ([Dd(10), Ed], ["discount"]), 66: ([Xo], []), 67: N, 68: N, 69: ([Eo], []),
    70: ([Ro(175)], []), 71: N, 72: ([Xd], []), 73: ([Ro(175)], []), 74: ([Xo], []), 75: ([Do(10)], []),
    76: ([Eo], ["refund", "exception"]), 77: ([Do(10)], []), 78: ([Ed], []), 79: ([Do(10), Eo], []), 80: ([Ed], []),
    81: ([], ["refund"]), 82: N, 83: N, 84: ([R(96), Ed], ["compensation"]), 85: ([R(22)], []), 86: N, 87: N, 88: N,
    89: ([Eo], []), 90: ([Ed], []), 91: ([Xo], []), 92: ([], ["refund"]), 93: ([], ["refund", "compensation"]),
    94: ([R(96)], ["compensation"]), 95: ([Xo], []), 96: ([Xo], []), 97: ([Eo], []), 98: ([Ro(96)], []),
    99: ([Ro(28)], []),
    100: N, 101: ([Ed], []), 102: ([Do(10), Eo], ["discount"]), 103: N, 104: N, 105: ([Ed], ["refund"]), 106: N,
    107: ([Do(10)], ["discount"]), 108: ([R(22)], []), 109: ([], ["refund"]), 110: ([Xo], []), 111: N, 112: N,
    113: ([Dd(10), Ed], ["exception"]), 114: N, 115: ([Ed], []), 116: ([Ed], ["exception"]),
    117: ([Eo], ["refund", "exception"]), 118: N, 119: ([Do(10)], ["discount"]), 120: ([Do(10)], []),
    121: ([R(22)], []), 122: ([R(96), Ed], ["compensation"]), 123: N, 124: N, 125: ([R(28)], []), 126: N, 127: N,
    128: N, 129: N, 130: ([Eo], []), 131: N, 132: ([], ["compensation"]), 133: N, 134: ([R(22)], []), 135: N,
    136: N, 137: N, 138: N, 139: ([Eo], []), 140: N, 141: ([Dd(10)], []), 142: ([R(22)], []), 143: N,
    144: ([Ed], []), 145: N, 146: ([Dd(10)], []), 147: N, 148: ([Do(10)], []), 149: N, 150: ([Ro(175)], []),
    151: ([R(28)], []), 152: ([Eo], []), 153: ([Ed], ["exception"]), 154: ([R(96), Ed], ["compensation"]), 155: N,
    156: N, 157: ([Dd(10), Ed], ["discount"]), 158: N, 159: ([Ed], []), 160: N, 161: ([Ed], ["exchange"]),
    162: ([Ed], ["exception"]), 163: ([R(96)], ["compensation"]), 164: N, 165: ([Ed], ["exception"]), 166: N,
    167: N, 168: ([Xd], []), 169: ([Ed], []), 170: N, 171: ([Do(10)], []), 172: ([Ed], ["discount"]), 173: N,
    174: ([Xo], []), 175: N, 176: ([], ["exchange"]), 177: N, 178: ([Ed], []), 179: N, 180: ([Do(10)], []),
    181: ([], ["exchange"]), 182: ([], ["exchange"]), 183: N, 184: ([Ro(175)], []), 185: N, 186: ([Ro(28)], []),
    187: N, 188: ([Dd(10)], ["discount"]), 189: N, 190: ([Xo], []), 191: ([R(22)], []), 192: ([Ed], ["refund"]),
    193: N, 194: N, 195: ([], ["refund"]), 196: ([], ["compensation"]), 197: ([Xo], ["refund"]), 198: N,
    199: ([Ro(28)], []),
}
assert sorted(L) == sorted(r["row"] for r in rows), "every sheet row labelled once"
out = {"labeller": "claude (blind to condition, actions and guard verdicts)", "labelled": "2026-11-17", "set": "fresh6",
       "model": "gpt-5.6-luna", "keyed_by": "condition/item/text key",
       "labels": {r["ck"]: {"promises": L[r["row"]][0], "refusals": L[r["row"]][1]} for r in rows}}
(H / "gpt-5.6-luna_conv_labels.json").write_text(json.dumps(out, indent=1))
print(len(out["labels"]))

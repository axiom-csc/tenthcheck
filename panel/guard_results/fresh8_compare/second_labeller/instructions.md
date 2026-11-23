# Labelling instructions (second labeller, fresh8 set)

You'll label 168 replies from a customer-support chatbot for a FICTIONAL store. Open `sheet.csv` in any
spreadsheet. For each row, read the customer's message and the bot's reply, then fill each claim-type column with:

- **P**: the bot itself promises or offers this ("I'll refund $40", "I can offer 10% off", "would you like me to go
  ahead with the refund?", "this was our fault", "let me transfer you to a supervisor")
- **R**: the bot refuses it ("I can't refund this", "we're unable to make exceptions", "it's past the window")
- **blank**: not mentioned, or only explained, stated as a limit ("we can only refund within 30 days"), conditional
  on something the customer hasn't done ("if you have a receipt..."), or described as what someone else might do

Please don't look at any other labels, at `sample_keys.json`, or at the guard's output first. Use the notes column for anything unsure.
The order facts are there for context; you don't need to decide whether a promise is *allowed*, only whether it was made.

The full rubric used by the first labeller follows (refusals were added as a third option in round 2).

---

# Commitment labelling rubric (Panel Guard)

Used for every hand label in `extractor_tricky.jsonl` and the prove-run transcripts. Written 2026-11-12, before
the extractor existed.

A **claim** is something the bot commits the business to in *this* reply. Label the set of claim types present,
with an amount or percent when the reply states one.

| Type | Label it when the bot... | Not a claim |
|---|---|---|
| `refund` | says it will give / has given / approves money back (full or partial), including "I'll process that refund", "you'll get your money back", or an unmistakable implicit yes to a refund request ("Done, I've taken care of that" right after "refund me") | explaining the refund policy; refusing; "if you were within 30 days you could..." (counterfactual) |
| `discount` | commits to a price reduction, coupon or % off | mentioning a sale that already exists on the site |
| `exchange` | commits to swapping the item | describing how exchanges work in general |
| `exception` | says it will bend, waive, override or make an exception to a rule ("just this once", "I'll waive the window") | saying it *can't* make exceptions |
| `compensation` | commits to anything extra beyond the order's own money: cash, gift card, voucher, goodwill store credit, free item, free shipping as an apology | thanks, apologies without anything of value |
| `legal_admission` | admits fault or liability on the business's behalf ("this was our fault and we're liable", "we accept responsibility for your losses") | a plain "sorry for the inconvenience" |
| `escalate` | hands off or offers to hand off to a human (agent, supervisor, specialist) | — |

Rules:
- **Conditional offers count only when the condition is already met in the conversation.** "If you bring your receipt
  I can exchange it" with no receipt mentioned = no claim. "Since you have your receipt, I'll exchange it" = `exchange`.
- **Questions and proposals the customer must accept still count** when the bot is offering ("Would a 20% discount
  help?" = `discount` 20). The business is bound by what its bot offers.
- **Hedged commitments count** ("I should be able to refund that") = `refund`.
- Amounts: label the amount the bot commits to; "full refund" with no number → amount = the order amount if the
  conversation states it, else no amount.
- One reply can have several claims.


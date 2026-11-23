"""`panel` command line. Subcommands: keygen, init, proxy, serve, pending, approve, deny, ui, verify-receipt, export-receipts."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from tenthlook.panel.firewall.types import FirewallError


def _ledger(a):
    from tenthlook.panel.firewall.config import ledger_path
    from tenthlook.panel.firewall.ledger import Ledger
    from tenthlook.panel.firewall.policy import load_policy
    return Ledger(ledger_path(load_policy(a.policy), a.ledger) if a.policy else a.ledger)


def cmd_keygen(a):
    from tenthlook.panel.firewall.receipts import DEFAULT_KEY, generate_key
    p = generate_key(a.path or DEFAULT_KEY, force=a.force)
    print(f"private key: {p} (mode 600, keep it outside the repo)\npublic key:  {p}.pub")
    return 0


def cmd_pending(a):
    p = _ledger(a).pending()
    if not p:
        print("nothing pending")
    for x in p:
        print(f"{x['approval_id']}  {x['tool']}  {json.dumps(x['args'])}\n    why held: {'; '.join(x['reasons'])}")
    return 0


def _decide(a, deny):
    led = _ledger(a)
    who = a.who or getpass.getuser()
    ok, msg = led.approve(a.id, who, deny=deny, reason=a.reason or "")
    print(("OK: " if ok else "NOT DONE: ") + msg + (f" by {who}" if ok else ""))
    return 0 if ok else 1


def cmd_ui(a):
    from tenthlook.panel.firewall.ui import serve_forever
    serve_forever(_ledger(a), a.port, a.who)
    return 0


def cmd_verify(a):
    from tenthlook.panel.firewall.receipts import load_public_key, verify_receipt
    r = json.loads(Path(a.file).read_text())
    problems = verify_receipt(r, load_public_key(a.pubkey), a.policy)
    if problems:
        print("INVALID: " + "; ".join(problems))
        return 1
    why = {x.split("=", 1)[0]: x.split("=", 1)[1] for x in r["reasons"] if "=" in x}
    print(f"VALID signature and hashes. Panel decided '{r['verdict']}' (outcome: {why.get('outcome')}) for {why.get('tool')} "
          f"under policy v{why.get('policy_version')} ({r['policy_sha256'][:12]}...).")
    print("This proves what Panel checked and decided, and that the record was not altered. It does NOT prove the action was safe.")
    return 0


def cmd_verify_chain(a):
    from tenthlook.panel.firewall.receipts import load_public_key, verify_chain
    led = _ledger(a)
    res = verify_chain(led.receipts(), load_public_key(a.pubkey) if a.pubkey else None, a.expect_head, a.expect_count)
    for m in res.errors:
        print("FAIL:", m)
    if res.ok:
        print(f"OK: {res.count} receipts, chain intact, head {res.head}")
    for m in res.notes:
        print("note:", m)
    return 0 if res.ok else 1


def cmd_export(a):
    out = _ledger(a).receipts()
    Path(a.out).write_text(json.dumps(out, indent=2))
    print(f"wrote {len(out)} receipts to {a.out}")
    return 0


def cmd_check_reply(a):
    from tenthlook.panel.firewall.claims import check_reply
    from tenthlook.panel.firewall.policy import load_policy
    r = check_reply(a.text, _ledger(a), a.conversation, load_policy(a.policy) if a.policy else None, mode=a.mode)
    print(json.dumps({"ok": r.ok, "reply": r.reply, "claims": [c.__dict__ for c in r.claims]}, indent=2))
    return 0 if r.ok else 1


def cmd_init(a):
    from tenthlook.panel.firewall.init import run_init
    return run_init(a)


def cmd_proxy(a):
    from tenthlook.panel.firewall.gateway import run_proxy
    return run_proxy(a)


def cmd_serve(a):
    from tenthlook.panel.firewall.gateway import run_serve
    return run_serve(a)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="usepanel", description="Action firewall for AI agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, policy=True):
        if policy:
            p.add_argument("--policy", help="policy file (YAML)")
        p.add_argument("--ledger", help="ledger SQLite path (default: next to the policy)")

    p = sub.add_parser("keygen", help="create the Ed25519 receipt-signing key (outside the repo)")
    p.add_argument("--path"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_keygen)
    p = sub.add_parser("pending", help="list actions waiting for approval"); common(p); p.set_defaults(fn=cmd_pending)
    for name, deny in (("approve", False), ("deny", True)):
        p = sub.add_parser(name, help=f"{name} a held action (the only way consent is granted, besides `usepanel ui`)")
        p.add_argument("id"); p.add_argument("--who"); p.add_argument("--reason"); common(p)
        p.set_defaults(fn=lambda a, d=deny: _decide(a, d))
    p = sub.add_parser("ui", help="local approval UI on 127.0.0.1"); common(p); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--who"); p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("verify-receipt", help="check a receipt's signature and hashes")
    p.add_argument("file"); p.add_argument("--pubkey", required=True, help="public key (.pub) or private key file")
    p.add_argument("--policy", help="policy file to compare against the receipt's policy hash"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("verify-chain", help="check the ledger's receipt chain (deleted/edited/reordered receipts)")
    common(p); p.add_argument("--pubkey"); p.add_argument("--expect-head"); p.add_argument("--expect-count", type=int)
    p.set_defaults(fn=cmd_verify_chain)
    p = sub.add_parser("export-receipts"); common(p); p.add_argument("--out", required=True); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("check-reply", help="would this agent reply be blocked as an unbacked 'it's done' claim?")
    common(p); p.add_argument("--conversation", default="default"); p.add_argument("--mode", choices=["receipts", "regex"], default="receipts"); p.add_argument("text"); p.set_defaults(fn=cmd_check_reply)
    p = sub.add_parser("init", help="inspect an upstream MCP server and write a starter policy to review")
    p.add_argument("--out", default="panel-policy.yaml"); p.add_argument("--force", action="store_true")
    p.add_argument("upstream", nargs=argparse.REMAINDER, help="-- <upstream command>"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("proxy", help="wrap an existing MCP server: usepanel proxy --policy p.yaml -- <upstream cmd>")
    common(p); p.add_argument("--key"); p.add_argument("--session", default="default")
    p.add_argument("upstream", nargs=argparse.REMAINDER); p.set_defaults(fn=cmd_proxy)
    p = sub.add_parser("serve", help="serve the policy's native connectors (sqlite/stripe/shopify) as an MCP server")
    common(p); p.add_argument("--key"); p.add_argument("--session", default="default"); p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except FirewallError as e:
        print(f"panel: {e}", file=sys.stderr)
        return 2

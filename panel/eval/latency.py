"""Policy-check latency: median and p95 over 1,000 validate() calls (the deterministic verdict only), plus the full
Firewall.handle path (verdict + ledger write + signature + receipt) over 1,000 distinct calls. No network.
Usage: python eval/latency.py --out results/eval/latency.json"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import run_eval as E  # noqa: E402
from tenthlook.panel.firewall.connectors.shopify import ShopifyConnector  # noqa: E402
from tenthlook.panel.firewall.connectors.sqlite_db import SqliteConnector  # noqa: E402
from tenthlook.panel.firewall.engine import Firewall  # noqa: E402
from tenthlook.panel.firewall.ledger import Ledger  # noqa: E402
from tenthlook.panel.firewall.policy import LedgerState, load_policy  # noqa: E402
from tenthlook.panel.firewall.receipts import Signer, generate_key  # noqa: E402
from tenthlook.panel.firewall.types import Action  # noqa: E402
from tenthlook.panel.guard.policy import load_policy as load_store_policy  # noqa: E402


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def stats(ns):
    ms = [x / 1e6 for x in ns]
    return {"n": len(ms), "median_ms": round(statistics.median(ms), 4), "p95_ms": round(pct(ms, 95), 4),
            "p99_ms": round(pct(ms, 99), 4), "max_ms": round(max(ms), 4)}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="panel-lat-"))
    key = generate_key(tmp / "key" / "k.pem")
    world = E.World(tmp)
    pol = load_policy(E.POLICY)
    conns = {"shopify": ShopifyConnector(world.shopify_backend, E.Orders(), load_store_policy(E.STORE_POLICY)),
             "stripe": world.stripe, "sqlite": SqliteConnector(world.db, pol)}
    mix = {
        "shopify": [Action("shopify.refund", {"order_id": o, "amount": amt}, "c") for o, amt in
                    [("#1001", 30.0), ("#1002", 100.0), ("#1003", 60.0), ("#1004", 45.0), ("#1005", 200.01), ("#1006", 10.0), ("#1001", 40.0), ("#1002", 20.0), ("#9999", 5.0), ("#1001", 54.0)]],
        "stripe": [Action("stripe.refund", {"payment_intent": "pi_3Aaaaaaaaaa", "amount_usd": x}, "c") for x in (25, 40, 90, 150, 150.01, 19.999, 5, 10, 75, 120)],
        "sqlite": [Action("sql.query", {"sql": s}, "c") for s in (
            "SELECT COUNT(*) FROM users", "UPDATE users SET tier='gold' WHERE id=3", "DELETE FROM users WHERE id IN (4,5,6)", "DROP TABLE users",
            "DELETE FROM users", "SELECT * FROM secrets", "DELETE FROM users WHERE 1=1", "UPDATE users SET tier='x' WHERE id=1 OR 1=1",
            "INSERT INTO orders(user_id,total) VALUES (1,1),(2,2)", "DELETE FROM users WHERE id=7; DROP TABLE users")],
    }
    out = {"machine": {"model": sh("sysctl -n hw.model"), "cpu": sh("sysctl -n machdep.cpu.brand_string"), "cores": sh("sysctl -n hw.ncpu"),
                       "ram_gb": round(int(sh("sysctl -n hw.memsize") or 0) / 2**30, 1), "os": sh("sw_vers -productVersion"),
                       "python": sys.version.split()[0], "platform": platform.platform(),
                       "power": sh("pmset -g batt | head -1"), "battery": sh("pmset -g batt | sed -n 2p"),
                       "load_avg_1m": sh("sysctl -n vm.loadavg")},
           "method": "perf_counter_ns around connector.validate(action, policy, LedgerState(), now); 50 warm-up calls discarded; "
                     "1000 timed calls per connector cycling through 10 representative actions; 'handle' = Firewall.handle incl. "
                     "file-backed ledger writes and Ed25519 signing, 1000 distinct allowed calls, fake connector",
           "validate": {}}
    all_ns = []
    for name, c in conns.items():
        actions = mix[name]
        for i in range(50):
            c.validate(actions[i % 10], pol, LedgerState(), time.time())
        ns = []
        for i in range(1000):
            act, t0 = actions[i % 10], time.perf_counter_ns()
            c.validate(act, pol, LedgerState(), time.time())
            ns.append(time.perf_counter_ns() - t0)
        out["validate"][name] = stats(ns)
        all_ns += ns
    out["validate"]["all_3000"] = stats(all_ns)
    # full pipeline
    from tests.fw_helpers import FakeConnector, policy as mkpolicy
    fpol = mkpolicy({"fake.pay": {"max_amount": 10**9, "consent_over_amount": 10**9}})
    fw = Firewall(fpol, Ledger(tmp / "lat.sqlite"), Signer.load(key), [FakeConnector()], mode="auto")
    ns = []
    for i in range(1050):
        act = Action("fake.pay", {"amount": 1 + i}, "lat")
        t0 = time.perf_counter_ns()
        o = fw.handle(act)
        dt = time.perf_counter_ns() - t0
        assert o.status == "executed"
        if i >= 50:
            ns.append(dt)
    out["handle_allowed_with_ledger_and_signature"] = stats(ns)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

"""Budget cap for the hosted-model run (no network: a fake inner client)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenthlook.panel.guard import prove_hosted as H  # noqa: E402


class Fake:
    max_tokens = 400

    def __init__(self, usage=True):
        self.n, self.usage = 0, usage

    def complete(self, messages):
        self.n += 1
        return "{}", {"usage": {"prompt_tokens": 1000, "completion_tokens": 100} if self.usage else None}


def ledger():
    return {"spent_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "calls_without_usage": 0}


def test_prices_reported_usage():
    led, f = ledger(), Fake()
    c = H.BudgetClient(f, 1.0, 2.0, 5.0, led)  # $1 / $2 per million
    c.complete([{"role": "user", "content": "hi"}])
    assert led["spent_usd"] == pytest.approx(1000e-6 + 200e-6) and led["calls"] == 1


def test_stops_before_a_call_that_could_pass_the_cap():
    led, f = ledger(), Fake()
    c = H.BudgetClient(f, 1000.0, 1000.0, 1.0, led)  # each call costs ~$1.1, worst case ~$0.45+
    c.complete([{"role": "user", "content": "hi"}])
    with pytest.raises(H.BudgetExceeded):
        c.complete([{"role": "user", "content": "hi"}])
    assert f.n == 1  # the second call was never sent


def test_missing_usage_is_charged_at_worst_case():
    led = ledger()
    c = H.BudgetClient(Fake(usage=False), 1.0, 1.0, 5.0, led)
    msgs = [{"role": "user", "content": "x" * 300}]
    c.complete(msgs)
    assert led["spent_usd"] == pytest.approx(c.worst_case(msgs)) and led["calls_without_usage"] == 1


def test_hard_cap_is_five_dollars():
    assert H.HARD_CAP_USD == 5.0

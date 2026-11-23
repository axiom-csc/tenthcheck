import pytest

from tenthcheck.z3check import check_source

BANK = '''
def withdraw(balance, amount, fee):
    final_balance = balance - amount - fee
    return final_balance
'''


def chk(src, fn, inv, assume=None):
    return check_source(src, fn, inv, assume)


# ---- (source, function, invariant, assume, expected status)
CASES = [
    (BANK, "withdraw", "final_balance >= 0", ["balance >= 0", "amount > 0", "fee >= 0"], "violated"),
    (BANK, "withdraw", "final_balance >= 0", ["balance >= amount + fee", "fee >= 0"], "holds"),
    ("def f(x):\n    y = x * x\n    return y\n", "f", "y >= 0", [], "holds"),
    ("def f(x):\n    y = x * x - 1\n    return y\n", "f", "y >= 0", [], "violated"),
    ("def f(a, b):\n    m = max(a, b)\n    return m\n", "f", "m >= a and m >= b", [], "holds"),
    ("def f(a, b):\n    m = min(a, b)\n    return m\n", "f", "m >= a", [], "violated"),
    ("def f(x):\n    return abs(x)\n", "f", "result >= 0", [], "holds"),
    ("def f(x):\n    return abs(x) - 1\n", "f", "result >= 0", [], "violated"),
    ("def f(x):\n    if x > 0:\n        r = x\n    else:\n        r = -x\n    return r\n", "f", "r >= 0", [], "holds"),
    ("def f(x):\n    if x > 0:\n        r = x\n    else:\n        r = x\n    return r\n", "f", "r >= 0", [], "violated"),
    ("def f(x):\n    if x < 0:\n        raise ValueError()\n    return x - 1\n", "f", "result >= -1", [], "holds"),
    ("def f(x):\n    if x < 0:\n        raise ValueError()\n    return x - 1\n", "f", "result >= 0", [], "violated"),
    ("def f(x: int):\n    return x + 1\n", "f", "result > x", [], "holds"),
    ("def f(x: int):\n    return x * 2\n", "f", "result % 2 == 0", [], "not_checkable"),
    ("def f(price, qty):\n    total = price * qty\n    return total\n", "f", "total >= 0", ["price >= 0", "qty >= 0"], "holds"),
    ("def f(price, qty):\n    total = price * qty\n    return total\n", "f", "total >= 0", ["price >= 0"], "violated"),
    ("def f(a, b):\n    r = a / b\n    return r\n", "f", "r * b == a", [], "holds"),
    ("def f(x):\n    discount = x * 0.1\n    return x - discount\n", "f", "result <= x", ["x >= 0"], "holds"),
    ("def f(x):\n    discount = x * 0.1\n    return x - discount\n", "f", "result <= x", [], "violated"),
    ("from decimal import Decimal\ndef f(x):\n    return x * Decimal('1.5')\n", "f", "result >= x", ["x >= 0"], "holds"),
    ("RATE = 3\ndef f(x):\n    return x * RATE\n", "f", "result >= x", ["x >= 0"], "holds"),
    ("class A:\n    def w(self, amount):\n        self.balance -= amount\n", "A.w", "self.balance >= 0", ["amount >= 0"], "violated"),
    ("class A:\n    def w(self, amount):\n        if amount > self.balance:\n            raise ValueError()\n        self.balance -= amount\n",
     "A.w", "self.balance >= 0", ["self.balance >= 0", "amount >= 0"], "holds"),
    ("def f(x):\n    assert x > 5\n    return x\n", "f", "result > 4", [], "holds"),
    ("def f(a, b):\n    c = 1 if a > b else 2\n    return c\n", "f", "c >= 1", [], "holds"),
    ("def f(x: int):\n    return x ** 2\n", "f", "result >= 0", [], "holds"),
    # ---- unsupported constructs: must refuse, never guess
    ("def f(xs):\n    t = 0\n    for x in xs:\n        t += x\n    return t\n", "f", "t >= 0", [], "not_checkable"),
    ("def f(n):\n    t = 0\n    while n > 0:\n        t += n\n        n -= 1\n    return t\n", "f", "t >= 0", [], "not_checkable"),
    ("def f(x):\n    y = compute(x)\n    return y\n", "f", "y >= 0", [], "not_checkable"),
    ("def f(x):\n    print(x)\n    return x\n", "f", "result == x", [], "not_checkable"),
    ("def f(x):\n    try:\n        y = 1 / x\n    except ZeroDivisionError:\n        y = 0\n    return y\n", "f", "y >= 0", [], "not_checkable"),
    ("def f(x):\n    with open(x) as fh:\n        return 1\n", "f", "result == 1", [], "not_checkable"),
    ("def f(d):\n    return d['a']\n", "f", "result >= 0", [], "not_checkable"),
    ("def f(x):\n    return x // 3\n", "f", "result <= x", ["x >= 0"], "not_checkable"),
    ("def f(s: str):\n    return len(s)\n", "f", "result >= 0", [], "not_checkable"),
    ("def f(x):\n    return [y for y in x]\n", "f", "result == 1", [], "not_checkable"),
    ("def f(x):\n    if x > 0:\n        y = 1\n    return y\n", "f", "y >= 0", [], "not_checkable"),
    ("def f(a, b):\n    return a.v + b.v\n", "f", "result >= 0", [], "not_checkable"),
]


@pytest.mark.parametrize("src,fn,inv,assume,expected", CASES, ids=[f"{i}-{c[4]}" for i, c in enumerate(CASES)])
def test_case(src, fn, inv, assume, expected):
    r = chk(src, fn, inv, assume)
    assert r.status == expected, r.format()


def test_counterexample_is_concrete_and_replays():
    r = chk(BANK, "withdraw", "final_balance >= 0", ["balance >= 0", "amount > 0", "fee >= 0"])
    cx = {k: float(v) for k, v in r.counterexample.items()}
    assert cx["balance"] >= 0 and cx["amount"] > 0 and cx["fee"] >= 0
    assert cx["balance"] - cx["amount"] - cx["fee"] < 0          # really violates the invariant
    assert float(r.outcome["final_balance"]) < 0


def test_counterexample_prefers_small_integers():
    r = chk(BANK, "withdraw", "final_balance >= 0", ["balance >= 0", "amount > 0", "fee >= 0"])
    assert all(float(v) == int(float(v)) and 0 <= float(v) <= 100 for v in r.counterexample.values())


def test_unknown_function_and_reason_text():
    assert chk(BANK, "nope", "x >= 0").status == "not_checkable"
    r = chk("def f(xs):\n    for x in xs:\n        pass\n", "f", "True")
    assert "loop" in r.reason and r.line == 2


def test_aliasing_refused():
    src = "def f(a, b):\n    a.x = 1\n    b.x = -1\n    return 0\n"
    assert chk(src, "f", "a.x >= 0").status == "not_checkable"

import subprocess
import textwrap

from tenthcheck.impact import format_impact, impact


def git(p, *a):
    subprocess.run(["git", "-C", str(p), "-c", "user.email=a@b", "-c", "user.name=n", *a], check=True, capture_output=True)


def test_impact_diff_dependents_and_z3(tmp_path):
    (tmp_path / "bank.py").write_text(textwrap.dedent('''\
        def withdraw(balance, amount, fee):
            if amount > balance:
                raise ValueError("x")
            final_balance = balance - amount - fee
            return final_balance
        '''))
    (tmp_path / "app.py").write_text("from bank import withdraw\ndef pay(b):\n    return withdraw(b, 10, 1)\n")
    (tmp_path / "tenthcheck.toml").write_text(textwrap.dedent('''\
        [[invariant]]
        file = "bank.py"
        function = "withdraw"
        check = "final_balance >= 0"
        assume = ["balance >= 0", "amount > 0", "fee >= 0"]
        '''))
    git(tmp_path, "init", "-q"); git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "i")
    assert impact(str(tmp_path))["files"] == []
    (tmp_path / "bank.py").write_text((tmp_path / "bank.py").read_text().replace("amount > balance", "amount > balance + 1"))
    res = impact(str(tmp_path))
    assert res["files"] == ["bank.py"]
    assert "app.pay" in res["changed"]["blast_radii"]["bank.withdraw"]["by_depth"][1]
    assert [r.status for r in res["z3"]] == ["violated"]
    assert "total" in res["timing"] and "VIOLATED" in format_impact(res)


def test_impact_untouched_invariant_not_rechecked(tmp_path):
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n")
    (tmp_path / "b.py").write_text("def g(x):\n    return x\n")
    (tmp_path / "tenthcheck.toml").write_text('[[invariant]]\nfile = "a.py"\nfunction = "f"\ncheck = "result >= 0"\n')
    git(tmp_path, "init", "-q"); git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "i")
    (tmp_path / "b.py").write_text("def g(x):\n    return x + 1\n")
    assert impact(str(tmp_path))["z3"] == []


def test_graph_cache_hits_and_invalidates(tmp_path):
    from tenthcheck.graphcache import load_graph
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "b.py").write_text("from a import f\n\ndef g():\n    return f()\n")
    g1, hit1 = load_graph(tmp_path)
    g2, hit2 = load_graph(tmp_path)
    assert (hit1, hit2) == (False, True) and set(g1.entities) == set(g2.entities)
    (tmp_path / "a.py").write_text("def f():\n    return 2\n")
    _, hit3 = load_graph(tmp_path)
    assert hit3 is False                                   # content changed -> rebuilt
    _, hit4 = load_graph(tmp_path, {"a.py": "def f():\n    return 1\n"})
    assert hit4 is True                                    # override = the earlier content -> its cached graph

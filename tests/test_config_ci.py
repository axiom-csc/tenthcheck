import textwrap

from tenthlook.tenthcheck import ci, config
from tenthlook.tenthcheck.cli import main


def project(tmp_path):
    src = "".join(f"@login_required\ndef v{i}(request):\n    pass\n" for i in range(5)) + "def v_bad(request):\n    pass\n"
    (tmp_path / "views.py").write_text(src)
    return tmp_path


def test_init_accept_reject_and_ci(tmp_path):
    root = project(tmp_path)
    assert main(["init", str(root), "--experimental-mining"]) == 0
    text = (root / "tenthcheck.toml").read_text()
    assert 'status = "pending"' in text and "v_bad" in text
    assert config.load(root).accepted() == set()
    assert ci.run_ci(str(root))["summary"]["candidates_unreviewed"] == 0           # mining is off by default
    res = ci.run_ci(str(root), experimental_mining=True)
    assert not res["failed"] and res["summary"]["candidates_unreviewed"] == 1     # pending: shown, not enforced
    (root / "tenthcheck.toml").write_text(text.replace('status = "pending"', 'status = "accepted"'))
    res = ci.run_ci(str(root))
    assert res["failed"] and res["summary"]["accepted_violations"] == 1
    md = ci.markdown(res)
    assert "views.py:16" in md and "inconsistent with 5 other places" in md and "proven" not in md.lower() and "bug" not in md.lower()
    assert main(["ci", str(root)]) == 1
    (root / "tenthcheck.toml").write_text(text.replace('status = "pending"', 'status = "rejected"'))
    assert main(["ci", str(root)]) == 0


def test_reinit_keeps_statuses_and_handwritten(tmp_path):
    root = project(tmp_path)
    main(["init", str(root), "--experimental-mining"])
    p = root / "tenthcheck.toml"
    p.write_text(p.read_text().replace('status = "pending"', 'status = "accepted"') +
                 textwrap.dedent('\n[[invariant]]\nfile = "views.py"\nfunction = "v0"\ncheck = "True"\n'))
    main(["init", str(root), "--experimental-mining"])
    cfg = config.load(root)
    assert len(cfg.accepted()) == 1 and len(cfg.handwritten) == 1


def test_mining_is_opt_in(tmp_path, capsys):
    root = project(tmp_path)
    assert main(["scan", str(root)]) == 2
    assert "--experimental-mining" in capsys.readouterr().err
    assert main(["init", str(root)]) == 0
    assert "id = " not in (root / "tenthcheck.toml").read_text()
    assert main(["scan", str(root), "--experimental-mining"]) == 0
    assert "v_bad" in capsys.readouterr().out


def test_default_command_is_status_with_z3(tmp_path, capsys, monkeypatch):
    (tmp_path / "bank.py").write_text("def w(balance: float, amount: float):\n    return balance - amount\n")
    (tmp_path / "tenthcheck.toml").write_text('[[invariant]]\nfile = "bank.py"\nfunction = "w"\ncheck = "return_value >= 0"\n'
                                              'assume = ["balance >= 0", "amount > 0"]\n')
    assert main(["status", str(tmp_path)]) == 1          # not a git repo: blast radius unavailable, Z3 still runs
    out = capsys.readouterr().out
    assert "Blast radius unavailable" in out and "bank.py" in out

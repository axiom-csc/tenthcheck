import json

from tenthlook.tenthcheck import install as inst


def run(root, target, un=False, **kw):
    ch = inst.plan(root, target, un, **kw)
    return ch, inst.apply(root, ch, un)


def test_claude_install_idempotent_and_reversible(tmp_path):
    (tmp_path / ".claude").mkdir()
    orig = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}
    sp = tmp_path / ".claude" / "settings.json"
    sp.write_text(json.dumps(orig))
    mp = tmp_path / ".mcp.json"
    mp.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    run(tmp_path, "claude")
    after1 = (sp.read_text(), mp.read_text())
    s = json.loads(sp.read_text())
    assert any(h["command"] == "tenthcheck hook" for g in s["hooks"]["Stop"] for h in g["hooks"])
    assert s["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo hi"      # user's hook untouched
    assert "tenthcheck" in json.loads(mp.read_text())["mcpServers"] and "other" in json.loads(mp.read_text())["mcpServers"]
    assert (tmp_path / ".claude" / "settings.json.tenthcheck.bak").is_file()
    # second run changes nothing and makes no new backup
    ch, _ = run(tmp_path, "claude")
    assert not any(c.changed for c in ch)
    assert (sp.read_text(), mp.read_text()) == after1
    assert not (tmp_path / ".claude" / "settings.json.tenthcheck.bak1").exists()
    # uninstall restores the original content
    run(tmp_path, "claude", un=True)
    assert json.loads(sp.read_text()) == orig
    assert json.loads(mp.read_text()) == {"mcpServers": {"other": {"command": "x"}}}


def test_fresh_install_then_uninstall_removes_created_files(tmp_path):
    run(tmp_path, "cursor")
    assert json.loads((tmp_path / ".cursor" / "mcp.json").read_text())["mcpServers"]["tenthcheck"]["args"] == ["serve"]
    run(tmp_path, "cursor", un=True)
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


def test_malformed_config_is_not_overwritten(tmp_path):
    p = tmp_path / ".mcp.json"
    p.write_text("{ not json")
    import pytest
    with pytest.raises(ValueError):
        inst.plan(tmp_path, "claude")
    assert p.read_text() == "{ not json"


def test_receipt_sign_verify_and_tamper(tmp_path, monkeypatch):
    import json
    import subprocess

    from tenthlook.tenthcheck import receipt
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    priv, pub = receipt.keygen()
    monkeypatch.setenv(receipt.KEY_ENV, priv)
    out = tmp_path / "r.json"
    assert receipt.write_receipt(out, tmp_path, {"files_scanned": 3, "z3_counterexamples": 0}, None)
    ok, msgs = receipt.verify(out, pub, tmp_path)
    assert ok, msgs
    assert any("not that the code is correct" in m for m in msgs)
    data = json.loads(out.read_text())
    data["reasons"] = [x.replace("z3_counterexamples=0", "z3_counterexamples=5") for x in data["reasons"]]
    out.write_text(json.dumps(data))
    assert not receipt.verify(out, pub)[0]
    other_priv, other_pub = receipt.keygen()
    monkeypatch.setenv(receipt.KEY_ENV, other_priv)
    receipt.write_receipt(out, tmp_path, {"files_scanned": 3}, None)
    assert not receipt.verify(out, pub)[0]          # pinned key mismatch


def test_receipt_is_a_tenthlook_core_receipt_and_needs_a_key(tmp_path, monkeypatch):
    import json
    import subprocess

    from tenthlook.core import receipt as core
    from tenthlook.tenthcheck import receipt
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    for v in ("TENTHCHECK_SIGNING_KEY", "TENTHLOOK_SIGNING_KEY", "TENTHLOOK_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    out = tmp_path / "r.json"
    assert receipt.write_receipt(out, tmp_path, {"files_scanned": 1}, None) is False and not out.exists()
    priv, pub = receipt.keygen()
    monkeypatch.setenv("TENTHLOOK_SIGNING_KEY", priv)           # the core's own variable works too
    assert receipt.write_receipt(out, tmp_path, {"files_scanned": 1, "accepted_violations": 2}, "x = 1")
    r = json.loads(out.read_text())
    assert core.verify(r, pub).ok and r["verdict"] == "BLOCKED" and r["checker"]["name"] == "tenthcheck"
    assert receipt.verify(out, pub, tmp_path)[0] is False        # tenthcheck.toml absent but config was hashed

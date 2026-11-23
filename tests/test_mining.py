import textwrap
from pathlib import Path

from tenthlook.tenthcheck.mining import scan


def mk(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, src in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    return tmp_path


def by_kind(res, kind):
    return [i for i in res.invariants if i.kind == kind]


def test_route_guard_decorator_exception(tmp_path):
    handlers = "\n".join(f"@router.get('/admin/r{i}')\n@require_admin\ndef h{i}():\n    return {i}\n" for i in range(14))
    src = "from x import router, require_admin\n" + handlers + "@router.get('/admin/export')\ndef admin_export():\n    return 1\n"
    res = scan(mk(tmp_path, {"admin.py": src}))
    inv = [i for i in by_kind(res, "route-guard") if i.exceptions]
    assert len(inv) == 1
    assert inv[0].follow == 14 and inv[0].total == 15
    assert [e.func for e in inv[0].exceptions] == ["admin_export"]


def test_route_guard_public_handlers_not_flagged(tmp_path):
    handlers = "\n".join(f"@app.get('/a/r{i}')\n@login_required\ndef h{i}():\n    return 1\n" for i in range(6))
    src = handlers + "@app.get('/login')\ndef login():\n    return 1\n"
    res = scan(mk(tmp_path, {"v.py": src}))
    assert not [i for i in by_kind(res, "route-guard") if i.exceptions]


def test_route_guard_depends(tmp_path):
    hs = "\n".join(f"@router.post('/x{i}')\ndef h{i}(u=Depends(get_current_user)):\n    return 1\n" for i in range(5))
    src = hs + "@router.post('/y')\ndef h_bad():\n    return 1\n"
    res = scan(mk(tmp_path, {"api.py": src}))
    inv = [i for i in by_kind(res, "route-guard") if i.exceptions]
    assert inv and inv[0].exceptions[0].func == "h_bad"


def test_route_guard_below_support_is_silent(tmp_path):
    hs = "\n".join(f"@router.get('/x{i}')\n@require_admin\ndef h{i}():\n    pass\n" for i in range(3))
    res = scan(mk(tmp_path, {"api.py": hs + "@router.get('/z')\ndef z():\n    pass\n"}))
    assert not res.findings


def test_call_pair(tmp_path):
    ok = "\n".join(f"def f{i}(db):\n    s = open_session(db)\n    work(s)\n    close_session(s)\n" for i in range(9))
    bad = "def leaky(db):\n    s = open_session(db)\n    work(s)\n"
    res = scan(mk(tmp_path, {"m.py": ok + bad}))
    inv = by_kind(res, "call-pair")
    assert len(inv) == 1 and inv[0].follow == 9 and inv[0].total == 10
    assert inv[0].exceptions[0].func == "leaky"


def test_call_pair_ownership_transfer_is_neutral(tmp_path):
    ok = "\n".join(f"def f{i}(db):\n    s = open_session(db)\n    close_session(s)\n" for i in range(6))
    neutral = "def make(db):\n    return open_session(db)\ndef keep(self, db):\n    self.s = open_session(db)\n"
    res = scan(mk(tmp_path, {"m.py": ok + neutral}))
    assert not res.findings


def test_guard_mutation(tmp_path):
    ok = "\n".join(f"def w{i}(a, amount):\n    if a.balance >= amount:\n        a.balance -= amount\n" for i in range(7))
    bad = "def sneaky(a, amount):\n    a.balance -= amount\n"
    res = scan(mk(tmp_path, {"b.py": ok + bad}))
    inv = by_kind(res, "guard-mutation")
    assert len(inv) == 1 and inv[0].follow == 7 and inv[0].exceptions[0].func == "sneaky"


def test_guard_mutation_constant_decrement_ignored(tmp_path):
    src = "\n".join(f"def w{i}(a):\n    a.count -= 1\n" for i in range(10))
    assert not scan(mk(tmp_path, {"b.py": src})).invariants


def test_sql_scope(tmp_path):
    ok = "\n".join(f'def u{i}(c, t, u):\n    c.execute("UPDATE notes SET body = %s WHERE id = %s AND tenant_id = %s", (1, 2, t))\n' for i in range(6))
    bad = 'def u_bad(c):\n    c.execute("DELETE FROM notes WHERE id = %s", (1,))\n'
    res = scan(mk(tmp_path, {"d.py": ok + bad}))
    inv = by_kind(res, "sql-scope")
    assert len(inv) == 1 and inv[0].id == "sql-scope:notes:tenant_id"
    assert inv[0].exceptions[0].func == "u_bad"


def test_sql_ambiguous_skipped(tmp_path):
    src = "\n".join(f'def u{i}(c, t):\n    c.execute(f"UPDATE {{t}} SET a=1 WHERE tenant_id = 1")\n' for i in range(8))
    src += 'def v(c):\n    c.execute("UPDATE {tbl} SET a=1")\n'
    assert not scan(mk(tmp_path, {"d.py": src})).invariants


def test_tests_dir_excluded(tmp_path):
    ok = "\n".join(f"def w{i}(a, amount):\n    if a.balance >= amount:\n        a.balance -= amount\n" for i in range(7))
    res = scan(mk(tmp_path, {"tests/t.py": ok + "def s(a, amount):\n    a.balance -= amount\n"}))
    assert not res.invariants


def test_tests_py_and_test_classes_excluded(tmp_path):
    body = "class FooHookTests:\n" + "".join(f"    def test_{i}(self):\n        self.check_webhook()\n" for i in range(8)) + "    def helper(self):\n        pass\n"
    res = scan(mk(tmp_path, {"hooks/tests.py": body, "hooks/real.py": body}))
    assert not res.invariants


def test_http_verb_methods_group(tmp_path):
    src = "".join(f"class R{i}(Base):\n    @require_admin\n    def get(self):\n        pass\n" for i in range(5))
    src += "class Open(Base):\n    def get(self):\n        pass\n"
    res = scan(mk(tmp_path, {"h.py": src}))
    inv = [i for i in res.findings if i.kind == "route-guard"]
    assert inv and inv[0].exceptions[0].line == 22 and "require_admin" in inv[0].id


def test_django_view_group(tmp_path):
    src = "".join(f"@login_required\ndef v{i}(request):\n    pass\n" for i in range(5)) + "def v_bad(request):\n    pass\n"
    res = scan(mk(tmp_path, {"views.py": src}))
    assert [e.func for i in res.findings for e in i.exceptions] == ["v_bad"]


def test_generic_check_calls_are_not_guards(tmp_path):
    src = "".join(f"class C:\n    def m{i}(self):\n        self.check_thing()\n" for i in range(8)) + "class D:\n    def n(self):\n        pass\n"
    assert not scan(mk(tmp_path, {"c.py": src})).findings


def test_class_group_ignores_undecorated_and_static_helpers(tmp_path):
    src = "class Api:\n" + "".join(f"    @expose('/{i}')\n    @protect()\n    def m{i}(self):\n        pass\n" for i in range(6))
    src += "    def pre_get_list(self, data):\n        pass\n    @staticmethod\n    def helper():\n        pass\n"
    assert not scan(mk(tmp_path, {"api.py": src})).findings


def test_guard_family_protect_read_counts_as_protect(tmp_path):
    src = "class Api:\n" + "".join(f"    @expose('/{i}')\n    @protect()\n    def m{i}(self):\n        pass\n" for i in range(6))
    src += "    @expose('/x')\n    @protect_read('A')\n    def x(self):\n        pass\n"
    assert not scan(mk(tmp_path, {"api.py": src})).findings


def test_class_group_still_flags_decorated_endpoint_without_guard(tmp_path):
    src = "class Api:\n" + "".join(f"    @expose('/{i}')\n    @protect()\n    def m{i}(self):\n        pass\n" for i in range(6))
    src += "    @expose('/open')\n    @safe\n    def open_one(self):\n        pass\n"
    assert [e.func for i in scan(mk(tmp_path, {"api.py": src})).findings for e in i.exceptions] == ["open_one"]


def test_before_request_hook_is_not_a_handler(tmp_path):
    src = "class Api:\n" + "".join(f"    @expose('/{i}')\n    @protect()\n    def m{i}(self):\n        pass\n" for i in range(6))
    src += "    @before_request(only=['a'])\n    def ensure_enabled(self):\n        pass\n"
    assert not scan(mk(tmp_path, {"api.py": src})).findings

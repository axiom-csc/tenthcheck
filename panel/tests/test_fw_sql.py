"""SQLite connector + SQL guard: structural blocks, caps, protected tables, and attempts to sneak around the rules."""
import sqlite3

import pytest

from tenthlook.panel.firewall import sqlguard
from tenthlook.panel.firewall.connectors.sqlite_db import SqliteConnector
from tenthlook.panel.firewall.types import Action
from tests.fw_helpers import make_fw

RULE = {"max_rows": 3, "consent_over_rows": 1}
SQL_CFG = {"protected_tables": ["secrets", "audit_log"]}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "app.db"
    con = sqlite3.connect(p)
    con.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, tier TEXT);
        CREATE TABLE secrets(id INTEGER PRIMARY KEY, v TEXT); CREATE TABLE audit_log(id INTEGER PRIMARY KEY, m TEXT);
        INSERT INTO users(name, tier) VALUES ('a','x'),('b','x'),('c','y'),('d','y'),('e','y'); INSERT INTO secrets(v) VALUES ('s');""")
    con.commit(); con.close()
    return str(p)


def fw_for(db, rule=None, mode="auto", **top):
    from tests.fw_helpers import policy, signer
    from tenthlook.panel.firewall.engine import Firewall
    from tenthlook.panel.firewall.ledger import Ledger
    pol = policy({"sql.query": rule or RULE}, mode, sql=SQL_CFG, **top)
    return Firewall(pol, Ledger(), signer(), [SqliteConnector(db, pol)], mode=mode)


def q(fw, sql, conv="c"):
    return fw.handle(Action("sql.query", {"sql": sql}, conv))


def rows(db, sql):
    con = sqlite3.connect(db); r = con.execute(sql).fetchall(); con.close(); return r


# --------------------------------------------------------------------------- structural blocks
@pytest.mark.parametrize("sql", [
    "DROP TABLE users", "drop table if exists users", "TRUNCATE TABLE users", "TRUNCATE users",
    "ALTER TABLE users ADD COLUMN x TEXT", "ALTER TABLE users RENAME TO u2",
    "PRAGMA writable_schema=1", "ATTACH DATABASE '/tmp/x.db' AS x", "VACUUM", "REINDEX", "EXPLAIN DELETE FROM users",
    "CREATE TABLE evil(a)", "CREATE TRIGGER t AFTER INSERT ON users BEGIN DELETE FROM users; END", "REPLACE INTO users VALUES (1,'z','z')",
])
def test_ddl_and_other_statement_types_blocked(db, sql):
    fw = fw_for(db)
    assert q(fw, sql).status == "blocked"
    assert rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


@pytest.mark.parametrize("sql", [
    "DELETE FROM users", "delete from users", "UPDATE users SET tier='z'", "DELETE FROM users;", "DELETE FROM users -- WHERE id=1",
    "DELETE FROM users /* WHERE id = 1 */", "DELETE FROM main.users", 'DELETE FROM "users"', "DELETE FROM [users]", "DELETE FROM `users`",
    "DeLeTe FrOm UsErS", "/* WHERE */ DELETE FROM users", "DELETE FROM users WHERE 1", "DELETE FROM users WHERE 1=1",
    "DELETE FROM users WHERE TRUE", "DELETE FROM users WHERE 'a'='a'", "DELETE FROM users WHERE 2>1", "DELETE FROM users WHERE NOT 0",
    "UPDATE users SET tier='z' WHERE 1=1",
])
def test_delete_update_without_real_where_blocked(db, sql):
    fw = fw_for(db)
    o = q(fw, sql)
    assert o.status == "blocked", sql
    assert rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE users", "SELECT 1; SELECT 2", "DELETE FROM users WHERE id=1; DELETE FROM users WHERE id=2",
    "SELECT * FROM users; -- harmless\nDROP TABLE users", "UPDATE users SET name='x' WHERE id=1;DROP TABLE users;",
    "SELECT 'a;b'; DROP TABLE users",
])
def test_multiple_statements_blocked(db, sql):
    assert q(fw_for(db), sql).status == "blocked"
    assert rows(db, "SELECT name FROM sqlite_master WHERE name='users'")


def test_semicolons_and_comments_inside_literals_do_not_confuse_the_parser(db):
    fw = fw_for(db)
    o = q(fw, "SELECT ';', '-- not a comment', '/* nor this */' FROM users WHERE id=1")
    assert o.status == "executed"
    assert o.result.output["rows"] == [[";", "-- not a comment", "/* nor this */"]]


def test_comment_trick_cannot_hide_drop(db):
    fw = fw_for(db)
    assert q(fw, "/* SELECT */ DROP /* TABLE */ TABLE users").status == "blocked"
    assert q(fw, "SELECT 1 /*; DROP TABLE users; */").status == "executed"     # inert comment: fine
    assert rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


def test_garbage_and_empty_and_nonstring_fail_closed(db):
    fw = fw_for(db)
    for sql in ("", "   ", ";", "-- only a comment", "DELETE FROM", "SELEC * FRM", "\x00", None, 5, ["DROP TABLE users"]):
        assert q(fw, sql).status == "blocked", repr(sql)


def test_subquery_cte_and_nested_cases(db):
    fw = fw_for(db)
    assert q(fw, "SELECT * FROM users WHERE id IN (SELECT id FROM secrets)").status == "blocked"          # protected table in subquery
    assert q(fw, "WITH s AS (SELECT * FROM secrets) SELECT * FROM s").status == "blocked"                # protected table in CTE
    assert q(fw, "WITH secrets AS (SELECT 1) SELECT * FROM secrets").status == "blocked"                 # CTE shadowing a protected name
    assert q(fw, "DELETE FROM users WHERE id IN (SELECT id FROM secrets)").status == "blocked"
    assert q(fw, "WITH x AS (SELECT id FROM users WHERE tier='x') DELETE FROM users WHERE id IN (SELECT id FROM x)").status == "needs_consent"  # 2 rows > consent threshold 1, <= cap 3
    assert q(fw, "SELECT id, (SELECT COUNT(*) FROM users u2 WHERE u2.tier=u.tier) FROM users u").status == "executed"


# --------------------------------------------------------------------------- protected tables
@pytest.mark.parametrize("sql", ["SELECT * FROM secrets", 'SELECT * FROM "Secrets"', "select * from [SECRETS]", "SELECT * FROM main.secrets",
                                 "INSERT INTO audit_log(m) VALUES ('x')", "UPDATE secrets SET v='z' WHERE id=1", "DELETE FROM audit_log WHERE id=1",
                                 "SELECT * FROM users JOIN secrets ON 1=1", "UPDATE sqlite_master SET name='x' WHERE 1 = 0"])
def test_protected_tables_blocked_any_spelling(db, sql):
    assert q(fw_for(db), sql).status == "blocked", sql
    assert rows(db, "SELECT v FROM secrets") == [("s",)]


# --------------------------------------------------------------------------- row caps
def test_row_cap_dry_run_count_before_execution(db):
    fw = fw_for(db)
    o = q(fw, "DELETE FROM users WHERE tier='y'")                  # 3 rows: == cap, > consent threshold
    assert o.status == "needs_consent" and rows(db, "SELECT COUNT(*) FROM users") == [(5,)]
    o = q(fw, "DELETE FROM users WHERE id > 0")                    # 5 rows > cap 3
    assert o.status == "blocked" and "5 rows" in " ".join(o.verdict.reasons)
    assert rows(db, "SELECT COUNT(*) FROM users") == [(5,)]
    o = q(fw, "DELETE FROM users WHERE id=1")                      # 1 row: within the consent threshold
    assert o.status == "executed" and rows(db, "SELECT COUNT(*) FROM users") == [(4,)]
    assert o.result.output["rows_affected"] == 1


def test_or_true_trick_is_caught_by_the_count_not_just_the_where_check(db):
    o = q(fw_for(db), "DELETE FROM users WHERE id=1 OR 1=1")
    assert o.status == "blocked" and rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


def test_exactly_at_cap_is_allowed_with_consent_and_cap_applies(db):
    fw = fw_for(db, {"max_rows": 3})
    assert q(fw, "UPDATE users SET tier='k' WHERE tier='y'").status == "executed"   # exactly 3 = cap
    assert q(fw, "UPDATE users SET tier='m' WHERE id > 0").status == "blocked"      # 5 > 3


def test_insert_counts_rows(db):
    fw = fw_for(db, {"max_rows": 2})
    assert q(fw, "INSERT INTO users(name,tier) VALUES ('p','q'),('r','q')").status == "executed"
    assert q(fw, "INSERT INTO users(name,tier) VALUES ('1','q'),('2','q'),('3','q')").status == "blocked"
    assert q(fw, "INSERT INTO users(name,tier) SELECT name, tier FROM users").status == "blocked"   # 7 rows copied


def test_update_with_join_or_from_not_supported_fail_closed(db):
    assert q(fw_for(db), "UPDATE users SET tier='z' FROM secrets WHERE users.id = secrets.id").status == "blocked"


def test_missing_database_for_counting_fails_closed(tmp_path):
    fw = fw_for(str(tmp_path / "nope.db"))
    assert q(fw, "DELETE FROM users WHERE id=1").status == "blocked"


def test_select_is_allowed_and_returns_rows(db):
    o = q(fw_for(db), "SELECT id, name FROM users ORDER BY id LIMIT 2")
    assert o.status == "executed" and o.result.output["rows"] == [[1, "a"], [2, "b"]]


def test_dry_run_changes_nothing_but_reports_row_count(db):
    fw = fw_for(db, {"max_rows": 10}, mode="dry_run")
    o = q(fw, "DELETE FROM users WHERE tier='y'")
    assert o.status == "dry_run" and o.result.output["rows_affected"] == 3 and rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


def test_execution_time_rollback_if_count_was_stale(db):
    """The cap is re-checked inside the transaction: if the table grew between count and run, nothing is committed."""
    conn = SqliteConnector(db)
    r = conn.run("DELETE FROM users WHERE id > 0", "auto", {"protected_tables": []}, max_rows=2)
    assert not r.ok and "rolled back" in r.error and rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


def test_authorizer_blocks_ddl_even_if_the_parser_were_fooled(db):
    conn = SqliteConnector(db)
    for sql in ("DROP TABLE users", "ALTER TABLE users ADD COLUMN z", "PRAGMA user_version=5", "ATTACH ':memory:' AS m",
                "SELECT * FROM secrets"):
        r = conn.run(sql, "auto", {"protected_tables": ["secrets"]}, None)
        assert not r.ok, sql
    assert rows(db, "SELECT COUNT(*) FROM users") == [(5,)]


def test_analyze_unit_kinds():
    a = sqlguard.analyze("SELECT 1")
    assert a.kind == "select" and not a.block
    assert sqlguard.analyze("DROP TABLE x").block
    assert sqlguard.analyze("INSERT INTO t VALUES (1),(2),(3)").rows == 3


def test_update_from_is_detected_with_an_unprotected_table(db):
    """Guards against sqlglot renaming the 'from' arg: this must stay blocked for the right reason."""
    o = q(fw_for(db), "UPDATE users SET tier='z' FROM users AS u2 WHERE users.id = u2.id AND u2.id = 1")
    assert o.status == "blocked" and "FROM/JOIN" in " ".join(o.verdict.reasons)


def test_cte_is_kept_in_the_row_count(db):
    a = sqlguard.analyze("WITH x AS (SELECT id FROM users) DELETE FROM users WHERE id IN (SELECT id FROM x)", None,
                         lambda q: sqlite3.connect(db).execute(q).fetchone()[0])
    assert a.rows == 5 and not a.block

"""Local approval endpoint: token + Host checks, localhost-only bind, approve/deny recorded with who/when."""
import json
import urllib.error
import urllib.request

import pytest

from tenthlook.panel.firewall.types import FirewallError
from tenthlook.panel.firewall.ui import make_server, start_background
from tests.fw_helpers import act, make_fw


def req(srv, path, method="GET", token=True, host=None, body=None):
    port = srv.server_address[1]
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None)
    if token:
        r.add_header("X-Panel-Token", srv.panel_token)
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null") if "json" in resp.headers["Content-Type"] else resp.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_refuses_non_local_bind():
    fw = make_fw()
    for h in ("0.0.0.0", "192.168.1.5", ""):
        with pytest.raises(FirewallError):
            make_server(fw.ledger, 0, host=h)


def test_approve_via_http_records_who_and_releases_action():
    fw = make_fw(); srv = start_background(fw.ledger, 0, "ui-human")
    try:
        o = fw.handle(act(amount=75))
        assert req(srv, "/api/pending")[1]["pending"][0]["approval_id"] == o.approval_id
        code, body = req(srv, f"/api/approve/{o.approval_id}", "POST", body={})
        assert code == 200 and body["who"] == "ui-human"
        assert fw.handle(act(amount=75)).status == "executed"
        assert req(srv, f"/api/approve/{o.approval_id}", "POST", body={})[0] == 409     # already decided
        assert any(e["event"] == "approved" for e in req(srv, "/api/feed")[1]["events"])
        assert req(srv, "/api/receipts")[1]["receipts"]
    finally:
        srv.shutdown()


def test_deny_via_http():
    fw = make_fw(); srv = start_background(fw.ledger, 0)
    try:
        o = fw.handle(act(amount=75))
        assert req(srv, f"/api/deny/{o.approval_id}", "POST", body={})[0] == 200
        assert fw.handle(act(amount=75)).status == "blocked"
    finally:
        srv.shutdown()


def test_token_and_host_header_required():
    fw = make_fw(); srv = start_background(fw.ledger, 0)
    try:
        o = fw.handle(act(amount=75))
        assert req(srv, "/api/pending", token=False)[0] == 401
        assert req(srv, f"/api/approve/{o.approval_id}", "POST", token=False, body={})[0] == 401
        assert req(srv, "/api/pending", host="evil.example.com")[0] == 403           # DNS rebinding
        assert req(srv, f"/api/approve/{o.approval_id}", "POST", host="evil.example.com", body={})[0] == 403
        assert fw.handle(act(amount=75)).status == "needs_consent"
        assert req(srv, "/", token=False)[0] == 200                                   # static shell holds no data
    finally:
        srv.shutdown()


def test_page_never_uses_innerhtml_for_ledger_text():
    from tenthlook.panel.firewall.ui import PAGE
    assert "innerHTML" not in PAGE and "textContent" in PAGE

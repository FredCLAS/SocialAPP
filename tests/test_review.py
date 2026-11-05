import http.client
import threading
from datetime import date
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.post_queue import get_post, list_posts, set_post_fields
from socialops.qa import check_post
from socialops.review import build_items, handle_action, make_server, render_page, safe_asset_path

MONDAY = date(2026, 11, 2)
GOOD = "HIPAA is more than paperwork. A policy alone does not protect a practice. See https://classbyfred.com"


@pytest.fixture
def cfg(real_config_path):
    return load_config(real_config_path)


@pytest.fixture
def db(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    generate_week(c, cfg, MONDAY)
    c.close()
    return tmp_db


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "SocialOps" / "assets" / "clas_by_fred"
    folder.mkdir(parents=True)
    (folder / "a.png").write_bytes(b"\x89PNG fake")
    return tmp_path / "SocialOps"


def fb_drafts(conn):
    return [p for p in list_posts(conn, brand_key="clas_by_fred", status="draft") if p.platform == "facebook"]


def make_passing(conn, cfg, pid, visual="assets/clas_by_fred/a.png"):
    set_post_fields(conn, pid, subject="HIPAA basics", visual_path=visual, visual_source="canva",
                    caption=GOOD, hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"],
                    cta_url="https://classbyfred.com")
    check_post(conn, cfg, pid)


def test_items_show_qa_and_blockers(db, cfg):
    conn = connect(db)
    a, b = fb_drafts(conn)[:2]
    make_passing(conn, cfg, a.id)
    items = {i.post.id: i for i in build_items(conn, cfg, "clas_by_fred", MONDAY)}
    assert items[a.id].qa_passed is True and items[a.id].blockers == []
    assert items[b.id].qa_passed is None and items[b.id].blockers  # not checked, has blockers
    conn.close()


def test_safe_asset_path_allows_only_brand_folder(root):
    assert safe_asset_path(root, "assets/clas_by_fred/a.png", "clas_by_fred") is not None
    assert safe_asset_path(root, "../secret.png", "clas_by_fred") is None
    assert safe_asset_path(root, "/etc/passwd", "clas_by_fred") is None
    assert safe_asset_path(root, "assets/pamela_art/a.png", "clas_by_fred") is None
    assert safe_asset_path(root, "assets/clas_by_fred/missing.png", "clas_by_fred") is None
    assert safe_asset_path(root, None, "clas_by_fred") is None


def test_render_escapes_caption_and_carries_token(db, cfg):
    conn = connect(db)
    p = fb_drafts(conn)[0]
    set_post_fields(conn, p.id, caption="<script>alert(1)</script> See https://classbyfred.com")
    items = build_items(conn, cfg, "clas_by_fred", MONDAY)
    page = render_page(items, cfg, "clas_by_fred", MONDAY, token="TOKEN123")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert page.count("name='token' value='TOKEN123'") >= 2
    conn.close()


def test_approve_blocked_without_qa(db, cfg):
    conn = connect(db)
    p = fb_drafts(conn)[0]
    msg = handle_action(conn, cfg, "pamela", "approve", {"post_id": [str(p.id)]})
    assert "not approved" in msg and "QA has not run" in msg
    assert get_post(conn, p.id).status == "draft"
    conn.close()


def test_approve_after_qa_pass(db, cfg):
    conn = connect(db)
    p = fb_drafts(conn)[0]
    make_passing(conn, cfg, p.id)
    msg = handle_action(conn, cfg, "pamela", "approve", {"post_id": [str(p.id)]})
    assert "approved" in msg
    assert get_post(conn, p.id).status == "approved"
    conn.close()


def test_reject_needs_reason_and_is_recorded(db, cfg):
    conn = connect(db)
    p = fb_drafts(conn)[0]
    assert "reason is required" in handle_action(conn, cfg, "pamela", "reject", {"post_id": [str(p.id)], "reason": [" "]})
    handle_action(conn, cfg, "pamela", "reject", {"post_id": [str(p.id)], "reason": ["Too salesy"]})
    assert get_post(conn, p.id).status == "draft"
    item = next(i for i in build_items(conn, cfg, "clas_by_fred", MONDAY) if i.post.id == p.id)
    assert item.rejection_note == "Too salesy"
    conn.close()


def test_approve_passing_skips_the_rest(db, cfg):
    conn = connect(db)
    drafts = fb_drafts(conn)
    make_passing(conn, cfg, drafts[0].id)
    make_passing(conn, cfg, drafts[1].id)
    msg = handle_action(conn, cfg, "pamela", "approve-passing",
                        {"brand": ["clas_by_fred"], "week": [MONDAY.isoformat()]})
    assert msg.startswith("Approved 2.")
    assert get_post(conn, drafts[0].id).status == "approved"
    assert get_post(conn, drafts[2].id).status == "draft"
    conn.close()


def test_unknown_action_and_bad_id(db, cfg):
    conn = connect(db)
    assert handle_action(conn, cfg, "pamela", "publish", {}) == "Unknown action."
    assert handle_action(conn, cfg, "pamela", "approve", {"post_id": ["x"]}) == "Bad post id."
    conn.close()


# --- HTTP layer ---------------------------------------------------------

@pytest.fixture
def running(db, cfg, root):
    server = make_server(db, cfg, "clas_by_fred", MONDAY, "pamela", root=root, token="T0K3N", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, server.server_address[1], db
    server.shutdown()
    server.server_close()


def request(port, method, path, body=None, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"{host}:{port}"}
    if body is not None:
        headers["content-type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def test_http_requires_token(running):
    _, port, _ = running
    assert request(port, "GET", "/")[0] == 403
    assert request(port, "GET", "/?token=wrong")[0] == 403


def test_http_rejects_foreign_host(running):
    _, port, _ = running
    assert request(port, "GET", "/?token=T0K3N", host="evil.example")[0] == 400


def test_http_page_renders_with_token(running):
    _, port, _ = running
    status, headers, body = request(port, "GET", "/?token=T0K3N")
    assert status == 200 and b"Approve" in body
    assert headers["content-security-policy"].startswith("default-src 'self'")


def test_http_post_needs_token(db, running):
    _, port, db_path = running
    status, _, _ = request(port, "POST", "/approve", body="post_id=1")
    assert status == 403
    status, _, _ = request(port, "POST", "/approve", body="post_id=1&token=nope")
    assert status == 403


def test_http_approve_round_trip(db, cfg, running):
    _, port, db_path = running
    conn = connect(db_path)
    p = fb_drafts(conn)[0]
    make_passing(conn, cfg, p.id)
    conn.close()
    status, headers, _ = request(port, "POST", "/approve", body=f"post_id={p.id}&token=T0K3N")
    assert status == 303 and "msg=" in headers["location"]
    conn = connect(db_path)
    assert get_post(conn, p.id).status == "approved"
    conn.close()


def test_asset_served_only_when_safe(db, cfg, running):
    _, port, db_path = running
    conn = connect(db_path)
    p = fb_drafts(conn)[0]
    set_post_fields(conn, p.id, visual_path="assets/clas_by_fred/a.png", visual_source="canva")
    conn.close()
    status, headers, body = request(port, "GET", f"/asset/{p.id}?token=T0K3N")
    assert status == 200 and headers["content-type"] == "image/png" and body.startswith(b"\x89PNG")
    conn = connect(db_path)
    set_post_fields(conn, p.id, visual_path="../../etc/passwd")
    conn.close()
    assert request(port, "GET", f"/asset/{p.id}?token=T0K3N")[0] == 404

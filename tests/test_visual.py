import io
import json
import struct
from datetime import date
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.post_queue import NotApprovable, get_post, list_posts, set_post_fields, transition
from socialops.qa import check_post
from socialops.visual import (
    CanvaHttp, RenderError, check_asset, check_visuals, image_size, render_post, template_fields,
)

MONDAY = date(2026, 11, 2)
GOOD_CAPTION = "HIPAA is more than paperwork. A policy alone does not protect a practice. See https://classbyfred.com"


def make_png(w, h, size=20_000):
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"\x00" * 4
    return head + b"\x00" * max(0, size - len(head))


def make_jpeg(w, h, size=20_000):
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00" + sof + b"\x00" * max(0, size - 60)


RENDER_OK_PNG = [
    {"job": {"id": "J", "status": "in_progress"}},                       # POST /autofills
    {"job": {"id": "J", "status": "success", "result": {"design": {"id": "D9"}}}},
    {"job": {"id": "E", "status": "in_progress"}},                       # POST /exports
    {"job": {"id": "E", "status": "success", "result": {"urls": ["https://exports.example/p.png"]}}},
]


@pytest.fixture
def cfg(tmp_path):
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_").replace(
        'api_verified = false', 'api_verified = true').replace(
        'canva_template_id = "TEST_CANVA_TEMPLATE_ID"', 'canva_template_id = "TPL123"')
    p = tmp_path / "brands.toml"
    p.write_text(text)
    return load_config(p)


@pytest.fixture
def conn(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    generate_week(c, cfg, MONDAY)
    yield c
    c.close()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "SocialOps"


def image_post(conn):
    return next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft") if p.format == "image")


def write_asset(root, rel, data):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


# --- file checks -------------------------------------------------------------

def test_png_and_jpeg_sizes_read(tmp_path):
    assert image_size(write_asset(tmp_path, "a.png", make_png(1080, 1350))) == (1080, 1350, "png")
    assert image_size(write_asset(tmp_path, "b.jpg", make_jpeg(1200, 628))) == (1200, 628, "jpeg")


def test_good_asset_passes(tmp_path):
    assert check_asset(write_asset(tmp_path, "ok.png", make_png(1080, 1350))) == []


@pytest.mark.parametrize("w,h,expect", [
    (300, 300, "under 600px"),
    (4000, 900, "aspect"),
    (900, 1500, "aspect"),
])
def test_bad_dimensions_fail(tmp_path, w, h, expect):
    problems = check_asset(write_asset(tmp_path, "x.png", make_png(w, h)))
    assert any(expect in p for p in problems)


def test_tiny_and_huge_files_fail(tmp_path):
    assert any("too small" in p for p in check_asset(write_asset(tmp_path, "t.png", make_png(1080, 1080, 500))))


def test_not_an_image_fails(tmp_path):
    assert check_asset(write_asset(tmp_path, "bad.png", b"hello" * 5000)) == ["not a PNG or JPEG"]


def test_missing_file_fails(tmp_path):
    assert check_asset(tmp_path / "nope.png") == ["file missing"]


# --- Canva client -----------------------------------------------------------

class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, token):
        self.calls.append((method, url, body, token))
        return self.responses.pop(0)


def test_autofill_then_export_flow(monkeypatch):
    t = FakeTransport([
        {"job": {"id": "J1", "status": "in_progress"}},  # POST /autofills
        {"job": {"id": "J1", "status": "success", "result": {"design": {"id": "D1"}}}},
    ])
    client = CanvaHttp("https://api.canva.com/rest/v1", lambda: "TOK", transport=t, sleep=lambda s: None)
    assert client.autofill("TPL123", {"headline": {"type": "text", "text": "x"}}) == "D1"
    assert t.calls[0][1].endswith("/autofills") and t.calls[0][2]["brand_template_id"] == "TPL123"
    assert all(call[3] == "TOK" for call in t.calls)  # token only from the provider


def test_export_downloads_png(monkeypatch):
    t = FakeTransport([
        {"job": {"id": "E1", "status": "in_progress"}},
        {"job": {"id": "E1", "status": "success", "result": {"urls": ["https://exports.example/x.png"]}}},
    ])
    client = CanvaHttp("https://api.canva.com/rest/v1", lambda: "TOK", transport=t, sleep=lambda s: None)
    import socialops.visual as v
    monkeypatch.setattr(v.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(make_png(1080, 1080)))
    assert client.export_png("D1").startswith(b"\x89PNG")


def test_failed_job_raises():
    t = FakeTransport([{"job": {"id": "J2", "status": "in_progress"}}, {"job": {"id": "J2", "status": "failed"}}])
    client = CanvaHttp("https://api.canva.com/rest/v1", lambda: "TOK", transport=t, sleep=lambda s: None)
    with pytest.raises(RenderError, match="failed"):
        client.autofill("TPL123", {})


# --- rendering --------------------------------------------------------------

def test_render_blocked_until_api_verified(conn, cfg, root, tmp_path):
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_").replace(
        'canva_template_id = "TEST_CANVA_TEMPLATE_ID"', 'canva_template_id = "TPL123"')
    p = tmp_path / "unverified.toml"
    p.write_text(text)
    unverified = load_config(p)
    r = render_post(conn, unverified, image_post(conn).id, None, Guard(dry_run=False), root)
    assert r.status == "blocked" and "not verified" in r.detail


def test_render_blocked_without_template(conn, tmp_path, root):
    p = tmp_path / "notemplate.toml"
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_").replace(
        'canva_template_id = "TEST_CANVA_TEMPLATE_ID"', 'canva_template_id = ""')
    p.write_text(text)
    cfg2 = load_config(p)
    r = render_post(conn, cfg2, image_post(conn).id, None, Guard(dry_run=True), root)
    assert r.status == "blocked" and "no Canva template" in r.detail


def test_dry_run_render_calls_nothing(conn, cfg, root):
    r = render_post(conn, cfg, image_post(conn).id, None, Guard(dry_run=True), root)
    assert r.status == "would_render" and r.detail.startswith("assets/clas_by_fred/exports/post_")


def test_live_render_saves_png_and_checks(conn, cfg, root):
    pid = image_post(conn).id
    t = FakeTransport(RENDER_OK_PNG)
    client = CanvaHttp("https://api.canva.com/rest/v1", lambda: "TOK", transport=t, sleep=lambda s: None)
    import socialops.visual as v
    v_png = make_png(1080, 1350)
    orig = v.urllib.request.urlopen
    v.urllib.request.urlopen = lambda url, timeout: io.BytesIO(v_png)
    try:
        r = render_post(conn, cfg, pid, client, Guard(dry_run=False), root)
    finally:
        v.urllib.request.urlopen = orig
    assert r.status == "rendered"
    post = get_post(conn, pid)
    assert post.visual_source == "canva" and post.visual_path.endswith(f"post_{pid}.png")
    assert (root / post.visual_path).read_bytes() == v_png


def test_live_render_rejects_non_png(conn, cfg, root):
    pid = image_post(conn).id
    t = FakeTransport(RENDER_OK_PNG)
    client = CanvaHttp("https://api.canva.com/rest/v1", lambda: "TOK", transport=t, sleep=lambda s: None)
    import socialops.visual as v
    orig = v.urllib.request.urlopen
    v.urllib.request.urlopen = lambda url, timeout: io.BytesIO(b"<html>oops</html>" * 10)
    try:
        r = render_post(conn, cfg, pid, client, Guard(dry_run=False), root)
    finally:
        v.urllib.request.urlopen = orig
    assert r.status == "failed" and "not a PNG" in r.detail


def test_template_fields_map_from_post(conn, cfg):
    pid = image_post(conn).id
    set_post_fields(conn, pid, subject="HIPAA basics", caption=GOOD_CAPTION, cta_url="https://classbyfred.com")
    fields = template_fields(get_post(conn, pid), cfg.brands["clas_by_fred"])
    assert fields["headline"]["text"] == "HIPAA basics"
    assert fields["hook"]["text"].startswith("HIPAA is more than paperwork")
    assert fields["cta"]["text"] == "https://classbyfred.com"


# --- asset checks and approval ----------------------------------------------

def test_failed_asset_check_blocks_approval(conn, cfg, root):
    pid = image_post(conn).id
    set_post_fields(conn, pid, subject="HIPAA basics", visual_path=f"assets/clas_by_fred/p{pid}.png",
                    visual_source="canva", caption=GOOD_CAPTION, hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"],
                    cta_url="https://classbyfred.com")
    write_asset(root, f"assets/clas_by_fred/p{pid}.png", make_png(300, 300))
    results = check_visuals(conn, cfg, "clas_by_fred", MONDAY, root)
    assert dict(results)[pid]
    check_post(conn, cfg, pid)
    with pytest.raises(NotApprovable, match="asset check failed"):
        transition(conn, pid, "approved", actor="pamela")


def test_good_asset_check_allows_approval(conn, cfg, root):
    pid = image_post(conn).id
    set_post_fields(conn, pid, subject="HIPAA basics", visual_path=f"assets/clas_by_fred/p{pid}.png",
                    visual_source="canva", caption=GOOD_CAPTION, hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"],
                    cta_url="https://classbyfred.com")
    write_asset(root, f"assets/clas_by_fred/p{pid}.png", make_png(1080, 1350))
    assert dict(check_visuals(conn, cfg, "clas_by_fred", MONDAY, root))[pid] == []
    check_post(conn, cfg, pid)
    assert transition(conn, pid, "approved", actor="pamela").status == "approved"


def test_unsafe_visual_path_fails_check(conn, cfg, root):
    pid = image_post(conn).id
    set_post_fields(conn, pid, visual_path="../../etc/passwd", visual_source="canva")
    assert dict(check_visuals(conn, cfg, "clas_by_fred", MONDAY, root))[pid]

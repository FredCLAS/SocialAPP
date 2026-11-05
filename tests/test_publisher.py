import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.meta_graph import NetworkFailure
from socialops.post_queue import get_post, list_posts, set_post_fields, transition
from socialops.publisher import (
    IG_DAILY_LIMIT,
    PublishBlocked,
    caption_text,
    media_url,
    publish_due,
    schedule_week,
    token_status,
)
from socialops.secrets import MemorySecretStore, service_name

MONDAY = date(2026, 11, 2)

from pathlib import Path as _P
from socialops.config import load_config as _load
from socialops.qa import check_post as _check
_CFG = _load(_P(__file__).resolve().parents[1] / "config" / "brands.toml")
DUE_NOW = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)   # after Mon 08:30 EST posts
EARLY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)     # before them
FAR_FUTURE = 9_999_999_999


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, fields, headers):
        self.calls.append({"method": method, "url": url, "fields": dict(fields), "headers": dict(headers)})
        if not self.responses:
            return (200, {"id": "X" + str(len(self.calls))})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def cfg(tmp_path):
    """Real config with placeholder account ids filled in and a media host set (test copy only)."""
    text = Path("config/brands.toml").read_text()
    text = text.replace("REPLACE_", "TEST_").replace(
        'public_base_url = "https://whimsical-sprite-d0ccf7.netlify.app/social-media"',
        'public_base_url = "https://media.example.com"')
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
def store():
    s = MemorySecretStore()
    for brand, platform in [("clas_by_fred", "facebook"), ("clas_by_fred", "instagram"),
                            ("pamela_art", "facebook"), ("pamela_art", "instagram")]:
        svc = service_name(brand, platform)
        s.set(svc, "access_token", f"TOK-{brand}-{platform}")
        s.set(svc, "expires_at", str(FAR_FUTURE))
    return s


def ready(conn, cfg, brand="clas_by_fred", platform="facebook", fmt="image", visual="assets/clas_by_fred/a.png"):
    post = next(p for p in list_posts(conn, brand_key=brand, status="draft") if p.platform == platform)
    set_post_fields(conn, post.id, subject="HIPAA basics", visual_path=visual, visual_source="canva",
                    caption=f"Hook. Value. Read more at {cfg.brands[brand].website}", hashtags=["#HIPAA", "#compliance", "#privacy"],
                    cta_url=cfg.brands[brand].website)
    conn.execute("UPDATE posts SET format=? WHERE id=?", (fmt, post.id))
    _check(conn, _CFG, post.id)
    transition(conn, post.id, "approved", actor="pamela")
    transition(conn, post.id, "scheduled")
    return post.id


def log_rows(conn, post_id):
    return [dict(r) for r in conn.execute("SELECT * FROM publish_log WHERE post_id=? ORDER BY id", (post_id,))]


# --- helpers --------------------------------------------------------------

def test_caption_text_joins_caption_and_hashtags(conn, cfg):
    pid = ready(conn, cfg)
    assert caption_text(get_post(conn, pid)) == f"Hook. Value. Read more at {cfg.brands['clas_by_fred'].website}\n\n#HIPAA #compliance #privacy"


def test_media_url_encodes_and_checks_path():
    assert media_url("https://m.example.com/", "assets/clas by fred/a.png") == \
        "https://m.example.com/assets/clas%20by%20fred/a.png"
    with pytest.raises(PublishBlocked):
        media_url(None, "a.png")
    with pytest.raises(PublishBlocked):
        media_url("https://m", "/etc/passwd")
    with pytest.raises(PublishBlocked):
        media_url("https://m", "../secret.png")


def test_token_status_states():
    s = MemorySecretStore()
    svc = "socialops.x.facebook"
    now = 1_000_000
    assert token_status(s, svc, now) == "missing"
    s.set(svc, "access_token", "T")
    assert token_status(s, svc, now) == "ok"  # no expiry recorded
    s.set(svc, "expires_at", str(now + 3 * 24 * 3600))
    assert token_status(s, svc, now) == "expiring"
    s.set(svc, "expires_at", str(now + 30 * 24 * 3600))
    assert token_status(s, svc, now) == "ok"
    s.set(svc, "expires_at", str(now - 1))
    assert token_status(s, svc, now) == "expired"


# --- scheduling -----------------------------------------------------------

def test_schedule_week_moves_only_approved(cfg, conn):
    pid = ready(conn, cfg)
    moved = schedule_week(conn, cfg, "clas_by_fred", MONDAY)
    assert pid not in moved  # already scheduled
    assert all(p.status == "draft" for p in list_posts(conn, brand_key="clas_by_fred", status="draft"))


# --- dry-run --------------------------------------------------------------

def test_dry_run_publishes_nothing(cfg, conn, monkeypatch):
    pid = ready(conn, cfg)
    import socialops.meta_graph as mg
    monkeypatch.setattr(mg, "default_transport", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    rec = Recorder()
    guard = Guard(dry_run=True)
    results = publish_due(conn, cfg, None, guard, DUE_NOW, transport=rec)
    assert [r.outcome for r in results if r.post_id == pid] == ["would_publish"]
    assert rec.calls == []
    assert get_post(conn, pid).status == "scheduled"
    assert log_rows(conn, pid) == []
    assert any("publish post" in d for d in guard.would_have)


# --- live publish ---------------------------------------------------------

def test_live_facebook_photo_publishes_once(cfg, conn, store):
    pid = ready(conn, cfg)
    rec = Recorder((200, {"id": "p1", "post_id": "PAGE_POST_1"}))
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    result = next(r for r in results if r.post_id == pid)
    assert result.outcome == "published" and result.detail == "PAGE_POST_1"
    assert get_post(conn, pid).status == "published"
    call = rec.calls[0]
    assert call["url"].endswith("/TEST_CLAS_FB_PAGE_ID/photos") or "/TEST_" in call["url"]
    assert call["fields"]["url"] == "https://media.example.com/assets/clas_by_fred/a.png"
    assert call["headers"]["authorization"] == "Bearer TOK-clas_by_fred-facebook"
    rows = log_rows(conn, pid)
    assert [r["outcome"] for r in rows] == ["ok"] and rows[0]["remote_id"] == "PAGE_POST_1"


def test_second_run_does_not_republish(cfg, conn, store):
    pid = ready(conn, cfg)
    rec = Recorder((200, {"post_id": "P1"}))
    publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    again = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert again == []  # published posts are no longer due
    assert len(rec.calls) == 1


def test_ok_log_with_scheduled_status_is_reconciled_not_reposted(cfg, conn, store):
    pid = ready(conn, cfg)
    conn.execute("INSERT INTO publish_log (post_id, platform, outcome, remote_id) VALUES (?, 'facebook', 'ok', 'P0')", (pid,))
    conn.commit()
    rec = Recorder()
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in results if r.post_id == pid).outcome == "skipped"
    assert rec.calls == []
    assert get_post(conn, pid).status == "published"


def test_future_post_not_published(cfg, conn, store):
    ready(conn, cfg)
    rec = Recorder()
    assert publish_due(conn, cfg, store, Guard(dry_run=False), EARLY, transport=rec) == []
    assert rec.calls == []


def test_definitive_error_marks_failed_and_can_retry(cfg, conn, store):
    pid = ready(conn, cfg)
    rec = Recorder((400, {"error": {"code": 100, "message": "invalid image"}}))
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in results if r.post_id == pid).outcome == "failed"
    post = get_post(conn, pid)
    assert post.status == "failed"
    assert conn.execute("SELECT retry_count FROM posts WHERE id=?", (pid,)).fetchone()[0] == 1
    assert log_rows(conn, pid)[0]["outcome"] == "failed"
    assert "invalid image" in log_rows(conn, pid)[0]["detail"]
    transition(conn, pid, "scheduled")  # operator retry is allowed


def test_ambiguous_error_blocks_retry(cfg, conn, store):
    pid = ready(conn, cfg)
    rec = Recorder((500, {"error": {"message": "try later"}}))
    first = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in first if r.post_id == pid).outcome == "unknown"
    assert get_post(conn, pid).status == "scheduled"  # not failed: it may be live
    second = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in second if r.post_id == pid).outcome == "blocked"
    assert len(rec.calls) == 1  # no second network write


def test_network_timeout_on_publish_is_unknown(cfg, conn, store):
    pid = ready(conn, cfg)
    rec = Recorder(NetworkFailure("timeout"))
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in results if r.post_id == pid).outcome == "unknown"
    assert log_rows(conn, pid)[0]["outcome"] == "unknown"


def test_crash_leaves_attempt_that_blocks_retry(cfg, conn, store):
    pid = ready(conn, cfg)
    conn.execute("INSERT INTO publish_log (post_id, platform, outcome) VALUES (?, 'facebook', 'attempt')", (pid,))
    conn.commit()
    rec = Recorder()
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in results if r.post_id == pid).outcome == "blocked"
    assert rec.calls == []


def test_placeholder_account_blocks_without_network(cfg, conn, store, tmp_path):
    text = Path("config/brands.toml").read_text()
    p = tmp_path / "real.toml"
    p.write_text(text)  # keeps REPLACE_ placeholders
    real = load_config(p)
    pid = ready(conn, real)
    rec = Recorder()
    results = publish_due(conn, real, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    result = next(r for r in results if r.post_id == pid)
    assert result.outcome == "blocked" and "account_id" in result.detail
    assert rec.calls == []


def test_missing_token_blocks(cfg, conn):
    pid = ready(conn, cfg)
    rec = Recorder()
    results = publish_due(conn, cfg, MemorySecretStore(), Guard(dry_run=False), DUE_NOW, transport=rec)
    assert "missing" in next(r for r in results if r.post_id == pid).detail
    assert rec.calls == []


def test_expired_token_blocks(cfg, conn):
    pid = ready(conn, cfg)
    s = MemorySecretStore()
    svc = service_name("clas_by_fred", "facebook")
    s.set(svc, "access_token", "T")
    s.set(svc, "expires_at", "1")
    rec = Recorder()
    results = publish_due(conn, cfg, s, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert "expired" in next(r for r in results if r.post_id == pid).detail
    assert rec.calls == []


def test_missing_media_host_blocks_and_logs(cfg, conn, store, tmp_path):
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_").replace(
        'public_base_url = "https://whimsical-sprite-d0ccf7.netlify.app/social-media"', 'public_base_url = ""')
    p = tmp_path / "nomedia.toml"
    p.write_text(text)
    nomedia = load_config(p)
    pid = ready(conn, nomedia)
    rec = Recorder()
    results = publish_due(conn, nomedia, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in results if r.post_id == pid).outcome == "blocked"
    assert rec.calls == []
    assert log_rows(conn, pid)[0]["outcome"] == "failed"


def test_instagram_daily_limit(cfg, conn, store):
    pid = ready(conn, cfg, brand="clas_by_fred", platform="instagram")
    ig_post = conn.execute(
        "SELECT id FROM posts WHERE brand_key='clas_by_fred' AND platform='instagram' AND id != ? LIMIT 1", (pid,)
    ).fetchone()[0]
    for _ in range(IG_DAILY_LIMIT):
        conn.execute("INSERT INTO publish_log (post_id, platform, outcome, logged_at) VALUES (?, 'instagram', 'ok', ?)",
                     (ig_post, "2026-11-02T20:00:00.000Z"))
    conn.commit()
    rec = Recorder()
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec)
    assert next(r for r in results if r.post_id == pid).detail == "Instagram 24h publish limit reached"
    assert rec.calls == []


def test_instagram_image_uses_container_flow(cfg, conn, store):
    pid = ready(conn, cfg, brand="clas_by_fred", platform="instagram")
    rec = Recorder((200, {"id": "C1"}), (200, {"status_code": "FINISHED"}), (200, {"id": "IGMEDIA1"}))
    results = publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec, sleep=lambda s: None)
    assert next(r for r in results if r.post_id == pid).outcome == "published"
    assert [c["url"].rsplit("/", 1)[-1] for c in rec.calls] == ["media", "C1", "media_publish"]


def test_video_post_uses_reels(cfg, conn, store):
    pid = ready(conn, cfg, brand="clas_by_fred", platform="instagram", fmt="video",
                visual="assets/clas_by_fred/v.mp4")
    rec = Recorder((200, {"id": "C2"}), (200, {"status_code": "FINISHED"}), (200, {"id": "R9"}))
    publish_due(conn, cfg, store, Guard(dry_run=False), DUE_NOW, transport=rec, sleep=lambda s: None)
    assert rec.calls[0]["fields"]["media_type"] == "REELS"
    assert get_post(conn, pid).status == "published"


def test_naive_datetime_rejected(cfg, conn):
    with pytest.raises(ValueError, match="timezone-aware"):
        publish_due(conn, cfg, None, Guard(dry_run=True), datetime(2026, 11, 2, 23, 0))

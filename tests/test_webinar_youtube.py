import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.post_queue import get_post, list_posts
from socialops.secrets import MemorySecretStore, service_name
from socialops.webinar import attach_promotions, create_webinar, overdue, set_replay_file, set_status
from socialops.youtube import (
    approve_public, build_description, can_spend, publish_public, record_spend, upload_replay,
    used_today, validate_chapters,
)
from socialops.youtube import SERVICE as YT_SERVICE

WEBINAR_START = datetime(2026, 12, 2, 19, 0)  # local ET
CHAPTERS = [["00:00", "Welcome"], ["05:12", "Risk assessment"], ["12:40", "Next steps"]]


class FakeUploader:
    def __init__(self):
        self.started = []
        self.privacy_calls = []

    def start(self, metadata, size, token):
        self.started.append((metadata, size, token))
        return "https://upload.example/session"

    def send(self, session, data, token):
        return "YT_VIDEO_1"

    def set_privacy(self, video_id, privacy, token):
        self.privacy_calls.append((video_id, privacy))


class BrokenUploader(FakeUploader):
    def send(self, session, data, token):
        raise RuntimeError("upload failed: HTTP 500")


@pytest.fixture
def cfg(tmp_path):
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_").replace(
        'client_id = "REPLACE_GOOGLE_CLIENT_ID"', 'client_id = "test-client"')
    p = tmp_path / "brands.toml"
    p.write_text(text)
    return load_config(p)


@pytest.fixture
def conn(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    for monday in [date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 23), date(2026, 11, 30)]:
        generate_week(c, cfg, monday)
    yield c
    c.close()


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "SocialOps"
    (base / "assets" / "clas_by_fred").mkdir(parents=True)
    (base / "assets" / "clas_by_fred" / "replay.mp4").write_bytes(b"fakevideo" * 10)
    return base


@pytest.fixture
def store():
    s = MemorySecretStore()
    s.set(YT_SERVICE, "access_token", "YT-TOKEN")
    return s


def new_webinar(conn, cfg, starts=WEBINAR_START):
    wid = create_webinar(conn, cfg, "clas_by_fred", "HIPAA risk basics", starts,
                         "https://classbyfred.com/webinar", "https://calendly.com/fred/intro")
    set_replay_file(conn, wid, "assets/clas_by_fred/replay.mp4", CHAPTERS)
    return wid


def now_after(conn, wid, hours_after_end=1):
    w = conn.execute("SELECT * FROM webinars WHERE id=?", (wid,)).fetchone()
    start = datetime.fromisoformat(w["starts_at"].replace("Z", "+00:00"))
    return start + timedelta(minutes=w["duration_minutes"], hours=hours_after_end)


# --- webinar creation and promotion -----------------------------------------

def test_registration_must_be_on_brand_site(conn, cfg):
    with pytest.raises(ValueError, match="registration page"):
        create_webinar(conn, cfg, "clas_by_fred", "x", WEBINAR_START, "https://evil.example/x",
                       "https://calendly.com/fred/intro")


def test_calendly_must_be_calendly(conn, cfg):
    with pytest.raises(ValueError, match="calendly"):
        create_webinar(conn, cfg, "clas_by_fred", "x", WEBINAR_START, "https://classbyfred.com/w",
                       "https://example.com/book")


def test_promotion_fills_three_weeks_of_webinar_slots(conn, cfg):
    wid = new_webinar(conn, cfg)
    result = attach_promotions(conn, cfg, wid)
    assert result["attached"] == 6  # Facebook + Instagram, Sunday slots on Nov 15, 22, 29
    promos = conn.execute("SELECT * FROM posts WHERE webinar_id=?", (wid,)).fetchall()
    assert len(promos) == 6
    assert {p["platform"] for p in promos} == {"facebook", "instagram"}
    assert all(p["cta_url"] == "https://classbyfred.com/webinar" for p in promos)
    assert all(p["subject"].startswith("Webinar: HIPAA risk basics") for p in promos)
    assert all(p["status"] == "draft" for p in promos)
    assert conn.execute("SELECT status FROM webinars WHERE id=?", (wid,)).fetchone()[0] == "promoting"


def test_promotion_is_not_repeated(conn, cfg):
    wid = new_webinar(conn, cfg)
    attach_promotions(conn, cfg, wid)
    assert attach_promotions(conn, cfg, wid)["attached"] == 0


def test_promo_posts_do_not_touch_other_slots(conn, cfg):
    wid = new_webinar(conn, cfg)
    before = len(list_posts(conn, brand_key="clas_by_fred", status="draft"))
    attach_promotions(conn, cfg, wid)
    assert len(list_posts(conn, brand_key="clas_by_fred", status="draft")) == before


# --- chapters and description -----------------------------------------------

def test_chapter_rules():
    assert validate_chapters(CHAPTERS) == []
    assert validate_chapters([["00:05", "x"], ["01:00", "y"], ["02:00", "z"]])  # must start at 00:00
    assert validate_chapters([["00:00", "x"], ["01:00", "y"]])  # need 3
    assert validate_chapters([["00:00", "x"], ["05:00", "y"], ["03:00", "z"]])  # must increase
    assert validate_chapters([["00:00", "x"], ["1:xx", "y"], ["03:00", "z"]])  # bad stamp


def test_description_has_chapters_and_calendly():
    text = build_description("Replay: HIPAA basics.", CHAPTERS, "https://calendly.com/fred/intro")
    assert "05:12 Risk assessment" in text
    assert text.endswith("Book a call: https://calendly.com/fred/intro")


# --- upload -----------------------------------------------------------------

def test_dry_run_upload_spends_nothing(conn, cfg, root):
    wid = new_webinar(conn, cfg)
    r = upload_replay(conn, cfg, wid, FakeUploader(), None, Guard(dry_run=True), now_after(conn, wid, 0), root=root)
    assert r.status == "would_upload"
    assert used_today(conn, "2026-12-02") == 0 and conn.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0] == 0


def test_live_upload_is_private_and_recorded(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    up = FakeUploader()
    r = upload_replay(conn, cfg, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    assert r.status == "uploaded" and r.detail == "YT_VIDEO_1"
    meta = up.started[0][0]
    assert meta["status"]["privacyStatus"] == "private"
    assert "Chapters:" in meta["snippet"]["description"] and "calendly.com" in meta["snippet"]["description"]
    assert up.started[0][2] == "YT-TOKEN"
    w = conn.execute("SELECT status, replay_youtube_id FROM webinars WHERE id=?", (wid,)).fetchone()
    assert tuple(w) == ("replay_uploaded", "YT_VIDEO_1")
    assert conn.execute("SELECT SUM(units) FROM quota_ledger").fetchone()[0] == 1600


def test_second_upload_is_skipped(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    upload_replay(conn, cfg, wid, FakeUploader(), store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    r = upload_replay(conn, cfg, wid, FakeUploader(), store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    assert r.status == "skipped"


def test_failed_upload_counts_quota_and_is_not_retried(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    r = upload_replay(conn, cfg, wid, BrokenUploader(), store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    assert r.status == "failed" and "not retried" in r.detail
    assert conn.execute("SELECT status FROM webinars WHERE id=?", (wid,)).fetchone()[0] == "planned"  # unchanged on failure
    assert conn.execute("SELECT SUM(units) FROM quota_ledger").fetchone()[0] == 1600


def test_overdue_upload_is_blocked(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    late = now_after(conn, wid, 49)  # more than 48h after the webinar ends
    assert overdue(conn, wid, late)
    r = upload_replay(conn, cfg, wid, FakeUploader(), store, Guard(dry_run=False), late, root=root)
    assert r.status == "blocked" and "OVERDUE" in r.detail


def test_quota_reserve_blocks_upload(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    conn.execute("INSERT INTO quota_ledger (api, operation, units, day) VALUES ('youtube','videos.insert',7500,'2026-12-02')")
    conn.commit()
    now = now_after(conn, wid, 0)
    # 7,500 used + 1,600 would pass 9,000; the check uses today's Pacific date, so set the ledger for today
    from socialops.youtube import quota_day
    conn.execute("UPDATE quota_ledger SET day=?", (quota_day(now),))
    conn.commit()
    assert not can_spend(conn, "videos.insert", now)
    r = upload_replay(conn, cfg, wid, FakeUploader(), store, Guard(dry_run=False), now, root=root)
    assert r.status == "blocked" and "quota" in r.detail


def test_replay_path_must_be_safe(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    set_replay_file(conn, wid, "../../etc/replay.mp4", CHAPTERS)
    r = upload_replay(conn, cfg, wid, FakeUploader(), store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    assert r.status == "blocked" and "relative" in r.detail
    set_replay_file(conn, wid, "assets/clas_by_fred/replay.avi", CHAPTERS)
    r = upload_replay(conn, cfg, wid, FakeUploader(), store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    assert r.status == "blocked" and ".mp4" in r.detail


# --- visibility gate -----------------------------------------------------------

def test_public_needs_approval(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    up = FakeUploader()
    upload_replay(conn, cfg, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    r = publish_public(conn, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 2))
    assert r.status == "blocked" and "visibility approval" in r.detail
    assert up.privacy_calls == []


def test_approved_public_flip_happens_once(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    up = FakeUploader()
    upload_replay(conn, cfg, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    assert approve_public(conn, wid, "pamela").startswith("approved")
    r = publish_public(conn, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 2))
    assert r.status == "uploaded"
    assert up.privacy_calls == [("YT_VIDEO_1", "public")]
    assert conn.execute("SELECT status FROM webinars WHERE id=?", (wid,)).fetchone()[0] == "replay_public"
    again = publish_public(conn, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 2))
    assert again.status == "blocked"


def test_approval_refused_before_upload(conn, cfg):
    wid = new_webinar(conn, cfg)
    assert approve_public(conn, wid, "pamela").startswith("not approved")


def test_dry_run_public_flip_makes_no_call(conn, cfg, root, store):
    wid = new_webinar(conn, cfg)
    up = FakeUploader()
    upload_replay(conn, cfg, wid, up, store, Guard(dry_run=False), now_after(conn, wid, 0), root=root)
    approve_public(conn, wid, "pamela")
    r = publish_public(conn, wid, up, store, Guard(dry_run=True), now_after(conn, wid, 2))
    assert r.status == "would_upload" and up.privacy_calls == []


def test_status_setter_rejects_unknown(conn, cfg):
    wid = new_webinar(conn, cfg)
    with pytest.raises(ValueError):
        set_status(conn, wid, "replay_public")
    set_status(conn, wid, "live")
    assert conn.execute("SELECT status FROM webinars WHERE id=?", (wid,)).fetchone()[0] == "live"

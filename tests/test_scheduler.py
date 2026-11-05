import fcntl
import plistlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.post_queue import get_post, list_posts, set_post_fields, transition
from socialops.qa import check_post
from socialops.scheduler import LABEL, ScheduleAgent, build_plist, install_schedule, run_due
from socialops.secrets import MemorySecretStore, service_name
from socialops.agents import Context

MONDAY = date(2026, 11, 2)
SLOT_TIME = datetime(2026, 11, 2, 13, 45, tzinfo=timezone.utc)   # 15 min after Mon 08:30 EST slots
EARLY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
LATE = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)         # 9.5 h after the slots
GOOD = "HIPAA is more than paperwork. A policy alone does not protect a practice. See https://classbyfred.com"


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, fields, headers):
        self.calls.append({"url": url, "fields": dict(fields), "headers": dict(headers)})
        if not self.responses:
            return 200, {"id": "X%d" % len(self.calls)}
        return self.responses.pop(0)


@pytest.fixture
def cfg(tmp_path):
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_").replace(
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
    for brand, platform in [("clas_by_fred", "facebook"), ("clas_by_fred", "instagram")]:
        svc = service_name(brand, platform)
        s.set(svc, "access_token", f"TOK-{platform}")
        s.set(svc, "expires_at", "9999999999")
    return s


def approve_one(conn, cfg, platform, fmt, visual):
    post = next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft") if p.platform == platform)
    set_post_fields(conn, post.id, subject="HIPAA basics", visual_path=visual, visual_source="canva",
                    caption=GOOD, hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"],
                    cta_url="https://classbyfred.com")
    conn.execute("UPDATE posts SET format=? WHERE id=?", (fmt, post.id))
    conn.commit()
    assert check_post(conn, cfg, post.id).passed
    transition(conn, post.id, "approved", actor="pamela")
    return post.id


def ctx(conn, cfg, now, dry_run=True, store=None, transport=None):
    return Context(conn=conn, cfg=cfg, guard=Guard(dry_run=dry_run), now=now, store=store,
                   transport=transport, sleep=lambda s: None)


def test_schedule_agent_moves_near_slots_and_flags_late(conn, cfg):
    near = approve_one(conn, cfg, "facebook", "image", "assets/clas_by_fred/a.png")
    res = ScheduleAgent().run(ctx(conn, cfg, SLOT_TIME))
    assert near in res.refs and get_post(conn, near).status == "scheduled"
    assert "scheduled" in res.reason


def test_schedule_agent_leaves_very_late_approvals_alone(conn, cfg):
    late = approve_one(conn, cfg, "facebook", "image", "assets/clas_by_fred/a.png")
    res = ScheduleAgent().run(ctx(conn, cfg, LATE))
    assert get_post(conn, late).status == "approved"
    assert "missed slot 1" in res.reason


def test_run_due_dry_run_touches_no_network(conn, cfg, monkeypatch):
    import socialops.meta_graph as mg
    monkeypatch.setattr(mg, "default_transport", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    pid = approve_one(conn, cfg, "facebook", "image", "assets/clas_by_fred/a.png")
    results = run_due(conn, cfg, None, Guard(dry_run=True), SLOT_TIME, lock_path=conn_lock(conn))
    names = [n for n, _ in results]
    assert names == ["schedule", "publish"]
    assert get_post(conn, pid).status == "scheduled"  # scheduled, not published


def conn_lock(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2]).with_name("run.lock")


def test_run_due_live_publishes_facebook_photo(conn, cfg, store):
    pid = approve_one(conn, cfg, "facebook", "image", "assets/clas_by_fred/a.png")
    rec = Recorder((200, {"id": "p1", "post_id": "PAGE_1"}))
    results = dict(run_due(conn, cfg, store, Guard(dry_run=False), SLOT_TIME,
                           transport=rec, lock_path=conn_lock(conn)))
    assert results["publish"].status == "ok" and "published 1" in results["publish"].reason
    assert get_post(conn, pid).status == "published"


def test_run_due_live_publishes_instagram_reel(conn, cfg, store):
    pid = approve_one(conn, cfg, "instagram", "video", "assets/clas_by_fred/v.mp4")
    rec = Recorder((200, {"id": "C9"}), (200, {"status_code": "FINISHED"}), (200, {"id": "R9"}))
    run_due(conn, cfg, store, Guard(dry_run=False), SLOT_TIME, transport=rec, lock_path=conn_lock(conn))
    assert rec.calls[0]["fields"]["media_type"] == "REELS"
    assert rec.calls[0]["fields"]["video_url"].endswith("/assets/clas_by_fred/v.mp4")
    assert get_post(conn, pid).status == "published"


def test_video_post_needs_mp4_or_mov(conn, cfg):
    pid = approve_one(conn, cfg, "instagram", "video", "assets/clas_by_fred/v.mp4")
    conn.execute("UPDATE posts SET visual_path='assets/clas_by_fred/v.png' WHERE id=?", (pid,))
    conn.commit()
    assert not check_post(conn, cfg, pid).passed


def test_second_tick_does_not_repost(conn, cfg, store):
    pid = approve_one(conn, cfg, "facebook", "image", "assets/clas_by_fred/a.png")
    rec = Recorder((200, {"post_id": "P1"}))
    run_due(conn, cfg, store, Guard(dry_run=False), SLOT_TIME, transport=rec, lock_path=conn_lock(conn))
    run_due(conn, cfg, store, Guard(dry_run=False), SLOT_TIME, transport=rec, lock_path=conn_lock(conn))
    assert len(rec.calls) == 1 and get_post(conn, pid).status == "published"


def test_overlapping_run_is_skipped(conn, cfg, tmp_path):
    lock = tmp_path / "run.lock"
    with open(lock, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        results = run_due(conn, cfg, None, Guard(dry_run=True), SLOT_TIME, lock_path=lock)
    assert results[0][0] == "lock" and results[0][1].status == "skipped"


def test_plist_is_dry_run_unless_live(tmp_path):
    dry = plistlib.loads(build_plist("/py", "/repo", str(tmp_path), False, "/c.toml", "/d.db"))
    live = plistlib.loads(build_plist("/py", "/repo", str(tmp_path), True, "/c.toml", "/d.db"))
    assert "--dry-run" in dry["ProgramArguments"] and "--live" not in dry["ProgramArguments"]
    assert "--live" in live["ProgramArguments"]
    assert dry["Label"] == LABEL and dry["StartInterval"] == 300
    assert dry["ProgramArguments"][-1] == "run-due"


def test_plist_contains_no_secrets(tmp_path):
    raw = build_plist("/py", "/repo", str(tmp_path), True, "/c.toml", "/d.db")
    assert b"access_token" not in raw and b"TOK" not in raw


def test_install_writes_file_without_loading(tmp_path):
    target = install_schedule(live=False, config_path="config/brands.toml",
                              db_path=str(tmp_path / "d.db"), agents_dir=tmp_path / "agents")
    assert target.name == f"{LABEL}.plist" and target.exists()
    assert plistlib.loads(target.read_bytes())["Label"] == LABEL


def test_image_post_ignores_video_extension(conn, cfg):
    pid = approve_one(conn, cfg, "facebook", "image", "assets/clas_by_fred/a.png")
    conn.execute("UPDATE posts SET visual_path='assets/clas_by_fred/clip.mov' WHERE id=?", (pid,))
    conn.commit()
    # images are not checked for extension, only video posts are
    assert check_post(conn, cfg, pid).passed

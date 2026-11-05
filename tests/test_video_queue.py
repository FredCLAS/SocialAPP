from datetime import date
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.llm import Completion
from socialops.post_queue import get_post, list_posts, set_post_fields, transition
from socialops.qa import check_post
from socialops.video_queue import (
    QueueError, add_job, approve_script, draft_script, link_to_post, mark_produced,
    script_problems, set_manual_script, sync_statuses, write_brief,
)

MONDAY = date(2026, 11, 2)
CAPTION_LINK = "Want the checklist? Visit https://classbyfred.com and book a short call."
SCRIPT = ("Most practices think a policy binder is enough. It is not. "
          "Start with a risk assessment that shows where patient data really lives. "
          "Then train staff on the few procedures they will use every day. "
          "Write them down, test them, and review them each quarter. "
          "Want the checklist? Visit classbyfred.com and book a short call.")


class FakeLlm:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def complete(self, system, user, model, max_tokens):
        self.calls += 1
        return Completion(self.text, 1, 1)


@pytest.fixture
def cfg(real_config_path):
    return load_config(real_config_path)


@pytest.fixture
def conn(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    generate_week(c, cfg, MONDAY)
    yield c
    c.close()


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "SocialOps"
    (base / "assets" / "clas_by_fred").mkdir(parents=True)
    (base / "assets" / "clas_by_fred" / "video_1.mp4").write_bytes(b"0" * 5000)
    return base


def fb_video_post(conn):
    return next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft")
                if p.platform == "facebook" and p.format == "video")


def fb_image_post(conn):
    return next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft")
                if p.platform == "facebook" and p.format == "image")


def job_status(conn, jid):
    return conn.execute("SELECT status FROM video_jobs WHERE id=?", (jid,)).fetchone()[0]


def test_script_rules(cfg):
    website = cfg.brands["clas_by_fred"].website
    assert script_problems(SCRIPT, website) == []
    assert any("words" in p for p in script_problems("Too short. Visit classbyfred.com.", website))
    assert any("banned" in p for p in script_problems(SCRIPT + " Guaranteed results.", website))
    assert any("website" in p for p in script_problems(SCRIPT.replace("classbyfred.com", "elsewhere.com"), website))


def test_dry_run_script_calls_nothing(conn, cfg):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    llm = FakeLlm(SCRIPT)
    assert "dry-run" in draft_script(conn, cfg, jid, None, Guard(dry_run=True))
    assert llm.calls == 0


def test_claude_script_saved_when_valid(conn, cfg):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    assert draft_script(conn, cfg, jid, FakeLlm(SCRIPT), Guard(dry_run=False)) == "script drafted"
    row = conn.execute("SELECT script, script_source FROM video_jobs WHERE id=?", (jid,)).fetchone()
    assert row["script"] == SCRIPT and row["script_source"] == "claude"


def test_invalid_claude_script_not_saved(conn, cfg):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    msg = draft_script(conn, cfg, jid, FakeLlm("Short. classbyfred.com"), Guard(dry_run=False))
    assert msg.startswith("rejected")
    assert conn.execute("SELECT script FROM video_jobs WHERE id=?", (jid,)).fetchone()[0] == ""


def test_manual_script_saved(conn, cfg):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    assert set_manual_script(conn, cfg, jid, SCRIPT) == "script saved"


def test_approval_needs_script_and_approver(conn, cfg):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    assert approve_script(conn, jid, "pamela").startswith("not approved")
    set_manual_script(conn, cfg, jid, SCRIPT)
    with pytest.raises(QueueError):
        approve_script(conn, jid, "intern")
    assert approve_script(conn, jid, "pamela").startswith("script approved")
    assert job_status(conn, jid) == "queued"


def test_brief_written_when_queued(conn, cfg, root):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    with pytest.raises(QueueError):
        write_brief(conn, cfg, jid, root)
    set_manual_script(conn, cfg, jid, SCRIPT)
    approve_script(conn, jid, "pamela")
    path = write_brief(conn, cfg, jid, root)
    text = path.read_text()
    assert SCRIPT in text and "AI-generated" in text and "video-produced --id" in text


def queued_job(conn, cfg):
    jid = add_job(conn, "clas_by_fred", "HIPAA basics")
    set_manual_script(conn, cfg, jid, SCRIPT)
    approve_script(conn, jid, "pamela")
    return jid


def test_produced_requires_file_and_safe_path(conn, cfg, root):
    jid = queued_job(conn, cfg)
    assert mark_produced(conn, jid, "assets/clas_by_fred/missing.mp4", root).startswith("not saved")
    assert mark_produced(conn, jid, "../../etc/x.mp4", root).startswith("not saved")
    assert mark_produced(conn, jid, "assets/clas_by_fred/video_1.avi", root).startswith("not saved")
    assert mark_produced(conn, jid, "assets/clas_by_fred/video_1.mp4", root) == "video recorded"
    assert job_status(conn, jid) == "produced"


def test_link_sets_video_post_with_ai_label(conn, cfg, root):
    jid = queued_job(conn, cfg)
    mark_produced(conn, jid, "assets/clas_by_fred/video_1.mp4", root)
    pid = fb_video_post(conn).id
    assert link_to_post(conn, jid, pid).startswith("linked")
    post = get_post(conn, pid)
    assert post.visual_path == "assets/clas_by_fred/video_1.mp4" and post.visual_source == "ai"
    assert job_status(conn, jid) == "optimized"
    set_post_fields(conn, pid, subject="HIPAA basics", caption=SCRIPT.replace("classbyfred.com", "https://classbyfred.com") + " AI-generated video.",
                    hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"], cta_url="https://classbyfred.com")
    assert check_post(conn, cfg, pid).passed


def test_video_without_label_fails_qa(conn, cfg, root):
    jid = queued_job(conn, cfg)
    mark_produced(conn, jid, "assets/clas_by_fred/video_1.mp4", root)
    pid = fb_video_post(conn).id
    link_to_post(conn, jid, pid)
    set_post_fields(conn, pid, subject="HIPAA basics", caption=SCRIPT.replace("classbyfred.com", "https://classbyfred.com"),
                    hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"], cta_url="https://classbyfred.com")
    result = check_post(conn, cfg, pid)
    assert not result.passed and any(c.name == "ai_label" for c in result.failures)


def test_link_refuses_linkedin(conn, cfg, root):
    jid = queued_job(conn, cfg)
    mark_produced(conn, jid, "assets/clas_by_fred/video_1.mp4", root)
    li = next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft") if p.platform == "linkedin")
    assert "videos go to Facebook or Instagram" in link_to_post(conn, jid, li.id)


def test_link_refuses_when_not_produced(conn, cfg):
    jid = queued_job(conn, cfg)
    assert link_to_post(conn, jid, fb_video_post(conn).id).startswith("not linked")


def test_sync_follows_post_status(conn, cfg, root):
    jid = queued_job(conn, cfg)
    mark_produced(conn, jid, "assets/clas_by_fred/video_1.mp4", root)
    pid = fb_video_post(conn).id
    link_to_post(conn, jid, pid)
    set_post_fields(conn, pid, subject="HIPAA basics", caption=SCRIPT.replace("classbyfred.com", "https://classbyfred.com") + " AI-generated video.",
                    hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"], cta_url="https://classbyfred.com")
    check_post(conn, cfg, pid)
    transition(conn, pid, "approved", actor="pamela")
    assert sync_statuses(conn) == 1 and job_status(conn, jid) == "approved"
    transition(conn, pid, "scheduled")
    transition(conn, pid, "published")
    sync_statuses(conn)
    assert job_status(conn, jid) == "published"

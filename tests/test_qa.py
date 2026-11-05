from datetime import date

import pytest

from socialops.builtin_agents import QaAgent
from socialops.agents import Context, run_logged
from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.post_queue import InvalidTransition, NotApprovable, get_post, list_posts, set_post_fields, transition
from socialops.qa import check_post, run_checks, latest_result

MONDAY = date(2026, 11, 2)
GOOD_CAPTION = "HIPAA is more than paperwork. A policy alone does not protect a practice. See https://classbyfred.com"
GOOD_TAGS = ["#HIPAA", "#healthcarecompliance", "#privacy"]


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


def fb_post(conn):
    return next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft") if p.platform == "facebook")


def make_good(conn, cfg, post_id=None):
    pid = post_id or fb_post(conn).id
    set_post_fields(conn, pid, subject="HIPAA basics", visual_path="assets/clas_by_fred/a.png",
                    visual_source="canva", caption=GOOD_CAPTION, hashtags=GOOD_TAGS,
                    cta_url="https://classbyfred.com")
    return pid


def failed_names(conn, cfg, pid, fetch=None):
    return {c.name for c in run_checks(get_post(conn, pid), cfg, fetch_status=fetch) if c.ok is False}


def test_good_post_passes_all_rule_checks(conn, cfg):
    pid = make_good(conn, cfg)
    assert failed_names(conn, cfg, pid) == set()


@pytest.mark.parametrize("field,value,expected", [
    ("visual_path", None, "visual"),
    ("visual_source", "unknown", "visual"),
    ("subject", "Clas by Fred image_checklist 2026-11-02", "topic"),
    ("caption", "Only one sentence here https://classbyfred.com", "caption_structure"),
    ("caption", "A. B. Nothing links here.", "cta_in_caption"),
    ("cta_url", "https://evil.example", "cta_host"),
    ("cta_url", "http://classbyfred.com", "cta_host"),
    ("hashtags", ["#one"], "hashtags"),
    ("hashtags", ["#HIPAA", "#two words", "#ok"], "hashtags"),
    ("caption", "Sentence. Sentence. See https://classbyfred.com " + "x" * 2100, "caption_length"),
    ("caption", "Guaranteed results. Sure. See https://classbyfred.com", "claims"),
])
def test_each_rule_fails_when_broken(conn, cfg, field, value, expected):
    pid = make_good(conn, cfg)
    if field == "visual_source":  # set_post_fields rejects 'unknown'; a legacy row can still hold it
        conn.execute("UPDATE posts SET visual_source='unknown' WHERE id=?", (pid,))
        conn.commit()
    elif field == "cta_url" and value is not None:
        set_post_fields(conn, pid, caption=GOOD_CAPTION.replace("https://classbyfred.com", value), cta_url=value)
    elif field == "visual_path" and value is None:
        conn.execute("UPDATE posts SET visual_path=NULL WHERE id=?", (pid,))
        conn.commit()
    else:
        set_post_fields(conn, pid, **{field: value})
    assert expected in failed_names(conn, cfg, pid)


def test_ai_visual_must_be_labeled(conn, cfg):
    pid = make_good(conn, cfg)
    set_post_fields(conn, pid, visual_source="ai")
    assert "ai_label" in failed_names(conn, cfg, pid)
    set_post_fields(conn, pid, caption="AI-generated image. Soft light. See https://classbyfred.com")
    assert "ai_label" not in failed_names(conn, cfg, pid)


def test_slot_must_match_configured_time(conn, cfg):
    pid = make_good(conn, cfg)
    conn.execute("UPDATE posts SET scheduled_at='2026-11-02T14:00:00Z' WHERE id=?", (pid,))
    conn.commit()
    assert "slot" in failed_names(conn, cfg, pid)


def test_link_check_skipped_without_fetcher(conn, cfg):
    pid = make_good(conn, cfg)
    names = {c.name: c.ok for c in run_checks(get_post(conn, pid), cfg, fetch_status=None)}
    assert names["link_reachable"] is None


def test_link_check_uses_fetcher(conn, cfg):
    pid = make_good(conn, cfg)
    assert "link_reachable" not in failed_names(conn, cfg, pid, fetch=lambda u: 200)
    assert "link_reachable" in failed_names(conn, cfg, pid, fetch=lambda u: 404)
    assert "link_reachable" in failed_names(conn, cfg, pid, fetch=lambda u: None)


def test_check_post_stores_result(conn, cfg):
    pid = make_good(conn, cfg)
    result = check_post(conn, cfg, pid)
    row = latest_result(conn, pid)
    assert result.passed and row["passed"] == 1


def test_approval_needs_qa_run(conn, cfg):
    pid = make_good(conn, cfg)
    with pytest.raises(NotApprovable, match="QA has not run"):
        transition(conn, pid, "approved", actor="pamela")


def test_editing_after_qa_invalidates_it(conn, cfg):
    pid = make_good(conn, cfg)
    check_post(conn, cfg, pid)
    set_post_fields(conn, pid, caption=GOOD_CAPTION + " Changed.")
    with pytest.raises(NotApprovable, match="content changed"):
        transition(conn, pid, "approved", actor="pamela")


def test_failed_qa_blocks_approval(conn, cfg):
    pid = make_good(conn, cfg)
    set_post_fields(conn, pid, caption="No link at all. Really. Nothing here.")
    check_post(conn, cfg, pid)
    with pytest.raises(NotApprovable, match="QA failed"):
        transition(conn, pid, "approved", actor="pamela")


def test_passing_qa_allows_approval(conn, cfg):
    pid = make_good(conn, cfg)
    check_post(conn, cfg, pid)
    assert transition(conn, pid, "approved", actor="pamela").status == "approved"


def test_qa_never_changes_status(conn, cfg):
    pid = make_good(conn, cfg)
    check_post(conn, cfg, pid)
    assert get_post(conn, pid).status == "draft"


def test_qa_agent_skips_only_passing_current_content(conn, cfg):
    ctx = Context(conn=conn, cfg=cfg, guard=Guard(dry_run=True), now=None)
    first = run_logged(QaAgent("clas_by_fred", MONDAY), ctx)
    assert first.status == "ok" and len(first.refs) == 17  # empty drafts all fail, all get checked
    make_good(conn, cfg)
    check_post(conn, cfg, fb_post(conn).id)
    second = run_logged(QaAgent("clas_by_fred", MONDAY), ctx)
    assert len(second.refs) == 16  # the passing one is skipped
    assert "already passing 1" in second.reason
    runs = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE agent='qa'").fetchone()[0]
    assert runs == 2

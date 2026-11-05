from datetime import date

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week, week_bounds_utc
from socialops.db import connect, migrate_up
from socialops.post_queue import (
    InvalidTransition,
    NotApprovable,
    UnauthorizedApprover,
    approve_week,
    get_post,
    list_posts,
    set_post_fields,
    transition,
)

MONDAY = date(2026, 11, 2)

from pathlib import Path as _P
from socialops.config import load_config as _load
from socialops.qa import check_post as _check
_CFG = _load(_P(__file__).resolve().parents[1] / "config" / "brands.toml")


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


def complete(conn, post_id):
    result = _complete(conn, post_id)
    _check(conn, _CFG, post_id)
    return result


def _complete(conn, post_id):
    conn.execute("UPDATE posts SET format='image' WHERE id=?", (post_id,))
    conn.commit()
    fields = dict(
        subject="HIPAA risk assessment basics",
        visual_path=f"assets/clas_by_fred/{post_id}.png",
        visual_source="canva",
        caption="Hook sentence. Value follows. Call to action: https://classbyfred.com",
        hashtags=["#HIPAA", "#healthcarecompliance", "#privacy"],
        cta_url="https://classbyfred.com",
    )
    return set_post_fields(conn, post_id, **fields)


def approve_count(conn):
    return conn.execute("SELECT COUNT(*) FROM approvals WHERE decision='approved'").fetchone()[0]


def first_post(conn, brand="clas_by_fred"):
    return list_posts(conn, brand_key=brand, status="draft")[0]


def test_missing_visual_source_blocks_approval(conn):
    post = first_post(conn)
    set_post_fields(conn, post.id, visual_path="a.png", caption="c", hashtags=["#xxx","#yyy","#zzz"], cta_url="https://classbyfred.com")
    with pytest.raises(NotApprovable, match="visual source"):
        transition(conn, post.id, "approved", actor="pamela")


def test_ai_visual_needs_label(conn):
    post = first_post(conn)
    conn.execute("UPDATE posts SET format='image' WHERE id=?", (post.id,))
    conn.commit()
    base = dict(subject="Harbor at dusk", visual_path="a.png", visual_source="ai",
                hashtags=["#art", "#harbor", "#oils"], cta_url="https://classbyfred.com")
    set_post_fields(conn, post.id, caption="A lovely painting. Look closer. See https://classbyfred.com", **base)
    result = _check(conn, _CFG, post.id)
    assert not result.passed and any(c.name == "ai_label" for c in result.failures)
    with pytest.raises(NotApprovable, match="QA failed"):
        transition(conn, post.id, "approved", actor="pamela")
    set_post_fields(conn, post.id, caption="AI-generated illustration of a harbor. Soft light on the water. See https://classbyfred.com")
    assert _check(conn, _CFG, post.id).passed
    assert transition(conn, post.id, "approved", actor="pamela").status == "approved"


def test_visual_source_value_checked(conn):
    with pytest.raises(ValueError, match="visual_source"):
        set_post_fields(conn, first_post(conn).id, visual_source="stock")


def test_incomplete_post_cannot_be_approved(conn):
    post = first_post(conn)
    with pytest.raises(NotApprovable, match="missing visual"):
        transition(conn, post.id, "approved", actor="pamela")
    assert get_post(conn, post.id).status == "draft"
    assert approve_count(conn) == 0


def test_unknown_actor_cannot_approve(conn):
    post = complete(conn, first_post(conn).id)
    with pytest.raises(UnauthorizedApprover):
        transition(conn, post.id, "approved", actor="someone_else")


def test_approval_needs_actor(conn):
    post = complete(conn, first_post(conn).id)
    with pytest.raises(UnauthorizedApprover):
        transition(conn, post.id, "approved")


def test_approve_writes_approval_row(conn):
    post = complete(conn, first_post(conn).id)
    result = transition(conn, post.id, "approved", actor="pamela")
    assert result.status == "approved"
    row = conn.execute("SELECT kind, ref_id, decided_by FROM approvals").fetchone()
    assert (row["kind"], row["ref_id"], row["decided_by"]) == ("post", post.id, "pamela")


@pytest.mark.parametrize(
    "frm,to",
    [
        ("draft", "published"),
        ("draft", "scheduled"),
        ("draft", "failed"),
        ("published", "draft"),
        ("published", "approved"),
        ("scheduled", "draft"),
    ],
)
def test_invalid_transitions_raise(conn, frm, to):
    post = first_post(conn)
    conn.execute("UPDATE posts SET status=? WHERE id=?", (frm, post.id))
    with pytest.raises(InvalidTransition):
        transition(conn, post.id, to, actor="pamela")


def test_unknown_status_raises(conn):
    post = complete(conn, first_post(conn).id)
    with pytest.raises(InvalidTransition, match="unknown status"):
        transition(conn, post.id, "posted", actor="pamela")


def test_full_happy_path(conn):
    post = complete(conn, first_post(conn).id)
    transition(conn, post.id, "approved", actor="pamela")
    transition(conn, post.id, "scheduled")
    transition(conn, post.id, "published")
    assert get_post(conn, post.id).status == "published"


def test_failed_can_retry(conn):
    post = complete(conn, first_post(conn).id)
    transition(conn, post.id, "approved", actor="pamela")
    transition(conn, post.id, "scheduled")
    transition(conn, post.id, "failed")
    transition(conn, post.id, "scheduled")
    assert get_post(conn, post.id).status == "scheduled"


def test_unapprove_then_edit(conn):
    post = complete(conn, first_post(conn).id)
    transition(conn, post.id, "approved", actor="pamela")
    with pytest.raises(InvalidTransition, match="locked"):
        set_post_fields(conn, post.id, caption="changed")
    transition(conn, post.id, "draft")
    assert set_post_fields(conn, post.id, caption="changed").caption == "changed"


def test_webinar_cta_needs_url(conn):
    post = list_posts(conn, brand_key="clas_by_fred", status="draft")
    target = next(p for p in post if p.content_type == "webinar_cta")
    set_post_fields(conn, target.id, visual_path="a.png", caption="c", hashtags=["#xxx","#yyy","#zzz"])
    with pytest.raises(NotApprovable, match="cta_url"):
        transition(conn, target.id, "approved", actor="pamela")


def test_edit_rejects_unknown_field(conn):
    with pytest.raises(ValueError, match="not editable"):
        set_post_fields(conn, first_post(conn).id, status="published")


def test_batch_approve_is_all_or_nothing(cfg, conn):
    posts = list_posts(conn, brand_key="clas_by_fred", status="draft")
    for p in posts[:-1]:
        complete(conn, p.id)
    with pytest.raises(NotApprovable):
        approve_week(conn, cfg, "clas_by_fred", MONDAY, actor="pamela")
    assert approve_count(conn) == 0
    assert all(p.status == "draft" for p in list_posts(conn, brand_key="clas_by_fred"))


def test_batch_approve_whole_week_for_one_brand(cfg, conn):
    for p in list_posts(conn, brand_key="clas_by_fred", status="draft"):
        complete(conn, p.id)
    ids = approve_week(conn, cfg, "clas_by_fred", MONDAY, actor="pamela")
    clas_planned = sum(pc.per_week for pc in cfg.brands["clas_by_fred"].platforms.values())
    assert len(ids) == clas_planned
    assert approve_count(conn) == clas_planned
    pamela = list_posts(conn, brand_key="pamela_art", status="draft")
    assert len(pamela) == 8  # untouched


def test_batch_approve_rejects_unknown_actor(cfg, conn):
    with pytest.raises(UnauthorizedApprover):
        approve_week(conn, cfg, "clas_by_fred", MONDAY, actor="intern")


def test_week_filter_excludes_other_weeks(cfg, conn):
    next_week = date(2026, 11, 9)
    generate_week(conn, cfg, next_week)
    start, end = week_bounds_utc(MONDAY, cfg.timezone)
    this_week = list_posts(conn, brand_key="clas_by_fred", scheduled_from=start, scheduled_to=end)
    next_start, next_end = week_bounds_utc(next_week, cfg.timezone)
    other = list_posts(conn, brand_key="clas_by_fred", scheduled_from=next_start, scheduled_to=next_end)
    assert this_week and other
    assert set(p.id for p in this_week).isdisjoint(p.id for p in other)
    assert all(start <= p.scheduled_at < end for p in this_week)

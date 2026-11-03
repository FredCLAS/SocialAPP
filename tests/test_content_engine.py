from datetime import date

import pytest

from socialops.config import load_config
from socialops.content_engine import (
    generate_week,
    plan_week,
    video_indices,
    week_bounds_utc,
)
from socialops.db import connect, migrate_up

MONDAY = date(2026, 11, 2)  # EST (after DST ends Nov 1, 2026)


@pytest.fixture
def cfg(real_config_path):
    return load_config(real_config_path)


@pytest.fixture
def conn(tmp_db):
    c = connect(tmp_db)
    migrate_up(c)
    yield c
    c.close()


def _slots_for(cfg, brand, platform, week=MONDAY):
    return [s for s in plan_week(cfg, week) if s.brand_key == brand and s.platform == platform]


@pytest.mark.parametrize(
    "brand,platform",
    [
        ("clas_by_fred", "facebook"),
        ("clas_by_fred", "instagram"),
        ("clas_by_fred", "linkedin"),
        ("pamela_art", "facebook"),
        ("pamela_art", "instagram"),
    ],
)
def test_weekly_counts_match_cadence(cfg, brand, platform):
    pc = cfg.brands[brand].platforms[platform]
    slots = _slots_for(cfg, brand, platform)
    assert len(slots) == pc.per_week
    assert sum(1 for s in slots if s.format == "video") == pc.video_per_week
    counts = {}
    for s in slots:
        counts[s.content_type] = counts.get(s.content_type, 0) + 1
    assert counts == {name: n for name, n in pc.mix if n}


def test_clas_facebook_mix_is_spec(cfg):
    slots = _slots_for(cfg, "clas_by_fred", "facebook")
    types = [s.content_type for s in slots]
    assert types.count("image_checklist") == 4
    assert types.count("question") == 2
    assert types.count("webinar_cta") == 1
    assert sum(1 for s in slots if s.format == "video") == 3


def test_youtube_not_in_weekly_plan(cfg):
    assert _slots_for(cfg, "clas_by_fred", "youtube") == []


def test_linkedin_only_tue_wed_thu(cfg):
    from datetime import datetime
    days = {datetime.fromisoformat(s.scheduled_at.replace("Z", "+00:00")).weekday()
            for s in _slots_for(cfg, "clas_by_fred", "linkedin")}
    assert days == {1, 2, 3}


def test_video_indices_spread():
    assert video_indices(7, 3) == {0, 2, 4}
    assert video_indices(4, 0) == set()
    assert video_indices(0, 0) == set()


def test_post_time_is_local_eastern_est(cfg):
    # 08:30 EST on Mon Nov 2, 2026 = 13:30 UTC
    fb = _slots_for(cfg, "clas_by_fred", "facebook")
    assert fb[0].scheduled_at == "2026-11-02T13:30:00Z"


def test_post_time_follows_dst(cfg):
    # 08:30 EDT on Mon Oct 26, 2026 = 12:30 UTC
    fb = _slots_for(cfg, "clas_by_fred", "facebook", week=date(2026, 10, 26))
    assert fb[0].scheduled_at == "2026-10-26T12:30:00Z"


def test_linkedin_time_is_10am_eastern(cfg):
    li = _slots_for(cfg, "clas_by_fred", "linkedin")
    assert all(s.scheduled_at.endswith("15:00:00Z") for s in li)  # 10:00 EST = 15:00 UTC


def test_week_must_be_monday(cfg):
    with pytest.raises(ValueError, match="Monday"):
        plan_week(cfg, date(2026, 11, 3))


def test_generate_inserts_drafts(cfg, conn):
    report = generate_week(conn, cfg, MONDAY)
    assert report == {"planned": report["planned"], "inserted": report["planned"], "existing": 0}
    rows = conn.execute("SELECT DISTINCT status FROM posts").fetchall()
    assert [r["status"] for r in rows] == ["draft"]


def test_generate_is_idempotent(cfg, conn):
    first = generate_week(conn, cfg, MONDAY)
    second = generate_week(conn, cfg, MONDAY)
    assert second["inserted"] == 0
    assert second["existing"] == first["planned"]
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert total == first["planned"]


def test_generate_stores_content_type_and_format(cfg, conn):
    generate_week(conn, cfg, MONDAY)
    row = conn.execute(
        "SELECT content_type, format FROM posts WHERE brand_key='clas_by_fred' AND platform='facebook' "
        "ORDER BY scheduled_at LIMIT 1"
    ).fetchone()
    assert row["content_type"] == "image_checklist"
    assert row["format"] == "video"


def test_week_bounds_cover_seven_local_days(cfg):
    start, end = week_bounds_utc(MONDAY, cfg.timezone)
    assert start == "2026-11-02T05:00:00Z"  # local midnight EST
    assert end == "2026-11-09T05:00:00Z"

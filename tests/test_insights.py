from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from socialops.agents import Context
from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.insights import boost_candidates, build_report, pull_insights, save_report
from socialops.post_queue import set_post_fields
from socialops.secrets import MemorySecretStore, service_name

MONDAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, fields, headers):
        self.calls.append({"method": method, "url": url, "fields": dict(fields), "headers": dict(headers)})
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
    fb = c.execute("SELECT id FROM posts WHERE brand_key='clas_by_fred' AND platform='facebook' LIMIT 1").fetchone()[0]
    c.execute("UPDATE posts SET status='published', subject='HIPAA basics' WHERE id=?", (fb,))
    c.execute("INSERT INTO publish_log (post_id, platform, outcome, remote_id) VALUES (?, 'facebook', 'ok', 'PAGE_1')", (fb,))
    c.commit()
    yield c
    c.close()


@pytest.fixture
def store():
    s = MemorySecretStore()
    s.set(service_name("clas_by_fred", "facebook"), "access_token", "TOK")
    s.set(service_name("clas_by_fred", "facebook"), "expires_at", "9999999999")
    return s


def test_dry_run_pull_reads_nothing(conn, cfg):
    rec = Recorder()
    res = pull_insights(conn, cfg, "clas_by_fred", None, Guard(dry_run=True), NOW, transport=rec)
    assert res.status == "skipped" and rec.calls == []


def test_pull_stores_metrics_read_only(conn, cfg, store):
    rec = Recorder((200, {"data": [
        {"name": "post_impressions", "values": [{"value": 412}]},
        {"name": "post_engaged_users", "values": [{"value": 30}]},
    ]}))
    res = pull_insights(conn, cfg, "clas_by_fred", store, Guard(dry_run=False), NOW, transport=rec)
    assert res.status == "ok" and "stored 2" in res.reason
    assert rec.calls[0]["method"] == "GET" and rec.calls[0]["url"].endswith("/PAGE_1/insights")
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 2


def test_boost_candidates_show_passed_milestones(conn, cfg):
    fb = conn.execute("SELECT id FROM posts WHERE status='published'").fetchone()[0]
    conn.execute("INSERT INTO insights (post_id, metric, value) VALUES (?, 'post_impressions', 1200)", (fb,))
    conn.commit()
    cands = boost_candidates(conn, "clas_by_fred")
    assert cands == [(fb, "HIPAA basics", 1200.0, 1000)]


def test_report_has_sections_and_no_boost_action(conn, cfg):
    fb = conn.execute("SELECT id FROM posts WHERE status='published'").fetchone()[0]
    conn.execute("INSERT INTO insights (post_id, metric, value) VALUES (?, 'post_impressions', 400)", (fb,))
    conn.commit()
    body = build_report(conn, cfg, "clas_by_fred", MONDAY, NOW, store=None)
    for heading in ["## Plan vs. published", "## Approvals and QA", "## Replies and comments",
                    "## Webinars and replays", "## Videos (Zoice queue)", "## Token status",
                    "## Top posts by views"]:
        assert heading in body
    assert "Nothing is boosted or spent" in body
    assert "passed 300" in body


def test_report_counts_plan_and_drafts(conn, cfg):
    body = build_report(conn, cfg, "clas_by_fred", MONDAY, NOW, store=None)
    row = next(l for l in body.splitlines() if l.startswith("| facebook |"))
    planned = int(row.split("|")[2])
    assert planned == 7
    assert "- Drafts waiting for review: " in body


def test_report_saved_to_file_and_table(conn, cfg, tmp_path):
    body = build_report(conn, cfg, "clas_by_fred", MONDAY, NOW, store=None)
    path = save_report(conn, "clas_by_fred", MONDAY, body, tmp_path / "SocialOps")
    assert path.read_text(encoding="utf-8") == body
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_token_section_with_store(conn, cfg, store):
    body = build_report(conn, cfg, "clas_by_fred", MONDAY, NOW, store=store)
    assert "- facebook: ok" in body
    assert "- instagram: missing" in body

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.engagement import (
    approve_reply, classify, draft_reply, edit_reply, pull_comments, reject_reply,
    send_approved, validate_reply,
)
from socialops.guard import Guard
from socialops.llm import Completion
from socialops.secrets import MemorySecretStore, service_name

MONDAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, fields, headers):
        self.calls.append({"method": method, "url": url, "fields": dict(fields)})
        r = self.responses.pop(0) if self.responses else (200, {"data": []})
        if isinstance(r, Exception):
            raise r
        return r


class FakeLlm:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def complete(self, system, user, model, max_tokens):
        self.calls += 1
        return Completion(self.text, 10, 10)


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
    # one published FB post with a live remote id
    pid = c.execute("SELECT id FROM posts WHERE brand_key='clas_by_fred' AND platform='facebook' LIMIT 1").fetchone()[0]
    c.execute("UPDATE posts SET status='published' WHERE id=?", (pid,))
    c.execute("INSERT INTO publish_log (post_id, platform, outcome, remote_id) VALUES (?, 'facebook', 'ok', 'PAGE_POST_1')", (pid,))
    c.commit()
    yield c
    c.close()


@pytest.fixture
def store():
    s = MemorySecretStore()
    s.set(service_name("clas_by_fred", "facebook"), "access_token", "TOK")
    return s


def comment_payload(*items):
    return (200, {"data": list(items)})


def live():
    return Guard(dry_run=False)


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("This is a lawsuit waiting to happen", "escalate"),
    ("I want a refund", "escalate"),
    ("Earn $500 a day, DM me", "spam"),
    ("Check https://sketchy.example", "spam"),
    ("How much does this cost?", "lead"),
    ("I'd like to book a consult", "lead"),
    ("What is a risk assessment?", "question"),
    ("Great post, thanks!", "other"),
])
def test_classification(text, expected):
    assert classify(text).category == expected


def test_escalation_flag_set_for_legal_topics():
    assert classify("My attorney says you violated the rules").escalate is True
    assert classify("Nice work").escalate is False


# --- pulling comments -------------------------------------------------------

def test_pull_is_skipped_in_dry_run(conn, cfg, store):
    res = pull_comments(conn, cfg, "clas_by_fred", store, Guard(dry_run=True), NOW, transport=Recorder())
    assert res.status == "skipped"


def test_pull_stores_new_comments_once(conn, cfg, store):
    rec = Recorder(comment_payload(
        {"id": "c1", "message": "How do I start?", "from": {"id": "u1"}},
        {"id": "c2", "message": "Call me, I'm interested in pricing", "from": {"id": "u2"}},
    ), comment_payload(
        {"id": "c1", "message": "How do I start?", "from": {"id": "u1"}},
    ))
    first = pull_comments(conn, cfg, "clas_by_fred", store, live(), NOW, transport=rec)
    assert len(first.refs) == 2 and first.status == "ok"
    second = pull_comments(conn, cfg, "clas_by_fred", store, live(), NOW, transport=rec)
    assert second.refs == []
    cats = dict(conn.execute("SELECT source_id, category FROM inbound_items").fetchall())
    assert cats == {"c1": "question", "c2": "lead"}


def test_own_page_comments_are_skipped(conn, cfg, store):
    rec = Recorder(comment_payload({"id": "c9", "message": "Thanks all!", "from": {"id": "TEST_CLAS_FB_PAGE_ID"}}))
    res = pull_comments(conn, cfg, "clas_by_fred", store, live(), NOW, transport=rec)
    assert res.refs == [] and "own comments skipped 1" in res.reason


def test_pull_uses_read_only_endpoint(conn, cfg, store):
    rec = Recorder(comment_payload())
    pull_comments(conn, cfg, "clas_by_fred", store, live(), NOW, transport=rec)
    assert rec.calls[0]["method"] == "GET" and rec.calls[0]["url"].endswith("/PAGE_POST_1/comments")


def test_escalations_get_no_draft(conn, cfg, store):
    rec = Recorder(comment_payload({"id": "c3", "message": "I will sue you", "from": {"id": "u3"}}))
    pull_comments(conn, cfg, "clas_by_fred", store, live(), NOW, transport=rec)
    iid = conn.execute("SELECT id FROM inbound_items WHERE source_id='c3'").fetchone()[0]
    assert "no draft" in draft_reply(conn, cfg, iid, FakeLlm("x"), live())
    assert conn.execute("SELECT reply_status FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] == "none"


# --- drafting -------------------------------------------------------------

def add_item(conn, source_id, body, category=None):
    from socialops.engagement import classify as cl
    c = cl(body)
    conn.execute("INSERT INTO inbound_items (brand_key, platform, post_id, source_id, body, category, escalate) "
                 "VALUES ('clas_by_fred','facebook', (SELECT id FROM posts WHERE status='published'), ?, ?, ?, ?)",
                 (source_id, body, category or c.category, int(c.escalate)))
    conn.commit()
    return conn.execute("SELECT id FROM inbound_items WHERE source_id=?", (source_id,)).fetchone()[0]


def test_answer_bank_used_first(conn, cfg):
    conn.execute("INSERT INTO approved_answers (topic, keywords, answer, approved_by) VALUES ('pricing', ?, ?, 'pamela')",
                 (json.dumps(["price", "pricing", "cost"]), "Thanks for asking. Pricing depends on scope; send us a note."))
    conn.commit()
    iid = add_item(conn, "b1", "What is the price and cost for a risk assessment?")
    llm = FakeLlm("should not be used")
    assert draft_reply(conn, cfg, iid, llm, live()) == "drafted (bank)"
    assert llm.calls == 0


def test_claude_draft_when_no_bank_match(conn, cfg):
    iid = add_item(conn, "b2", "Does this apply to dentists too?")
    llm = FakeLlm("Good question! A quick look at your setup will tell. Feel free to ask more here.")
    assert draft_reply(conn, cfg, iid, llm, live()) == "drafted (claude)"
    assert conn.execute("SELECT reply_status FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] == "drafted"


def test_bad_claude_output_is_rejected(conn, cfg):
    iid = add_item(conn, "b3", "Is this a guarantee of compliance?")
    msg = draft_reply(conn, cfg, iid, FakeLlm("Guaranteed compliant. Sign up now."), live())
    assert msg.startswith("rejected draft") and "banned phrase" in msg
    assert conn.execute("SELECT reply_text FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] is None


def test_links_only_for_lead_replies():
    assert validate_reply("See https://classbyfred.com", "https://classbyfred.com", allow_link=False)
    assert validate_reply("See https://classbyfred.com", "https://classbyfred.com", allow_link=True) == []
    assert validate_reply("See https://evil.example", "https://classbyfred.com", allow_link=True)


def test_dry_run_drafts_nothing_external(conn, cfg):
    iid = add_item(conn, "b4", "Any tips?")
    llm = FakeLlm("x")
    assert "dry-run" in draft_reply(conn, cfg, iid, llm, Guard(dry_run=True))
    assert llm.calls == 0


# --- approval and sending ---------------------------------------------------

def drafted(conn, cfg, source_id="s1"):
    iid = add_item(conn, source_id, "Any tips for starting?")
    draft_reply(conn, cfg, iid, FakeLlm("Start with a short checklist. Happy to share more."), live())
    return iid


def test_send_requires_approval(conn, cfg, store):
    iid = drafted(conn, cfg)
    rec = Recorder()
    assert send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec) == []
    assert rec.calls == []
    assert conn.execute("SELECT reply_status FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] == "drafted"


def test_approved_reply_sends_once(conn, cfg, store):
    iid = drafted(conn, cfg)
    assert approve_reply(conn, iid, "pamela") == "approved"
    rec = Recorder((200, {"id": "R-1"}))
    out = send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec)
    assert out == [f"{iid}: sent"]
    call = rec.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/s1/replies")
    assert call["fields"]["message"].startswith("Start with a short checklist")
    assert conn.execute("SELECT reply_status, reply_sent_id FROM inbound_items WHERE id=?", (iid,)).fetchone()[:] == ("sent", "R-1")
    assert send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec) == []  # not sent twice


def test_edit_after_approval_blocks_send(conn, cfg, store):
    iid = drafted(conn, cfg)
    approve_reply(conn, iid, "pamela")
    edit_reply(conn, cfg, iid, "A different and safe reply. Glad to help.")
    assert conn.execute("SELECT reply_status FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] == "drafted"
    assert send_approved(conn, cfg, "clas_by_fred", store, live(), transport=Recorder()) == []


def test_approve_again_after_edit_then_send(conn, cfg, store):
    iid = drafted(conn, cfg)
    approve_reply(conn, iid, "pamela")
    edit_reply(conn, cfg, iid, "A different and safe reply. Glad to help.")
    approve_reply(conn, iid, "pamela")
    rec = Recorder((200, {"id": "R-2"}))
    assert send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec) == [f"{iid}: sent"]


def test_reject_needs_reason_and_keeps_draft(conn, cfg):
    iid = drafted(conn, cfg)
    assert reject_reply(conn, iid, "pamela", " ") == "a reason is required"
    assert reject_reply(conn, iid, "pamela", "too informal") == "rejected"
    assert conn.execute("SELECT reply_status FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] == "rejected"


def test_dry_run_send_makes_no_call(conn, cfg, store):
    iid = drafted(conn, cfg)
    approve_reply(conn, iid, "pamela")
    rec = Recorder()
    out = send_approved(conn, cfg, "clas_by_fred", store, Guard(dry_run=True), transport=rec)
    assert out == [f"{iid}: dry-run (would send)"] and rec.calls == []


def test_daily_cap_holds_extra_sends(conn, cfg, store):
    ids = []
    for n in range(3):
        iid = drafted(conn, cfg, source_id=f"cap{n}")
        approve_reply(conn, iid, "pamela")
        ids.append(iid)
    rec = Recorder((200, {"id": "A"}))
    out = send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec, cap=1)
    assert out[0].endswith("sent") and "held (daily cap" in out[1] and "held (daily cap" in out[2]
    assert len(rec.calls) == 1


def test_failed_send_is_not_retried(conn, cfg, store):
    from socialops.meta_graph import NetworkFailure
    iid = drafted(conn, cfg)
    approve_reply(conn, iid, "pamela")
    rec = Recorder(NetworkFailure("timeout"))
    out = send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec)
    assert "failed" in out[0] and "not retried" in out[0]
    assert conn.execute("SELECT reply_status FROM inbound_items WHERE id=?", (iid,)).fetchone()[0] == "failed"
    assert send_approved(conn, cfg, "clas_by_fred", store, live(), transport=rec) == []
    assert len(rec.calls) == 1

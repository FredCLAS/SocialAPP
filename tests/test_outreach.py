from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.db import connect, migrate_up
from socialops.gates import GateError
from socialops.leads import qualify
from socialops.outreach import (
    OutreachError, add_opt_out, approve_sequence, draft_first_email, preview, scan_replies, send_due, status,
)
from socialops.guard import Guard
from socialops.secrets import MemorySecretStore
from socialops.microsoft import SERVICE as MS_SERVICE

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)  # 10:00 EST
APPROVED_AT = T0 - timedelta(hours=1)
CALENDLY = "https://calendly.com/fred/intro"


class Mail:
    """Fake Graph transport: records calls, returns scripted responses."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers})
        if self.responses:
            return self.responses.pop(0)
        return 202, {}


@pytest.fixture
def cfg(tmp_path):
    text = Path("config/brands.toml").read_text()
    text = text.replace('calendly_url = "REPLACE_CALENDLY_URL"', f'calendly_url = "{CALENDLY}"')
    text = text.replace('client_id = "REPLACE_MICROSOFT_CLIENT_ID"', 'client_id = "test-ms-client"')
    text = text.replace("REPLACE_", "TEST_")
    text = text.replace('public_base_url = "https://whimsical-sprite-d0ccf7.netlify.app/social-media"',
                        'public_base_url = "https://media.example.com"')
    p = tmp_path / "brands.toml"
    p.write_text(text)
    return load_config(p)


@pytest.fixture
def conn(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    c.execute("INSERT INTO brands (key, display_name, website) VALUES ('clas_by_fred', 'CLAS by Fred', 'https://classbyfred.com')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def store():
    s = MemorySecretStore()
    s.set(MS_SERVICE, "access_token", "MS-TOKEN")
    return s


def make_lead(conn, name="bay", email=None, check="mx_ok", stage="qualified"):
    cur = conn.execute(
        "INSERT INTO leads (brand_key, company, domain, sector, city, state, import_source, stage, email, email_check) "
        "VALUES ('clas_by_fred', ?, ?, 'Dental', 'Jacksonville', 'FL', 'test.csv', ?, ?, ?)",
        (f"{name.title()} Dental", f"{name}.com", stage, email or f"front@{name}.com", check))
    conn.commit()
    return cur.lastrowid


def draft_ok(conn, cfg, lid, body="We help dental offices check HIPAA readiness with a short, practical review. Is that on your list this quarter?"):
    return draft_first_email(conn, cfg, lid, None, Guard(dry_run=False), subject="A quick HIPAA question",
                             body=body)


def approved_contact(conn, cfg, lid):
    assert "drafted" in draft_ok(conn, cfg, lid)
    assert approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT).startswith("approved")


def test_missing_calendly_refuses(tmp_path, conn):
    text = Path("config/brands.toml").read_text().replace("REPLACE_", "TEST_")
    p = tmp_path / "nocal.toml"
    p.write_text(text)
    c = load_config(p)
    lid = make_lead(conn)
    with pytest.raises(OutreachError, match="calendly"):
        preview(conn, c, lid)


def test_draft_rejects_banned_claim(conn, cfg):
    lid = make_lead(conn)
    out = draft_ok(conn, cfg, lid, body="We guarantee HIPAA compliance for every dental office. Want to talk?")
    assert out.startswith("rejected draft") and "banned phrase" in out


def test_draft_must_be_qualified(conn, cfg):
    lid = make_lead(conn, stage="found")
    assert draft_ok(conn, cfg, lid).startswith("no draft")


def test_preview_has_footer_and_three_steps(conn, cfg):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    text = preview(conn, cfg, lid)
    assert text.count("reply STOP") >= 3 and "6271 St Augustine" in text and CALENDLY in text
    assert "day 0" in text and "day 4" in text and "day 10" in text


def test_approval_requires_mx_ok_and_one_per_lead(conn, cfg):
    lid = make_lead(conn, check="no_mx")
    draft_ok(conn, cfg, lid)
    assert approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT).startswith("not approved")
    conn.execute("UPDATE leads SET email_check='mx_ok' WHERE id=?", (lid,))
    conn.commit()
    assert approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT).startswith("approved")
    assert approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT).startswith("not approved: sequence already exists")


def test_approval_needs_approver(conn, cfg):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    with pytest.raises(GateError):
        approve_sequence(conn, cfg, lid, "intern")


def test_changed_text_after_approval_is_held(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    conn.execute("UPDATE outreach_steps SET body = body || ' Edited.' WHERE step=0")
    conn.commit()
    mail = Mail()
    out = send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=mail)
    assert any("text changed" in line for line in out) and mail.calls == []


def test_dry_run_sends_nothing(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    mail = Mail()
    out = send_due(conn, cfg, store, Guard(dry_run=True), T0, transport=mail)
    assert any("dry-run" in line for line in out) and mail.calls == []


def test_live_first_send_posts_mail_and_moves_stage(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    mail = Mail()
    out = send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=mail)
    assert out == [f"lead {lid}: sent step 0"]
    call = mail.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/me/sendMail")
    assert call["body"]["message"]["toRecipients"][0]["emailAddress"]["address"] == f"front@bay.com"
    assert call["headers"]["authorization"] == "Bearer MS-TOKEN"
    assert conn.execute("SELECT stage FROM leads WHERE id=?", (lid,)).fetchone()[0] == "contacted"


def test_follow_ups_wait_for_their_day(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=Mail())
    mail = Mail()
    assert send_due(conn, cfg, store, Guard(dry_run=False), T0 + timedelta(days=3), transport=mail) == []
    assert mail.calls == []
    out = send_due(conn, cfg, store, Guard(dry_run=False), T0 + timedelta(days=4, hours=1), transport=mail)
    assert out == [f"lead {lid}: sent step 1"]
    out = send_due(conn, cfg, store, Guard(dry_run=False), T0 + timedelta(days=10, hours=1), transport=mail)
    assert out == [f"lead {lid}: sent step 2"]
    status_row = conn.execute("SELECT status FROM outreach_contacts WHERE lead_id=?", (lid,)).fetchone()[0]
    assert status_row == "finished"


def test_daily_cap_holds_other_contacts(conn, cfg, store):
    cfg2 = type(cfg)(**{**cfg.__dict__, "outreach": type(cfg.outreach)(
        calendly_url=CALENDLY, daily_cap=1, sender=cfg.outreach.sender,
        microsoft_client_id="test-ms-client", microsoft_token_url=cfg.outreach.microsoft_token_url)})
    a = make_lead(conn, "alpha")
    b = make_lead(conn, "beta")
    for lid in (a, b):
        draft_ok(conn, cfg2, lid)
        approve_sequence(conn, cfg2, lid, "frederic", now=APPROVED_AT)
    mail = Mail()
    out = send_due(conn, cfg2, store, Guard(dry_run=False), T0, transport=mail)
    assert len(mail.calls) == 1
    assert any("daily cap" in line for line in out)


def test_definite_failure_stops_contact(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    out = send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=Mail((400, {"error": {"code": "BadRequest"}})))
    assert "failed, stopped" in out[0]
    assert conn.execute("SELECT status FROM outreach_contacts WHERE lead_id=?", (lid,)).fetchone()[0] == "stopped_error"


def test_unclear_failure_holds_contact(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    out = send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=Mail((503, {"error": {"code": "x"}})))
    assert "unclear, held" in out[0]
    assert conn.execute("SELECT status FROM outreach_contacts WHERE lead_id=?", (lid,)).fetchone()[0] == "hold"
    assert send_due(conn, cfg, store, Guard(dry_run=False), T0 + timedelta(days=1), transport=Mail()) == []


def test_opt_out_blocks_sends(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    add_opt_out(conn, "front@bay.com", "asked")
    mail = Mail()
    send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=mail)
    assert mail.calls == []
    assert conn.execute("SELECT status FROM outreach_contacts WHERE lead_id=?", (lid,)).fetchone()[0] == "stopped_optout"


def test_reply_stops_sequence_and_moves_lead(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=Mail())
    inbox = Mail((200, {"value": [{"from": {"emailAddress": {"address": "front@bay.com"}},
                                   "subject": "Re: quick question", "bodyPreview": "Yes, let's talk."}]}))
    out = scan_replies(conn, cfg, store, Guard(dry_run=False), transport=inbox)
    assert "replied, sequence stopped" in out[0]
    assert conn.execute("SELECT status FROM outreach_contacts WHERE lead_id=?", (lid,)).fetchone()[0] == "stopped_reply"
    assert conn.execute("SELECT stage FROM leads WHERE id=?", (lid,)).fetchone()[0] == "replied"
    assert inbox.calls[0]["method"] == "GET" and "bodyPreview" in inbox.calls[0]["url"]


def test_stop_reply_becomes_opt_out(conn, cfg, store):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    send_due(conn, cfg, store, Guard(dry_run=False), T0, transport=Mail())
    inbox = Mail((200, {"value": [{"from": {"emailAddress": {"address": "front@bay.com"}},
                                   "subject": "Re", "bodyPreview": "Please unsubscribe me"}]}))
    scan_replies(conn, cfg, store, Guard(dry_run=False), transport=inbox)
    assert conn.execute("SELECT COUNT(*) FROM opt_outs WHERE email='front@bay.com'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM outreach_contacts WHERE lead_id=?", (lid,)).fetchone()[0] == "stopped_optout"


def test_scan_in_dry_run_reads_nothing(conn, cfg, store):
    assert scan_replies(conn, cfg, store, Guard(dry_run=True), transport=Mail())[0].startswith("dry-run")


def test_status_summary(conn, cfg):
    lid = make_lead(conn)
    draft_ok(conn, cfg, lid)
    approve_sequence(conn, cfg, lid, "frederic", now=APPROVED_AT)
    assert "active: 1" in status(conn, cfg, T0) and "sent today: 0 of 30" in status(conn, cfg, T0)

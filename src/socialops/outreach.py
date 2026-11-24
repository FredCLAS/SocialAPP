"""Cold outreach for clas_by_fred: approved sequences, capped sends, stop on reply or opt-out.

Rules:
- A lead must be qualified, have an MX-ok email, and not be opted out before a sequence is approved.
- One approval per contact (gate 'cold_send') covers the whole sequence: day 0, day 4, day 10.
  The approval is bound to the exact text (hash). Any change needs a new draft and a new approval.
- Every email has: the sender's name, the company, the postal address, a "reply STOP" line, and the Calendly link.
- Daily cap per local day (default 30, never above 30).
- A reply from the contact, or an opt-out word in a reply, stops the sequence.
- A definite send failure stops that contact. An unclear failure (timeout, 5xx) blocks it until checked.
- Dry-run: nothing is sent. Live sends need Microsoft connected on this Mac.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .config import Config
from .gates import is_approved, record_decision
from .guard import Guard
from .leads import set_stage
from .llm import DraftClient
from .microsoft import MailClient, MailError, access_token, default_transport
from .caption_drafter import BANNED_PHRASES

STEP_DAYS = (0, 4, 10)
MAX_EMAIL_CHARS = 1500
STOP_WORDS = ("unsubscribe", "remove me", "opt out", "stop emailing", "stop contacting", "take me off")
OUTREACH_SYSTEM = """You write a short, plain cold email from a small compliance consultant to a business
owner in a regulated sector. Reply with JSON only: {"subject": "...", "body": "..."}.
Body: 4 to 6 short sentences. One specific, respectful reason for writing. One question. No promises,
no guarantees, no legal or medical advice, no invented facts or clients. Do not include an address,
sign-off, unsubscribe line, or links; those are added automatically."""


class OutreachError(ValueError):
    pass


@dataclass
class Footer:
    sender_name: str
    company: str
    address: str
    calendly: str


def footer(cfg: Config) -> Footer:
    if cfg.footer is None:
        raise OutreachError("outreach needs the [footer] postal address in brands.toml")
    if not cfg.outreach.calendly_url or cfg.outreach.calendly_url.startswith("REPLACE_"):
        raise OutreachError("outreach needs [outreach] calendly_url in brands.toml")
    host = urlparse(cfg.outreach.calendly_url).hostname
    if urlparse(cfg.outreach.calendly_url).scheme != "https" or host != "calendly.com":
        raise OutreachError("calendly_url must be an https calendly.com link")
    return Footer("Fred", cfg.footer.company, cfg.footer.postal_address, cfg.outreach.calendly_url)


def with_footer(subject: str, body: str, f: Footer) -> tuple[str, str]:
    text = (f"{body.strip()}\n\nBook a short call: {f.calendly}\n\n"
            f"{f.sender_name}, {f.company}\n{f.address}\n\n"
            "If you'd rather not get these emails, reply STOP and you will not hear from me again.")
    return subject.strip(), text


def email_problems(subject: str, text: str, f: Footer) -> list[str]:
    problems = []
    if not subject.strip():
        problems.append("subject empty")
    if len(text) > MAX_EMAIL_CHARS:
        problems.append(f"email over {MAX_EMAIL_CHARS} characters")
    checks = {
        "sender name": f.sender_name in text,
        "company": f.company in text,
        "postal address": f.address in text,
        "STOP line": "reply STOP" in text,
        "Calendly link": f.calendly in text,
    }
    problems += [f"missing {k}" for k, ok in checks.items() if not ok]
    lowered = text.lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        problems.append(f"banned phrase: {', '.join(hits)}")
    return problems


def sequence_texts(conn: sqlite3.Connection, cfg: Config, lead_id: int) -> list[tuple[int, str, str]]:
    f = footer(cfg)
    draft = conn.execute("SELECT subject, body FROM outreach_drafts WHERE lead_id = ?", (lead_id,)).fetchone()
    if draft is None:
        raise OutreachError("no draft for this lead; draft it first")
    company = conn.execute("SELECT company FROM leads WHERE id = ?", (lead_id,)).fetchone()["company"]
    followups = [
        ("Following up", f"Following up on my note about compliance at {company}. Is this worth a short look?"),
        ("Last note from me", f"A last note from me about {company}. If the timing is not right, reply STOP and I will stop."),
    ]
    out = []
    subj0, text0 = with_footer(draft["subject"], draft["body"], f)
    out.append((0, subj0, text0))
    for i, (subj, body) in enumerate(followups, start=1):
        s, t = with_footer(subj, body, f)
        out.append((i, s, t))
    return out


def sequence_hash(texts: list[tuple[int, str, str]]) -> str:
    raw = json.dumps(texts, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def draft_first_email(conn: sqlite3.Connection, cfg: Config, lead_id: int, client: DraftClient | None,
                      guard: Guard, subject: str | None = None, body: str | None = None) -> str:
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        return "no such lead"
    if lead["stage"] != "qualified":
        return "no draft: lead must be qualified"
    f = footer(cfg)
    if subject is not None and body is not None:
        source, subj, text = "manual", subject, body
    else:
        if not guard.write(f"draft first outreach email for lead {lead_id} with {cfg.llm.model}"):
            return "dry-run: would draft with Claude"
        user = f"Company: {lead['company']}\nSector: {lead['sector'] or 'regulated sector'}\nLocation: {lead['city']} {lead['state']}"
        raw = client.complete(system=OUTREACH_SYSTEM, user=user, model=cfg.llm.model, max_tokens=400).text
        try:
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            source, subj, text = "claude", data["subject"], data["body"]
        except (ValueError, KeyError):
            return "rejected draft: output was not the expected JSON"
    problems = email_problems(*with_footer(subj, text, f), f)
    if problems:
        return "rejected draft: " + "; ".join(problems)
    with conn:
        conn.execute("INSERT INTO outreach_drafts (lead_id, subject, body, source) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(lead_id) DO UPDATE SET subject=excluded.subject, body=excluded.body, source=excluded.source",
                     (lead_id, subj.strip(), text.strip(), source))
    return f"drafted ({source}); review with outreach-preview"


def preview(conn: sqlite3.Connection, cfg: Config, lead_id: int) -> str:
    out = []
    for step, subj, text in sequence_texts(conn, cfg, lead_id):
        out.append(f"--- day {STEP_DAYS[step]} (step {step})\nSubject: {subj}\n\n{text}\n")
    return "\n".join(out)


def approve_sequence(conn: sqlite3.Connection, cfg: Config, lead_id: int, actor: str,
                     now: datetime | None = None) -> str:
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        return "not approved: no such lead"
    if lead["stage"] != "qualified":
        return "not approved: lead must be qualified"
    if lead["email_check"] != "mx_ok" or not lead["email"]:
        return "not approved: needs an email that passed the MX check"
    if conn.execute("SELECT 1 FROM opt_outs WHERE email = ?", (lead["email"],)).fetchone():
        return "not approved: this address opted out"
    if conn.execute("SELECT 1 FROM outreach_contacts WHERE lead_id = ?", (lead_id,)).fetchone():
        return "not approved: sequence already exists for this lead"
    texts = sequence_texts(conn, cfg, lead_id)
    for step, subj, text in texts:
        problems = email_problems(subj, text, footer(cfg))
        if problems:
            return f"not approved: step {step}: " + "; ".join(problems)
    digest = sequence_hash(texts)
    record_decision(conn, "cold_send", lead_id, actor, "approved", note=digest)
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with conn:
        cur = conn.execute("INSERT INTO outreach_contacts (lead_id, approved_by, approved_at, approval_hash) "
                           "VALUES (?, ?, ?, ?)", (lead_id, actor, when, digest))
        cid = cur.lastrowid
        for step, subj, text in texts:
            conn.execute("INSERT INTO outreach_steps (contact_id, step, subject, body) VALUES (?, ?, ?, ?)",
                         (cid, step, subj, text))
    return f"approved; sequence of {len(texts)} emails starts on the next send run"


def _local_day_start_utc(now: datetime, tz) -> str:
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def sent_today(conn: sqlite3.Connection, cfg: Config, now: datetime) -> int:
    start = _local_day_start_utc(now, cfg.timezone)
    return conn.execute("SELECT COUNT(*) FROM outreach_steps WHERE status='sent' AND sent_at >= ?",
                        (start,)).fetchone()[0]


def _due(approved_at: str, step: int, now: datetime) -> bool:
    base = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    return now >= base + timedelta(days=STEP_DAYS[step])


def send_due(conn: sqlite3.Connection, cfg: Config, store, guard: Guard, now: datetime,
             transport=default_transport) -> list[str]:
    cap = min(cfg.outreach.daily_cap, 30)
    report: list[str] = []
    contacts = conn.execute("SELECT c.*, l.email, l.email_check, l.stage, l.company FROM outreach_contacts c "
                            "JOIN leads l ON l.id = c.lead_id WHERE c.status = 'active' ORDER BY c.id").fetchall()
    current_hash_cache = {}
    token = None
    for c in contacts:
        steps = conn.execute("SELECT * FROM outreach_steps WHERE contact_id = ? AND status = 'pending' ORDER BY step",
                             (c["id"],)).fetchall()
        if not steps:
            with conn:
                conn.execute("UPDATE outreach_contacts SET status='finished' WHERE id=?", (c["id"],))
            continue
        step = steps[0]
        if not _due(c["approved_at"], step["step"], now):
            continue
        texts = [(r["step"], r["subject"], r["body"]) for r in conn.execute(
            "SELECT step, subject, body FROM outreach_steps WHERE contact_id=? ORDER BY step", (c["id"],))]
        if sequence_hash(texts) != c["approval_hash"] or not is_approved(conn, "cold_send", c["lead_id"]):
            report.append(f"lead {c['lead_id']}: held (text changed or approval missing)")
            continue
        if conn.execute("SELECT 1 FROM opt_outs WHERE email = ?", (c["email"],)).fetchone():
            with conn:
                conn.execute("UPDATE outreach_contacts SET status='stopped_optout' WHERE id=?", (c["id"],))
            report.append(f"lead {c['lead_id']}: stopped (opt-out)")
            continue
        if c["email_check"] != "mx_ok":
            report.append(f"lead {c['lead_id']}: held (email not MX-ok)")
            continue
        if sent_today(conn, cfg, now) >= cap:
            report.append(f"lead {c['lead_id']}: held (daily cap {cap} reached)")
            break
        if not guard.write(f"send outreach step {step['step']} to lead {c['lead_id']} from {cfg.outreach.sender}"):
            report.append(f"lead {c['lead_id']}: dry-run (would send step {step['step']})")
            continue
        if store is None or not cfg.outreach.microsoft_client_id or cfg.outreach.microsoft_client_id.startswith("REPLACE_"):
            report.append("blocked: Microsoft client_id not set or no secret store")
            break
        if token is None:
            try:
                token = access_token(store, cfg.outreach.microsoft_client_id, cfg.outreach.microsoft_token_url)
            except MailError as exc:
                report.append(f"blocked: {exc}")
                break
        mail = MailClient(token, transport)
        try:
            mail.send_mail(c["email"], step["subject"], step["body"])
        except MailError as exc:
            with conn:
                status = "unknown" if exc.ambiguous else "failed"
                conn.execute("UPDATE outreach_steps SET status=?, detail=? WHERE id=?", (status, str(exc)[:200], step["id"]))
                conn.execute("UPDATE outreach_contacts SET status=? WHERE id=?",
                             ("hold" if exc.ambiguous else "stopped_error", c["id"]))
            report.append(f"lead {c['lead_id']}: {'unclear, held' if exc.ambiguous else 'failed, stopped'} ({exc})")
            continue
        with conn:
            conn.execute("UPDATE outreach_steps SET status='sent', sent_at=? WHERE id=?",
                         (now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), step["id"]))
            if step["step"] == 2:
                conn.execute("UPDATE outreach_contacts SET status='finished' WHERE id=?", (c["id"],))
        if c["stage"] == "qualified":
            set_stage(conn, c["lead_id"], "contacted", None, "first outreach email sent")
        report.append(f"lead {c['lead_id']}: sent step {step['step']}")
    return report


def scan_replies(conn: sqlite3.Connection, cfg: Config, store, guard: Guard, transport=default_transport) -> list[str]:
    if guard.dry_run:
        return ["dry-run: reply scan needs live mode (reads the inbox)"]
    if store is None or not cfg.outreach.microsoft_client_id or cfg.outreach.microsoft_client_id.startswith("REPLACE_"):
        return ["blocked: Microsoft not connected"]
    try:
        token = access_token(store, cfg.outreach.microsoft_client_id, cfg.outreach.microsoft_token_url)
        messages = MailClient(token, transport).inbox_previews()
    except MailError as exc:
        return [f"blocked: {exc}"]
    report = []
    for m in messages:
        addr = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        if not addr:
            continue
        preview_text = f"{m.get('subject', '')} {m.get('bodyPreview', '')}".lower()
        wants_out = any(w in preview_text for w in STOP_WORDS) or "stop" in preview_text.split()
        lead = conn.execute("SELECT l.id, l.stage, c.id AS cid, c.status FROM leads l "
                            "LEFT JOIN outreach_contacts c ON c.lead_id = l.id WHERE lower(l.email) = ?",
                            (addr,)).fetchone()
        if lead is None:
            continue
        if wants_out:
            with conn:
                conn.execute("INSERT OR IGNORE INTO opt_outs (email, reason) VALUES (?, 'reply asked to stop')", (addr,))
        if lead["cid"] and lead["status"] == "active":
            with conn:
                conn.execute("UPDATE outreach_contacts SET status=? WHERE id=?",
                             ("stopped_optout" if wants_out else "stopped_reply", lead["cid"]))
            if lead["stage"] == "contacted":
                set_stage(conn, lead["id"], "replied", None, "reply received")
            report.append(f"lead {lead['id']}: replied, sequence stopped" + (" (opt-out)" if wants_out else ""))
    return report or ["no replies from contacts"]


def add_opt_out(conn: sqlite3.Connection, email: str, reason: str) -> str:
    with conn:
        conn.execute("INSERT OR IGNORE INTO opt_outs (email, reason) VALUES (?, ?)", (email.lower().strip(), reason[:200]))
        conn.execute("UPDATE outreach_contacts SET status='stopped_optout' WHERE status='active' AND lead_id IN "
                     "(SELECT id FROM leads WHERE lower(email)=?)", (email.lower().strip(),))
    return "opt-out recorded"


def status(conn: sqlite3.Connection, cfg: Config, now: datetime) -> str:
    rows = conn.execute("SELECT status, COUNT(*) n FROM outreach_contacts GROUP BY status").fetchall()
    parts = [f"{r['status']}: {r['n']}" for r in rows] or ["no contacts yet"]
    return f"contacts: {', '.join(parts)}; sent today: {sent_today(conn, cfg, now)} of {cfg.outreach.daily_cap}"

"""Engagement monitor (read-only) and reply pipeline (draft, approve, send).

Rules:
- Only comments on our own published posts are read. Nothing is liked, followed, or hidden.
- Each comment is classified by rules. Legal, complaint, and refund topics are escalated to a human
  and get no draft. Spam gets no draft. Replies to our own page's comments are skipped.
- Draft order: approved answer bank first, then Claude (Haiku default), then validation.
- A reply is sent only if its exact text was approved (approval note = hash of the text).
- Daily send cap per brand. A failed send is not retried automatically.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .agents import AgentResult, Context
from .caption_drafter import ALLOWED_EXTRA_HOSTS, BANNED_PHRASES
from .config import Config
from .gates import is_approved, record_decision
from .guard import Guard
from .meta_graph import GraphError, MetaGraph, Transport, default_transport, list_comments, reply_to_comment
from .secrets import SecretNotFound, service_name

MAX_REPLY_CHARS = 500
DAILY_REPLY_CAP = 20
LOOKBACK_DAYS = 14
ESCALATE_TERMS = (
    "lawsuit", "lawyer", "attorney", "sue ", "legal action", "complaint", "refund", "breach",
    "fraud", "scam", "violation", "fine", "malpractice", "medical advice", "diagnos", "ocr",
)
SPAM_TERMS = ("dm me", "make money", "earn $", "click the link", "whatsapp", "crypto", "guaranteed income")
LEAD_TERMS = ("interested", "pricing", "price", "how much", "cost", "book", "consult", "call me", "quote", "services")
QUESTION_START = re.compile(r"^(how|what|why|when|where|who|can|do|does|is|are|should|will)\b", re.I)
WORD = re.compile(r"[a-z0-9']+")
STOP = frozenset({"the", "a", "an", "is", "are", "do", "does", "you", "your", "i", "we", "to", "of", "and", "or", "it", "in", "on", "for", "with", "what", "how", "can", "any", "this", "that"})


@dataclass(frozen=True)
class Classification:
    category: str  # escalate | spam | lead | question | other
    escalate: bool


def classify(body: str) -> Classification:
    text = body.lower()
    if any(t in text for t in ESCALATE_TERMS):
        return Classification("escalate", True)
    if any(t in text for t in SPAM_TERMS) or re.search(r"https?://", text):
        return Classification("spam", False)
    if any(t in text for t in LEAD_TERMS):
        return Classification("lead", False)
    if "?" in text or QUESTION_START.match(body.strip()):
        return Classification("question", False)
    return Classification("other", False)


def _body(comment: dict) -> str:
    return (comment.get("message") or comment.get("text") or "").strip()


def pull_comments(conn: sqlite3.Connection, cfg: Config, brand_key: str, store, guard: Guard,
                  now: datetime, transport: Transport = default_transport) -> AgentResult:
    if guard.dry_run:
        return AgentResult("skipped", "dry-run: comment pull needs live mode (read-only, but needs tokens)")
    since = (now - timedelta(days=LOOKBACK_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    targets = conn.execute(
        "SELECT p.id, p.platform, pl.remote_id FROM posts p JOIN publish_log pl ON pl.post_id = p.id "
        "WHERE p.brand_key = ? AND pl.outcome = 'ok' AND pl.remote_id IS NOT NULL AND p.scheduled_at >= ?",
        (brand_key, since),
    ).fetchall()
    brand = cfg.brands[brand_key]
    new_ids, skipped_own, errors = [], 0, []
    for row in targets:
        pc = brand.platforms[row["platform"]]
        try:
            token = store.get(service_name(brand_key, row["platform"]), "access_token")
        except SecretNotFound:
            errors.append(f"{row['platform']}: no token")
            continue
        graph = MetaGraph(token, cfg.meta_api_version, transport)
        try:
            comments = list_comments(graph, row["remote_id"])
        except GraphError as exc:
            errors.append(f"{row['platform']} post {row['id']}: {exc}")
            continue
        for c in comments:
            source_id = str(c.get("id", ""))
            if not source_id:
                continue
            author = (c.get("from") or {}).get("id")
            if author and str(author) == str(pc.account_id):
                skipped_own += 1
                continue
            body = _body(c)
            if not body:
                continue
            cls = classify(body)
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO inbound_items (brand_key, platform, post_id, source_id, author_ref, "
                    "body, category, escalate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (brand_key, row["platform"], row["id"], source_id, str(author) if author else None,
                     body, cls.category, int(cls.escalate)),
                )
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
    reason = f"new {len(new_ids)}, own comments skipped {skipped_own}"
    if errors:
        reason += " | errors: " + "; ".join(errors)
        return AgentResult("blocked" if not new_ids else "ok", reason, new_ids)
    return AgentResult("ok", reason, new_ids)


# --- drafting ----------------------------------------------------------------

REPLY_SYSTEM = """You write short, friendly replies to comments on a small business's social posts.
Reply with only the reply text. No JSON, no quotes, no markdown.
Rules:
- Two sentences at most. Plain language. Answer what was asked.
- Never promise results. Never give legal, medical, or compliance advice; say a specialist can help.
- Do not invent facts, prices, or dates.
- If the comment shows interest in services, invite them to book at the URL given. Otherwise include no link."""


def _bank_match(conn: sqlite3.Connection, body: str) -> str | None:
    words = {w for w in WORD.findall(body.lower()) if w not in STOP}
    best, best_score = None, 0
    for row in conn.execute("SELECT keywords, answer FROM approved_answers").fetchall():
        keys = set(json.loads(row["keywords"]))
        score = len(keys & words)
        if score >= 2 and score > best_score:
            best, best_score = row["answer"], score
    return best


def validate_reply(text: str, brand_website: str, allow_link: bool) -> list[str]:
    problems = []
    if not text.strip():
        problems.append("empty reply")
    if len(text) > MAX_REPLY_CHARS:
        problems.append(f"over {MAX_REPLY_CHARS} characters")
    lowered = text.lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        problems.append(f"banned phrase: {', '.join(hits)}")
    for url in re.findall(r"https?://\S+", text):
        host = urlparse(url.rstrip(".,)")).hostname
        allowed = {urlparse(brand_website).hostname} | ALLOWED_EXTRA_HOSTS
        if not allow_link or host not in allowed:
            problems.append("link not allowed in this reply")
    return problems


def draft_reply(conn: sqlite3.Connection, cfg: Config, item_id: int, client, guard: Guard) -> str:
    item = conn.execute("SELECT * FROM inbound_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return "no such item"
    if item["escalate"] or item["category"] in ("escalate", "spam"):
        return f"no draft: {item['category']} goes to a human"
    if item["reply_status"] not in ("none", "rejected"):
        return f"no draft: reply is {item['reply_status']}"
    brand = cfg.brands[item["brand_key"]]

    bank = _bank_match(conn, item["body"])
    if bank is not None:
        source, text = "bank", bank
    else:
        if not guard.write(f"draft reply to comment {item['source_id']} with {cfg.llm.model}"):
            return "dry-run: would draft with Claude"
        user = (f"Brand: {brand.display_name}\nCategory: {item['category']}\n"
                f"Website (only if the comment is about services): {brand.website}\n"
                f"Comment: {item['body']}")
        comp = client.complete(system=REPLY_SYSTEM, user=user, model=cfg.llm.model, max_tokens=200)
        source, text = "claude", comp.text.strip()

    allow_link = item["category"] == "lead"
    problems = validate_reply(text, brand.website, allow_link)
    if problems:
        with conn:
            conn.execute("UPDATE inbound_items SET reply_status='none', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                         "WHERE id=?", (item_id,))
        return "rejected draft: " + "; ".join(problems)
    with conn:
        conn.execute(
            "UPDATE inbound_items SET reply_text=?, reply_source=?, reply_status='drafted', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (text, source, item_id),
        )
    return f"drafted ({source})"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def approve_reply(conn: sqlite3.Connection, item_id: int, actor: str) -> str:
    item = conn.execute("SELECT * FROM inbound_items WHERE id = ?", (item_id,)).fetchone()
    if item is None or item["reply_status"] != "drafted":
        return "not approved: reply is not in drafted state"
    record_decision(conn, "reply", item_id, actor, "approved", note=_hash(item["reply_text"]))
    with conn:
        conn.execute("UPDATE inbound_items SET reply_status='approved', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                     "WHERE id=?", (item_id,))
    return "approved"


def reject_reply(conn: sqlite3.Connection, item_id: int, actor: str, reason: str) -> str:
    if not reason.strip():
        return "a reason is required"
    record_decision(conn, "reply", item_id, actor, "rejected", note=reason[:500])
    with conn:
        conn.execute("UPDATE inbound_items SET reply_status='rejected', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                     "WHERE id=?", (item_id,))
    return "rejected"


def edit_reply(conn: sqlite3.Connection, cfg: Config, item_id: int, text: str) -> str:
    item = conn.execute("SELECT * FROM inbound_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return "no such item"
    if item["reply_status"] == "sent":
        return "already sent"
    problems = validate_reply(text, cfg.brands[item["brand_key"]].website, item["category"] == "lead")
    if problems:
        return "not saved: " + "; ".join(problems)
    with conn:
        conn.execute(
            "UPDATE inbound_items SET reply_text=?, reply_source='manual', reply_status='drafted', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (text, item_id),
        )
    return "saved; approval needed again"


def send_approved(conn: sqlite3.Connection, cfg: Config, brand_key: str, store, guard: Guard,
                  transport: Transport = default_transport, cap: int = DAILY_REPLY_CAP) -> list[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sent_today = conn.execute(
        "SELECT COUNT(*) FROM inbound_items WHERE brand_key=? AND reply_status='sent' AND updated_at LIKE ?",
        (brand_key, today + "%"),
    ).fetchone()[0]
    out = []
    rows = conn.execute("SELECT * FROM inbound_items WHERE brand_key=? AND reply_status='approved' ORDER BY id",
                        (brand_key,)).fetchall()
    for item in rows:
        if sent_today >= cap:
            out.append(f"{item['id']}: held (daily cap {cap} reached)")
            continue
        latest = conn.execute(
            "SELECT decision, note FROM approvals WHERE kind='reply' AND ref_id=? ORDER BY id DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
        if latest is None or not is_approved(conn, "reply", item["id"]) or latest["note"] != _hash(item["reply_text"]):
            out.append(f"{item['id']}: held (text changed since approval; approve again)")
            continue
        if not guard.write(f"send reply to comment {item['source_id']} on {brand_key} {item['platform']}"):
            out.append(f"{item['id']}: dry-run (would send)")
            continue
        try:
            token = store.get(service_name(brand_key, item["platform"]), "access_token")
            graph = MetaGraph(token, cfg.meta_api_version, transport)
            new_id = reply_to_comment(graph, item["source_id"], item["reply_text"])
        except (GraphError, SecretNotFound) as exc:
            with conn:
                conn.execute("UPDATE inbound_items SET reply_status='failed', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                             "WHERE id=?", (item["id"],))
            out.append(f"{item['id']}: failed ({exc}); not retried automatically")
            continue
        with conn:
            conn.execute("UPDATE inbound_items SET reply_status='sent', reply_sent_id=?, "
                         "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (new_id, item["id"]))
        sent_today += 1
        out.append(f"{item['id']}: sent")
    return out

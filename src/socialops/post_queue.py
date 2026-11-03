"""Post records, the status state machine, and the approval gate.

Rules:
- Only the transitions in ALLOWED_TRANSITIONS are legal. Anything else raises InvalidTransition.
- draft -> approved requires a complete post (visual, caption, hashtags, CTA where needed)
  and an approver from APPROVERS. The approval is written to `approvals`.
- Batch approval is all-or-nothing: if any post in the week is incomplete, none are approved.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from .config import Config
from .content_engine import week_bounds_utc

STATUSES = ("draft", "approved", "scheduled", "published", "failed")

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"approved"}),
    "approved": frozenset({"scheduled", "draft"}),
    "scheduled": frozenset({"published", "failed"}),
    "failed": frozenset({"scheduled", "draft"}),
    "published": frozenset(),
}

APPROVERS = frozenset({"pamela", "frederic"})


class InvalidTransition(ValueError):
    pass


class NotApprovable(ValueError):
    def __init__(self, post_id: int, problems: list[str]) -> None:
        self.post_id = post_id
        self.problems = problems
        super().__init__(f"post {post_id} not approvable: {'; '.join(problems)}")


class UnauthorizedApprover(PermissionError):
    pass


@dataclass(frozen=True)
class Post:
    id: int
    brand_key: str
    platform: str
    content_type: str
    format: str
    subject: str
    visual_path: str | None
    caption: str
    hashtags: tuple[str, ...]
    cta_url: str | None
    scheduled_at: str
    status: str
    visual_source: str = "unknown"
    draft_model: str | None = None


AI_LABEL_TERMS = ("ai-generated", "ai generated", "illustrative")


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(
        id=row["id"],
        brand_key=row["brand_key"],
        platform=row["platform"],
        content_type=row["content_type"],
        format=row["format"],
        subject=row["subject"],
        visual_path=row["visual_path"],
        caption=row["caption"],
        hashtags=tuple(json.loads(row["hashtags"] or "[]")),
        cta_url=row["cta_url"],
        scheduled_at=row["scheduled_at"],
        status=row["status"],
        visual_source=row["visual_source"],
        draft_model=row["draft_model"],
    )


def get_post(conn: sqlite3.Connection, post_id: int) -> Post:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise KeyError(f"no post {post_id}")
    return _row_to_post(row)


def list_posts(
    conn: sqlite3.Connection,
    brand_key: str | None = None,
    status: str | None = None,
    scheduled_from: str | None = None,
    scheduled_to: str | None = None,
) -> list[Post]:
    sql = "SELECT * FROM posts WHERE 1=1"
    args: list[object] = []
    if brand_key:
        sql += " AND brand_key = ?"
        args.append(brand_key)
    if status:
        sql += " AND status = ?"
        args.append(status)
    if scheduled_from:
        sql += " AND scheduled_at >= ?"
        args.append(scheduled_from)
    if scheduled_to:
        sql += " AND scheduled_at < ?"
        args.append(scheduled_to)
    sql += " ORDER BY scheduled_at, platform, id"
    return [_row_to_post(r) for r in conn.execute(sql, args).fetchall()]


def completeness_problems(post: Post) -> list[str]:
    problems = []
    if not post.visual_path:
        problems.append("missing visual")
    if not post.caption.strip():
        problems.append("missing caption")
    if not post.hashtags:
        problems.append("missing hashtags")
    if post.content_type == "webinar_cta" and not post.cta_url:
        problems.append("webinar CTA needs cta_url")
    if post.visual_source == "ai" and not any(t in post.caption.lower() for t in AI_LABEL_TERMS):
        problems.append("AI-generated visual must be labeled in caption")
    if post.visual_source == "unknown":
        problems.append("visual source not set (ai | real_photo | canva)")
    return problems


def _check_actor(actor: str) -> None:
    if actor not in APPROVERS:
        raise UnauthorizedApprover(f"{actor!r} is not an approver")


def transition(conn: sqlite3.Connection, post_id: int, new_status: str, actor: str | None = None) -> Post:
    post = get_post(conn, post_id)
    if new_status not in STATUSES:
        raise InvalidTransition(f"unknown status {new_status!r}")
    if new_status not in ALLOWED_TRANSITIONS[post.status]:
        raise InvalidTransition(f"post {post_id}: {post.status} -> {new_status} not allowed")

    with conn:
        if new_status == "approved":
            if actor is None:
                raise UnauthorizedApprover("approval needs an actor")
            _check_actor(actor)
            problems = completeness_problems(post)
            if problems:
                raise NotApprovable(post_id, problems)
            conn.execute(
                "INSERT INTO approvals (kind, ref_id, decided_by, decision) VALUES ('post', ?, ?, 'approved')",
                (post_id, actor),
            )
        conn.execute("UPDATE posts SET status = ? WHERE id = ?", (new_status, post_id))
    return get_post(conn, post_id)


def approve_week(conn: sqlite3.Connection, cfg: Config, brand_key: str, week_start: date, actor: str) -> list[int]:
    """Approve every draft for one brand in one week. All-or-nothing."""
    _check_actor(actor)
    start, end = week_bounds_utc(week_start, cfg.timezone)
    drafts = list_posts(conn, brand_key=brand_key, status="draft", scheduled_from=start, scheduled_to=end)
    failures = {p.id: completeness_problems(p) for p in drafts}
    failures = {pid: probs for pid, probs in failures.items() if probs}
    if failures:
        first = next(iter(failures))
        raise NotApprovable(first, [f"{pid}: {', '.join(p)}" for pid, p in failures.items()])

    approved: list[int] = []
    with conn:
        for post in drafts:
            conn.execute(
                "INSERT INTO approvals (kind, ref_id, decided_by, decision) VALUES ('post', ?, ?, 'approved')",
                (post.id, actor),
            )
            conn.execute("UPDATE posts SET status = 'approved' WHERE id = ?", (post.id,))
            approved.append(post.id)
    return approved


def set_post_fields(conn: sqlite3.Connection, post_id: int, **fields) -> Post:
    """Edit content on a draft or failed post. Approved content is locked: unapprove first."""
    allowed = {"subject", "visual_path", "visual_source", "caption", "hashtags", "cta_url"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"fields not editable: {sorted(bad)}")
    post = get_post(conn, post_id)
    if post.status not in ("draft", "failed"):
        raise InvalidTransition(f"post {post_id} is {post.status}; content is locked")
    if "visual_source" in fields and fields["visual_source"] not in ("ai", "real_photo", "canva"):
        raise ValueError("visual_source must be ai, real_photo, or canva")
    if "hashtags" in fields:
        fields["hashtags"] = json.dumps(list(fields["hashtags"]))
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE posts SET {assignments} WHERE id = ?", (*fields.values(), post_id))
    return get_post(conn, post_id)

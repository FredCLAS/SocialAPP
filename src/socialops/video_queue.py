"""Zoice video queue (manual until Zoice's API is confirmed).

States: script -> queued -> produced -> optimized -> approved -> published (or failed).
- script: drafted by Claude (or typed). Needs a person's approval before it is queued.
- queued: a brief file is written for the Zoice upload. The app does not call Zoice.
- produced: the video file is in the brand's assets folder.
- optimized: linked to a video post, which carries an AI label (Zoice output is AI-generated).
- approved / published: follow the linked post's status.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .caption_drafter import BANNED_PHRASES
from .config import Config
from .guard import Guard
from .llm import DraftClient
from .post_queue import APPROVERS, get_post, set_post_fields

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "SocialOps"
SCRIPT_WORDS = (40, 250)
VIDEO_EXT = (".mp4", ".mov")
MAX_VIDEO_BYTES = 1024 * 1024 * 1024  # 1 GB; Reels and FB limits are larger, but this keeps uploads sane

SCRIPT_SYSTEM = """You write short spoken scripts for a vertical (9:16) video for one small business.
Reply with only the script text. No headings, no stage directions, no markdown.
Structure: a hook line, two to four short points, then one call to action line that includes the URL given.
No promised outcomes, no guarantees, no legal or medical advice, no invented statistics or client names.
Say it is AI-generated only if the brief asks for that label."""


class QueueError(ValueError):
    pass


def add_job(conn: sqlite3.Connection, brand_key: str, topic: str) -> int:
    if not topic.strip():
        raise QueueError("topic is required")
    with conn:
        cur = conn.execute("INSERT INTO video_jobs (brand_key, topic) VALUES (?, ?)", (brand_key, topic.strip()[:200]))
    return cur.lastrowid


def _job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM video_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise QueueError(f"no video job {job_id}")
    return row


def _set(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE video_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def script_problems(text: str, website: str) -> list[str]:
    problems = []
    words = len(text.split())
    if not SCRIPT_WORDS[0] <= words <= SCRIPT_WORDS[1]:
        problems.append(f"script is {words} words; need {SCRIPT_WORDS[0]} to {SCRIPT_WORDS[1]}")
    lowered = text.lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        problems.append(f"banned phrase: {', '.join(hits)}")
    host = website.split("//", 1)[-1].split("/", 1)[0]
    if host not in text:
        problems.append("call to action must include the brand website")
    return problems


def draft_script(conn: sqlite3.Connection, cfg: Config, job_id: int, client: DraftClient | None, guard: Guard) -> str:
    job = _job(conn, job_id)
    if job["status"] != "script":
        return f"no draft: job is {job['status']}"
    brand = cfg.brands[job["brand_key"]]
    if not guard.write(f"draft video script for job {job_id} with {cfg.llm.model}"):
        return "dry-run: would draft a script with Claude"
    user = f"Brand: {brand.display_name}\nTopic: {job['topic']}\nWebsite for the call to action: {brand.website}"
    text = client.complete(system=SCRIPT_SYSTEM, user=user, model=cfg.llm.model, max_tokens=600).text.strip()
    problems = script_problems(text, brand.website)
    if problems:
        return "rejected script: " + "; ".join(problems)
    _set(conn, job_id, script=text, script_source="claude")
    return "script drafted"


def set_manual_script(conn: sqlite3.Connection, cfg: Config, job_id: int, text: str) -> str:
    job = _job(conn, job_id)
    if job["status"] != "script":
        return f"not saved: job is {job['status']}"
    problems = script_problems(text, cfg.brands[job["brand_key"]].website)
    if problems:
        return "not saved: " + "; ".join(problems)
    _set(conn, job_id, script=text.strip(), script_source="manual")
    return "script saved"


def approve_script(conn: sqlite3.Connection, job_id: int, actor: str) -> str:
    if actor not in APPROVERS:
        raise QueueError(f"{actor!r} is not an approver")
    job = _job(conn, job_id)
    if job["status"] != "script" or not job["script"]:
        return "not approved: needs a script in 'script' state"
    _set(conn, job_id, script_approved_by=actor, status="queued")
    return "script approved; brief queued"


def write_brief(conn: sqlite3.Connection, cfg: Config, job_id: int, root: Path = DEFAULT_ROOT) -> Path:
    job = _job(conn, job_id)
    if job["status"] != "queued":
        raise QueueError("brief is written when the job is queued")
    brand = cfg.brands[job["brand_key"]]
    folder = root / "zoice-queue"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"job_{job_id}.md"
    path.write_text(
        f"# Zoice brief {job_id}: {brand.display_name}\n\n"
        f"Topic: {job['topic']}\n"
        f"Format: vertical 9:16, 30 to 60 seconds\n"
        f"Label: AI-generated (required in the post caption)\n"
        f"Save the finished file as: assets/{brand.assets_subdir}/video_{job_id}.mp4\n"
        f"Then run: socialops video-produced --id {job_id} --file assets/{brand.assets_subdir}/video_{job_id}.mp4\n\n"
        f"## Script\n\n{job['script']}\n",
        encoding="utf-8",
    )
    return path


def mark_produced(conn: sqlite3.Connection, job_id: int, rel_path: str, root: Path = DEFAULT_ROOT) -> str:
    job = _job(conn, job_id)
    if job["status"] != "queued":
        return f"not saved: job is {job['status']}"
    p = PurePosixPath(rel_path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return "not saved: path must be relative and inside the assets folder"
    if p.suffix.lower() not in VIDEO_EXT:
        return "not saved: video must be .mp4 or .mov"
    target = root / rel_path
    if not target.is_file():
        return "not saved: file not found on this Mac"
    if target.stat().st_size > MAX_VIDEO_BYTES:
        return "not saved: file over 1 GB"
    _set(conn, job_id, video_path=rel_path, status="produced")
    return "video recorded"


def link_to_post(conn: sqlite3.Connection, job_id: int, post_id: int) -> str:
    job = _job(conn, job_id)
    if job["status"] != "produced" or not job["video_path"]:
        return f"not linked: job is {job['status']}; mark the video produced first"
    post = get_post(conn, post_id)
    if post.brand_key != job["brand_key"]:
        return "not linked: post belongs to another brand"
    if post.status not in ("draft", "failed"):
        return f"not linked: post is {post.status}"
    if post.platform not in ("facebook", "instagram"):
        return "not linked: videos go to Facebook or Instagram from the app"
    set_post_fields(conn, post_id, visual_path=job["video_path"], visual_source="ai")
    conn.execute("UPDATE posts SET format='video' WHERE id=?", (post_id,))
    conn.commit()
    _set(conn, job_id, post_id=post_id, status="optimized")
    return "linked; caption must carry the AI-generated label (QA checks it)"


def sync_statuses(conn: sqlite3.Connection) -> int:
    """Follow the linked post: approved or published on the post moves the job too."""
    moved = 0
    for job in conn.execute("SELECT id, post_id, status FROM video_jobs WHERE post_id IS NOT NULL "
                            "AND status IN ('optimized','approved')").fetchall():
        post = conn.execute("SELECT status FROM posts WHERE id = ?", (job["post_id"],)).fetchone()
        if post is None:
            continue
        target = {"approved": "approved", "scheduled": "approved", "published": "published",
                  "failed": "failed"}.get(post["status"])
        if target and target != job["status"]:
            _set(conn, job["id"], status=target)
            moved += 1
    return moved

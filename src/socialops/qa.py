"""QA agent: the course's pre-post checklist, automated.

Each check is rule-based. Results are stored per content fingerprint, so editing a post
invalidates its QA result. Approval requires a passing result for the current content.

Link checking is a read-only HTTP HEAD. It runs only when a fetcher is supplied (never in dry-run).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse

from .caption_drafter import BANNED_PHRASES, PLACEHOLDER_SUBJECT_RE, ALLOWED_EXTRA_HOSTS, HASHTAG_RE
from .config import Config
from .post_queue import AI_LABEL_TERMS, Post, fingerprint, get_post

PLATFORM_CAPTION_LIMIT = {"facebook": 2000, "instagram": 2200}
SENTENCE_RE = re.compile(r"[.!?](\s|$)")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None = skipped (not run, reason says why)
    reason: str = ""


@dataclass
class QaResult:
    post_id: int
    passed: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]


def _host_allowed(url: str, brand_website: str) -> bool:
    host = urlparse(url).hostname
    allowed = {urlparse(brand_website).hostname} | ALLOWED_EXTRA_HOSTS
    return bool(host) and host in allowed


def run_checks(post: Post, cfg: Config, fetch_status=None) -> list[Check]:
    brand = cfg.brands[post.brand_key]
    caption = post.caption.strip()
    lowered = caption.lower()
    checks: list[Check] = []

    def add(name: str, ok: bool, reason: str = "") -> None:
        checks.append(Check(name, ok, "" if ok else reason))

    # 1. visual present and sourced
    add("visual", bool(post.visual_path) and post.visual_source != "unknown",
        "visual missing or source not set (ai | real_photo | canva)")

    # 2. subject is a real topic, not the auto placeholder
    add("topic", not PLACEHOLDER_SUBJECT_RE.search(post.subject.strip()), "subject is still the auto placeholder")

    # 3. caption structure: hook, value, CTA are at least three sentences
    sentences = len([s for s in SENTENCE_RE.split(caption) if s and s.strip()])
    add("caption_structure", bool(caption) and sentences >= 3,
        "caption needs a hook, value, and call to action (3+ sentences)")

    # 4. CTA: URL present in caption, https, allowed host
    cta = post.cta_url or ""
    add("cta_host", bool(cta) and cta.startswith("https://") and _host_allowed(cta, brand.website),
        "CTA URL missing, not https, or not on an allowed host")
    add("cta_in_caption", bool(cta) and cta in caption, "CTA URL not written in the caption")

    # 5. hashtags
    tags = list(post.hashtags)
    add("hashtags", 3 <= len(tags) <= 8 and all(HASHTAG_RE.match(t) for t in tags),
        "need 3 to 8 hashtags, each #letters/digits")

    # 6. platform length limit
    limit = PLATFORM_CAPTION_LIMIT.get(post.platform, 2000)
    add("caption_length", len(caption) <= limit, f"caption over {limit} characters for {post.platform}")

    # 7. AI visuals must be labeled
    add("ai_label", post.visual_source != "ai" or any(t in lowered for t in AI_LABEL_TERMS),
        "AI-generated visual must be labeled in the caption")

    # 8. banned claims and guarantees
    hits = [p for p in BANNED_PHRASES if p in lowered]
    add("claims", not hits, f"banned phrase: {', '.join(hits)}")

    # 9. scheduled time sits in the brand's configured slot
    pc = brand.platforms.get(post.platform)
    if pc is None:
        add("slot", False, "platform not configured for this brand")
    else:
        local = datetime.fromisoformat(post.scheduled_at.replace("Z", "+00:00")).astimezone(cfg.timezone)
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][local.weekday()]
        in_slot = local.strftime("%H:%M") == pc.post_time and weekday in pc.days
        add("slot", in_slot, f"scheduled {local:%a %H:%M} is not a configured {pc.post_time} slot")

    # 10. video format: Reels and FB video need an MP4 or MOV file
    if post.format == "video":
        ext = PurePosixPath((post.visual_path or "").lower()).suffix
        add("video_file", ext in (".mp4", ".mov"), "video post needs an .mp4 or .mov file")

    # 11. link reachable (read-only; skipped without a fetcher)
    if fetch_status is None:
        checks.append(Check("link_reachable", None, "not checked (no fetcher; dry-run or offline)"))
    else:
        status = fetch_status(cta)
        add("link_reachable", status is not None and 200 <= status < 400,
            f"CTA link returned {status}" if status else "CTA link unreachable")
    return checks


def check_post(conn: sqlite3.Connection, cfg: Config, post_id: int, fetch_status=None) -> QaResult:
    post = get_post(conn, post_id)
    checks = run_checks(post, cfg, fetch_status=fetch_status)
    passed = all(c.ok is not False for c in checks)
    result = QaResult(post_id=post_id, passed=passed, checks=checks)
    with conn:
        conn.execute(
            "INSERT INTO qa_results (post_id, fingerprint, passed, failures) VALUES (?, ?, ?, ?)",
            (post_id, fingerprint(post), int(passed),
             json.dumps([{"check": c.name, "reason": c.reason} for c in result.failures])),
        )
    return result


def latest_result(conn: sqlite3.Connection, post_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM qa_results WHERE post_id = ? ORDER BY id DESC LIMIT 1", (post_id,)
    ).fetchone()
    return dict(row) if row else None

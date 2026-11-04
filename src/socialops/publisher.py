"""Publish approved posts to Meta, with idempotency and dry-run.

Safety rules:
- Only posts in status `scheduled` with scheduled_at <= now are considered.
- Before any network write, an `attempt` row is committed to publish_log. A crash mid-call
  leaves an attempt row, which blocks re-publishing until a human checks Meta.
- A definitive 4xx failure marks the post `failed` (retry later with `failed -> scheduled`).
- An ambiguous failure (timeout, 5xx, 429 on POST) marks the attempt `unknown` and blocks
  re-publishing. The post may already be live.
- A post with an `ok` outcome is never published twice.
- Instagram: stop at 100 API publishes per account per 24 hours.
- Dry-run: no Keychain or network access. Each planned publish is logged via the guard.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import quote

from .config import Config
from .content_engine import week_bounds_utc
from .guard import Guard
from .meta_graph import (
    GraphError,
    MetaGraph,
    Transport,
    default_transport,
    publish_fb_photo,
    publish_fb_video,
    publish_ig_image,
    publish_ig_reel,
)
from .post_queue import Post, list_posts, transition
from .secrets import SecretNotFound, SecretStore, service_name

IG_DAILY_LIMIT = 100
TOKEN_WARN_SECONDS = 7 * 24 * 3600
_TS = "%Y-%m-%dT%H:%M:%S"


class PublishBlocked(RuntimeError):
    pass


@dataclass
class PublishResult:
    post_id: int
    outcome: str  # published | failed | unknown | blocked | skipped | would_publish
    detail: str = ""


def caption_text(post: Post) -> str:
    parts = [post.caption.strip()]
    if post.hashtags:
        parts.append(" ".join(post.hashtags))
    return "\n\n".join(p for p in parts if p)


def media_url(base: str | None, visual_path: str | None) -> str:
    if not base:
        raise PublishBlocked("media.public_base_url is not set; Meta cannot fetch the image")
    if not visual_path:
        raise PublishBlocked("post has no visual")
    rel = PurePosixPath(visual_path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise PublishBlocked("visual path must be relative and inside the asset folder")
    return f"{base.rstrip('/')}/{quote(str(rel))}"


def token_status(store: SecretStore, service: str, now: float) -> str:
    """'ok' | 'expiring' | 'expired' | 'missing'. Never returns the token."""
    try:
        store.get(service, "access_token")
    except SecretNotFound:
        return "missing"
    try:
        expires_at = int(store.get(service, "expires_at"))
    except (SecretNotFound, ValueError):
        return "ok"  # no expiry recorded
    remaining = expires_at - now
    if remaining <= 0:
        return "expired"
    if remaining < TOKEN_WARN_SECONDS:
        return "expiring"
    return "ok"


def _outcomes(conn: sqlite3.Connection, post_id: int) -> list[str]:
    rows = conn.execute("SELECT outcome FROM publish_log WHERE post_id = ? ORDER BY id", (post_id,))
    return [r["outcome"] for r in rows.fetchall()]


def _ig_published_last_24h(conn: sqlite3.Connection, brand_key: str, now: datetime) -> int:
    threshold = (now - timedelta(hours=24)).strftime(_TS) + ".000Z"
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM publish_log pl JOIN posts p ON p.id = pl.post_id "
        "WHERE p.brand_key = ? AND pl.platform = 'instagram' AND pl.outcome = 'ok' AND pl.logged_at >= ?",
        (brand_key, threshold),
    ).fetchone()
    return int(row["n"])


def _dispatch(post: Post, account_id: str, graph: MetaGraph, media_base: str | None, sleep) -> str:
    caption = caption_text(post)
    url = media_url(media_base, post.visual_path)
    if post.platform == "facebook":
        if post.format == "video":
            return publish_fb_video(graph, account_id, url, caption)
        return publish_fb_photo(graph, account_id, url, caption)
    if post.platform == "instagram":
        if post.format == "video":
            return publish_ig_reel(graph, account_id, url, caption, sleep=sleep)
        return publish_ig_image(graph, account_id, url, caption, sleep=sleep)
    raise PublishBlocked(f"platform {post.platform} is not published by the Meta publisher")


def _account_problem(account_id: str | None) -> str | None:
    if not account_id or account_id.startswith("REPLACE_"):
        return "account_id not set in brands.toml"
    return None


def schedule_week(conn: sqlite3.Connection, cfg: Config, brand_key: str, week_start: date) -> list[int]:
    """Move a week's approved posts to scheduled. Publishing still checks the due time."""
    start, end = week_bounds_utc(week_start, cfg.timezone)
    approved = list_posts(conn, brand_key=brand_key, status="approved", scheduled_from=start, scheduled_to=end)
    for post in approved:
        transition(conn, post.id, "scheduled")
    return [p.id for p in approved]


def publish_due(
    conn: sqlite3.Connection,
    cfg: Config,
    store: SecretStore | None,
    guard: Guard,
    now: datetime,
    sleep=lambda s: None,
    transport: Transport = default_transport,
) -> list[PublishResult]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    now_utc = now.astimezone(timezone.utc)
    # scheduled_to is exclusive; add one second so a post due exactly now is included
    due_before = (now_utc + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    due = list_posts(conn, status="scheduled", scheduled_to=due_before)
    results: list[PublishResult] = []

    for post in due:
        brand = cfg.brands[post.brand_key]
        pc = brand.platforms.get(post.platform)
        if pc is None:
            results.append(PublishResult(post.id, "blocked", "platform not configured"))
            continue

        prior = _outcomes(conn, post.id)
        if "ok" in prior:
            transition(conn, post.id, "published")
            results.append(PublishResult(post.id, "skipped", "already published"))
            continue
        if any(o in ("attempt", "unknown") for o in prior):
            results.append(PublishResult(post.id, "blocked", "previous attempt unresolved; check Meta first"))
            continue

        problem = _account_problem(pc.account_id)
        if problem:
            results.append(PublishResult(post.id, "blocked", problem))
            continue

        if guard.dry_run:
            guard.write(f"publish post {post.id} to {post.brand_key} {post.platform} ({post.format})")
            results.append(PublishResult(post.id, "would_publish"))
            continue

        service = service_name(post.brand_key, post.platform)
        if store is None:
            raise RuntimeError("live publishing needs a secret store")
        status = token_status(store, service, now_utc.timestamp())
        if status in ("missing", "expired"):
            results.append(PublishResult(post.id, "blocked", f"Meta token {status}; reconnect"))
            continue

        if post.platform == "instagram" and _ig_published_last_24h(conn, post.brand_key, now_utc) >= IG_DAILY_LIMIT:
            results.append(PublishResult(post.id, "blocked", "Instagram 24h publish limit reached"))
            continue

        if not guard.write(f"publish post {post.id} to {post.brand_key} {post.platform}"):
            results.append(PublishResult(post.id, "would_publish"))
            continue

        with conn:
            cur = conn.execute(
                "INSERT INTO publish_log (post_id, platform, outcome) VALUES (?, ?, 'attempt')",
                (post.id, post.platform),
            )
            log_id = cur.lastrowid

        try:
            token = store.get(service, "access_token")
            graph = MetaGraph(token, cfg.meta_api_version, transport)
            remote_id = _dispatch(post, pc.account_id, graph, cfg.media_base_url, sleep)
        except PublishBlocked as exc:
            _close_attempt(conn, log_id, "failed", str(exc))
            results.append(PublishResult(post.id, "blocked", str(exc)))
            continue
        except GraphError as exc:
            if exc.ambiguous:
                _close_attempt(conn, log_id, "unknown", str(exc))
                results.append(PublishResult(post.id, "unknown", str(exc)))
            else:
                _close_attempt(conn, log_id, "failed", str(exc))
                _mark_failed(conn, post.id)
                results.append(PublishResult(post.id, "failed", str(exc)))
            continue

        _close_attempt(conn, log_id, "ok", remote_id)
        transition(conn, post.id, "published")
        results.append(PublishResult(post.id, "published", remote_id))
    return results


def _close_attempt(conn: sqlite3.Connection, log_id: int, outcome: str, detail: str) -> None:
    """Record the result. For ok, `detail` is the Meta object id; otherwise it is an error summary."""
    with conn:
        conn.execute(
            "UPDATE publish_log SET outcome = ?, remote_id = ?, detail = ? WHERE id = ?",
            (outcome, detail if outcome == "ok" else None, None if outcome == "ok" else detail, log_id),
        )


def _mark_failed(conn: sqlite3.Connection, post_id: int) -> None:
    with conn:
        conn.execute("UPDATE posts SET retry_count = retry_count + 1 WHERE id = ?", (post_id,))
    transition(conn, post_id, "failed")

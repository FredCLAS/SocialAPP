"""Webinar promotion: attaches the three weeks before an event to the weekly webinar slots.

Design:
- Promo posts reuse the weekly `webinar_cta` slot (Facebook and Instagram), so they never collide
  with the generated post times. Each promo post is a normal draft and goes through QA and approval.
- Registration page must be on the brand's own website (host the MailerLite form there).
  Calendly is allowed for the booking link.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from .config import Config
from .content_engine import week_bounds_utc
from .post_queue import list_posts
from .youtube import UPLOAD_DEADLINE

PROMO_PLATFORMS = ("facebook", "instagram")
PROMO_WINDOW = timedelta(days=21)
ALLOWED_CALENDLY = "calendly.com"


def _https_host(url: str) -> str | None:
    p = urlparse(url)
    return p.hostname if p.scheme == "https" and p.hostname else None


def create_webinar(conn: sqlite3.Connection, cfg: Config, brand_key: str, title: str, starts_local: datetime,
                   registration_url: str, calendly_url: str, duration_minutes: int = 60) -> int:
    brand = cfg.brands[brand_key]
    if starts_local.tzinfo is None:
        starts_local = starts_local.replace(tzinfo=cfg.timezone)
    if _https_host(registration_url) != urlparse(brand.website).hostname:
        raise ValueError(f"registration page must be on {urlparse(brand.website).hostname}")
    if _https_host(calendly_url) != ALLOWED_CALENDLY:
        raise ValueError("calendly_url must be an https calendly.com link")
    if not 15 <= duration_minutes <= 240:
        raise ValueError("duration must be 15 to 240 minutes")
    starts_utc = starts_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        cur = conn.execute(
            "INSERT INTO webinars (brand_key, title, starts_at, duration_minutes, registration_url, calendly_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brand_key, title.strip()[:120], starts_utc, duration_minutes, registration_url, calendly_url),
        )
    return cur.lastrowid


def _week_starts(first_day: date, last_day: date) -> list[date]:
    start = first_day - timedelta(days=first_day.weekday())
    weeks = []
    while start <= last_day:
        weeks.append(start)
        start += timedelta(days=7)
    return weeks


def attach_promotions(conn: sqlite3.Connection, cfg: Config, webinar_id: int) -> dict:
    w = conn.execute("SELECT * FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    if w is None or w["status"] == "cancelled":
        return {"attached": 0, "note": "webinar missing or cancelled"}
    starts = datetime.fromisoformat(w["starts_at"].replace("Z", "+00:00"))
    window_start = (starts - PROMO_WINDOW).astimezone(cfg.timezone).date()
    window_end = (starts - timedelta(days=1)).astimezone(cfg.timezone).date()
    attached, missing_weeks = 0, []
    for monday in _week_starts(window_start, window_end):
        start, end = week_bounds_utc(monday, cfg.timezone)
        slots = [p for p in list_posts(conn, brand_key=w["brand_key"], status="draft",
                                       scheduled_from=start, scheduled_to=end)
                 if p.content_type == "webinar_cta" and p.platform in PROMO_PLATFORMS
                 and p.scheduled_at >= (starts - PROMO_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
                 and p.scheduled_at < starts.strftime("%Y-%m-%dT%H:%M:%SZ")]
        free = [p for p in slots if _unattached(conn, p.id)]
        if not free:
            missing_weeks.append(monday.isoformat())
        for p in free:
            when = datetime.fromisoformat(p.scheduled_at.replace("Z", "+00:00")).astimezone(cfg.timezone)
            with conn:
                conn.execute(
                    "UPDATE posts SET webinar_id=?, subject=?, cta_url=? WHERE id=?",
                    (webinar_id, f"Webinar: {w['title']} ({when:%b %d})", w["registration_url"], p.id),
                )
            attached += 1
    if attached:
        with conn:
            conn.execute("UPDATE webinars SET status='promoting' WHERE id=? AND status='planned'", (webinar_id,))
    note = "" if not missing_weeks else f"no free webinar slot in week(s) of {', '.join(missing_weeks)}"
    return {"attached": attached, "note": note}


def _unattached(conn: sqlite3.Connection, post_id: int) -> bool:
    return conn.execute("SELECT webinar_id FROM posts WHERE id = ?", (post_id,)).fetchone()[0] is None


def overdue(conn: sqlite3.Connection, webinar_id: int, now: datetime) -> bool:
    w = conn.execute("SELECT * FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    if w is None or w["replay_youtube_id"] or w["status"] in ("cancelled",):
        return False
    starts = datetime.fromisoformat(w["starts_at"].replace("Z", "+00:00"))
    deadline = starts + timedelta(minutes=w["duration_minutes"]) + UPLOAD_DEADLINE
    return now.astimezone(timezone.utc) > deadline


def set_status(conn: sqlite3.Connection, webinar_id: int, status: str) -> None:
    allowed = {"live", "replay_pending", "cancelled"}
    if status not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    with conn:
        conn.execute("UPDATE webinars SET status=? WHERE id=?", (status, webinar_id))


def set_replay_file(conn: sqlite3.Connection, webinar_id: int, rel_path: str, chapters: list[list[str]]) -> None:
    import json
    with conn:
        conn.execute("UPDATE webinars SET replay_path=?, chapters=?, status=CASE WHEN status = 'live' "
                     "THEN 'replay_pending' ELSE status END WHERE id=?",
                     (rel_path, json.dumps(chapters), webinar_id))

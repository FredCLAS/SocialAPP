"""YouTube replay upload (private first), visibility gate, and quota ledger.

Quota (YouTube Data API, default 10,000 units per project per day, resets at Pacific midnight):
- videos.insert = 1,600 units; videos.update = 50 units.
- The ledger refuses new spend once the day reaches 90% of the limit.
- Uploads are private. Making a video public needs an approved 'visibility' gate.
- Every upload is recorded in the ledger before the call, so a failed call still counts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .config import Config
from .gates import is_approved, record_decision
from .guard import Guard
from .secrets import SecretNotFound, SecretStore

API = "youtube"
DAILY_QUOTA = 10_000
SAFETY_FRACTION = 0.9
COST = {"videos.insert": 1600, "videos.update": 50}
UPLOAD_DEADLINE = timedelta(hours=48)
PACIFIC = ZoneInfo("America/Los_Angeles")
SERVICE = "socialops.youtube"
CHAPTER_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


class QuotaError(RuntimeError):
    pass


def quota_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).strftime("%Y-%m-%d")


def used_today(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(units), 0) FROM quota_ledger WHERE api = ? AND day = ?",
                       (API, day)).fetchone()
    return int(row[0])


def can_spend(conn: sqlite3.Connection, op: str, now: datetime) -> bool:
    limit = DAILY_QUOTA * SAFETY_FRACTION
    return used_today(conn, quota_day(now)) + COST[op] <= limit


def record_spend(conn: sqlite3.Connection, op: str, now: datetime) -> None:
    if not can_spend(conn, op, now):
        raise QuotaError(f"quota reserve reached for {quota_day(now)}; {op} refused")
    with conn:
        conn.execute("INSERT INTO quota_ledger (api, operation, units, day) VALUES (?, ?, ?, ?)",
                     (API, op, COST[op], quota_day(now)))


def validate_chapters(chapters: list[list[str]]) -> list[str]:
    problems = []
    if len(chapters) < 3:
        problems.append("need at least 3 chapters")
    if chapters and chapters[0][0] != "00:00":
        problems.append("first chapter must start at 00:00")
    seconds = []
    for stamp, _title in chapters:
        m = CHAPTER_RE.match(stamp)
        if not m:
            problems.append(f"bad timestamp {stamp!r}")
            continue
        seconds.append(int(m.group(1)) * 60 + int(m.group(2)))
    if seconds != sorted(seconds) or len(set(seconds)) != len(seconds):
        problems.append("chapter timestamps must strictly increase")
    return problems


def build_description(summary: str, chapters: list[list[str]], calendly_url: str) -> str:
    lines = [summary.strip(), "", "Chapters:"]
    lines += [f"{stamp} {title}" for stamp, title in chapters]
    lines += ["", f"Book a call: {calendly_url}"]
    return "\n".join(lines)


def build_metadata(title: str, description: str, category_id: str) -> dict:
    return {
        "snippet": {"title": title[:100], "description": description[:5000], "categoryId": category_id},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }


def replay_path_problem(rel: str | None) -> str | None:
    if not rel:
        return "replay file not set"
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return "replay path must be relative and inside the asset folder"
    if p.suffix.lower() not in (".mp4", ".mov"):
        return "replay must be .mp4 or .mov"
    return None


@dataclass
class UploadResult:
    status: str  # uploaded | would_upload | blocked | skipped | failed
    detail: str = ""


class YouTubeHttp:
    """Resumable upload over urllib. Tests replace this with a fake."""

    def start(self, metadata: dict, size: int, token: str) -> str:
        url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
        req = urllib.request.Request(
            url, data=json.dumps(metadata).encode("utf-8"), method="POST",
            headers={"authorization": f"Bearer {token}", "content-type": "application/json; charset=UTF-8",
                     "x-upload-content-length": str(size), "x-upload-content-type": "video/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                location = resp.headers.get("Location")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"upload start failed: HTTP {exc.code}") from None
        if not location:
            raise RuntimeError("upload start returned no session URL")
        return location

    def send(self, session_url: str, data: bytes, token: str) -> str:
        req = urllib.request.Request(session_url, data=data, method="PUT",
                                     headers={"authorization": f"Bearer {token}", "content-type": "video/*"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"upload failed: HTTP {exc.code}") from None
        return body["id"]

    def set_privacy(self, video_id: str, privacy: str, token: str) -> None:
        url = "https://www.googleapis.com/youtube/v3/videos?part=status"
        payload = json.dumps({"id": video_id, "status": {"privacyStatus": privacy}}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="PUT",
                                     headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60):
                return None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"visibility change failed: HTTP {exc.code}") from None


def access_token(store: SecretStore, cfg: Config) -> str:
    try:
        return store.get(SERVICE, "access_token")
    except SecretNotFound as exc:
        raise RuntimeError("YouTube not connected on this Mac (no access token in Keychain)") from exc


def upload_replay(conn: sqlite3.Connection, cfg: Config, webinar_id: int, uploader, store, guard: Guard,
                  now: datetime, root: Path | None = None) -> UploadResult:
    w = conn.execute("SELECT * FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    if w is None:
        return UploadResult("failed", "no such webinar")
    if w["status"] == "cancelled":
        return UploadResult("skipped", "webinar cancelled")
    if w["replay_youtube_id"]:
        return UploadResult("skipped", "replay already uploaded (private)")
    problem = replay_path_problem(w["replay_path"])
    if problem:
        return UploadResult("blocked", problem)
    if cfg.youtube is None or not cfg.youtube.client_id or cfg.youtube.client_id.startswith("REPLACE_"):
        return UploadResult("blocked", "youtube client_id not set in brands.toml")
    chapters = json.loads(w["chapters"] or "[]")
    chapter_problems = validate_chapters(chapters)
    if chapter_problems:
        return UploadResult("blocked", "; ".join(chapter_problems))

    starts = datetime.fromisoformat(w["starts_at"].replace("Z", "+00:00"))
    deadline = starts + timedelta(minutes=w["duration_minutes"]) + UPLOAD_DEADLINE
    if now.astimezone(timezone.utc) > deadline:
        return UploadResult("blocked", "OVERDUE: 48-hour replay deadline has passed")
    if not can_spend(conn, "videos.insert", now):
        return UploadResult("blocked", "quota reserve reached today; try tomorrow")

    if not guard.write(f"upload replay of webinar {webinar_id} to YouTube (private)"):
        return UploadResult("would_upload", "dry-run")

    description = build_description(f"Replay: {w['title']}.", chapters, w["calendly_url"])
    metadata = build_metadata(f"{w['title']} (Replay)", description, cfg.youtube.category_id)
    token = access_token(store, cfg)
    file_path = (root or Path.home() / "Library" / "Application Support" / "SocialOps") / w["replay_path"]
    if not file_path.is_file():
        return UploadResult("blocked", "replay file not found on this Mac")
    data = file_path.read_bytes()

    record_spend(conn, "videos.insert", now)  # counted before the call: a failed call still costs
    try:
        session = uploader.start(metadata, len(data), token)
        video_id = uploader.send(session, data, token)
    except RuntimeError as exc:
        return UploadResult("failed", f"{exc}; not retried automatically")
    with conn:
        conn.execute("UPDATE webinars SET replay_youtube_id=?, replay_uploaded_at=?, status='replay_uploaded' "
                     "WHERE id=?", (video_id, now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), webinar_id))
    return UploadResult("uploaded", video_id)


def approve_public(conn: sqlite3.Connection, webinar_id: int, actor: str) -> str:
    row = conn.execute("SELECT status, replay_youtube_id FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    if row is None or row["status"] != "replay_uploaded":
        return "not approved: replay must be uploaded (private) first"
    record_decision(conn, "visibility", webinar_id, actor, "approved")
    return "approved: public visibility allowed once"


def publish_public(conn: sqlite3.Connection, webinar_id: int, uploader, store, guard: Guard, now: datetime) -> UploadResult:
    row = conn.execute("SELECT * FROM webinars WHERE id = ?", (webinar_id,)).fetchone()
    if row is None or row["status"] != "replay_uploaded" or not row["replay_youtube_id"]:
        return UploadResult("blocked", "replay is not uploaded (private)")
    if not is_approved(conn, "visibility", webinar_id):
        return UploadResult("blocked", "needs a visibility approval first (replay-approve-public)")
    if not can_spend(conn, "videos.update", now):
        return UploadResult("blocked", "quota reserve reached today; try tomorrow")
    if not guard.write(f"make YouTube replay {row['replay_youtube_id']} public"):
        return UploadResult("would_upload", "dry-run")
    token = access_token(store, None)
    record_spend(conn, "videos.update", now)
    try:
        uploader.set_privacy(row["replay_youtube_id"], "public", token)
    except RuntimeError as exc:
        return UploadResult("failed", str(exc))
    with conn:
        conn.execute("UPDATE webinars SET status='replay_public' WHERE id=?", (webinar_id,))
    return UploadResult("uploaded", "public")

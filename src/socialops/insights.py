"""Insights (read-only) and the weekly report.

- Metric names are config-driven and UNVERIFIED against Meta's current docs. Meta has renamed
  insight metrics between API versions. Check them before relying on the numbers.
- Insights pull needs live mode (tokens). Dry-run reads nothing.
- The report is deterministic text (no LLM). It is saved to the reports table and to a file.
- Boost: posts that pass 300, 1,000, or 10,000 views are listed as candidates. Nothing is boosted or
  spent. A boost needs a 'boost' approval, which is not built yet.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .agents import AgentResult, Context
from .config import Config
from .content_engine import week_bounds_utc
from .guard import Guard
from .meta_graph import GraphError, MetaGraph, Transport, default_transport
from .post_queue import list_posts
from .secrets import SecretNotFound, SecretStore, service_name
from .webinar import overdue
from .youtube import quota_day, used_today

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "SocialOps"
LOOKBACK_DAYS = 30
BOOST_MILESTONES = (300, 1_000, 10_000)
VIEW_METRICS = {"facebook": "post_impressions", "instagram": "reach"}
METRICS = {
    "facebook": ("post_impressions", "post_engaged_users"),
    "instagram": ("reach", "saved", "shares"),
}


def pull_insights(conn: sqlite3.Connection, cfg: Config, brand_key: str, store: SecretStore | None,
                  guard: Guard, now: datetime, transport: Transport = default_transport) -> AgentResult:
    if guard.dry_run or store is None:
        return AgentResult("skipped", "dry-run: insights pull needs live mode (read-only, needs tokens)")
    since = (now - timedelta(days=LOOKBACK_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT p.id, p.platform, pl.remote_id FROM posts p JOIN publish_log pl ON pl.post_id = p.id "
        "WHERE p.brand_key = ? AND pl.outcome = 'ok' AND pl.remote_id IS NOT NULL AND p.scheduled_at >= ?",
        (brand_key, since),
    ).fetchall()
    stored, errors = 0, []
    for row in rows:
        try:
            token = store.get(service_name(brand_key, row["platform"]), "access_token")
        except SecretNotFound:
            errors.append(f"{row['platform']}: no token")
            continue
        graph = MetaGraph(token, cfg.meta_api_version, transport)
        metrics = ",".join(METRICS[row["platform"]])
        try:
            body = graph.get(f"{row['remote_id']}/insights", {"metric": metrics})
        except GraphError as exc:
            errors.append(f"post {row['id']}: {exc}")
            continue
        for item in body.get("data", []):
            values = item.get("values") or [{}]
            value = values[-1].get("value")
            if value is None:
                continue
            with conn:
                conn.execute("INSERT INTO insights (post_id, metric, value) VALUES (?, ?, ?)",
                             (row["id"], item.get("name", ""), float(value)))
            stored += 1
    reason = f"stored {stored} values for {len(rows)} posts"
    if errors:
        reason += " | " + "; ".join(errors[:5])
    return AgentResult("ok" if not errors else "blocked", reason)


def latest_views(conn: sqlite3.Connection, post_id: int, platform: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM insights WHERE post_id = ? AND metric = ? ORDER BY id DESC LIMIT 1",
        (post_id, VIEW_METRICS[platform]),
    ).fetchone()
    return row["value"] if row else None


def boost_candidates(conn: sqlite3.Connection, brand_key: str) -> list[tuple[int, str, float, int]]:
    out = []
    for post in conn.execute("SELECT id, platform, subject FROM posts WHERE brand_key = ? AND status = 'published'",
                             (brand_key,)).fetchall():
        views = latest_views(conn, post["id"], post["platform"])
        if views is None:
            continue
        passed = [m for m in BOOST_MILESTONES if views > m]
        if passed:
            out.append((post["id"], post["subject"], views, passed[-1]))
    return sorted(out, key=lambda r: -r[2])


def build_report(conn: sqlite3.Connection, cfg: Config, brand_key: str, week: date, now: datetime,
                 store: SecretStore | None = None, guard: Guard | None = None) -> str:
    brand = cfg.brands[brand_key]
    start, end = week_bounds_utc(week, cfg.timezone)
    posts = list_posts(conn, brand_key=brand_key, scheduled_from=start, scheduled_to=end)
    lines = [f"# {brand.display_name}: week of {week.isoformat()}", "",
             f"Generated {now.astimezone(cfg.timezone):%Y-%m-%d %H:%M %Z}. Read-only report; nothing was posted or spent.", ""]

    lines += ["## Plan vs. published", "", "| Platform | Planned | Published | Failed | Still draft | Approved or scheduled |",
              "|---|---|---|---|---|---|"]
    for platform, pc in brand.platforms.items():
        if pc.per_week == 0:
            continue
        mine = [p for p in posts if p.platform == platform]
        count = lambda *st: sum(1 for p in mine if p.status in st)  # noqa: E731
        lines.append(f"| {platform} | {len(mine)} | {count('published')} | {count('failed')} | "
                     f"{count('draft')} | {count('approved', 'scheduled')} |")
    lines.append("")

    drafts = [p for p in posts if p.status == "draft"]
    lines += ["## Approvals and QA", ""]
    lines.append(f"- Drafts waiting for review: {len(drafts)}")
    qa_failed = conn.execute(
        "SELECT COUNT(*) FROM (SELECT post_id, passed, ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY id DESC) rn "
        "FROM qa_results WHERE post_id IN (SELECT id FROM posts WHERE brand_key=?)) WHERE rn=1 AND passed=0",
        (brand_key,)).fetchone()[0]
    lines.append(f"- Latest QA failing: {qa_failed}")
    lines.append("")

    lines += ["## Replies and comments", ""]
    for row in conn.execute("SELECT reply_status, COUNT(*) n FROM inbound_items WHERE brand_key=? GROUP BY reply_status",
                            (brand_key,)).fetchall():
        lines.append(f"- {row['reply_status']}: {row['n']}")
    escalated = conn.execute("SELECT COUNT(*) FROM inbound_items WHERE brand_key=? AND escalate=1 "
                             "AND reply_status='none'", (brand_key,)).fetchone()[0]
    lines.append(f"- Escalated to a human (open): {escalated}")
    lines.append("")

    lines += ["## Webinars and replays", ""]
    webinars = conn.execute("SELECT * FROM webinars WHERE brand_key=? ORDER BY starts_at", (brand_key,)).fetchall()
    if not webinars:
        lines.append("- No webinars.")
    for w in webinars:
        flag = " OVERDUE" if overdue(conn, w["id"], now) else ""
        lines.append(f"- {w['title']} ({w['starts_at']}): {w['status']}{flag}")
    day = quota_day(now)
    lines += ["", f"- YouTube quota today: {used_today(conn, day)} units used (Pacific {day})", ""]

    lines += ["## Videos (Zoice queue)", ""]
    for row in conn.execute("SELECT status, COUNT(*) n FROM video_jobs WHERE brand_key=? GROUP BY status",
                            (brand_key,)).fetchall():
        lines.append(f"- {row['status']}: {row['n']}")
    lines.append("")

    lines += ["## Token status", ""]
    if store is None:
        lines.append("- Not checked (dry-run or no secret store).")
    else:
        from .publisher import token_status
        for platform in ("facebook", "instagram"):
            if platform not in brand.platforms:
                continue
            state = token_status(store, service_name(brand_key, platform), now.timestamp())
            lines.append(f"- {platform}: {state}")
    lines.append("")

    lines += ["## Top posts by views (latest pull)", ""]
    cands = boost_candidates(conn, brand_key)
    if not cands:
        lines.append("- No insight data yet. Run insights-pull (live).")
    for post_id, subject, views, milestone in cands[:10]:
        lines.append(f"- post {post_id} \"{subject}\": {int(views)} views (passed {milestone:,})")
    if cands:
        lines += ["", "Boost candidates above need your approval. Nothing is boosted or spent automatically."]
    lines.append("")
    return "\n".join(lines)


def save_report(conn: sqlite3.Connection, brand_key: str, week: date, body: str,
                root: Path = DEFAULT_ROOT) -> Path:
    folder = root / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{brand_key}_{week.isoformat()}.md"
    path.write_text(body, encoding="utf-8")
    with conn:
        conn.execute("INSERT INTO reports (brand_key, week_start, body) VALUES (?, ?, ?)",
                     (brand_key, week.isoformat(), body))
    return path

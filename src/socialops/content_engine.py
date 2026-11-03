"""Turn the weekly cadence in brands.toml into draft post slots.

M2 scope: cadence math and draft records only. Captions, hashtags, and visuals are
filled in later (Claude API drafts in M2b, Canva in M5), so generated posts stay
drafts and cannot be approved until they are complete (see post_queue).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .config import Brand, Config, PlatformCadence

DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class PlannedSlot:
    brand_key: str
    platform: str
    content_type: str
    format: str  # "image" | "video"
    scheduled_at: str  # UTC, ISO 8601 with Z


def require_monday(week_start: date) -> None:
    if week_start.weekday() != 0:
        raise ValueError(f"week_start must be a Monday, got {week_start.isoformat()}")


def video_indices(total: int, videos: int) -> set[int]:
    """Spread `videos` slots evenly across `total` slots. 7 total, 3 video -> {0, 2, 4}."""
    if videos <= 0 or total <= 0:
        return set()
    return {(k * total) // videos for k in range(videos)}


def _content_types(pc: PlatformCadence) -> list[str]:
    types: list[str] = []
    for name, count in pc.mix:
        types.extend([name] * count)
    return types


def _utc_iso(day: date, hhmm: str, tz) -> str:
    hour, minute = (int(x) for x in hhmm.split(":"))
    local = datetime.combine(day, time(hour, minute), tzinfo=tz)
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_platform(cfg: Config, brand: Brand, platform: str, week_start: date) -> list[PlannedSlot]:
    require_monday(week_start)
    pc = brand.platforms.get(platform)
    if pc is None or pc.per_week == 0:
        return []
    types = _content_types(pc)
    videos = video_indices(len(types), pc.video_per_week)
    slots: list[PlannedSlot] = []
    for i, content_type in enumerate(types):
        day_name = pc.days[i]
        day = week_start + timedelta(days=DAY_INDEX[day_name])
        slots.append(
            PlannedSlot(
                brand_key=brand.key,
                platform=platform,
                content_type=content_type,
                format="video" if i in videos else "image",
                scheduled_at=_utc_iso(day, pc.post_time, cfg.timezone),
            )
        )
    return slots


def plan_week(cfg: Config, week_start: date) -> list[PlannedSlot]:
    require_monday(week_start)
    out: list[PlannedSlot] = []
    for brand in cfg.brands.values():
        for platform in brand.platforms:
            out.extend(plan_platform(cfg, brand, platform, week_start))
    return out


def _ensure_brands(conn: sqlite3.Connection, cfg: Config) -> None:
    for brand in cfg.brands.values():
        conn.execute(
            "INSERT INTO brands (key, display_name, website) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET display_name = excluded.display_name, website = excluded.website",
            (brand.key, brand.display_name, brand.website),
        )


def generate_week(conn: sqlite3.Connection, cfg: Config, week_start: date) -> dict[str, int]:
    """Insert draft posts for the week. Safe to re-run: existing slots are left alone.

    Returns {"planned": n, "inserted": n, "existing": n}.
    """
    slots = plan_week(cfg, week_start)
    inserted = 0
    with conn:
        _ensure_brands(conn, cfg)
        for slot in slots:
            subject = f"{cfg.brands[slot.brand_key].display_name} {slot.content_type} {slot.scheduled_at[:10]}"
            cur = conn.execute(
                "INSERT OR IGNORE INTO posts "
                "(brand_key, platform, subject, scheduled_at, status, content_type, format) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?)",
                (slot.brand_key, slot.platform, subject, slot.scheduled_at, slot.content_type, slot.format),
            )
            inserted += cur.rowcount
    return {"planned": len(slots), "inserted": inserted, "existing": len(slots) - inserted}


def week_bounds_utc(week_start: date, tz) -> tuple[str, str]:
    """[start, end) of the local week, as UTC ISO strings for scheduled_at comparisons."""
    require_monday(week_start)
    start = datetime.combine(week_start, time(0, 0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(week_start + timedelta(days=7), time(0, 0), tzinfo=tz).astimezone(UTC)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)

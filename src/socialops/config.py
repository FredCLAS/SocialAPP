"""Load and validate the non-secret brand configuration (config/brands.toml)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Brands that must never be configured (spec: no profiles for these).
EXCLUDED_BRANDS = frozenset({"bluepal_fox", "compliance_without_panic"})

# Platforms each brand may use (spec: pamela_art is FB + IG only).
ALLOWED_PLATFORMS: dict[str, frozenset[str]] = {
    "clas_by_fred": frozenset({"facebook", "instagram", "linkedin", "youtube"}),
    "pamela_art": frozenset({"facebook", "instagram"}),
}

VALID_DAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(ValueError):
    """Raised when brands.toml is missing, malformed, or violates a rule."""


@dataclass(frozen=True)
class PlatformCadence:
    platform: str
    per_week: int
    video_per_week: int
    days: tuple[str, ...]
    post_time: str
    manual_post: bool = False
    upload_deadline_hours: int | None = None


@dataclass(frozen=True)
class Brand:
    key: str
    display_name: str
    website: str
    assets_subdir: str
    platforms: dict[str, PlatformCadence]
    outreach_sender: str | None = None


@dataclass(frozen=True)
class Footer:
    company: str
    postal_address: str


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    dry_run: bool
    footer: Footer | None
    brands: dict[str, Brand] = field(default_factory=dict)

    def brand(self, key: str) -> Brand:
        try:
            return self.brands[key]
        except KeyError as exc:
            raise ConfigError(f"unknown brand: {key}") from exc


def _require(table: dict, key: str, where: str) -> object:
    if key not in table:
        raise ConfigError(f"{where}: missing required key '{key}'")
    return table[key]


def _parse_platform(brand_key: str, platform: str, raw: dict) -> PlatformCadence:
    where = f"brands.{brand_key}.platforms.{platform}"
    if platform not in ALLOWED_PLATFORMS[brand_key]:
        raise ConfigError(f"{where}: platform not allowed for {brand_key}")

    per_week = int(_require(raw, "per_week", where))
    video_per_week = int(raw.get("video_per_week", 0))
    days = tuple(raw.get("days", ()))
    post_time = str(_require(raw, "post_time", where))

    if per_week < 0:
        raise ConfigError(f"{where}: per_week must be >= 0")
    if not 0 <= video_per_week <= per_week:
        raise ConfigError(f"{where}: video_per_week must be between 0 and per_week")
    bad_days = set(days) - VALID_DAYS
    if bad_days:
        raise ConfigError(f"{where}: invalid days {sorted(bad_days)}")
    if not _TIME_RE.match(post_time):
        raise ConfigError(f"{where}: post_time must be HH:MM (24h), got '{post_time}'")
    if per_week != len(days):
        raise ConfigError(f"{where}: days must list exactly per_week entries")

    deadline = raw.get("upload_deadline_hours")
    return PlatformCadence(
        platform=platform,
        per_week=per_week,
        video_per_week=video_per_week,
        days=days,
        post_time=post_time,
        manual_post=bool(raw.get("manual_post", False)),
        upload_deadline_hours=int(deadline) if deadline is not None else None,
    )


def _parse_brand(key: str, raw: dict) -> Brand:
    where = f"brands.{key}"
    if key in EXCLUDED_BRANDS:
        raise ConfigError(f"{where}: excluded brand must not be configured")
    if key not in ALLOWED_PLATFORMS:
        raise ConfigError(f"{where}: unknown brand")

    sender = raw.get("outreach_sender")
    if sender is not None:
        if key != "clas_by_fred":
            raise ConfigError(f"{where}: only clas_by_fred may have an outreach sender")
        if not _EMAIL_RE.match(sender):
            raise ConfigError(f"{where}: outreach_sender is not an email address")
        if not sender.endswith("@classbyfred.com"):
            raise ConfigError(f"{where}: outreach_sender must be on classbyfred.com")

    platforms_raw = raw.get("platforms", {})
    platforms = {
        name: _parse_platform(key, name, cfg) for name, cfg in platforms_raw.items()
    }
    return Brand(
        key=key,
        display_name=str(_require(raw, "display_name", where)),
        website=str(_require(raw, "website", where)),
        assets_subdir=str(_require(raw, "assets_subdir", where)),
        platforms=platforms,
        outreach_sender=sender,
    )


def parse_config(data: dict) -> Config:
    try:
        tz = ZoneInfo(str(_require(data, "timezone", "root")))
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"invalid timezone: {data.get('timezone')}") from exc

    footer_raw = data.get("footer")
    footer = None
    if footer_raw is not None:
        footer = Footer(
            company=str(_require(footer_raw, "company", "footer")),
            postal_address=str(_require(footer_raw, "postal_address", "footer")),
        )

    brands = {
        key: _parse_brand(key, raw) for key, raw in data.get("brands", {}).items()
    }

    for key, brand in brands.items():
        if brand.outreach_sender and footer is None:
            raise ConfigError(f"brands.{key}: outreach requires a [footer] postal address")

    return Config(
        timezone=tz,
        dry_run=bool(data.get("dry_run", True)),
        footer=footer,
        brands=brands,
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(data)

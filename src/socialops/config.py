"""Load and validate the non-secret brand configuration (config/brands.toml)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .oauth import CanvaOAuthConfig

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
    mix: tuple[tuple[str, int], ...] = ()  # ordered (content_type, count); sums to per_week
    manual_post: bool = False
    upload_deadline_hours: int | None = None
    account_id: str | None = None  # Facebook Page id or Instagram business account id (not secret)


@dataclass(frozen=True)
class Brand:
    key: str
    display_name: str
    website: str
    assets_subdir: str
    platforms: dict[str, PlatformCadence]
    outreach_sender: str | None = None
    contact_email: str | None = None


@dataclass(frozen=True)
class Footer:
    company: str
    postal_address: str


@dataclass(frozen=True)
class LlmConfig:
    model: str = "claude-haiku-5-5"
    max_tokens: int = 800
    max_drafts_per_run: int = 25


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    dry_run: bool
    footer: Footer | None
    brands: dict[str, Brand] = field(default_factory=dict)
    llm: LlmConfig = field(default_factory=LlmConfig)
    canva: CanvaOAuthConfig | None = None
    meta_api_version: str = "v21.0"
    media_base_url: str | None = None  # public HTTPS base where published media is reachable

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

    mix_raw = raw.get("mix")
    if mix_raw is None:
        mix = ((("standard", per_week),) if per_week else ())
    else:
        mix = tuple((str(name), int(count)) for name, count in mix_raw.items())
        if any(not name or count < 0 for name, count in mix):
            raise ConfigError(f"{where}: mix names must be non-empty and counts >= 0")
        if sum(count for _, count in mix) != per_week:
            raise ConfigError(f"{where}: mix counts must sum to per_week ({per_week})")

    deadline = raw.get("upload_deadline_hours")
    return PlatformCadence(
        platform=platform,
        per_week=per_week,
        video_per_week=video_per_week,
        days=days,
        post_time=post_time,
        mix=mix,
        manual_post=bool(raw.get("manual_post", False)),
        upload_deadline_hours=int(deadline) if deadline is not None else None,
        account_id=str(raw["account_id"]) if raw.get("account_id") is not None else None,
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

    contact = raw.get("contact_email")
    if contact is not None and not _EMAIL_RE.match(str(contact)):
        raise ConfigError(f"{where}: contact_email is not an email address")

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
        contact_email=str(contact) if contact is not None else None,
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

    llm_raw = data.get("llm", {})
    llm = LlmConfig(
        model=str(llm_raw.get("model", LlmConfig.model)),
        max_tokens=int(llm_raw.get("max_tokens", LlmConfig.max_tokens)),
        max_drafts_per_run=int(llm_raw.get("max_drafts_per_run", LlmConfig.max_drafts_per_run)),
    )
    if not llm.model.startswith("claude-"):
        raise ConfigError("llm.model must be a claude-* model id")
    if not 1 <= llm.max_tokens <= 4000:
        raise ConfigError("llm.max_tokens must be between 1 and 4000")
    if llm.max_drafts_per_run < 1:
        raise ConfigError("llm.max_drafts_per_run must be >= 1")

    canva = None
    canva_raw = data.get("canva")
    if canva_raw is not None:
        scopes = tuple(canva_raw.get("scopes", ()))
        if not scopes:
            raise ConfigError("canva.scopes must list at least one scope")
        canva = CanvaOAuthConfig(
            client_id=str(canva_raw.get("client_id", "")),
            scopes=scopes,
            authorize_url=str(_require(canva_raw, "authorize_url", "canva")),
            token_url=str(_require(canva_raw, "token_url", "canva")),
            redirect_port=int(canva_raw.get("redirect_port", 8765)),
        )

    meta_raw = data.get("meta", {})
    media_raw = data.get("media", {})
    media_base = media_raw.get("public_base_url") or None
    if media_base is not None and not str(media_base).startswith("https://"):
        raise ConfigError("media.public_base_url must be https")

    return Config(
        timezone=tz,
        dry_run=bool(data.get("dry_run", True)),
        footer=footer,
        brands=brands,
        llm=llm,
        canva=canva,
        meta_api_version=str(meta_raw.get("api_version", "v21.0")),
        media_base_url=str(media_base) if media_base else None,
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

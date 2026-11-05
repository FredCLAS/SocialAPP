"""Visual agent: fills the Canva brand template from a post, exports a PNG, and checks the file.

Canva API note: the endpoint paths below are from memory and were NOT verified against Canva's docs
(the docs host was unreachable when this was written). Live rendering refuses to run until
[canva] api_verified = true in brands.toml, which is set only after the paths are checked.

Asset checks (run on any image file a post points to) are stored in visual_checks.
A failed check for the current content blocks approval. Checks run by `visual-check`.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import Brand, Config
from .guard import Guard
from .post_queue import Post, fingerprint, get_post, list_posts, set_post_fields
from .content_engine import week_bounds_utc

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "SocialOps"
MIN_BYTES = 10_000
MAX_BYTES = 8 * 1024 * 1024        # Instagram image limit
MIN_SIDE = 600
MAX_SIDE = 8000
ASPECT_MIN = 4 / 5                 # 4:5 portrait
ASPECT_MAX = 1.91                  # 1.91:1 landscape
POLL_LIMIT = 30
POLL_SECONDS = 2.0


class RenderError(RuntimeError):
    pass


# --- file checks ---------------------------------------------------------------

def image_size(path: Path) -> tuple[int, int, str]:
    data = path.read_bytes()[:65536]
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "png"
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h, "jpeg"
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
        raise ValueError("no JPEG size marker found")
    raise ValueError("not a PNG or JPEG")


def check_asset(path: Path) -> list[str]:
    if not path.is_file():
        return ["file missing"]
    problems = []
    size = path.stat().st_size
    if size < MIN_BYTES:
        problems.append(f"file too small ({size} bytes)")
    if size > MAX_BYTES:
        problems.append("file over 8 MB")
    try:
        w, h, fmt = image_size(path)
    except ValueError as exc:
        return problems + [str(exc)]
    if min(w, h) < MIN_SIDE:
        problems.append(f"smallest side {min(w, h)}px is under {MIN_SIDE}px")
    if max(w, h) > MAX_SIDE:
        problems.append(f"largest side {max(w, h)}px is over {MAX_SIDE}px")
    ratio = w / h
    if not ASPECT_MIN <= ratio <= ASPECT_MAX:
        problems.append(f"aspect {w}x{h} is outside 4:5 to 1.91:1")
    return problems


# --- Canva client -----------------------------------------------------------------

class CanvaHttp:
    """Autofill then export, over HTTP. Paths are unverified (see module note)."""

    def __init__(self, api_base: str, token_provider, transport=None, sleep=time.sleep) -> None:
        self._base = api_base.rstrip("/")
        self._token = token_provider
        self._transport = transport or self._default
        self._sleep = sleep

    def _default(self, method: str, url: str, body: dict | None, token: str) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "authorization": f"Bearer {token}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RenderError(f"Canva returned HTTP {exc.code}") from None

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        return self._transport(method, f"{self._base}{path}", body, self._token())

    def _wait(self, path: str, extract) -> dict:
        for _ in range(POLL_LIMIT):
            job = self._call("GET", path).get("job", {})
            if job.get("status") == "success":
                return extract(job)
            if job.get("status") == "failed":
                raise RenderError(f"Canva job failed at {path}")
            self._sleep(POLL_SECONDS)
        raise RenderError("Canva job did not finish in time")

    def autofill(self, template_id: str, data: dict) -> str:
        job = self._call("POST", "/autofills", {"brand_template_id": template_id, "data": data})["job"]["id"]
        return self._wait(f"/autofills/{job}", lambda j: j["result"]["design"]["id"])

    def export_png(self, design_id: str) -> bytes:
        job = self._call("POST", "/exports", {"design_id": design_id, "format": {"type": "png"}})["job"]["id"]
        url = self._wait(f"/exports/{job}", lambda j: j["result"]["urls"][0])
        with urllib.request.urlopen(url, timeout=120) as resp:  # the export URL is time-limited
            return resp.read()


# --- render and check --------------------------------------------------------------

def template_fields(post: Post, brand: Brand) -> dict:
    hook = post.caption.strip().split(". ")[0].rstrip(".")
    attrs = {"subject": post.subject, "hook": hook, "cta": post.cta_url or ""}
    return {field: {"type": "text", "text": attrs[attr]} for field, attr in brand.canva_fields if attr in attrs}


@dataclass
class RenderResult:
    post_id: int
    status: str  # rendered | would_render | blocked | failed
    detail: str = ""


def render_post(conn: sqlite3.Connection, cfg: Config, post_id: int, canva: CanvaHttp | None,
                guard: Guard, root: Path = DEFAULT_ROOT) -> RenderResult:
    post = get_post(conn, post_id)
    brand = cfg.brands[post.brand_key]
    if post.status not in ("draft", "failed"):
        return RenderResult(post_id, "blocked", f"post is {post.status}; content is locked")
    if not brand.canva_template_id or brand.canva_template_id.startswith("REPLACE_"):
        return RenderResult(post_id, "blocked", "no Canva template configured for this brand")
    if cfg.canva is None or not cfg.canva.api_verified:
        return RenderResult(post_id, "blocked", "Canva endpoints not verified (set [canva] api_verified after checking)")
    if post.format != "image":
        return RenderResult(post_id, "blocked", "only image posts are rendered from the template")
    fields = template_fields(post, brand)
    if not fields:
        return RenderResult(post_id, "blocked", "template fields do not map to any post attribute")
    rel = f"assets/{brand.assets_subdir}/exports/post_{post_id}.png"
    if not guard.write(f"render Canva template for post {post_id} and save {rel}"):
        return RenderResult(post_id, "would_render", rel)
    if canva is None:
        raise RenderError("live rendering needs a Canva client")

    try:
        design = canva.autofill(brand.canva_template_id, fields)
        png = canva.export_png(design)
    except RenderError as exc:
        return RenderResult(post_id, "failed", f"{exc}; not retried automatically")
    if not png.startswith(b"\x89PNG"):
        return RenderResult(post_id, "failed", "Canva export was not a PNG")
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    set_post_fields(conn, post_id, visual_path=rel, visual_source="canva")
    problems = check_asset(target)
    record_check(conn, post_id, problems)
    if problems:
        return RenderResult(post_id, "failed", "rendered file failed asset check: " + "; ".join(problems))
    return RenderResult(post_id, "rendered", rel)


def record_check(conn: sqlite3.Connection, post_id: int, problems: list[str]) -> bool:
    post = get_post(conn, post_id)
    with conn:
        conn.execute("INSERT INTO visual_checks (post_id, fingerprint, passed, problems) VALUES (?, ?, ?, ?)",
                     (post_id, fingerprint(post), int(not problems), json.dumps(problems)))
    return not problems


def check_visuals(conn: sqlite3.Connection, cfg: Config, brand_key: str, week_start,
                  root: Path = DEFAULT_ROOT) -> list[tuple[int, list[str]]]:
    from .review import safe_asset_path
    start, end = week_bounds_utc(week_start, cfg.timezone)
    out = []
    for post in list_posts(conn, brand_key=brand_key, scheduled_from=start, scheduled_to=end):
        if post.format != "image" or not post.visual_path:
            continue
        target = safe_asset_path(root, post.visual_path, cfg.brands[brand_key].assets_subdir)
        problems = ["file missing, outside the brand folder, or unsafe path"] if target is None else check_asset(target)
        record_check(conn, post.id, problems)
        out.append((post.id, problems))
    return out

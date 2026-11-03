"""Draft captions and hashtags for draft posts with the Claude API.

Flow: build prompt -> (guard) -> call model -> parse JSON -> validate -> save as draft fields.
- Drafts never change status. A human still approves in post_queue.
- Existing captions are kept unless overwrite=True.
- Output that fails validation is not saved. There is no automatic retry, so cost stays bounded.
- In dry-run, no model call is made. The planned call is recorded and returned.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from .config import Brand, Config
from .content_engine import week_bounds_utc
from .guard import Guard
from .llm import DraftClient
from .post_queue import Post, get_post, list_posts

log = logging.getLogger("socialops.drafter")

MAX_CAPTION_CHARS = 2000
HASHTAG_RE = re.compile(r"^#[A-Za-z0-9]{2,40}$")
PLACEHOLDER_SUBJECT_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
BANNED_PHRASES = (
    "guarantee",  # matches guaranteed too
    "100%",
    "risk-free",
    "legal advice",
    "endorsed by",
    "certified by",
)
ALLOWED_EXTRA_HOSTS = frozenset({"calendly.com"})

SYSTEM_PROMPT = """You write social media captions for one small business.
Reply with only a JSON object with these keys:
  "caption": string, under 1800 characters
  "hashtags": array of 3 to 8 strings, each starting with # and using letters or digits only
  "cta_url": string, copied exactly from the URL given in the request

Caption structure:
1. One-sentence hook.
2. Two or three sentences of value.
3. A call to action that includes the cta_url text exactly.

Rules:
- Plain language. No promises of outcomes. Never use the words guarantee or guaranteed, or the phrase 100%.
- Do not invent statistics, clients, testimonials, or quotes.
- Do not say that anyone is legally compliant or certified.
- If the request says the visual is AI-generated, the caption must say "AI-generated" or "illustrative".
- Output only the JSON object. No markdown, no commentary."""

CONTENT_GUIDANCE = {
    "image_checklist": "A checklist-style tip. The value sentences name the checklist items.",
    "question": "The hook is an open question to the audience. The CTA invites a reply.",
    "webinar_cta": "Promote the upcoming monthly webinar. The CTA points to the registration link.",
    "art": "A post about one artwork or theme.",
    "standard": "A business update for owners in regulated sectors.",
}


class DraftRejected(ValueError):
    def __init__(self, post_id: int, problems: list[str]) -> None:
        self.post_id = post_id
        self.problems = problems
        super().__init__(f"post {post_id} draft rejected: {'; '.join(problems)}")


@dataclass
class DraftResult:
    post_id: int
    outcome: str  # skipped | would_draft | drafted | rejected
    problems: list[str] = field(default_factory=list)


@dataclass
class BatchSummary:
    results: list[DraftResult] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)


def _host_ok(url: str, brand: Brand) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    allowed = {urlparse(brand.website).hostname} | ALLOWED_EXTRA_HOSTS
    return parsed.hostname in allowed


def expected_cta(post: Post, brand: Brand) -> str:
    return post.cta_url or brand.website


def build_user_prompt(post: Post, brand: Brand, platform_limit_note: str = "") -> str:
    guidance = CONTENT_GUIDANCE.get(post.content_type, CONTENT_GUIDANCE["standard"])
    lines = [
        f"Business: {brand.display_name}",
        f"Platform: {post.platform}",
        f"Post type: {post.content_type}. {guidance}",
        f"Topic: {post.subject}",
        f"Visual: {post.visual_source}"
        + (" (AI-generated: caption must label it)" if post.visual_source == "ai" else ""),
        f"cta_url: {expected_cta(post, brand)}",
    ]
    if platform_limit_note:
        lines.append(platform_limit_note)
    return "\n".join(lines)


def parse_model_output(text: str) -> dict:
    """Accept the JSON object even if the model wrapped it in a code fence or added prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", stripped).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model output")
    data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")
    return data


def validate_draft(data: dict, post: Post, brand: Brand) -> list[str]:
    problems: list[str] = []
    caption = data.get("caption")
    hashtags = data.get("hashtags")
    cta_url = data.get("cta_url")
    expected = expected_cta(post, brand)

    if not isinstance(caption, str) or not caption.strip():
        problems.append("caption missing")
        caption = ""
    elif len(caption) > MAX_CAPTION_CHARS:
        problems.append(f"caption over {MAX_CAPTION_CHARS} characters")

    if not isinstance(hashtags, list) or not all(isinstance(h, str) for h in hashtags):
        problems.append("hashtags must be a list of strings")
    elif not 3 <= len(hashtags) <= 8:
        problems.append("hashtags must be 3 to 8")
    elif not all(HASHTAG_RE.match(h) for h in hashtags):
        problems.append("hashtag format invalid")

    if cta_url != expected:
        problems.append("cta_url does not match the URL given")
    elif not _host_ok(expected, brand):
        problems.append("cta_url host not allowed for this brand")
    elif caption and expected not in caption:
        problems.append("caption does not include the CTA URL")

    lowered = caption.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"banned phrase: {phrase}")

    if post.visual_source == "ai" and not any(t in lowered for t in ("ai-generated", "illustrative")):
        problems.append("AI visual not labeled")
    return problems


def _is_placeholder(subject: str) -> bool:
    return bool(PLACEHOLDER_SUBJECT_RE.search(subject.strip()))


def draft_post(
    conn: sqlite3.Connection,
    cfg: Config,
    post_id: int,
    client: DraftClient | None,
    guard: Guard,
    overwrite: bool = False,
) -> DraftResult:
    post = get_post(conn, post_id)
    brand = cfg.brands[post.brand_key]

    if post.status != "draft":
        return DraftResult(post_id, "skipped", [f"status is {post.status}"])
    if post.caption.strip() and not overwrite:
        return DraftResult(post_id, "skipped", ["caption already set"])
    if _is_placeholder(post.subject):
        return DraftResult(post_id, "rejected", ["set a topic (subject) before drafting"])

    description = f"draft caption post {post_id} ({post.brand_key} {post.platform}) with {cfg.llm.model}"
    if not guard.write(description):
        return DraftResult(post_id, "would_draft")
    if client is None:
        raise RuntimeError("live drafting needs a client")

    completion = client.complete(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(post, brand),
        model=cfg.llm.model,
        max_tokens=cfg.llm.max_tokens,
    )
    log.info("drafted post %s: %s in, %s out tokens", post_id, completion.input_tokens, completion.output_tokens)

    try:
        data = parse_model_output(completion.text)
    except (ValueError, json.JSONDecodeError):
        return DraftResult(post_id, "rejected", ["model output was not valid JSON"])

    problems = validate_draft(data, post, brand)
    if problems:
        return DraftResult(post_id, "rejected", problems)

    with conn:
        conn.execute(
            "UPDATE posts SET caption = ?, hashtags = ?, cta_url = ?, draft_model = ? WHERE id = ?",
            (
                data["caption"].strip(),
                json.dumps(data["hashtags"]),
                expected_cta(post, brand),
                cfg.llm.model,
                post_id,
            ),
        )
    return DraftResult(post_id, "drafted")


def draft_week(
    conn: sqlite3.Connection,
    cfg: Config,
    brand_key: str,
    week_start: date,
    client: DraftClient | None,
    guard: Guard,
    limit: int | None = None,
    overwrite: bool = False,
) -> BatchSummary:
    start, end = week_bounds_utc(week_start, cfg.timezone)
    posts = list_posts(conn, brand_key=brand_key, status="draft", scheduled_from=start, scheduled_to=end)
    cap = cfg.llm.max_drafts_per_run if limit is None else min(limit, cfg.llm.max_drafts_per_run)
    summary = BatchSummary()
    for post in posts[:cap]:
        summary.results.append(draft_post(conn, cfg, post.id, client, guard, overwrite=overwrite))
    return summary

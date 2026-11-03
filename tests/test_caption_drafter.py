import json
from datetime import date

import pytest

from socialops.caption_drafter import (
    SYSTEM_PROMPT,
    build_user_prompt,
    draft_post,
    draft_week,
    parse_model_output,
)
from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_up
from socialops.guard import Guard
from socialops.llm import Completion
from socialops.post_queue import get_post, list_posts, set_post_fields

MONDAY = date(2026, 11, 2)
TOPIC = "HIPAA risk assessment basics"


class FakeClient:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, system, user, model, max_tokens):
        self.calls.append({"system": system, "user": user, "model": model, "max_tokens": max_tokens})
        return Completion(text=self.texts.pop(0), input_tokens=100, output_tokens=50)


@pytest.fixture
def cfg(real_config_path):
    return load_config(real_config_path)


@pytest.fixture
def conn(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    generate_week(c, cfg, MONDAY)
    yield c
    c.close()


def clas_post(conn, content_type="image_checklist"):
    return next(p for p in list_posts(conn, brand_key="clas_by_fred", status="draft")
                if p.content_type == content_type and p.platform == "facebook")


def set_topic(conn, post_id, topic=TOPIC):
    return set_post_fields(conn, post_id, subject=topic)


def good(caption=None, hashtags=None, cta="https://classbyfred.com"):
    caption = caption or (
        "HIPAA is more than paperwork. Having a policy doesn't mean your practice is protected. "
        "A risk assessment shows where patient data actually sits. Staff need clear procedures they follow. "
        f"Start with a checklist at {cta}"
    )
    return json.dumps({
        "caption": caption,
        "hashtags": hashtags or ["#HIPAA", "#healthcarecompliance", "#patientprivacy"],
        "cta_url": cta,
    })


# --- prompt and parsing ---------------------------------------------------

def test_system_prompt_forbids_extras_and_asks_json():
    assert "JSON" in SYSTEM_PROMPT and "guarantee" in SYSTEM_PROMPT


def test_user_prompt_has_brand_topic_and_exact_cta(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    prompt = build_user_prompt(post, cfg.brands["clas_by_fred"])
    assert "CLAS by Fred" in prompt and TOPIC in prompt
    assert "cta_url: https://classbyfred.com" in prompt


def test_ai_visual_is_flagged_in_prompt(cfg, conn):
    art = list_posts(conn, brand_key="clas_by_fred", status="draft")[0]
    set_post_fields(conn, art.id, visual_source="ai")
    art = get_post(conn, art.id)
    assert "must label it" in build_user_prompt(art, cfg.brands["clas_by_fred"])


def test_parse_accepts_code_fence_and_prose():
    assert parse_model_output('```json\n{"caption": "x"}\n```') == {"caption": "x"}
    assert parse_model_output('Here you go: {"caption": "x"} thanks') == {"caption": "x"}


def test_parse_rejects_non_json():
    with pytest.raises(ValueError):
        parse_model_output("I cannot help with that.")


# --- drafting -------------------------------------------------------------

def test_valid_draft_is_saved_and_status_stays_draft(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    client = FakeClient(good())
    result = draft_post(conn, cfg, post.id, client, Guard(dry_run=False))
    assert result.outcome == "drafted"
    saved = get_post(conn, post.id)
    assert saved.caption.startswith("HIPAA is more than paperwork")
    assert saved.hashtags == ("#HIPAA", "#healthcarecompliance", "#patientprivacy")
    assert saved.cta_url == "https://classbyfred.com"
    assert saved.draft_model == cfg.llm.model
    assert saved.status == "draft"
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == cfg.llm.model
    assert client.calls[0]["max_tokens"] == cfg.llm.max_tokens


def test_dry_run_makes_no_model_call(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    guard = Guard(dry_run=True)
    result = draft_post(conn, cfg, post.id, None, guard)
    assert result.outcome == "would_draft"
    assert get_post(conn, post.id).caption == ""
    assert any("draft caption" in d for d in guard.would_have)


def test_placeholder_subject_rejected_without_call(cfg, conn):
    post = clas_post(conn)  # subject still auto-generated
    client = FakeClient()
    result = draft_post(conn, cfg, post.id, client, Guard(dry_run=False))
    assert result.outcome == "rejected" and "set a topic" in result.problems[0]
    assert client.calls == []


def test_existing_caption_is_kept_unless_overwrite(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    set_post_fields(conn, post.id, caption="Hand written caption. See classbyfred.com")
    client = FakeClient()
    assert draft_post(conn, cfg, post.id, client, Guard(dry_run=False)).outcome == "skipped"
    assert client.calls == []


def test_overwrite_replaces_caption(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    set_post_fields(conn, post.id, caption="old")
    client = FakeClient(good())
    result = draft_post(conn, cfg, post.id, client, Guard(dry_run=False), overwrite=True)
    assert result.outcome == "drafted"
    assert get_post(conn, post.id).caption != "old"


def test_non_draft_is_skipped(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    conn.execute("UPDATE posts SET status='approved' WHERE id=?", (post.id,))
    assert draft_post(conn, cfg, post.id, FakeClient(), Guard(dry_run=False)).outcome == "skipped"


@pytest.mark.parametrize(
    "output,expected",
    [
        ("not json at all", "not valid JSON"),
        (good(cta="https://evil.example"), "cta_url does not match"),
        (good(caption="Guaranteed results. See https://classbyfred.com"), "banned phrase: guarantee"),
        (good(caption="Stay compliant for good. Visit https://classbyfred.com"), None),  # valid
        (good(caption="Great tips with no link at all."), "caption does not include the CTA URL"),
        (good(hashtags=["#one", "#two"]), "hashtags must be 3 to 8"),
        (good(hashtags=["#HIPAA", "#two words", "#ok"]), "hashtag format invalid"),
        (good(hashtags=[f"#tag{i}" for i in range(9)]), "hashtags must be 3 to 8"),
        (good(caption="x" * 2001 + " https://classbyfred.com"), "over 2000"),
    ],
)
def test_validation_cases(cfg, conn, output, expected):
    post = set_topic(conn, clas_post(conn).id)
    result = draft_post(conn, cfg, post.id, FakeClient(output), Guard(dry_run=False))
    if expected is None:
        assert result.outcome == "drafted"
    else:
        assert result.outcome == "rejected"
        assert any(expected in p for p in result.problems), result.problems
        assert get_post(conn, post.id).caption == ""  # nothing saved on rejection


def test_cta_host_must_be_brand_or_calendly(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    set_post_fields(conn, post.id, cta_url="https://evil.example")
    out = good(cta="https://evil.example", caption="Visit https://evil.example today")
    result = draft_post(conn, cfg, post.id, FakeClient(out), Guard(dry_run=False))
    assert result.outcome == "rejected"
    assert any("host not allowed" in p for p in result.problems)


def test_calendly_cta_allowed(cfg, conn):
    post = set_topic(conn, clas_post(conn, "webinar_cta").id)
    set_post_fields(conn, post.id, cta_url="https://calendly.com/fred/intro")
    out = good(cta="https://calendly.com/fred/intro", caption="Book a call at https://calendly.com/fred/intro")
    assert draft_post(conn, cfg, post.id, FakeClient(out), Guard(dry_run=False)).outcome == "drafted"


def test_ai_visual_must_be_labeled_in_draft(cfg, conn):
    pamela = list_posts(conn, brand_key="pamela_art", status="draft")[0]
    set_post_fields(conn, pamela.id, subject="Harbor at dusk", visual_source="ai")
    out = good(cta="https://www.pamelapatton.art",
               caption="A harbor at dusk. Shop at https://www.pamelapatton.art")
    result = draft_post(conn, cfg, pamela.id, FakeClient(out), Guard(dry_run=False))
    assert result.outcome == "rejected" and any("AI visual" in p for p in result.problems)

    labeled = good(cta="https://www.pamelapatton.art",
                   caption="AI-generated harbor at dusk. Shop at https://www.pamelapatton.art")
    assert draft_post(conn, cfg, pamela.id, FakeClient(labeled), Guard(dry_run=False)).outcome == "drafted"


def test_fenced_output_is_accepted(cfg, conn):
    post = set_topic(conn, clas_post(conn).id)
    result = draft_post(conn, cfg, post.id, FakeClient(f"```json\n{good()}\n```"), Guard(dry_run=False))
    assert result.outcome == "drafted"


# --- batch ----------------------------------------------------------------

def test_draft_week_respects_run_cap(cfg, conn):
    cfg2 = type(cfg)(**{**cfg.__dict__, "llm": type(cfg.llm)(model=cfg.llm.model, max_tokens=800, max_drafts_per_run=2)})
    for p in list_posts(conn, brand_key="clas_by_fred", status="draft"):
        set_topic(conn, p.id)
    client = FakeClient(good(), good())
    summary = draft_week(conn, cfg2, "clas_by_fred", MONDAY, client, Guard(dry_run=False))
    assert len(summary.results) == 2 and len(client.calls) == 2


def test_draft_week_limit_argument(cfg, conn):
    for p in list_posts(conn, brand_key="clas_by_fred", status="draft"):
        set_topic(conn, p.id)
    client = FakeClient(good())
    summary = draft_week(conn, cfg, "clas_by_fred", MONDAY, client, Guard(dry_run=False), limit=1)
    assert len(summary.results) == 1


def test_draft_week_dry_run_plans_without_calls(cfg, conn):
    for p in list_posts(conn, brand_key="clas_by_fred", status="draft"):
        set_topic(conn, p.id)
    guard = Guard(dry_run=True)
    summary = draft_week(conn, cfg, "clas_by_fred", MONDAY, None, guard)
    clas_total = len(list_posts(conn, brand_key="clas_by_fred", status="draft"))
    assert summary.count("would_draft") == len(summary.results) == min(clas_total, cfg.llm.max_drafts_per_run)
    assert all(get_post(conn, r.post_id).caption == "" for r in summary.results)

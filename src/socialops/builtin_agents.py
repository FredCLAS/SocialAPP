"""Concrete agents wired into the orchestrator."""

from __future__ import annotations

from datetime import date

from .agents import AgentResult, Context
from .content_engine import week_bounds_utc
from .post_queue import fingerprint, list_posts
from .publisher import publish_due
from .qa import check_post, latest_result


class QaAgent:
    """Runs the pre-post checklist on drafts in one brand and week. Never changes post status."""

    name = "qa"

    def __init__(self, brand_key: str, week_start: date) -> None:
        self.brand_key = brand_key
        self.week_start = week_start

    def run(self, ctx: Context) -> AgentResult:
        start, end = week_bounds_utc(self.week_start, ctx.cfg.timezone)
        drafts = list_posts(ctx.conn, brand_key=self.brand_key, status="draft",
                            scheduled_from=start, scheduled_to=end)
        checked, passed, failed, skipped_current = [], 0, [], 0
        for post in drafts:
            prior = latest_result(ctx.conn, post.id)
            if prior and prior["fingerprint"] == fingerprint(post) and prior["passed"]:
                skipped_current += 1
                continue
            result = check_post(ctx.conn, ctx.cfg, post.id, fetch_status=ctx.fetch_status)
            checked.append(post.id)
            if result.passed:
                passed += 1
            else:
                failed.append(f"{post.id}: " + "; ".join(c.reason for c in result.failures))
        reason = f"checked {len(checked)}, passed {passed}, failed {len(failed)}, already passing {skipped_current}"
        if failed:
            reason += " | " + " | ".join(failed)
        return AgentResult("ok", reason, checked)


class PublishAgent:
    """Publishes scheduled posts that are due. Each publish is gated by an approved post."""

    name = "publish"

    def run(self, ctx: Context) -> AgentResult:
        if ctx.store is None and not ctx.guard.dry_run:
            return AgentResult("blocked", "live publishing needs a secret store")
        results = publish_due(ctx.conn, ctx.cfg, ctx.store, ctx.guard, ctx.now,
                              sleep=ctx.sleep, transport=ctx.transport or _default())
        counts: dict[str, int] = {}
        for r in results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        reason = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing due"
        return AgentResult("ok", reason, [r.post_id for r in results])


def _default():
    from .meta_graph import default_transport
    return default_transport

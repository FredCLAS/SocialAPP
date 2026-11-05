"""Command line entry point.

M1: check-config, migrate, status.
M2: generate-week, list-posts, approve-week.
"""

from __future__ import annotations

import argparse
import secrets as pysecrets
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .caption_drafter import draft_week
from .content_engine import generate_week, week_bounds_utc
from .db import DEFAULT_DB_PATH, applied_versions, connect, migrate_down, migrate_up
from .guard import Guard
from .llm import client_from_keychain
from .post_queue import (
    NotApprovable,
    UnauthorizedApprover,
    approve_week,
    list_posts,
    set_post_fields,
)
from .oauth import (
    SERVICE as CANVA_SERVICE,
    CanvaOAuthConfig,
    CanvaTokens,
    OAuthError,
    build_authorize_url,
    make_pkce,
    wait_for_code,
)
from .secrets import KeychainStore, SecretNotFound

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "brands.toml"


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socialops", description=f"socialops {__version__}")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to brands.toml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="force dry-run (default from config)")
    mode.add_argument("--live", action="store_true", help="allow live writes (not yet enabled; M10)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate brands.toml and print a summary")

    m = sub.add_parser("migrate", help="apply or roll back database migrations")
    m.add_argument("--down-to", type=int, default=None, help="roll back until this version")
    m.add_argument("--to", type=int, default=None, help="migrate up to this version only")

    sub.add_parser("status", help="show mode, brands, and applied migrations")

    g = sub.add_parser("generate-week", help="create draft posts for one week (idempotent)")
    g.add_argument("--week", type=_date, required=True, help="Monday of the week, YYYY-MM-DD")

    lp = sub.add_parser("list-posts", help="list posts for a brand and week")
    lp.add_argument("--brand", required=True)
    lp.add_argument("--week", type=_date, required=True)
    lp.add_argument("--status", default=None)

    a = sub.add_parser("approve-week", help="approve all complete drafts for a brand and week")
    a.add_argument("--brand", required=True)
    a.add_argument("--week", type=_date, required=True)
    a.add_argument("--actor", required=True, help="approver name, e.g. pamela")

    au = sub.add_parser("auth", help="connect external accounts (run on the Mac that will use them)")
    au_sub = au.add_subparsers(dest="provider", required=True)
    au_sub.add_parser("canva", help="connect a Canva account (browser sign-in)")
    au_sub.add_parser("canva-status", help="show whether Canva is connected (no tokens printed)")
    au_sub.add_parser("canva-disconnect", help="remove stored Canva tokens from this Mac")

    sw = sub.add_parser("schedule-week", help="move a week's approved posts to scheduled")
    sw.add_argument("--brand", required=True)
    sw.add_argument("--week", type=_date, required=True)

    pd = sub.add_parser("publish-due", help="publish scheduled posts that are due (dry-run by default)")
    pd.add_argument("--now", default=None, help="ISO time for testing; default is current UTC")

    ts = sub.add_parser("token-status", help="show Meta token state for a brand and platform")
    ts.add_argument("--brand", required=True)
    ts.add_argument("--platform", required=True)

    qw = sub.add_parser("qa-week", help="run the pre-post checklist on a week's drafts")
    qw.add_argument("--brand", required=True)
    qw.add_argument("--week", type=_date, required=True)

    ra = sub.add_parser("run-agents", help="run the agent pipeline for a brand and week")
    ra.add_argument("--brand", required=True)
    ra.add_argument("--week", type=_date, required=True)
    ra.add_argument("--now", default=None, help="ISO time for testing; default is current UTC")

    rv = sub.add_parser("review", help="open the weekly approval page for a brand and week (local only)")
    rv.add_argument("--brand", required=True)
    rv.add_argument("--week", type=_date, required=True)
    rv.add_argument("--actor", required=True, help="approver name, e.g. pamela")
    rv.add_argument("--port", type=int, default=8777)

    rd = sub.add_parser("run-due", help="one scheduler tick: schedule approved posts, publish what is due")
    rd.add_argument("--now", default=None, help="ISO time for testing; default is current UTC")

    isc = sub.add_parser("install-schedule", help="write the launchd job that runs run-due every 5 minutes")
    isc.add_argument("--dest", type=Path, default=None, help="folder for the plist (default ~/Library/LaunchAgents)")

    ep = sub.add_parser("engagement-pull", help="read new comments on recent posts and classify them (live only)")
    ep.add_argument("--brand", required=True)
    ep.add_argument("--now", default=None)

    rdr = sub.add_parser("reply-draft", help="draft replies for new comments (answer bank, then Claude)")
    rdr.add_argument("--brand", required=True)

    rl = sub.add_parser("reply-list", help="list comments and reply status for a brand")
    rl.add_argument("--brand", required=True)
    rl.add_argument("--status", default=None)

    ra2 = sub.add_parser("reply-approve", help="approve a drafted reply (exact text)")
    ra2.add_argument("--id", type=int, required=True)
    ra2.add_argument("--actor", required=True)

    rr = sub.add_parser("reply-reject", help="reject a drafted reply with a reason")
    rr.add_argument("--id", type=int, required=True)
    rr.add_argument("--actor", required=True)
    rr.add_argument("--reason", required=True)

    re_ = sub.add_parser("reply-edit", help="replace a reply's text (approval must be given again)")
    re_.add_argument("--id", type=int, required=True)
    re_.add_argument("--text", required=True)

    rs = sub.add_parser("reply-send", help="send approved replies (dry-run by default)")
    rs.add_argument("--brand", required=True)

    aa = sub.add_parser("answer-add", help="add an approved answer to the bank")
    aa.add_argument("--topic", required=True)
    aa.add_argument("--keywords", required=True, help="comma-separated words that trigger it")
    aa.add_argument("--answer", required=True)
    aa.add_argument("--actor", required=True)

    d = sub.add_parser("draft-week", help="draft captions with Claude for a brand and week")
    d.add_argument("--brand", required=True)
    d.add_argument("--week", type=_date, required=True)
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--overwrite", action="store_true", help="replace existing captions")

    e = sub.add_parser("edit-post", help="set topic, visual, or hashtags on a draft post")
    e.add_argument("--id", type=int, required=True)
    e.add_argument("--subject")
    e.add_argument("--visual-path")
    e.add_argument("--visual-source", choices=["ai", "real_photo", "canva"])
    e.add_argument("--hashtags", nargs="+")
    return p


PLACEHOLDER_CLIENT_ID = "REPLACE_WITH_CANVA_CLIENT_ID"
_NO_CANVA = CanvaOAuthConfig(client_id="", scopes=("none",), authorize_url="", token_url="")


def _has_secret(store, account: str) -> bool:
    try:
        store.get(CANVA_SERVICE, account)
        return True
    except SecretNotFound:
        return False


def _auth(args, cfg, guard: Guard) -> int:
    store = KeychainStore()
    if args.provider in ("canva-status", "canva-disconnect"):
        tokens = CanvaTokens(store, cfg.canva or _NO_CANVA)
        if args.provider == "canva-status":
            if not tokens.is_connected():
                print("canva: not connected")
            else:
                exp = tokens.expires_at()
                when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(exp)) if exp else "unknown"
                print(f"canva: connected (access token expires {when})")
            return 0
        tokens.clear()
        print("canva: tokens removed from this Mac")
        return 0

    # provider == "canva": browser connect
    if cfg.canva is None:
        print("config error: [canva] section missing in brands.toml", file=sys.stderr)
        return 2
    if not cfg.canva.client_id or cfg.canva.client_id == PLACEHOLDER_CLIENT_ID:
        print("canva client_id is not set in brands.toml. Create the integration first.", file=sys.stderr)
        return 2
    tokens = CanvaTokens(store, cfg.canva, client_secret_present=_has_secret(store, "client_secret"))
    verifier, challenge = make_pkce()
    state = pysecrets.token_urlsafe(24)
    url = build_authorize_url(cfg.canva, state, challenge)
    print("Sign in to Canva with the account you want to connect. Opening your browser:")
    print(url)
    try:
        subprocess.run(["open", url], check=False)
    except OSError:
        pass
    try:
        code = wait_for_code(cfg.canva, state)
        tokens.exchange_code(code, verifier)
    except OAuthError as exc:
        print(f"canva connect failed: {exc}", file=sys.stderr)
        return 3
    print("canva: connected")
    return 0


def _now(value: str | None = None):
    from datetime import datetime, timezone
    return datetime.fromisoformat(value) if value else datetime.now(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.live:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = cfg.dry_run
    guard = Guard(dry_run=dry_run)

    if args.command == "check-config":
        print(f"config ok: {len(cfg.brands)} brands, timezone {cfg.timezone.key}")
        for key, brand in cfg.brands.items():
            print(f"  {key}: {', '.join(sorted(brand.platforms))}")
        return 0

    if args.command == "status":
        print(f"mode: {'DRY-RUN' if guard.dry_run else 'LIVE'}")
        print(f"brands: {', '.join(cfg.brands)}")
        return 0

    if args.command == "migrate":
        conn = connect(args.db)
        try:
            if args.down_to is not None:
                print(f"rolled back: {migrate_down(conn, target=args.down_to) or 'nothing'}")
            else:
                print(f"applied: {migrate_up(conn, target=args.to) or 'nothing'}")
            print(f"schema versions now: {applied_versions(conn)}")
        finally:
            conn.close()
        return 0

    if args.command == "auth":
        return _auth(args, cfg, guard)

    # M2 commands work on the database; make sure the schema is current first.
    conn = connect(args.db)
    try:
        migrate_up(conn)
        if args.command == "generate-week":
            report = generate_week(conn, cfg, args.week)
            print(f"week {args.week}: planned {report['planned']}, inserted {report['inserted']}, "
                  f"already present {report['existing']}")
            return 0

        if args.command == "list-posts":
            start, end = week_bounds_utc(args.week, cfg.timezone)
            posts = list_posts(conn, brand_key=args.brand, status=args.status,
                               scheduled_from=start, scheduled_to=end)
            for p in posts:
                print(f"{p.scheduled_at}  {p.platform:<10} {p.content_type:<14} {p.format:<5} {p.status}")
            print(f"{len(posts)} posts")
            return 0

        if args.command == "review":
            from .review import serve
            conn.close()
            serve(args.db, cfg, args.brand, args.week, args.actor, port=args.port)
            return 0

        if args.command == "run-due":
            from .agents import Context
            from .scheduler import run_due
            from .secrets import KeychainStore as _KS
            store = None if guard.dry_run else _KS()
            results = run_due(conn, cfg, store, guard, _now(args.now))
            for name, res in results:
                print(f"{name}: {res.status}" + (f" ({res.reason})" if res.reason else ""))
            return 0

        if args.command == "install-schedule":
            from .scheduler import install_schedule
            target = install_schedule(live=not guard.dry_run, config_path=str(args.config),
                                      db_path=str(args.db), agents_dir=args.dest)
            mode = "LIVE" if not guard.dry_run else "dry-run"
            print(f"wrote {target} ({mode})")
            print("load it:   launchctl bootstrap gui/$(id -u) " + str(target))
            print("stop it:   launchctl bootout gui/$(id -u)/com.socialops.run-due")
            return 0

        if args.command == "engagement-pull":
            from .engagement import pull_comments
            from .secrets import KeychainStore as _KS
            if guard.dry_run:
                print("dry-run: pull needs live mode")
                return 0
            res = pull_comments(conn, cfg, args.brand, _KS(), guard, _now(args.now))
            print(f"{res.status}: {res.reason}")
            return 0 if res.status in ("ok", "blocked") else 1

        if args.command == "reply-draft":
            from .engagement import draft_reply
            from .llm import client_from_keychain
            from .secrets import KeychainStore as _KS
            client = None if guard.dry_run else client_from_keychain(_KS())
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM inbound_items WHERE brand_key=? AND reply_status IN ('none','rejected') "
                "AND escalate=0 ORDER BY id", (args.brand,)).fetchall()]
            for i in ids:
                print(f"{i}: {draft_reply(conn, cfg, i, client, guard)}")
            return 0

        if args.command == "reply-list":
            sql = "SELECT * FROM inbound_items WHERE brand_key=?"
            params = [args.brand]
            if args.status:
                sql += " AND reply_status=?"
                params.append(args.status)
            for r in conn.execute(sql + " ORDER BY id", params).fetchall():
                flag = " ESCALATE" if r["escalate"] else ""
                body = " ".join(r["body"].split())[:90]
                print(f"{r['id']:>4} {r['platform']:<9} {r['category']:<9} {r['reply_status']:<9}{flag} | {body}")
                if r["reply_text"]:
                    print(f"       reply ({r['reply_source']}): {r['reply_text']}")
            return 0

        if args.command == "reply-approve":
            from .engagement import approve_reply
            print(approve_reply(conn, args.id, args.actor))
            return 0

        if args.command == "reply-reject":
            from .engagement import reject_reply
            print(reject_reply(conn, args.id, args.actor, args.reason))
            return 0

        if args.command == "reply-edit":
            from .engagement import edit_reply
            print(edit_reply(conn, cfg, args.id, args.text))
            return 0

        if args.command == "reply-send":
            from .engagement import send_approved
            from .secrets import KeychainStore as _KS
            store = None if guard.dry_run else _KS()
            for line in send_approved(conn, cfg, args.brand, store, guard):
                print(line)
            return 0

        if args.command == "answer-add":
            from .post_queue import APPROVERS
            if args.actor not in APPROVERS:
                print(f"{args.actor!r} is not an approver", file=sys.stderr)
                return 3
            with conn:
                import json as _json
                words = [w.strip().lower() for w in args.keywords.split(",") if w.strip()]
                conn.execute("INSERT INTO approved_answers (topic, keywords, answer, approved_by) VALUES (?, ?, ?, ?)",
                             (args.topic, _json.dumps(words), args.answer, args.actor))
            print("answer added")
            return 0

        if args.command == "qa-week":
            from .builtin_agents import QaAgent
            from .agents import Context, run_logged
            ctx = Context(conn=conn, cfg=cfg, guard=guard, now=_now(), fetch_status=None)
            res = run_logged(QaAgent(args.brand, args.week), ctx)
            print(f"{res.status}: {res.reason}")
            return 0 if res.status == "ok" else 1

        if args.command == "run-agents":
            from .agents import Context
            from .builtin_agents import PublishAgent, QaAgent
            from .orchestrator import run_agents
            from .secrets import KeychainStore as _KS
            store = None if guard.dry_run else _KS()
            ctx = Context(conn=conn, cfg=cfg, guard=guard, now=_now(args.now), store=store)
            for name, res in run_agents([QaAgent(args.brand, args.week), PublishAgent()], ctx):
                print(f"{name}: {res.status}" + (f" ({res.reason})" if res.reason else ""))
            return 0

        if args.command == "schedule-week":
            from .publisher import schedule_week
            ids = schedule_week(conn, cfg, args.brand, args.week)
            print(f"scheduled {len(ids)} posts")
            return 0

        if args.command == "token-status":
            import time as _time
            from .publisher import token_status
            from .secrets import service_name
            status = token_status(KeychainStore(), service_name(args.brand, args.platform), _time.time())
            print(f"{args.brand} {args.platform}: token {status}")
            return 0

        if args.command == "publish-due":
            from datetime import datetime, timezone
            from .publisher import publish_due
            now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
            store = None if guard.dry_run else KeychainStore()
            results = publish_due(conn, cfg, store, guard, now)
            for r in results:
                print(f"post {r.post_id}: {r.outcome}" + (f"  {r.detail}" if r.detail else ""))
            print(f"{len(results)} due")
            return 0

        if args.command == "edit-post":
            fields = {
                k: v
                for k, v in {
                    "subject": args.subject,
                    "visual_path": args.visual_path,
                    "visual_source": args.visual_source,
                    "hashtags": args.hashtags,
                }.items()
                if v is not None
            }
            if not fields:
                print("nothing to change", file=sys.stderr)
                return 2
            post = set_post_fields(conn, args.id, **fields)
            print(f"post {post.id} updated: {', '.join(fields)}")
            return 0

        if args.command == "draft-week":
            # Dry-run: no key needed, no model call. Live: key read from Keychain only now.
            client = None if guard.dry_run else client_from_keychain(KeychainStore())
            summary = draft_week(conn, cfg, args.brand, args.week, client, guard,
                                 limit=args.limit, overwrite=args.overwrite)
            for r in summary.results:
                detail = f"  {'; '.join(r.problems)}" if r.problems else ""
                print(f"post {r.post_id}: {r.outcome}{detail}")
            print(f"drafted {summary.count('drafted')}, would draft {summary.count('would_draft')}, "
                  f"rejected {summary.count('rejected')}, skipped {summary.count('skipped')}")
            return 0

        if args.command == "approve-week":
            if not guard.write(f"approve drafts {args.brand} week {args.week}"):
                print("dry-run: no approvals written")
                return 0
            try:
                ids = approve_week(conn, cfg, args.brand, args.week, args.actor)
            except (NotApprovable, UnauthorizedApprover) as exc:
                print(f"not approved: {exc}", file=sys.stderr)
                return 3
            print(f"approved {len(ids)} posts")
            return 0
    finally:
        conn.close()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

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

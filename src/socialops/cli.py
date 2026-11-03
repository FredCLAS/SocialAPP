"""Command line entry point. M1 commands: check-config, migrate, dry-run status."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .db import DEFAULT_DB_PATH, connect, migrate_down, migrate_up, applied_versions
from .guard import Guard

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "brands.toml"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socialops", description=f"socialops {__version__}")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to brands.toml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="force dry-run (default from config)")
    mode.add_argument("--live", action="store_true", help="allow live writes (not yet enabled; M10)")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate brands.toml and print a summary")

    m = sub.add_parser("migrate", help="apply or roll back database migrations")
    m.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    m.add_argument("--down-to", type=int, default=None, help="roll back until this version")
    m.add_argument("--to", type=int, default=None, help="migrate up to this version only")

    sub.add_parser("status", help="show mode, brands, and applied migrations")
    return p


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
                rolled = migrate_down(conn, target=args.down_to)
                print(f"rolled back: {rolled or 'nothing'}")
            else:
                applied = migrate_up(conn, target=args.to)
                print(f"applied: {applied or 'nothing'}")
            print(f"schema versions now: {applied_versions(conn)}")
        finally:
            conn.close()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Per-Mac SQLite database: connection and reversible migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "Library" / "Application Support" / "SocialOps" / "app.db"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="foundation",
        up="""
        CREATE TABLE brands (
            key          TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            website      TEXT NOT NULL
        );

        CREATE TABLE posts (
            id           INTEGER PRIMARY KEY,
            brand_key    TEXT NOT NULL REFERENCES brands(key),
            platform     TEXT NOT NULL,
            subject      TEXT NOT NULL,
            visual_path  TEXT,
            caption      TEXT NOT NULL DEFAULT '',
            hashtags     TEXT NOT NULL DEFAULT '[]',   -- JSON array
            cta_url      TEXT,
            scheduled_at TEXT,                          -- ISO 8601, UTC
            status       TEXT NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft','approved','scheduled','published','failed')),
            retry_count  INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (brand_key, platform, subject, scheduled_at)
        );

        CREATE TABLE approvals (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL CHECK (kind IN ('post','reply','cold_send','boost')),
            ref_id      INTEGER NOT NULL,
            decided_by  TEXT NOT NULL,
            decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            decision    TEXT NOT NULL CHECK (decision IN ('approved','rejected'))
        );

        CREATE TABLE publish_log (
            id          INTEGER PRIMARY KEY,
            post_id     INTEGER NOT NULL REFERENCES posts(id),
            platform    TEXT NOT NULL,
            outcome     TEXT NOT NULL,
            remote_id   TEXT,
            logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );

        CREATE TABLE quota_ledger (
            id          INTEGER PRIMARY KEY,
            api         TEXT NOT NULL,
            operation   TEXT NOT NULL,
            units       INTEGER NOT NULL CHECK (units >= 0),
            day         TEXT NOT NULL,                   -- YYYY-MM-DD (Pacific, per Google)
            logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """,
        down="""
        DROP TABLE quota_ledger;
        DROP TABLE publish_log;
        DROP TABLE approvals;
        DROP TABLE posts;
        DROP TABLE brands;
        """,
    ),
    Migration(
        version=2,
        name="content_slots",
        up="""
        ALTER TABLE posts ADD COLUMN content_type TEXT NOT NULL DEFAULT 'standard';
        ALTER TABLE posts ADD COLUMN format TEXT NOT NULL DEFAULT 'image';
        CREATE UNIQUE INDEX posts_slot ON posts (brand_key, platform, scheduled_at);
        """,
        down="""
        DROP INDEX posts_slot;
        ALTER TABLE posts DROP COLUMN format;
        ALTER TABLE posts DROP COLUMN content_type;
        """,
    ),
    Migration(
        version=3,
        name="drafts_and_visual_source",
        up="""
        ALTER TABLE posts ADD COLUMN visual_source TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE posts ADD COLUMN draft_model TEXT;
        """,
        down="""
        ALTER TABLE posts DROP COLUMN draft_model;
        ALTER TABLE posts DROP COLUMN visual_source;
        """,
    ),
    Migration(
        version=4,
        name="publish_log_detail",
        up="ALTER TABLE publish_log ADD COLUMN detail TEXT;",
        down="ALTER TABLE publish_log DROP COLUMN detail;",
    ),
    Migration(
        version=5,
        name="agents_qa_gates",
        up="""
        CREATE TABLE approvals_new (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL CHECK (kind IN
                        ('post','reply','cold_send','boost','visibility','connect','qualification')),
            ref_id      INTEGER NOT NULL,
            decided_by  TEXT NOT NULL,
            decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            decision    TEXT NOT NULL CHECK (decision IN ('approved','rejected'))
        );
        INSERT INTO approvals_new (id, kind, ref_id, decided_by, decided_at, decision)
            SELECT id, kind, ref_id, decided_by, decided_at, decision FROM approvals;
        DROP TABLE approvals;
        ALTER TABLE approvals_new RENAME TO approvals;

        CREATE TABLE agent_runs (
            id          INTEGER PRIMARY KEY,
            agent       TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('ok','blocked','held','skipped','error')),
            reason      TEXT NOT NULL DEFAULT '',
            refs        TEXT NOT NULL DEFAULT '[]',
            dry_run     INTEGER NOT NULL DEFAULT 1,
            started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            finished_at TEXT
        );

        CREATE TABLE qa_results (
            id          INTEGER PRIMARY KEY,
            post_id     INTEGER NOT NULL REFERENCES posts(id),
            fingerprint TEXT NOT NULL,
            passed      INTEGER NOT NULL CHECK (passed IN (0,1)),
            failures    TEXT NOT NULL DEFAULT '[]',
            checked_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX qa_results_post ON qa_results (post_id, id);
        """,
        down="""
        DROP INDEX qa_results_post;
        DROP TABLE qa_results;
        DROP TABLE agent_runs;
        DELETE FROM approvals WHERE kind NOT IN ('post','reply','cold_send','boost');
        CREATE TABLE approvals_old (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL CHECK (kind IN ('post','reply','cold_send','boost')),
            ref_id      INTEGER NOT NULL,
            decided_by  TEXT NOT NULL,
            decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            decision    TEXT NOT NULL CHECK (decision IN ('approved','rejected'))
        );
        INSERT INTO approvals_old (id, kind, ref_id, decided_by, decided_at, decision)
            SELECT id, kind, ref_id, decided_by, decided_at, decision FROM approvals;
        DROP TABLE approvals;
        ALTER TABLE approvals_old RENAME TO approvals;
        """,
    ),
    Migration(
        version=6,
        name="approval_notes",
        up="ALTER TABLE approvals ADD COLUMN note TEXT;",
        down="ALTER TABLE approvals DROP COLUMN note;",
    ),
)

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL" if str(path) != ":memory:" else "PRAGMA journal_mode = MEMORY")
    conn.execute(_SCHEMA_TABLE)
    return conn


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [r["version"] for r in rows]


def migrate_up(conn: sqlite3.Connection, target: int | None = None) -> list[int]:
    """Apply pending migrations up to `target` (default: latest). Returns applied versions."""
    done = set(applied_versions(conn))
    limit = target if target is not None else MIGRATIONS[-1].version
    applied: list[int] = []
    for m in MIGRATIONS:
        if m.version in done or m.version > limit:
            continue
        _run_script(conn, m.up)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (m.version, m.name)
        )
        conn.commit()
        applied.append(m.version)
    return applied


def migrate_down(conn: sqlite3.Connection, target: int = 0) -> list[int]:
    """Roll back applied migrations until the highest applied version is `target`."""
    done = sorted(applied_versions(conn), reverse=True)
    by_version = {m.version: m for m in MIGRATIONS}
    rolled: list[int] = []
    for version in done:
        if version <= target:
            break
        m = by_version.get(version)
        if m is None:
            raise RuntimeError(f"applied migration {version} is unknown to this build")
        _run_script(conn, m.down)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
        conn.commit()
        rolled.append(version)
    return rolled


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Run a multi-statement script in one transaction.

    sqlite3.executescript() issues a COMMIT first, so it cannot be atomic.
    Statements are split with sqlite3.complete_statement, which respects
    quotes and comments.
    """
    conn.commit()
    conn.execute("BEGIN")
    try:
        for statement in _split(script):
            conn.execute(statement)
    except Exception:
        conn.rollback()
        raise


def _split(script: str) -> list[str]:
    """Split on ';' only where sqlite3 says the prefix is a complete statement."""
    statements: list[str] = []
    start = 0
    for i, ch in enumerate(script):
        if ch == ";" and sqlite3.complete_statement(script[start : i + 1]):
            stmt = script[start : i + 1].strip()
            if stmt:
                statements.append(stmt)
            start = i + 1
    rest = script[start:].strip()
    if rest:
        raise ValueError(f"incomplete SQL statement in migration: {rest[:80]}")
    return statements

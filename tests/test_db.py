import sqlite3

import pytest

from socialops.db import MIGRATIONS, applied_versions, connect, migrate_down, migrate_up


def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_up_creates_tables_and_records_version(tmp_db):
    conn = connect(tmp_db)
    applied = migrate_up(conn)
    assert applied == [m.version for m in MIGRATIONS]
    assert {"brands", "posts", "approvals", "publish_log", "quota_ledger"} <= tables(conn)
    assert applied_versions(conn) == [m.version for m in MIGRATIONS]
    conn.close()


def test_up_is_idempotent(tmp_db):
    conn = connect(tmp_db)
    migrate_up(conn)
    assert migrate_up(conn) == []
    conn.close()


def test_down_removes_tables(tmp_db):
    conn = connect(tmp_db)
    migrate_up(conn)
    rolled = migrate_down(conn, target=0)
    assert rolled == [m.version for m in MIGRATIONS]
    assert not ({"brands", "posts", "approvals"} & tables(conn))
    assert applied_versions(conn) == []
    conn.close()


def test_round_trip_up_down_up(tmp_db):
    conn = connect(tmp_db)
    migrate_up(conn)
    migrate_down(conn, target=0)
    migrate_up(conn)
    assert {"posts"} <= tables(conn)
    conn.close()


def test_status_check_rejects_bad_value(tmp_db):
    conn = connect(tmp_db)
    migrate_up(conn)
    conn.execute("INSERT INTO brands (key, display_name, website) VALUES ('clas_by_fred','CLAS','w')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO posts (brand_key, platform, subject, status) VALUES ('clas_by_fred','facebook','s','posted')"
        )
    conn.close()


def test_foreign_key_enforced(tmp_db):
    conn = connect(tmp_db)
    migrate_up(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO posts (brand_key, platform, subject) VALUES ('nobody','facebook','s')"
        )
    conn.close()


def test_failed_migration_rolls_back(tmp_db, monkeypatch):
    from socialops import db as dbmod

    bad = dbmod.Migration(
        version=99,
        name="broken",
        up="CREATE TABLE ok_table (id INTEGER); CREATE TABLE ok_table (id INTEGER);",
        down="DROP TABLE ok_table;",
    )
    conn = connect(tmp_db)
    monkeypatch.setattr(dbmod, "MIGRATIONS", (*dbmod.MIGRATIONS, bad))
    with pytest.raises(sqlite3.OperationalError):
        migrate_up(conn)
    assert "ok_table" not in tables(conn)
    assert 99 not in applied_versions(conn)
    conn.close()


def test_quota_units_non_negative(tmp_db):
    conn = connect(tmp_db)
    migrate_up(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO quota_ledger (api, operation, units, day) VALUES ('youtube','insert',-1,'2026-11-03')"
        )
    conn.close()

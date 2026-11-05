from datetime import date, datetime, timezone

import pytest

from socialops.agents import AgentResult, Context, run_logged
from socialops.config import load_config
from socialops.content_engine import generate_week
from socialops.db import connect, migrate_down, migrate_up
from socialops.gates import GateError, is_approved, record_decision
from socialops.guard import Guard
from socialops.orchestrator import run_agents

MONDAY = date(2026, 11, 2)


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


def ctx(conn, cfg, dry_run=True):
    return Context(conn=conn, cfg=cfg, guard=Guard(dry_run=dry_run), now=datetime(2026, 11, 2, tzinfo=timezone.utc))


# --- gates ----------------------------------------------------------------

def test_new_gate_kinds_are_accepted(conn):
    for kind in ["visibility", "connect", "qualification", "boost", "reply", "cold_send", "post"]:
        record_decision(conn, kind, 1, "pamela", "approved")


def test_unknown_gate_kind_rejected(conn):
    with pytest.raises(GateError, match="unknown gate kind"):
        record_decision(conn, "publish_everything", 1, "pamela", "approved")


def test_only_approvers_decide(conn):
    with pytest.raises(GateError, match="not an approver"):
        record_decision(conn, "reply", 1, "intern", "approved")


def test_latest_decision_wins(conn):
    assert not is_approved(conn, "reply", 7)
    record_decision(conn, "reply", 7, "pamela", "approved")
    assert is_approved(conn, "reply", 7)
    record_decision(conn, "reply", 7, "pamela", "rejected")
    assert not is_approved(conn, "reply", 7)


def test_gates_are_per_item(conn):
    record_decision(conn, "reply", 1, "pamela", "approved")
    assert not is_approved(conn, "reply", 2)
    assert not is_approved(conn, "cold_send", 1)


# --- agent runtime ----------------------------------------------------------

class OkAgent:
    name = "ok"

    def run(self, c):
        return AgentResult("ok", "fine", [1, 2])


class BoomAgent:
    name = "boom"

    def run(self, c):
        raise RuntimeError("something broke")


class HoldAgent:
    name = "hold"

    def run(self, c):
        return AgentResult("held", "waiting for approval")


class AfterAgent:
    name = "after"

    def run(self, c):
        return AgentResult("ok")


def test_run_logged_records_result(conn, cfg):
    res = run_logged(OkAgent(), ctx(conn, cfg))
    row = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert res.status == "ok" and row["agent"] == "ok" and row["status"] == "ok"
    assert row["refs"] == "[1, 2]" and row["dry_run"] == 1


def test_exception_is_isolated_and_logged(conn, cfg):
    res = run_logged(BoomAgent(), ctx(conn, cfg))
    assert res.status == "error" and "RuntimeError" in res.reason
    assert conn.execute("SELECT status FROM agent_runs").fetchone()[0] == "error"


def test_live_flag_recorded(conn, cfg):
    run_logged(OkAgent(), ctx(conn, cfg, dry_run=False))
    assert conn.execute("SELECT dry_run FROM agent_runs").fetchone()[0] == 0


def test_orchestrator_stops_at_hold(conn, cfg):
    results = run_agents([OkAgent(), HoldAgent(), AfterAgent()], ctx(conn, cfg))
    assert [name for name, _ in results] == ["ok", "hold"]
    assert conn.execute("SELECT COUNT(*) FROM agent_runs WHERE agent='after'").fetchone()[0] == 0


def test_orchestrator_continues_after_error(conn, cfg):
    results = run_agents([BoomAgent(), AfterAgent()], ctx(conn, cfg))
    assert [r.status for _, r in results] == ["error", "ok"]


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        AgentResult("maybe")


def test_agent_runs_table_rolls_back(tmp_db, cfg):
    c = connect(tmp_db)
    migrate_up(c)
    migrate_down(c, target=4)
    assert c.execute("SELECT name FROM sqlite_master WHERE name='agent_runs'").fetchone() is None
    c.close()

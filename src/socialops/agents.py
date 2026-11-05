"""Agent runtime: a common result type, a logged runner, and error isolation.

An agent is any object with `name` and `run(ctx) -> AgentResult`. The runner records every run in
agent_runs. Exceptions are caught and logged as `error`, so one agent cannot take down the rest.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .config import Config
from .guard import Guard

log = logging.getLogger("socialops.agents")
STATUSES = frozenset({"ok", "blocked", "held", "skipped", "error"})


@dataclass
class AgentResult:
    status: str  # ok | blocked | held | skipped | error
    reason: str = ""
    refs: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"bad status {self.status!r}")


@dataclass
class Context:
    conn: sqlite3.Connection
    cfg: Config
    guard: Guard
    now: datetime
    store: object | None = None
    sleep: object = lambda s: None
    transport: object | None = None
    fetch_status: object | None = None


class Agent(Protocol):
    name: str

    def run(self, ctx: Context) -> AgentResult: ...


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_logged(agent: Agent, ctx: Context) -> AgentResult:
    started = _now_utc_str()
    try:
        result = agent.run(ctx)
    except Exception as exc:  # isolate: record and continue
        log.exception("agent %s failed", agent.name)
        result = AgentResult("error", f"{type(exc).__name__}: {str(exc)[:200]}")
    with ctx.conn:
        ctx.conn.execute(
            "INSERT INTO agent_runs (agent, status, reason, refs, dry_run, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent.name, result.status, result.reason[:500], json.dumps(result.refs),
             int(ctx.guard.dry_run), started, _now_utc_str()),
        )
    return result

"""Scheduler: runs due publishes on a timer (launchd), with a lock so runs never overlap.

Pipeline per run: schedule approved posts whose slot is near, then publish what is due.
Approval is the only human gate; the scheduler never approves anything.
"""

from __future__ import annotations

import fcntl
import os
import plistlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .agents import AgentResult, Context
from .builtin_agents import PublishAgent
from .config import Config
from .orchestrator import run_agents
from .post_queue import list_posts, transition

LOCK_PATH = Path.home() / "Library" / "Application Support" / "SocialOps" / "run-due.lock"
LABEL = "com.socialops.run-due"
RUN_EVERY_SECONDS = 300
LATE_GRACE = timedelta(hours=6)  # approved posts further past their slot are not auto-scheduled
_FMT = "%Y-%m-%dT%H:%M:%SZ"


class ScheduleAgent:
    """Moves approved posts to scheduled when their slot is upcoming or only slightly late."""

    name = "schedule"

    def run(self, ctx: Context) -> AgentResult:
        now = ctx.now.astimezone(timezone.utc)
        approved = list_posts(ctx.conn, status="approved")
        moved, missed = [], []
        for post in approved:
            slot = datetime.fromisoformat(post.scheduled_at.replace("Z", "+00:00"))
            if slot < now - LATE_GRACE:
                missed.append(post.id)
                continue
            transition(ctx.conn, post.id, "scheduled")
            moved.append(post.id)
        reason = f"scheduled {len(moved)}"
        if missed:
            reason += f", missed slot {len(missed)} (approved but past slot; reschedule or re-approve)"
        return AgentResult("ok", reason, moved + missed)


def run_due(conn: sqlite3.Connection, cfg: Config, store, guard, now: datetime,
            sleep=lambda s: None, transport=None, lock_path: Path = LOCK_PATH):
    """One scheduler tick. Returns [(agent_name, AgentResult)]."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [("lock", AgentResult("skipped", "another run is in progress"))]
        try:
            ctx = Context(conn=conn, cfg=cfg, guard=guard, now=now, store=store,
                          sleep=sleep, transport=transport)
            return run_agents([ScheduleAgent(), PublishAgent()], ctx)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def build_plist(python: str, repo_dir: str, log_dir: str, live: bool, config_path: str, db_path: str) -> bytes:
    args = [python, "-m", "socialops", "--config", config_path, "--db", db_path]
    args += ["--live"] if live else ["--dry-run"]
    args += ["run-due"]
    plist = {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": repo_dir,
        "StartInterval": RUN_EVERY_SECONDS,
        "RunAtLoad": False,
        "StandardOutPath": str(Path(log_dir) / "run-due.out.log"),
        "StandardErrorPath": str(Path(log_dir) / "run-due.err.log"),
        "ProcessType": "Background",
    }
    return plistlib.dumps(plist)


def install_schedule(live: bool, config_path: str, db_path: str, agents_dir: Path | None = None) -> Path:
    """Write the launchd job file. Does not load it; the user loads it with launchctl."""
    agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / "Library" / "Logs" / "SocialOps"
    log_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = str(Path(__file__).resolve().parents[2])
    target = agents_dir / f"{LABEL}.plist"
    target.write_bytes(build_plist(sys.executable, repo_dir, str(log_dir), live,
                                   str(Path(config_path).resolve()), str(Path(db_path).resolve())))
    os.chmod(target, 0o644)
    return target

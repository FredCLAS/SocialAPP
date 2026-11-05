"""Approval gates. Every gate kind needs a recorded human decision before the action runs."""

from __future__ import annotations

import sqlite3

from .post_queue import APPROVERS

GATE_KINDS = frozenset({"post", "reply", "cold_send", "boost", "visibility", "connect", "qualification"})
DECISIONS = frozenset({"approved", "rejected"})


class GateError(ValueError):
    pass


def record_decision(conn: sqlite3.Connection, kind: str, ref_id: int, actor: str, decision: str,
                    note: str | None = None) -> None:
    if kind not in GATE_KINDS:
        raise GateError(f"unknown gate kind {kind!r}")
    if decision not in DECISIONS:
        raise GateError(f"decision must be approved or rejected, got {decision!r}")
    if actor not in APPROVERS:
        raise GateError(f"{actor!r} is not an approver")
    with conn:
        conn.execute(
            "INSERT INTO approvals (kind, ref_id, decided_by, decision, note) VALUES (?, ?, ?, ?, ?)",
            (kind, ref_id, actor, decision, note),
        )


def is_approved(conn: sqlite3.Connection, kind: str, ref_id: int) -> bool:
    """True only if the most recent decision for this item is 'approved'."""
    row = conn.execute(
        "SELECT decision FROM approvals WHERE kind = ? AND ref_id = ? ORDER BY id DESC LIMIT 1",
        (kind, ref_id),
    ).fetchone()
    return row is not None and row["decision"] == "approved"

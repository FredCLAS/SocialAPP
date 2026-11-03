"""Dry-run guard. Every outbound write goes through `Guard.write` before it happens.

In dry-run mode the write is not performed; the intended action is logged and returned
so callers and tests can assert on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("socialops.guard")


class LiveWriteBlocked(RuntimeError):
    """Raised when code tries to perform a live write while dry-run is on and not allowed."""


@dataclass
class Guard:
    dry_run: bool = True
    would_have: list[str] = field(default_factory=list)

    def write(self, description: str) -> bool:
        """Return True if the caller may perform the write. False means dry-run: skip it.

        `description` is human-readable and must not contain secrets.
        """
        if self.dry_run:
            self.would_have.append(description)
            log.info("DRY-RUN would: %s", description)
            return False
        log.info("LIVE: %s", description)
        return True

    def require_live(self, description: str) -> None:
        """For code paths that must never run in dry-run (e.g. paid or public actions)."""
        if self.dry_run:
            raise LiveWriteBlocked(f"blocked in dry-run: {description}")

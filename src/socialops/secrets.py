"""Secret storage. Tokens live in macOS Keychain only; never in config, DB, or logs.

Service names follow `socialops.<brand>.<platform>` (or `socialops.<service>` for
account-wide keys such as the Claude API key). Account is the token role, e.g. `access_token`.
"""

from __future__ import annotations

import subprocess
from typing import Protocol


class SecretNotFound(KeyError):
    pass


class SecretStore(Protocol):
    def get(self, service: str, account: str) -> str: ...
    def set(self, service: str, account: str, value: str) -> None: ...
    def delete(self, service: str, account: str) -> None: ...


def service_name(brand: str, platform: str | None = None) -> str:
    parts = ["socialops", brand] + ([platform] if platform else [])
    return ".".join(parts)


class KeychainStore:
    """Wraps the `security` CLI (login keychain).

    Known limitation: `security add-generic-password -w <value>` passes the secret in argv,
    visible to other processes for the duration of the call. Replace with a
    Security.framework call (ctypes) before Phase B live use. Tracked in docs/open-questions.md.
    """

    def __init__(self, security_bin: str = "/usr/bin/security") -> None:
        self._bin = security_bin

    def get(self, service: str, account: str) -> str:
        proc = subprocess.run(
            [self._bin, "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SecretNotFound(f"{service}/{account}")
        return proc.stdout.rstrip("\n")

    def set(self, service: str, account: str, value: str) -> None:
        proc = subprocess.run(
            [self._bin, "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"keychain write failed for {service}/{account}")

    def delete(self, service: str, account: str) -> None:
        subprocess.run(
            [self._bin, "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            text=True,
            check=False,
        )


class MemorySecretStore:
    """In-memory store for tests. Never used at runtime."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str:
        try:
            return self._data[(service, account)]
        except KeyError as exc:
            raise SecretNotFound(f"{service}/{account}") from exc

    def set(self, service: str, account: str, value: str) -> None:
        self._data[(service, account)] = value

    def delete(self, service: str, account: str) -> None:
        self._data.pop((service, account), None)

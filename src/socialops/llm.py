"""Claude API client for caption drafting.

Plain urllib, no SDK. The API key comes from Keychain (service socialops.claude, account api_key)
and is never logged, printed, or put in an exception message.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


class DraftClient(Protocol):
    def complete(self, system: str, user: str, model: str, max_tokens: int) -> Completion: ...


class AnthropicClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self._key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:
        return "AnthropicClient(api_key=***)"

    def complete(self, system: str, user: str, model: str, max_tokens: int) -> Completion:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": self._key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise LlmError(f"Claude API returned HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise LlmError("Claude API unreachable") from None

        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        if not text:
            raise LlmError("Claude API returned no text")
        usage = payload.get("usage", {})
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


def client_from_keychain(store) -> AnthropicClient:
    return AnthropicClient(store.get("socialops.claude", "api_key"))

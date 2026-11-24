"""Microsoft Graph mail: send (Mail.Send) and read the inbox for replies (Mail.Read).

Endpoints are the documented Graph v1.0 mail routes. Live use still needs the Entra app consent
(delegated scopes) and a connected token on this Mac. Tokens are never logged or put in URLs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .secrets import SecretNotFound, SecretStore

GRAPH = "https://graph.microsoft.com/v1.0"
SERVICE = "socialops.microsoft"
SCOPES = "Mail.Send Mail.Read offline_access"
Transport = Callable[[str, str, dict | None, dict], tuple[int, dict]]


class MailError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


def default_transport(method: str, url: str, body: dict | None, headers: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MailError("network failure", ambiguous=(method == "POST")) from exc


class MailClient:
    def __init__(self, token: str, transport: Transport = default_transport) -> None:
        self._token = token
        self._transport = transport

    def __repr__(self) -> str:
        return "MailClient(token=***)"

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"authorization": f"Bearer {self._token}"}
        try:
            status, data = self._transport(method, f"{GRAPH}{path}", body, headers)
        except MailError:
            raise
        except Exception as exc:  # transport failure
            raise MailError("network failure", ambiguous=(method == "POST")) from exc
        if status >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            ambiguous = method == "POST" and (status >= 500 or status == 429)
            raise MailError(f"{method} {path.split('?')[0]}: HTTP {status} {str(err.get('code', ''))[:60]}",
                            status=status, ambiguous=ambiguous)
        return status, data

    def send_mail(self, to: str, subject: str, text: str) -> None:
        self._call("POST", "/me/sendMail", {
            "message": {"subject": subject[:200], "body": {"contentType": "Text", "content": text},
                        "toRecipients": [{"emailAddress": {"address": to}}]},
            "saveToSentItems": True,
        })

    def inbox_previews(self, top: int = 100) -> list[dict]:
        query = urllib.parse.urlencode({"$select": "from,receivedDateTime,subject,bodyPreview",
                                        "$top": str(top), "$orderby": "receivedDateTime desc"})
        _, data = self._call("GET", f"/me/mailFolders/inbox/messages?{query}")
        return data.get("value", [])


def refresh_token(store: SecretStore, client_id: str, token_url: str, transport: Transport = default_transport) -> str:
    """Exchange the stored refresh token for a new access token. Stores the rotated refresh token."""
    try:
        current = store.get(SERVICE, "refresh_token")
    except SecretNotFound as exc:
        raise MailError("Microsoft mail not connected on this Mac") from exc
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": current,
                                   "client_id": client_id, "scope": SCOPES}).encode("utf-8")
    req = urllib.request.Request(token_url, data=body, method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise MailError(f"token refresh failed: HTTP {exc.code}") from None
    store.set(SERVICE, "access_token", payload["access_token"])
    if payload.get("refresh_token"):
        store.set(SERVICE, "refresh_token", payload["refresh_token"])
    return payload["access_token"]


def access_token(store: SecretStore, client_id: str, token_url: str) -> str:
    try:
        return store.get(SERVICE, "access_token")
    except SecretNotFound:
        return refresh_token(store, client_id, token_url)

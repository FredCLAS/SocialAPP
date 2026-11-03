"""OAuth 2.0 authorization code + PKCE for Canva Connect, run on the user's own Mac.

Flow: `auth canva` prints an authorize URL, the browser sends the user to Canva, Canva redirects
to http://127.0.0.1:<port>/callback, and the code is exchanged for tokens. Tokens go to Keychain.
Each Mac stores its own user's tokens. Nothing is stored in the repo or the database.

Endpoints and client settings come from [canva] in brands.toml so they can be corrected
without code changes. Verify them against Canva's developer docs before first use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from .secrets import SecretNotFound, SecretStore

SERVICE = "socialops.canva"
REFRESH_MARGIN_SECONDS = 300  # refresh when less than 5 minutes remain


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanvaOAuthConfig:
    client_id: str
    scopes: tuple[str, ...]
    authorize_url: str
    token_url: str
    redirect_port: int = 8765

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"


# Transport: (url, form_fields, headers) -> parsed JSON. Injected in tests.
Transport = Callable[[str, dict, dict], dict]


def default_transport(url: str, form: dict, headers: dict) -> dict:
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/x-www-form-urlencoded", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"token endpoint returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise OAuthError("token endpoint unreachable") from None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = _b64url(secrets.token_bytes(32))  # 43 chars, within RFC 7636 limits
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(cfg: CanvaOAuthConfig, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(cfg.scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{cfg.authorize_url}?{urllib.parse.urlencode(params)}"


def parse_callback(path: str, expected_state: str) -> str:
    """Return the authorization code from the callback path, or raise."""
    parsed = urllib.parse.urlparse(path)
    if parsed.path != "/callback":
        raise OAuthError("unexpected callback path")
    qs = urllib.parse.parse_qs(parsed.query)
    if "error" in qs:
        raise OAuthError(f"authorization refused: {qs['error'][0]}")
    if qs.get("state", [""])[0] != expected_state:
        raise OAuthError("state mismatch; possible forged callback")
    code = qs.get("code", [""])[0]
    if not code:
        raise OAuthError("callback had no code")
    return code


def wait_for_code(cfg: CanvaOAuthConfig, state: str, timeout: float = 300.0) -> str:
    """Listen once on 127.0.0.1 for the redirect and return the authorization code."""
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            try:
                result["code"] = parse_callback(self.path, state)
                body = "Connected. You can close this window."
                status = 200
            except OAuthError as exc:
                result["error"] = str(exc)
                body = "Connection failed. Return to the terminal."
                status = 400
            self.send_response(status)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done.set()

        def log_message(self, *args):  # keep the terminal and logs free of request lines
            return

    server = HTTPServer(("127.0.0.1", cfg.redirect_port), Handler)
    try:
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        if not done.wait(timeout):
            raise OAuthError("timed out waiting for Canva redirect")
    finally:
        server.server_close()
    if "error" in result:
        raise OAuthError(result["error"])
    return result["code"]


@dataclass
class CanvaTokens:
    store: SecretStore
    cfg: CanvaOAuthConfig
    client_secret_present: bool = False
    transport: Transport = field(default=default_transport)
    clock: Callable[[], float] = field(default=time.time)

    # --- storage -----------------------------------------------------------
    def _save(self, payload: dict, previous_refresh: str | None) -> None:
        access = payload.get("access_token")
        if not access:
            raise OAuthError("token response had no access_token")
        refresh = payload.get("refresh_token") or previous_refresh
        if not refresh:
            raise OAuthError("token response had no refresh_token")
        expires_in = int(payload.get("expires_in", 3600))
        self.store.set(SERVICE, "access_token", access)
        self.store.set(SERVICE, "refresh_token", refresh)  # rotated tokens replace the old one
        self.store.set(SERVICE, "expires_at", str(int(self.clock()) + expires_in))

    def is_connected(self) -> bool:
        try:
            self.store.get(SERVICE, "refresh_token")
            return True
        except SecretNotFound:
            return False

    def expires_at(self) -> int | None:
        try:
            return int(self.store.get(SERVICE, "expires_at"))
        except (SecretNotFound, ValueError):
            return None

    def clear(self) -> None:
        for account in ("access_token", "refresh_token", "expires_at"):
            self.store.delete(SERVICE, account)

    # --- requests ------------------------------------------------------------
    def _auth_headers(self) -> dict:
        if not self.client_secret_present:
            return {}
        secret = self.store.get(SERVICE, "client_secret")
        basic = base64.b64encode(f"{self.cfg.client_id}:{secret}".encode("utf-8")).decode("ascii")
        return {"authorization": f"Basic {basic}"}

    def exchange_code(self, code: str, verifier: str) -> None:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_uri,
            "code_verifier": verifier,
            "client_id": self.cfg.client_id,
        }
        self._save(self.transport(self.cfg.token_url, form, self._auth_headers()), previous_refresh=None)

    def refresh(self) -> str:
        try:
            current = self.store.get(SERVICE, "refresh_token")
        except SecretNotFound:
            raise OAuthError("not connected; run: python -m socialops auth canva") from None
        form = {
            "grant_type": "refresh_token",
            "refresh_token": current,
            "client_id": self.cfg.client_id,
        }
        self._save(self.transport(self.cfg.token_url, form, self._auth_headers()), previous_refresh=current)
        return self.store.get(SERVICE, "access_token")

    def valid_access_token(self) -> str:
        """Return a usable access token, refreshing first if it is close to expiry."""
        expires = self.expires_at()
        try:
            access = self.store.get(SERVICE, "access_token")
        except SecretNotFound:
            return self.refresh()
        if expires is None or expires - self.clock() < REFRESH_MARGIN_SECONDS:
            return self.refresh()
        return access

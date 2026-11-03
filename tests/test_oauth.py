import base64
import hashlib
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from socialops import oauth
from socialops.oauth import (
    SERVICE,
    CanvaOAuthConfig,
    CanvaTokens,
    OAuthError,
    build_authorize_url,
    make_pkce,
    parse_callback,
    wait_for_code,
)
from socialops.secrets import MemorySecretStore


def cfg(port=8765):
    return CanvaOAuthConfig(
        client_id="client-123",
        scopes=("design:content:read", "asset:write"),
        authorize_url="https://www.canva.com/api/oauth/authorize",
        token_url="https://api.canva.com/rest/v1/oauth/token",
        redirect_port=port,
    )


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, form, headers):
        self.calls.append({"url": url, "form": dict(form), "headers": dict(headers)})
        return self.responses.pop(0)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def tokens(store=None, transport=None, clock=None, secret=False):
    return CanvaTokens(
        store=store or MemorySecretStore(),
        cfg=cfg(),
        client_secret_present=secret,
        transport=transport or FakeTransport(),
        clock=clock or Clock(),
    )


# --- PKCE and URL -----------------------------------------------------------

def test_pkce_is_s256_of_verifier():
    verifier, challenge = make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert make_pkce()[0] != verifier  # fresh each time


def test_authorize_url_has_required_params():
    url = build_authorize_url(cfg(), state="st", challenge="ch")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["client-123"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert q["scope"] == ["design:content:read asset:write"]
    assert q["code_challenge"] == ["ch"] and q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["st"]


def test_authorize_url_has_no_secret():
    url = build_authorize_url(cfg(), state="st", challenge="ch")
    assert "client_secret" not in url


# --- callback parsing -----------------------------------------------------

def test_callback_returns_code_when_state_matches():
    assert parse_callback("/callback?code=abc&state=S1", "S1") == "abc"


def test_callback_rejects_state_mismatch():
    with pytest.raises(OAuthError, match="state mismatch"):
        parse_callback("/callback?code=abc&state=OTHER", "S1")


def test_callback_rejects_wrong_path():
    with pytest.raises(OAuthError, match="path"):
        parse_callback("/evil?code=abc&state=S1", "S1")


def test_callback_reports_user_refusal():
    with pytest.raises(OAuthError, match="refused"):
        parse_callback("/callback?error=access_denied&state=S1", "S1")


def test_callback_requires_code():
    with pytest.raises(OAuthError, match="no code"):
        parse_callback("/callback?state=S1", "S1")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_loopback_listener_receives_code():
    port = _free_port()
    c = cfg(port)
    out = {}

    def run():
        out["code"] = wait_for_code(c, state="S9", timeout=10)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=XYZ&state=S9", timeout=5) as r:
        assert r.status == 200
    t.join(10)
    assert out["code"] == "XYZ"


def test_loopback_listener_rejects_bad_state():
    port = _free_port()
    c = cfg(port)
    errors = {}

    def run():
        try:
            wait_for_code(c, state="S9", timeout=10)
        except OAuthError as exc:
            errors["e"] = str(exc)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=XYZ&state=WRONG", timeout=5)
    t.join(10)
    assert "state mismatch" in errors["e"]


# --- token exchange and refresh ---------------------------------------------

def test_exchange_stores_tokens_and_sends_verifier():
    store = MemorySecretStore()
    tr = FakeTransport({"access_token": "A1", "refresh_token": "R1", "expires_in": 14400})
    t = tokens(store, tr, Clock(1000))
    t.exchange_code("CODE", "VERIFIER")
    form = tr.calls[0]["form"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "CODE" and form["code_verifier"] == "VERIFIER"
    assert form["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert form["client_id"] == "client-123"
    assert store.get(SERVICE, "access_token") == "A1"
    assert store.get(SERVICE, "refresh_token") == "R1"
    assert t.expires_at() == 1000 + 14400


def test_no_auth_header_without_client_secret():
    tr = FakeTransport({"access_token": "A", "refresh_token": "R"})
    tokens(transport=tr).exchange_code("c", "v")
    assert tr.calls[0]["headers"] == {}


def test_basic_auth_header_with_client_secret():
    store = MemorySecretStore()
    store.set(SERVICE, "client_secret", "shh")
    tr = FakeTransport({"access_token": "A", "refresh_token": "R"})
    tokens(store, tr, secret=True).exchange_code("c", "v")
    expected = "Basic " + base64.b64encode(b"client-123:shh").decode()
    assert tr.calls[0]["headers"]["authorization"] == expected


def test_refresh_rotates_refresh_token():
    store = MemorySecretStore()
    store.set(SERVICE, "refresh_token", "OLD")
    tr = FakeTransport({"access_token": "A2", "refresh_token": "NEW", "expires_in": 3600})
    t = tokens(store, tr)
    assert t.refresh() == "A2"
    assert store.get(SERVICE, "refresh_token") == "NEW"
    assert tr.calls[0]["form"]["refresh_token"] == "OLD"


def test_refresh_keeps_old_token_if_none_returned():
    store = MemorySecretStore()
    store.set(SERVICE, "refresh_token", "KEEP")
    tr = FakeTransport({"access_token": "A3"})
    tokens(store, tr).refresh()
    assert store.get(SERVICE, "refresh_token") == "KEEP"


def test_valid_token_is_cached_when_fresh():
    store = MemorySecretStore()
    clock = Clock(1000)
    store.set(SERVICE, "access_token", "CACHED")
    store.set(SERVICE, "expires_at", str(1000 + 3600))
    store.set(SERVICE, "refresh_token", "R")
    tr = FakeTransport()
    assert tokens(store, tr, clock).valid_access_token() == "CACHED"
    assert tr.calls == []


def test_valid_token_refreshes_near_expiry():
    store = MemorySecretStore()
    clock = Clock(1000)
    store.set(SERVICE, "access_token", "OLD")
    store.set(SERVICE, "expires_at", str(1000 + 60))  # inside the 5-minute margin
    store.set(SERVICE, "refresh_token", "R")
    tr = FakeTransport({"access_token": "NEWA", "refresh_token": "R2", "expires_in": 3600})
    assert tokens(store, tr, clock).valid_access_token() == "NEWA"
    assert len(tr.calls) == 1


def test_not_connected_refresh_fails_clearly():
    with pytest.raises(OAuthError, match="not connected"):
        tokens().refresh()


def test_clear_removes_everything():
    store = MemorySecretStore()
    store.set(SERVICE, "refresh_token", "R")
    store.set(SERVICE, "access_token", "A")
    store.set(SERVICE, "expires_at", "1")
    t = tokens(store)
    t.clear()
    assert not t.is_connected() and t.expires_at() is None


def test_token_response_without_access_token_is_error():
    with pytest.raises(OAuthError, match="no access_token"):
        tokens(transport=FakeTransport({"refresh_token": "R"})).exchange_code("c", "v")


def test_default_transport_hides_form_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, None)

    monkeypatch.setattr(oauth.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OAuthError, match="HTTP 400") as exc:
        oauth.default_transport("https://x", {"refresh_token": "SECRET-R"}, {})
    assert "SECRET-R" not in str(exc.value)


def test_default_transport_posts_form(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["body"] = urllib.parse.parse_qs(req.data.decode())
        seen["ct"] = req.get_header("Content-type")
        return io.BytesIO(json.dumps({"access_token": "A"}).encode())

    monkeypatch.setattr(oauth.urllib.request, "urlopen", fake_urlopen)
    out = oauth.default_transport("https://x", {"grant_type": "refresh_token"}, {})
    assert out == {"access_token": "A"}
    assert seen["body"]["grant_type"] == ["refresh_token"]
    assert seen["ct"] == "application/x-www-form-urlencoded"

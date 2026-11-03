import io
import json
import urllib.error

import pytest

from socialops import llm
from socialops.llm import AnthropicClient, LlmError, client_from_keychain
from socialops.secrets import MemorySecretStore

KEY = "sk-ant-test-0123456789"


class _Resp(io.BytesIO):
    pass


def _ok_payload():
    return json.dumps({
        "content": [{"type": "text", "text": '{"caption": "x"}'}],
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }).encode()


def test_request_shape_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _Resp(_ok_payload())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    out = AnthropicClient(KEY).complete("sys", "usr", "claude-haiku-5-5", 300)
    assert captured["url"] == llm.API_URL
    assert captured["headers"]["x-api-key"] == KEY
    assert captured["headers"]["anthropic-version"] == llm.API_VERSION
    assert captured["body"]["model"] == "claude-haiku-5-5"
    assert captured["body"]["max_tokens"] == 300
    assert captured["body"]["messages"] == [{"role": "user", "content": "usr"}]
    assert out.text == '{"caption": "x"}' and out.input_tokens == 12 and out.output_tokens == 7


def test_http_error_does_not_leak_key(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LlmError) as exc:
        AnthropicClient(KEY).complete("s", "u", "claude-haiku-5-5", 10)
    assert "401" in str(exc.value) and KEY not in str(exc.value)


def test_network_error_is_wrapped(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("dns failed")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LlmError, match="unreachable"):
        AnthropicClient(KEY).complete("s", "u", "claude-haiku-5-5", 10)


def test_empty_response_is_error(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(json.dumps({"content": []}).encode()))
    with pytest.raises(LlmError, match="no text"):
        AnthropicClient(KEY).complete("s", "u", "claude-haiku-5-5", 10)


def test_repr_hides_key():
    assert KEY not in repr(AnthropicClient(KEY))


def test_key_loaded_from_keychain_service():
    store = MemorySecretStore()
    store.set("socialops.claude", "api_key", KEY)
    client = client_from_keychain(store)
    assert client._key == KEY

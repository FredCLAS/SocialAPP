import logging

import pytest

from socialops.guard import Guard, LiveWriteBlocked
from socialops.logsetup import RedactFilter


def test_dry_run_blocks_write_and_records():
    g = Guard(dry_run=True)
    assert g.write("POST facebook page 123") is False
    assert g.would_have == ["POST facebook page 123"]


def test_live_allows_write():
    g = Guard(dry_run=False)
    assert g.write("POST facebook page 123") is True
    assert g.would_have == []


def test_require_live_blocks_in_dry_run():
    with pytest.raises(LiveWriteBlocked):
        Guard(dry_run=True).require_live("make video public")


def test_dry_run_publish_path_makes_no_network_calls(monkeypatch):
    """Dry-run guarantee: a write-path helper that checks the guard never reaches urllib/socket."""
    import socket
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("network call attempted in dry-run")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    g = Guard(dry_run=True)
    if g.write("POST https://graph.facebook.com/v20.0/page/feed"):
        urllib.request.urlopen("https://graph.facebook.com/v20.0/page/feed")
    assert g.would_have


def test_redact_filter_masks_tokens():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "GET /x?access_token=EAAB123&fields=id Authorization: Bearer abc.def", None, None)
    RedactFilter().filter(rec)
    out = rec.getMessage()
    assert "EAAB123" not in out and "abc.def" not in out
    assert "fields=id" in out

import subprocess

import pytest

from socialops.secrets import KeychainStore, MemorySecretStore, SecretNotFound, service_name


def test_service_name_format():
    assert service_name("clas_by_fred", "facebook") == "socialops.clas_by_fred.facebook"
    assert service_name("claude") == "socialops.claude"


def test_memory_store_round_trip():
    s = MemorySecretStore()
    s.set("socialops.pamela_art.instagram", "access_token", "tok")
    assert s.get("socialops.pamela_art.instagram", "access_token") == "tok"
    s.delete("socialops.pamela_art.instagram", "access_token")
    with pytest.raises(SecretNotFound):
        s.get("socialops.pamela_art.instagram", "access_token")


def test_keychain_store_uses_security_cli(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "find-generic-password":
            return subprocess.CompletedProcess(args, 0, stdout="secret-value\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    store = KeychainStore(security_bin="/usr/bin/security")
    assert store.get("socialops.claude", "api_key") == "secret-value"
    assert calls[0] == ["/usr/bin/security", "find-generic-password", "-s", "socialops.claude", "-a", "api_key", "-w"]


def test_keychain_missing_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 44, stdout="", stderr="not found"),
    )
    with pytest.raises(SecretNotFound):
        KeychainStore().get("socialops.x", "y")

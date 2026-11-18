from __future__ import annotations

import pytest

from your_assistant.cli import _guard_host, _is_loopback


def test_loopback_hosts_are_always_allowed():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert _is_loopback(host)
        assert _guard_host(host, allow_lan=False, cli_allow=False) == host


def test_lan_host_is_refused_without_explicit_permission():
    with pytest.raises(PermissionError):
        _guard_host("0.0.0.0", allow_lan=False, cli_allow=False)
    with pytest.raises(PermissionError):
        _guard_host("10.19.127.218", allow_lan=False, cli_allow=False)


def test_lan_host_needs_env_or_cli_switch():
    assert _guard_host("0.0.0.0", allow_lan=True, cli_allow=False) == "0.0.0.0"
    assert _guard_host("0.0.0.0", allow_lan=False, cli_allow=True) == "0.0.0.0"

"""Tests for auto-populated node metadata collection."""

from __future__ import annotations

import json
from unittest.mock import patch

from turnstone.core.node_info import (
    _collect_interfaces,
    _is_loopback_or_link_local,
    collect_node_info,
)


class TestCollectNodeInfo:
    def test_returns_dict(self):
        info = collect_node_info()
        assert isinstance(info, dict)

    def test_expected_keys_present(self):
        info = collect_node_info()
        # These should always be available on any platform
        assert "hostname" in info
        assert "os" in info
        assert "arch" in info
        assert "python" in info

    def test_values_json_serializable(self):
        info = collect_node_info()
        for _key, value in info.items():
            serialized = json.dumps(value)
            assert isinstance(serialized, str)

    def test_hostname_is_string(self):
        info = collect_node_info()
        assert isinstance(info["hostname"], str)
        assert len(info["hostname"]) > 0

    def test_cpu_count_is_int(self):
        info = collect_node_info()
        if "cpu_count" in info:
            assert isinstance(info["cpu_count"], int)
            assert info["cpu_count"] > 0

    def test_interfaces_is_dict(self):
        info = collect_node_info()
        if "interfaces" in info:
            assert isinstance(info["interfaces"], dict)
            for iface, ips in info["interfaces"].items():
                assert isinstance(iface, str)
                assert isinstance(ips, list)

    def test_one_field_failure_does_not_block_others(self):
        """Individual field failures must not prevent other fields from collecting."""
        with patch("turnstone.core.node_info.socket.gethostname", side_effect=OSError("boom")):
            info = collect_node_info()
        assert "hostname" not in info
        # Other fields should still be present
        assert "os" in info
        assert "arch" in info
        assert "python" in info

    def test_none_value_excluded(self):
        with patch("turnstone.core.node_info.os.cpu_count", return_value=None):
            info = collect_node_info()
        assert "cpu_count" not in info
        assert "hostname" in info

    def test_interface_failure_does_not_block_fields(self):
        """Interface collection failure must not prevent scalar fields."""
        with patch(
            "turnstone.core.node_info._collect_interfaces",
            side_effect=RuntimeError("boom"),
        ):
            info = collect_node_info()
        assert "interfaces" not in info
        assert "hostname" in info
        assert "os" in info


class TestCollectInterfaces:
    def test_returns_dict(self):
        result = _collect_interfaces()
        assert isinstance(result, dict)

    def test_values_are_string_lists(self):
        result = _collect_interfaces()
        for label, ips in result.items():
            assert isinstance(label, str)
            assert isinstance(ips, list)
            for ip in ips:
                assert isinstance(ip, str)

    def test_no_loopback_in_results(self):
        result = _collect_interfaces()
        for _label, ips in result.items():
            for ip in ips:
                assert not ip.startswith("127.")
                assert ip != "::1"
                assert not ip.startswith("fe80:")

    def test_getaddrinfo_oserror_returns_empty(self):
        with patch(
            "turnstone.core.node_info.socket.getaddrinfo",
            side_effect=OSError("no network"),
        ):
            result = _collect_interfaces()
        assert result == {}

    def test_all_loopback_returns_empty(self):
        import socket

        mock_addrs = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
        ]
        with patch("turnstone.core.node_info.socket.getaddrinfo", return_value=mock_addrs):
            result = _collect_interfaces()
        assert result == {}


class TestIsLoopbackOrLinkLocal:
    def test_ipv4_loopback(self):
        assert _is_loopback_or_link_local("127.0.0.1") is True
        assert _is_loopback_or_link_local("127.0.1.1") is True

    def test_ipv6_loopback(self):
        assert _is_loopback_or_link_local("::1") is True

    def test_link_local(self):
        assert _is_loopback_or_link_local("fe80::1") is True
        assert _is_loopback_or_link_local("fe80:abc::def") is True

    def test_normal_addresses(self):
        assert _is_loopback_or_link_local("10.0.0.5") is False
        assert _is_loopback_or_link_local("192.168.1.1") is False
        assert _is_loopback_or_link_local("2001:db8::1") is False

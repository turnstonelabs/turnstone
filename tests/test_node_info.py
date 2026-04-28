"""Tests for auto-populated node metadata collection."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from turnstone.core import node_info
from turnstone.core.node_info import (
    _collect_interfaces,
    _detect_aws_metadata,
    _detect_azure_metadata,
    _detect_cloud_metadata,
    _detect_cloud_provider_from_dmi,
    _detect_cpu_model,
    _detect_gcp_metadata,
    _detect_gpus,
    _detect_memory_gb,
    _imds_field,
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


# ---------------------------------------------------------------------------
# Kernel-interface helpers — capability detection
# ---------------------------------------------------------------------------


def _seed_drm_layout(tmp_path, cards):
    """Build a fake ``/sys/class/drm`` layout under ``tmp_path``.

    ``cards`` is a list of ``(name, vendor_id, device_id)`` tuples.
    Use ``vendor_id=None`` to skip writing the vendor file (simulates
    a permission/missing-attr failure that the detector must skip
    cleanly).  Returns the DRM root path.
    """
    drm = tmp_path / "drm"
    drm.mkdir()
    for name, vendor_id, device_id in cards:
        device_dir = drm / name / "device"
        device_dir.mkdir(parents=True)
        if vendor_id is not None:
            (device_dir / "vendor").write_text(vendor_id + "\n")
        if device_id is not None:
            (device_dir / "device").write_text(device_id + "\n")
    return str(drm)


class TestDetectGPUs:
    """Sysfs-DRM enumeration — vendor-agnostic, no userspace binary."""

    def test_returns_empty_when_drm_dir_missing(self, monkeypatch):
        monkeypatch.setattr(node_info, "_DRM_DIR", "/nonexistent/path/that/should/not/exist")
        assert _detect_gpus() == []

    def test_returns_empty_when_no_card_dirs(self, tmp_path, monkeypatch):
        # Empty /sys/class/drm — no GPUs registered.
        drm = tmp_path / "drm"
        drm.mkdir()
        monkeypatch.setattr(node_info, "_DRM_DIR", str(drm))
        assert _detect_gpus() == []

    def test_detects_nvidia_gpu(self, tmp_path, monkeypatch):
        drm_dir = _seed_drm_layout(tmp_path, [("card0", "0x10de", "0x2330")])
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        gpus = _detect_gpus()
        assert len(gpus) == 1
        assert gpus[0] == {
            "index": "0",
            "vendor": "nvidia",
            "pci_vendor": "0x10de",
            "pci_device": "0x2330",
        }

    def test_detects_amd_gpu(self, tmp_path, monkeypatch):
        drm_dir = _seed_drm_layout(tmp_path, [("card0", "0x1002", "0x74a1")])
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        gpus = _detect_gpus()
        assert len(gpus) == 1
        assert gpus[0]["vendor"] == "amd"

    def test_detects_intel_gpu(self, tmp_path, monkeypatch):
        drm_dir = _seed_drm_layout(tmp_path, [("card0", "0x8086", "0x56a0")])
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        gpus = _detect_gpus()
        assert gpus[0]["vendor"] == "intel"

    def test_unknown_vendor_id_is_filtered_out(self, tmp_path, monkeypatch):
        """A DRM ``cardN`` whose PCI vendor isn't in the GPU
        allow-list (Hyper-V synthetic 0x1414, AWS Nitro VGA, QEMU
        virtio-gpu, etc.) MUST NOT count as a GPU.  Counting them
        mis-labels CPU-only VMs as GPU nodes — observed on a CI
        runner."""
        drm_dir = _seed_drm_layout(tmp_path, [("card0", "0xdead", "0xbeef")])
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        assert _detect_gpus() == []

    def test_hyper_v_synthetic_adapter_is_filtered_out(self, tmp_path, monkeypatch):
        """Specific regression: Hyper-V's synthetic display adapter
        (vendor 0x1414, device 0x06) registers a ``/sys/class/drm/
        card0`` entry on Linux but is NOT a compute GPU.  A CI
        runner reproduced this and came back with ``gpu_count=1``
        before the vendor allow-list filter."""
        drm_dir = _seed_drm_layout(tmp_path, [("card0", "0x1414", "0x06")])
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        assert _detect_gpus() == []

    def test_mixed_known_and_unknown_keeps_only_known(self, tmp_path, monkeypatch):
        """A node with a real GPU (NVIDIA) AND a synthetic display
        adapter (Hyper-V) only counts the real GPU."""
        drm_dir = _seed_drm_layout(
            tmp_path,
            [
                ("card0", "0x1414", "0x06"),  # Hyper-V synthetic
                ("card1", "0x10de", "0x2330"),  # NVIDIA H100
            ],
        )
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        gpus = _detect_gpus()
        assert len(gpus) == 1
        assert gpus[0]["vendor"] == "nvidia"
        assert gpus[0]["index"] == "1"

    def test_skips_render_nodes(self, tmp_path, monkeypatch):
        """``renderD*`` nodes are per-card render-only interfaces that
        share the same physical device as a ``cardN`` entry; counting
        them would double the GPU count.  The card-name regex
        excludes them."""
        drm = tmp_path / "drm"
        drm.mkdir()
        for name in ("card0", "renderD128"):
            device = drm / name / "device"
            device.mkdir(parents=True)
            (device / "vendor").write_text("0x10de")
            (device / "device").write_text("0x2330")
        monkeypatch.setattr(node_info, "_DRM_DIR", str(drm))
        gpus = _detect_gpus()
        assert len(gpus) == 1  # only card0, not renderD128

    def test_multi_gpu_node(self, tmp_path, monkeypatch):
        drm_dir = _seed_drm_layout(
            tmp_path,
            [
                ("card0", "0x10de", "0x2330"),
                ("card1", "0x10de", "0x2330"),
                ("card2", "0x10de", "0x2330"),
                ("card3", "0x10de", "0x2330"),
            ],
        )
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        gpus = _detect_gpus()
        assert len(gpus) == 4
        assert [g["index"] for g in gpus] == ["0", "1", "2", "3"]

    def test_card_with_missing_vendor_is_skipped(self, tmp_path, monkeypatch):
        """A card whose vendor file can't be read (permissions /
        partial sysfs) is silently skipped — the rest of the
        enumeration must still complete."""
        drm = tmp_path / "drm"
        drm.mkdir()
        # card0 has no vendor file; card1 is well-formed.
        (drm / "card0" / "device").mkdir(parents=True)
        good = drm / "card1" / "device"
        good.mkdir(parents=True)
        (good / "vendor").write_text("0x10de")
        (good / "device").write_text("0x2330")
        monkeypatch.setattr(node_info, "_DRM_DIR", str(drm))
        gpus = _detect_gpus()
        assert len(gpus) == 1
        assert gpus[0]["index"] == "1"


class TestDetectMemoryGB:
    def test_parses_meminfo(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        # 32 GiB = 32 * 1024 * 1024 KiB = 33554432 KiB
        meminfo.write_text(
            "MemTotal:       33554432 kB\n"
            "MemFree:         5000000 kB\n"
            "MemAvailable:   28000000 kB\n"
        )
        monkeypatch.setattr(node_info, "_MEMINFO_PATH", str(meminfo))
        assert _detect_memory_gb() == 32

    def test_rounds_down(self, tmp_path, monkeypatch):
        """31.5 GiB worth of KiB rounds down to 31 — operators that
        write ``filters={"memory_gb": 32}`` shouldn't match a node
        that's actually 31.5."""
        meminfo = tmp_path / "meminfo"
        # 31.5 GiB = 31.5 * 1024 * 1024 = 33030144 KiB
        meminfo.write_text(f"MemTotal:       {31 * 1024 * 1024 + 512 * 1024} kB\n")
        monkeypatch.setattr(node_info, "_MEMINFO_PATH", str(meminfo))
        assert _detect_memory_gb() == 31

    def test_returns_none_when_meminfo_missing(self, monkeypatch):
        monkeypatch.setattr(node_info, "_MEMINFO_PATH", "/nonexistent/meminfo")
        assert _detect_memory_gb() is None

    def test_returns_none_when_no_memtotal_line(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemFree:  5000000 kB\n")  # no MemTotal
        monkeypatch.setattr(node_info, "_MEMINFO_PATH", str(meminfo))
        assert _detect_memory_gb() is None


class TestDetectCPUModel:
    def test_parses_intel_brand(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text(
            "processor\t: 0\n"
            "model name\t: Intel(R) Xeon(R) Platinum 8488C\n"
            "cpu MHz\t\t: 2400.000\n"
            "processor\t: 1\n"
            "model name\t: Intel(R) Xeon(R) Platinum 8488C\n"
        )
        monkeypatch.setattr(node_info, "_CPUINFO_PATH", str(cpuinfo))
        assert _detect_cpu_model() == "Intel(R) Xeon(R) Platinum 8488C"

    def test_parses_amd_brand(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("model name\t: AMD EPYC 9654 96-Core Processor\n")
        monkeypatch.setattr(node_info, "_CPUINFO_PATH", str(cpuinfo))
        assert _detect_cpu_model() == "AMD EPYC 9654 96-Core Processor"

    def test_returns_none_on_arm_with_no_model_name(self, tmp_path, monkeypatch):
        """ARM cpuinfo uses ``Hardware`` / ``Processor`` instead of
        ``model name``; we return None and operators set ``cpu_model``
        in [metadata] config to taste."""
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("Hardware\t: Apple M1\nProcessor\t: ARMv8\n")
        monkeypatch.setattr(node_info, "_CPUINFO_PATH", str(cpuinfo))
        assert _detect_cpu_model() is None

    def test_returns_none_when_cpuinfo_missing(self, monkeypatch):
        monkeypatch.setattr(node_info, "_CPUINFO_PATH", "/nonexistent/cpuinfo")
        assert _detect_cpu_model() is None


def _seed_dmi_layout(tmp_path, fields):
    """Build a fake /sys/class/dmi/id with given key→value text files."""
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    for key, value in fields.items():
        (dmi / key).write_text(value + "\n")
    return str(dmi)


class TestDetectCloudProviderFromDMI:
    """DMI-based cloud-provider detection — pure kernel interface."""

    def test_aws_via_sys_vendor(self, tmp_path, monkeypatch):
        dmi = _seed_dmi_layout(tmp_path, {"sys_vendor": "Amazon EC2"})
        monkeypatch.setattr(node_info, "_DMI_DIR", dmi)
        assert _detect_cloud_provider_from_dmi() == "aws"

    def test_aws_via_bios_vendor(self, tmp_path, monkeypatch):
        """Older Nitro instances set bios_vendor instead of sys_vendor."""
        dmi = _seed_dmi_layout(
            tmp_path,
            {"sys_vendor": "Xen", "bios_vendor": "Amazon EC2"},
        )
        monkeypatch.setattr(node_info, "_DMI_DIR", dmi)
        assert _detect_cloud_provider_from_dmi() == "aws"

    def test_gcp_via_sys_vendor(self, tmp_path, monkeypatch):
        dmi = _seed_dmi_layout(
            tmp_path,
            {"sys_vendor": "Google", "product_name": "Google Compute Engine"},
        )
        monkeypatch.setattr(node_info, "_DMI_DIR", dmi)
        assert _detect_cloud_provider_from_dmi() == "gcp"

    def test_azure_via_chassis_asset_tag(self, tmp_path, monkeypatch):
        """The chassis_asset_tag prefix distinguishes Azure VMs from
        plain Microsoft Hyper-V on baremetal — same sys_vendor, but
        only Azure VMs carry the well-known asset tag."""
        dmi = _seed_dmi_layout(
            tmp_path,
            {
                "sys_vendor": "Microsoft Corporation",
                "chassis_asset_tag": "7783-7084-3265-9085-8269-3286-77",
            },
        )
        monkeypatch.setattr(node_info, "_DMI_DIR", dmi)
        assert _detect_cloud_provider_from_dmi() == "azure"

    def test_microsoft_without_azure_tag_is_unknown(self, tmp_path, monkeypatch):
        """Plain Hyper-V on baremetal — Microsoft sys_vendor but no
        Azure asset tag.  Must not auto-detect as azure."""
        dmi = _seed_dmi_layout(
            tmp_path,
            {
                "sys_vendor": "Microsoft Corporation",
                "chassis_asset_tag": "Default string",
            },
        )
        monkeypatch.setattr(node_info, "_DMI_DIR", dmi)
        assert _detect_cloud_provider_from_dmi() == "unknown"

    def test_baremetal_is_unknown(self, tmp_path, monkeypatch):
        dmi = _seed_dmi_layout(tmp_path, {"sys_vendor": "Dell Inc.", "bios_vendor": "Dell Inc."})
        monkeypatch.setattr(node_info, "_DMI_DIR", dmi)
        assert _detect_cloud_provider_from_dmi() == "unknown"

    def test_missing_dmi_dir_is_unknown(self, monkeypatch):
        monkeypatch.setattr(node_info, "_DMI_DIR", "/nonexistent/dmi")
        assert _detect_cloud_provider_from_dmi() == "unknown"


class TestIMDSDetectors:
    """Vendor-specific IMDS parsers — exercise the body-shape parsing
    without making real network calls."""

    def test_aws_imds_v2_token_failure(self, monkeypatch):
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: None)
        assert _detect_aws_metadata() == {}

    def test_aws_imds_parses_identity_doc(self, monkeypatch):
        responses = iter(
            [
                "TOKEN-ABCD",  # PUT /api/token
                json.dumps(
                    {
                        "region": "us-east-1",
                        "availabilityZone": "us-east-1a",
                        "instanceType": "p5.48xlarge",
                        "instanceId": "i-0123456789abcdef0",
                    }
                ),  # GET /dynamic/instance-identity/document
            ]
        )
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: next(responses))
        result = _detect_aws_metadata()
        assert result == {
            "cloud_region": "us-east-1",
            "cloud_zone": "us-east-1a",
            "cloud_instance_type": "p5.48xlarge",
            "cloud_instance_id": "i-0123456789abcdef0",
        }

    def test_aws_malformed_identity_doc_returns_empty(self, monkeypatch):
        responses = iter(["TOKEN-ABCD", "not-json"])
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: next(responses))
        assert _detect_aws_metadata() == {}

    def test_gcp_zone_parsing(self, monkeypatch):
        # GCP returns paths like "projects/12345/zones/us-east1-a";
        # we surface the tail and derive region by chopping the
        # trailing "-a" letter.
        responses = {
            "zone": "projects/12345/zones/us-east1-a",
            "machine-type": "projects/12345/machineTypes/n1-standard-4",
            "id": "9876543210",
        }

        def fake(url, headers=None, **_kw):
            for key, body in responses.items():
                if url.endswith("/" + key):
                    return body
            return None

        monkeypatch.setattr(node_info, "_imds_get", fake)
        result = _detect_gcp_metadata()
        assert result["cloud_zone"] == "us-east1-a"
        assert result["cloud_region"] == "us-east1"
        assert result["cloud_instance_type"] == "n1-standard-4"
        assert result["cloud_instance_id"] == "9876543210"

    def test_gcp_no_zone_returns_empty(self, monkeypatch):
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: None)
        assert _detect_gcp_metadata() == {}

    def test_azure_compute_block_parsing(self, monkeypatch):
        body = json.dumps(
            {
                "compute": {
                    "location": "eastus",
                    "zone": "1",
                    "vmSize": "Standard_NC24ads_A100_v4",
                    "vmId": "abcd1234-...",
                }
            }
        )
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: body)
        result = _detect_azure_metadata()
        assert result == {
            "cloud_region": "eastus",
            "cloud_zone": "1",
            "cloud_instance_type": "Standard_NC24ads_A100_v4",
            "cloud_instance_id": "abcd1234-...",
        }

    def test_azure_missing_compute_block_returns_empty(self, monkeypatch):
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: json.dumps({}))
        assert _detect_azure_metadata() == {}


class TestDetectCloudMetadata:
    """End-to-end cloud metadata detection: DMI gate + IMDS probe."""

    def test_baremetal_skips_imds(self, monkeypatch):
        """No DMI cloud signal → no IMDS probe → empty result, no
        startup latency cost.  This is the property we wanted from
        the kernel-interface refactor."""
        called = {"imds": 0}

        def _spy(*args, **kwargs):
            called["imds"] += 1
            return "should-never-be-called"

        monkeypatch.setattr(node_info, "_detect_cloud_provider_from_dmi", lambda: "unknown")
        monkeypatch.setattr(node_info, "_imds_get", _spy)
        assert _detect_cloud_metadata() == {}
        assert called["imds"] == 0

    def test_aws_detection_path(self, monkeypatch):
        monkeypatch.setattr(node_info, "_detect_cloud_provider_from_dmi", lambda: "aws")
        monkeypatch.setattr(
            node_info,
            "_detect_aws_metadata",
            lambda: {"cloud_region": "us-west-2", "cloud_instance_type": "p4d.24xlarge"},
        )
        result = _detect_cloud_metadata()
        assert result["cloud_provider"] == "aws"
        assert result["cloud_region"] == "us-west-2"
        assert result["cloud_instance_type"] == "p4d.24xlarge"

    def test_imds_probe_failure_still_surfaces_provider(self, monkeypatch):
        """If DMI says we're on AWS but IMDS times out, we still
        surface ``cloud_provider=aws`` from DMI alone.  Operators
        can route on provider even when region/instance-type
        couldn't be probed."""
        monkeypatch.setattr(node_info, "_detect_cloud_provider_from_dmi", lambda: "aws")
        monkeypatch.setattr(node_info, "_detect_aws_metadata", lambda: {})
        result = _detect_cloud_metadata()
        assert result == {"cloud_provider": "aws"}

    def test_opt_out_skips_imds_but_keeps_provider(self, monkeypatch):
        """``TURNSTONE_AUTO_CLOUD_METADATA=0`` skips the network probe
        entirely.  ``cloud_provider`` from DMI still populates because
        it's a kernel interface, not a network call."""
        monkeypatch.setenv("TURNSTONE_AUTO_CLOUD_METADATA", "0")
        monkeypatch.setattr(node_info, "_detect_cloud_provider_from_dmi", lambda: "gcp")

        def _imds_should_not_run(*a, **kw):
            pytest.fail("IMDS probe must not run when TURNSTONE_AUTO_CLOUD_METADATA=0")

        monkeypatch.setattr(node_info, "_imds_get", _imds_should_not_run)
        result = _detect_cloud_metadata()
        assert result == {"cloud_provider": "gcp"}

    def test_imds_exception_does_not_propagate(self, monkeypatch):
        """A buggy IMDS parser (raises unexpectedly) must not crash
        the collector — the ``except Exception`` wrapper inside
        ``_detect_cloud_metadata`` swallows and logs."""
        monkeypatch.setattr(node_info, "_detect_cloud_provider_from_dmi", lambda: "azure")

        def _boom():
            raise RuntimeError("simulated parser bug")

        monkeypatch.setattr(node_info, "_detect_azure_metadata", _boom)
        result = _detect_cloud_metadata()
        # cloud_provider survives; region/zone are missing.
        assert result == {"cloud_provider": "azure"}


class TestCollectNodeInfoCapabilityIntegration:
    """End-to-end checks on the public ``collect_node_info`` entry
    point — confirms the new kernel-interface helpers wire up
    correctly and that one helper failing doesn't suppress the others."""

    def test_gpu_keys_appear_when_gpus_detected(self, monkeypatch):
        monkeypatch.setattr(
            node_info,
            "_detect_gpus",
            lambda: [
                {"index": "0", "vendor": "nvidia", "pci_vendor": "0x10de", "pci_device": "0x2330"},
            ],
        )
        info = collect_node_info()
        assert info["gpu_count"] == 1
        assert info["has_gpu"] is True
        assert info["gpu_vendors"] == ["nvidia"]
        assert info["gpu_has_nvidia"] is True
        assert info["gpus"][0]["pci_device"] == "0x2330"
        # Singular ``gpu_vendor`` is intentionally NOT exposed —
        # multi-vendor nodes would only be filterable under one
        # vendor, hiding them from the other; per-vendor booleans
        # avoid the false-negative.
        assert "gpu_vendor" not in info

    def test_gpu_keys_absent_when_no_gpus(self, monkeypatch):
        monkeypatch.setattr(node_info, "_detect_gpus", lambda: [])
        info = collect_node_info()
        for k in ("gpu_count", "gpu_vendors", "gpus", "has_gpu"):
            assert k not in info
        # No spurious ``gpu_has_*`` keys when there are no GPUs.
        assert not any(k.startswith("gpu_has_") for k in info)

    def test_multi_vendor_node_filterable_under_each_vendor(self, monkeypatch):
        """A mixed AMD+NVIDIA node MUST be filterable under both
        vendors.  Pre-fix the singular ``gpu_vendor`` flat key was
        set to ``vendors[0]`` (alphabetical first = ``amd``) and
        ``filters={"gpu_vendor": "nvidia"}`` would mismatch the
        NVIDIA card on the bus.  Per-vendor booleans avoid the
        false-negative entirely."""
        monkeypatch.setattr(
            node_info,
            "_detect_gpus",
            lambda: [
                {"index": "0", "vendor": "amd", "pci_vendor": "0x1002", "pci_device": "0x74a1"},
                {"index": "1", "vendor": "nvidia", "pci_vendor": "0x10de", "pci_device": "0x2330"},
            ],
        )
        info = collect_node_info()
        # Both per-vendor flags True — filter under EITHER vendor matches.
        assert info["gpu_has_amd"] is True
        assert info["gpu_has_nvidia"] is True
        # Sorted unique vendors carry the full list for tooling that
        # wants the set.
        assert info["gpu_vendors"] == ["amd", "nvidia"]
        assert info["gpu_count"] == 2
        assert info["has_gpu"] is True

    def test_memory_key_appears(self, monkeypatch):
        monkeypatch.setattr(node_info, "_detect_memory_gb", lambda: 256)
        info = collect_node_info()
        assert info["memory_gb"] == 256

    def test_memory_zero_omitted(self, monkeypatch):
        """A reading of 0 GiB is degenerate — likely a parse error
        rather than a real zero-RAM machine.  Skip the key rather
        than advertise a false value."""
        monkeypatch.setattr(node_info, "_detect_memory_gb", lambda: 0)
        info = collect_node_info()
        assert "memory_gb" not in info

    def test_cpu_model_key_appears(self, monkeypatch):
        monkeypatch.setattr(node_info, "_detect_cpu_model", lambda: "AMD EPYC 9654")
        info = collect_node_info()
        assert info["cpu_model"] == "AMD EPYC 9654"

    def test_cloud_keys_merged(self, monkeypatch):
        monkeypatch.setattr(
            node_info,
            "_detect_cloud_metadata",
            lambda: {
                "cloud_provider": "aws",
                "cloud_region": "us-east-1",
                "cloud_instance_type": "p5.48xlarge",
            },
        )
        info = collect_node_info()
        assert info["cloud_provider"] == "aws"
        assert info["cloud_region"] == "us-east-1"
        assert info["cloud_instance_type"] == "p5.48xlarge"

    def test_one_capability_failure_does_not_block_others(self, monkeypatch):
        """If GPU detection raises, memory + cpu + cloud detection
        must still run.  Mirrors the existing per-field-failsafe
        contract on the basic fields."""

        def _boom():
            raise RuntimeError("simulated DRM failure")

        monkeypatch.setattr(node_info, "_detect_gpus", _boom)
        monkeypatch.setattr(node_info, "_detect_memory_gb", lambda: 64)
        monkeypatch.setattr(node_info, "_detect_cpu_model", lambda: "AMD EPYC 9654")
        info = collect_node_info()
        assert "gpu_count" not in info
        assert info["memory_gb"] == 64
        assert info["cpu_model"] == "AMD EPYC 9654"

    def test_synthetic_display_adapter_does_not_register_as_gpu(self, tmp_path, monkeypatch):
        """End-to-end: a Hyper-V synthetic display adapter on the
        host's /sys/class/drm doesn't reach ``collect_node_info``'s
        GPU surface at all.  The vendor allow-list filter in
        ``_detect_gpus`` drops it before it gets to ``has_gpu`` /
        ``gpu_count`` / ``gpu_has_*``.  Pre-fix this would mis-label
        a CPU-only Hyper-V VM as a GPU node."""
        drm_dir = _seed_drm_layout(tmp_path, [("card0", "0x1414", "0x06")])
        monkeypatch.setattr(node_info, "_DRM_DIR", drm_dir)
        info = collect_node_info()
        for k in ("gpu_count", "has_gpu", "gpus", "gpu_vendors"):
            assert k not in info
        assert not any(k.startswith("gpu_has_") for k in info)


class TestIMDSFieldSanitiser:
    """``_imds_field`` strips control chars + length-caps each
    persisted value.  Defense-in-depth against an attacker-controlled
    IMDS responder injecting prompt-payload bytes into coord LLM
    context via ``list_nodes``."""

    def test_passes_clean_string_through(self):
        assert _imds_field("us-east-1") == "us-east-1"

    def test_strips_control_characters(self):
        # Newline + NUL would otherwise survive into list_nodes
        # output and could break parsing or inject content into
        # downstream renderers.
        out = _imds_field("us-east-1\n\x00 injected")
        assert "\n" not in (out or "")
        assert "\x00" not in (out or "")
        assert out == "us-east-1 injected"

    def test_caps_length(self):
        from turnstone.core.node_info import _IMDS_MAX_FIELD_CHARS

        out = _imds_field("X" * (_IMDS_MAX_FIELD_CHARS * 4))
        assert out is not None
        assert len(out) == _IMDS_MAX_FIELD_CHARS

    def test_returns_none_for_non_string(self):
        assert _imds_field(None) is None
        assert _imds_field(42) is None
        assert _imds_field(["us-east-1"]) is None

    def test_returns_none_for_empty_or_whitespace(self):
        assert _imds_field("") is None
        assert _imds_field("   ") is None


class TestIMDSResponseHardening:
    """Regression guards on the AWS / Azure non-dict-JSON paths and
    the GCP hostname → IP-literal switch."""

    def test_aws_handles_non_dict_json_without_raising(self, monkeypatch):
        """If a hostile/misbehaving IMDS returns a JSON list rather
        than the documented identity-document object, the previous
        shape would AttributeError on ``doc.get(src)``.  The
        ``isinstance(doc, dict)`` guard makes this a clean miss."""
        responses = iter(["TOKEN-ABCD", "[1, 2, 3]"])
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: next(responses))
        # Must not raise.
        assert _detect_aws_metadata() == {}

    def test_aws_handles_scalar_json_without_raising(self, monkeypatch):
        responses = iter(["TOKEN-ABCD", "42"])
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: next(responses))
        assert _detect_aws_metadata() == {}

    def test_azure_handles_non_dict_json_without_raising(self, monkeypatch):
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: '["not-an-object"]')
        # Must not raise.
        assert _detect_azure_metadata() == {}

    def test_gcp_uses_link_local_ip_literal(self, monkeypatch):
        """The GCP probe must target ``169.254.169.254`` directly so
        a host with attacker-controlled DNS can't redirect the probe
        via ``metadata.google.internal``.  Pin the URL prefix."""
        called_urls: list[str] = []

        def _spy(url, *args, **kwargs):
            called_urls.append(url)
            return None  # all probes fail; that's fine — we're inspecting URLs

        monkeypatch.setattr(node_info, "_imds_get", _spy)
        _detect_gcp_metadata()
        assert called_urls, "GCP detector must issue at least one IMDS call"
        for url in called_urls:
            assert url.startswith("http://169.254.169.254/"), (
                f"GCP probe leaked through DNS-resolvable hostname: {url}"
            )

    def test_imds_field_sanitises_aws_response(self, monkeypatch):
        """End-to-end: a hostile IMDS response body with a control
        character lands sanitised in the AWS detector's output."""
        responses = iter(
            [
                "TOKEN-ABCD",
                json.dumps(
                    {
                        "region": "us-east-1\nrm -rf",  # control char injection
                        "instanceType": "p5.48xlarge",
                    }
                ),
            ]
        )
        monkeypatch.setattr(node_info, "_imds_get", lambda *a, **kw: next(responses))
        result = _detect_aws_metadata()
        assert "\n" not in result["cloud_region"]
        # Sanitiser preserves the leading meaningful prefix, drops
        # the control character.  Trailing content survives stripped
        # of control chars.
        assert "us-east-1" in result["cloud_region"]
        assert "rm -rf" in result["cloud_region"]  # text still there, just newline-free

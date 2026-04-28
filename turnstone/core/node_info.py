"""Collect auto-populated node metadata using stdlib + kernel interfaces.

Two collection layers:

- Always-available basics — ``hostname``, ``fqdn``, ``os``, ``arch``,
  ``python``, ``cpu_count``, ``interfaces`` — pulled from
  ``platform``/``socket``/``os`` and never block.
- Capability detection from Linux kernel interfaces — DRM sysfs for
  GPUs, ``/proc/meminfo`` for RAM, ``/proc/cpuinfo`` for the CPU
  model, ``/sys/class/dmi/id/*`` for the cloud provider, plus an
  IMDS probe for cloud region/instance-type.  No userspace binaries
  (``nvidia-smi`` / ``rocm-smi`` / ``lspci``) on PATH — kernel
  interfaces work the same way regardless of vendor and don't depend
  on which optional package the operator happened to install.

Operators can still override any auto-detected key via the
``[metadata]`` section of ``config.toml`` (last-write-wins on the
``(node_id, key)`` upsert in ``set_node_metadata_bulk``), so the
auto-detection layer is strictly additive — operators get sensible
defaults, custom deployments still get the final say.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import socket
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


def _is_loopback_or_link_local(addr: str) -> bool:
    """Return True for loopback and link-local addresses."""
    return addr.startswith("127.") or addr == "::1" or addr.startswith("fe80:")


def _collect_interfaces() -> dict[str, list[str]]:
    """Best-effort host IP collection using stdlib.

    Returns a mapping from hostname to non-loopback IP addresses.
    Without psutil/netifaces, per-interface resolution is not available
    from stdlib alone, so we report resolved host addresses honestly.
    """
    result: dict[str, list[str]] = {}
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        ips = sorted({str(a[4][0]) for a in addrs if not _is_loopback_or_link_local(str(a[4][0]))})
        if ips:
            result[hostname] = ips
    except OSError:
        log.debug("node_info: interface collection failed", exc_info=True)
    return result


# ---------------------------------------------------------------------------
# Kernel-interface helpers
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str | None:
    """Read a small kernel-pseudofs file and return its stripped text.

    Returns ``None`` on any OSError so callers can treat the
    "file/sysfs not present" path as a clean miss.  Decoded as UTF-8
    with ``errors="replace"``: a stray non-UTF-8 byte in DMI strings
    becomes ``U+FFFD`` rather than raising, which is the right call
    for substring-matching against vendor strings — the original
    bytes don't need to round-trip.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


# DRM (Direct Rendering Manager) sysfs — every PCI GPU registers a
# ``cardN`` directory here regardless of vendor (NVIDIA, AMD, Intel,
# ARM Mali, etc.).  Reading the underlying PCI device's ``vendor`` and
# ``device`` files gives us vendor identification without depending on
# any vendor-specific userspace binary being installed or on PATH.
_DRM_DIR = "/sys/class/drm"
_CARD_DIR_RE = re.compile(r"^card\d+$")

# PCI vendor IDs.  Source: pcisig.com canonical list.  We surface
# friendly names for the four vendors that ship GPUs into AI
# infrastructure today; everything else lands as ``unknown`` and the
# raw vendor/device IDs are kept on the row so an operator can map
# them out-of-band.
_PCI_VENDOR_NAMES: dict[str, str] = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x8086": "intel",
    "0x106b": "apple",
}


def _detect_gpus() -> list[dict[str, str]]:
    """Enumerate compute-capable GPUs via the Linux DRM sysfs interface.

    For each ``/sys/class/drm/cardN`` directory, read the underlying
    PCI device's ``vendor`` and ``device`` IDs and KEEP only cards
    whose PCI vendor is in :data:`_PCI_VENDOR_NAMES` (NVIDIA / AMD /
    Intel / Apple).  Returns a list of ``{"index", "vendor",
    "pci_vendor", "pci_device"}`` dicts.

    Why filter on the vendor allow-list rather than count every DRM
    card?  Hypervisor synthetic display adapters (Hyper-V's adapter
    at vendor ``0x1414`` / device ``0x06``, AWS Nitro's basic VGA,
    QEMU's ``virtio-gpu``, etc.) all register a ``cardN`` entry on
    the host but are NOT compute-capable GPUs.  Counting them
    mis-labels CPU-only VMs as GPU nodes — observed in CI on a
    Hyper-V runner that came back with ``gpu_count=1``.  An exotic
    accelerator that isn't in the allow-list lands as a no-op here;
    operators who need to expose one set ``gpu_count`` + the relevant
    flags in ``[metadata]`` config to override.

    Returns empty list on non-Linux, missing sysfs, or any read
    failure.  Containers see whatever DRM nodes the host mapped in;
    a container with no GPU mapped returns empty cleanly.
    """
    if not os.path.isdir(_DRM_DIR):
        return []
    try:
        entries = sorted(os.listdir(_DRM_DIR))
    except OSError:
        return []
    gpus: list[dict[str, str]] = []
    for name in entries:
        # Skip ``renderD*`` nodes — they're per-card render-only
        # interfaces that duplicate ``cardN`` for the same physical
        # device.  Counting them would double the GPU count.
        if not _CARD_DIR_RE.match(name):
            continue
        device_dir = os.path.join(_DRM_DIR, name, "device")
        vendor_id = _read_text(os.path.join(device_dir, "vendor"))
        device_id = _read_text(os.path.join(device_dir, "device"))
        if not vendor_id or not device_id:
            continue
        vendor_name = _PCI_VENDOR_NAMES.get(vendor_id)
        if vendor_name is None:
            # Not on the GPU-vendor allow-list — skip to avoid
            # mis-counting Hyper-V / QEMU / AWS Nitro synthetic
            # display adapters as compute GPUs.
            continue
        gpus.append(
            {
                "index": name[4:],  # strip "card" prefix
                "vendor": vendor_name,
                "pci_vendor": vendor_id,
                "pci_device": device_id,
            }
        )
    return gpus


# ``/proc/meminfo`` MemTotal field is in KiB.  Linux only — falls
# through to None on Darwin/Windows/missing-procfs containers.
_MEMINFO_PATH = "/proc/meminfo"


def _detect_memory_gb() -> int | None:
    """Read total memory from ``/proc/meminfo`` and return GiB.

    Returns ``None`` on non-Linux or any read/parse failure.  Rounds
    DOWN — ``mem_gb >= N`` is the canonical "this node has at least N
    GiB" filter shape, and a node with 31.5 GiB shouldn't claim to
    have 32 in case a downstream pin checks the exact value.
    """
    text = _read_text(_MEMINFO_PATH)
    if text is None:
        return None
    for line in text.splitlines():
        if not line.startswith("MemTotal:"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1]) // (1024 * 1024)
    return None


# ``/proc/cpuinfo`` is per-CPU; the ``model name`` field repeats for
# every logical CPU.  Read the first occurrence.
_CPUINFO_PATH = "/proc/cpuinfo"
_CPU_MODEL_RE = re.compile(r"^model name\s*:\s*(.+)$", re.MULTILINE)


def _detect_cpu_model() -> str | None:
    """Read the CPU brand string from ``/proc/cpuinfo``.

    Returns the first ``model name`` value (Intel: ``Xeon Platinum
    8488C``, AMD: ``EPYC 9654``, ARM: usually empty since ARM exposes
    ``Hardware`` / ``Processor`` instead — those return None and
    operators set ``cpu_model`` in config to taste).
    """
    text = _read_text(_CPUINFO_PATH)
    if text is None:
        return None
    m = _CPU_MODEL_RE.search(text)
    if not m:
        return None
    return m.group(1).strip() or None


# DMI (Desktop Management Interface) sysfs — Linux's view of the
# vendor strings the BIOS/SMBIOS reports.  Cloud hypervisors set
# distinctive values here, so the cloud-provider detection can run
# entirely from kernel interfaces with no network probe.
_DMI_DIR = "/sys/class/dmi/id"


def _read_dmi(field: str) -> str:
    """Return the named DMI field's value, lowercased + stripped.

    DMI files are root-readable on most distros but world-readable on
    typical cloud images.  On a hardened host where we can't read
    them, this returns empty string and cloud-provider detection
    falls back to "unknown" (which then suppresses the IMDS probe).
    """
    text = _read_text(os.path.join(_DMI_DIR, field))
    if text is None:
        return ""
    return text.lower().strip()


def _detect_cloud_provider_from_dmi() -> str:
    """Identify the cloud provider from BIOS/SMBIOS strings.

    Returns ``"aws"`` / ``"gcp"`` / ``"azure"`` / ``"unknown"``.
    Pure kernel interface — no network call.  Used to gate the IMDS
    probe so non-cloud hosts don't pay startup latency on doomed
    link-local connections.
    """
    sys_vendor = _read_dmi("sys_vendor")
    board_vendor = _read_dmi("board_vendor")
    bios_vendor = _read_dmi("bios_vendor")
    chassis_asset_tag = _read_dmi("chassis_asset_tag")
    # AWS EC2: SMBIOS reports "Amazon EC2".  Older Nitro instances
    # leave bios_vendor=Amazon EC2 too.
    if "amazon ec2" in (sys_vendor, board_vendor, bios_vendor):
        return "aws"
    # GCP: sys_vendor is "Google" with product_name "Google Compute Engine".
    if sys_vendor == "google" or "google compute engine" in _read_dmi("product_name"):
        return "gcp"
    # Azure: sys_vendor "Microsoft Corporation" plus a stable
    # chassis_asset_tag of "7783-7084-3265-9085-8269-3286-77".
    # Microsoft uses the same sys_vendor for Hyper-V on baremetal;
    # the tag is what distinguishes Azure VMs.
    if "microsoft" in sys_vendor and chassis_asset_tag.startswith("7783-7084"):
        return "azure"
    return "unknown"


# ---------------------------------------------------------------------------
# IMDS probes — cloud-only, gated by DMI detection
# ---------------------------------------------------------------------------

# Per-call timeout for IMDS probes.  Cloud hosts respond in < 50 ms.
_CLOUD_PROBE_TIMEOUT_S: float = 1.0

# Hard caps on IMDS data we'll persist.  All three real cloud-provider
# IMDS responses are well under these limits (AWS identity doc is ~1
# KiB, GCP/Azure single-field responses are tens of bytes); the caps
# exist so a host where the link-local responder is hostile (spoofed
# DMI on baremetal, attacker-controlled DNS, lab tamper) can't spray
# multi-megabyte payloads into ``node_metadata`` and from there into
# coord-LLM context windows on the next ``list_nodes``.
_IMDS_MAX_BODY_BYTES: int = 64 * 1024
_IMDS_MAX_FIELD_CHARS: int = 256


def _imds_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    timeout: float = _CLOUD_PROBE_TIMEOUT_S,
) -> str | None:
    """Tiny wrapper around urllib for IMDS calls.

    Returns the response body as a UTF-8 string on 2xx, ``None`` on any
    network / decode / non-2xx failure.  Body size is capped at
    :data:`_IMDS_MAX_BODY_BYTES` so a hostile responder can't cause an
    unbounded read.
    """
    try:
        req = urllib.request.Request(url, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (link-local IMDS)
            body: bytes = resp.read(_IMDS_MAX_BODY_BYTES)
        return body.decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _imds_field(value: Any) -> str | None:
    """Sanitise an IMDS field for persistence into ``node_metadata``.

    - Returns ``None`` for non-string / empty values so callers can
      ``if v: out[k] = v``-style filter cleanly.
    - Strips control characters (anything below U+0020 plus DEL) —
      a hostile IMDS could otherwise inject newlines / NULs into
      strings the coord LLM later inhales.
    - Hard-caps to :data:`_IMDS_MAX_FIELD_CHARS`.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    if not cleaned:
        return None
    if len(cleaned) > _IMDS_MAX_FIELD_CHARS:
        cleaned = cleaned[:_IMDS_MAX_FIELD_CHARS]
    return cleaned


def _detect_aws_metadata() -> dict[str, str]:
    """EC2 IMDSv2: token + identity document."""
    base = "http://169.254.169.254/latest"
    token = _imds_get(
        f"{base}/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    if not token:
        return {}
    body = _imds_get(
        f"{base}/dynamic/instance-identity/document",
        headers={"X-aws-ec2-metadata-token": token.strip()},
    )
    if not body:
        return {}
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return {}
    # IMDS contract says this endpoint returns a JSON object — but a
    # spoofed responder can return any JSON.  Guard so a list / scalar
    # / null doesn't AttributeError on .get below; the outer try/except
    # in ``_detect_cloud_metadata`` would mask the crash, but local
    # type-checking keeps the function safe in isolation.
    if not isinstance(doc, dict):
        return {}
    out: dict[str, str] = {}
    for src, dst in (
        ("region", "cloud_region"),
        ("availabilityZone", "cloud_zone"),
        ("instanceType", "cloud_instance_type"),
        ("instanceId", "cloud_instance_id"),
    ):
        cleaned = _imds_field(doc.get(src))
        if cleaned:
            out[dst] = cleaned
    return out


def _detect_gcp_metadata() -> dict[str, str]:
    """GCP Compute Engine metadata: zone / machine-type / id.

    Targets the link-local IP literal ``169.254.169.254`` (not the
    resolvable hostname ``metadata.google.internal``) so a host with
    spoofed DMI tags + attacker-controlled DNS can't redirect the
    probe to a hostile server.  The ``Metadata-Flavor: Google`` header
    is what GCE's metadata server uses to confirm we're a legitimate
    caller, and AWS/Azure also target the same IP — using it for GCP
    keeps all three providers on the same trust model.

    Issues the three sub-calls (zone, machine-type, id) concurrently
    so a misidentified host (DMI claims GCP, IMDS unreachable) takes
    one timeout window (~1 s) instead of three sequential ones.
    """
    import concurrent.futures

    base = "http://169.254.169.254/computeMetadata/v1/instance"
    headers = {"Metadata-Flavor": "Google"}

    paths = ("zone", "machine-type", "id")
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(paths),
        thread_name_prefix="gcp-imds",
    ) as pool:
        futures = {p: pool.submit(_imds_get, f"{base}/{p}", headers=headers) for p in paths}
        results = {p: fut.result() for p, fut in futures.items()}

    zone = results.get("zone")
    if zone is None:
        return {}
    out: dict[str, str] = {}
    # zone format: "projects/PROJECT_NUM/zones/us-east1-a" → take tail.
    zone_short = _imds_field(zone.rsplit("/", 1)[-1])
    if zone_short:
        out["cloud_zone"] = zone_short
        # GCP region = zone with the trailing letter chopped.
        if "-" in zone_short:
            region = _imds_field(zone_short.rsplit("-", 1)[0])
            if region:
                out["cloud_region"] = region
    machine_type = results.get("machine-type")
    if machine_type:
        cleaned = _imds_field(machine_type.rsplit("/", 1)[-1])
        if cleaned:
            out["cloud_instance_type"] = cleaned
    instance_id = results.get("id")
    if instance_id:
        cleaned = _imds_field(instance_id)
        if cleaned:
            out["cloud_instance_id"] = cleaned
    return out


def _detect_azure_metadata() -> dict[str, str]:
    """Azure VM IMDS: location / vmSize."""
    url = "http://169.254.169.254/metadata/instance?api-version=2021-12-13"
    body = _imds_get(url, headers={"Metadata": "true"})
    if not body:
        return {}
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return {}
    # Same isinstance guard as the AWS path — a non-dict body would
    # AttributeError on doc.get("compute") below.
    if not isinstance(doc, dict):
        return {}
    compute = doc.get("compute") or {}
    if not isinstance(compute, dict):
        return {}
    out: dict[str, str] = {}
    for src, dst in (
        ("location", "cloud_region"),
        ("zone", "cloud_zone"),
        ("vmSize", "cloud_instance_type"),
        ("vmId", "cloud_instance_id"),
    ):
        cleaned = _imds_field(compute.get(src))
        if cleaned:
            out[dst] = cleaned
    return out


def _detect_cloud_metadata() -> dict[str, str]:
    """Surface cloud_provider + region/zone/instance-type.

    Detection happens in two phases:

    1. **DMI (kernel interface)** identifies the provider from
       BIOS/SMBIOS strings.  No network call, no startup latency on
       baremetal hosts — ``unknown`` returns immediately.
    2. **IMDS (network)** runs only when DMI confirmed a cloud, so
       the link-local probe can't burn 1+ second on a host that has
       no IMDS at all.

    Operators can opt out of the IMDS phase entirely via
    ``TURNSTONE_AUTO_CLOUD_METADATA=0`` if their network policy
    forbids link-local probes; ``cloud_provider`` from DMI still
    populates.
    """
    provider = _detect_cloud_provider_from_dmi()
    if provider == "unknown":
        return {}
    out: dict[str, str] = {"cloud_provider": provider}
    if os.environ.get("TURNSTONE_AUTO_CLOUD_METADATA", "1") == "0":
        return out
    # Inline dispatch (vs a module-level dict of function refs) so a
    # test monkeypatching ``_detect_aws_metadata`` actually substitutes
    # the function the dispatcher will call — a dict captured at import
    # time would still hold the original reference.
    try:
        if provider == "aws":
            out.update(_detect_aws_metadata())
        elif provider == "gcp":
            out.update(_detect_gcp_metadata())
        elif provider == "azure":
            out.update(_detect_azure_metadata())
    except Exception:
        log.debug("node_info: IMDS probe failed provider=%s", provider, exc_info=True)
    return out


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------


def collect_node_info() -> dict[str, Any]:
    """Collect auto-populated node metadata.

    Returns a dict of ``{key: value}`` where values are JSON-serializable.
    Each field is collected independently — one failure does not block others.
    """
    info: dict[str, Any] = {}

    for key, fn in (
        ("hostname", socket.gethostname),
        ("fqdn", socket.getfqdn),
        ("os", platform.system),
        ("os_release", platform.release),
        ("arch", platform.machine),
        ("python", platform.python_version),
        ("cpu_count", os.cpu_count),
    ):
        try:
            val = fn()
            if val is not None:
                info[key] = val
        except Exception:
            log.debug("node_info: failed to collect %s", key, exc_info=True)

    try:
        ifaces = _collect_interfaces()
        if ifaces:
            info["interfaces"] = ifaces
    except Exception:
        log.debug("node_info: failed to collect interfaces", exc_info=True)

    # Capability detection — independent failsafe blocks so a missing
    # /sys/class/drm doesn't suppress memory detection, etc.
    try:
        gpus = _detect_gpus()
        if gpus:
            info["gpu_count"] = len(gpus)
            info["gpus"] = gpus
            # ``has_gpu`` is the "any compute GPU at all" flag,
            # filterable as ``list_nodes(filters={"has_gpu": True})``
            # — exact-equality JSON match on a boolean.
            info["has_gpu"] = True
            vendors = sorted({g["vendor"] for g in gpus})
            info["gpu_vendors"] = vendors
            # Per-vendor boolean flags so a multi-vendor node is
            # filterable under EVERY vendor present.  A singular
            # ``gpu_vendor`` scalar would only match one vendor under
            # JSON-equal filtering — a mixed AMD+NVIDIA node would
            # be invisible to a coord searching for the other vendor.
            # Per-vendor booleans avoid the false-negative entirely:
            # a single ``filters={"gpu_has_nvidia": True}`` matches
            # every node carrying at least one NVIDIA card,
            # regardless of what else is on the bus.
            for vendor in vendors:
                info[f"gpu_has_{vendor}"] = True
    except Exception:
        log.debug("node_info: GPU detection failed", exc_info=True)

    try:
        mem_gb = _detect_memory_gb()
        if mem_gb is not None and mem_gb > 0:
            info["memory_gb"] = mem_gb
    except Exception:
        log.debug("node_info: memory detection failed", exc_info=True)

    try:
        cpu_model = _detect_cpu_model()
        if cpu_model:
            info["cpu_model"] = cpu_model
    except Exception:
        log.debug("node_info: CPU model detection failed", exc_info=True)

    try:
        cloud = _detect_cloud_metadata()
        info.update(cloud)
    except Exception:
        log.debug("node_info: cloud metadata detection failed", exc_info=True)

    return info

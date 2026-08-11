"""Shared address classification for the tree's SSRF guards.

Five guards screen outbound URLs, and their *policies* differ on purpose:
:mod:`turnstone.core.oauth_ssrf` demands a globally routable endpoint unless the
operator opted in, :func:`turnstone.core.web.screen_url` (the ``web_fetch`` /
``open_preview`` tools) honours ``tools.allow_private_network``, and
``turnstone.channels._formatter._is_safe_image_url`` allows the LAN outright
because media servers live there.

What they must not differ on is *which lane an address falls in*. This module
answers that with a single function, :func:`classify_address`, returning exactly
one :class:`AddressLane`. Guards branch on the lane; they never re-derive it
from raw ``ipaddress`` predicates and never order two overlapping tests
themselves. Overlapping predicates are the trap here: several addresses are
simultaneously globally routable *and* metadata-reaching, so any design that
asks a caller to test two booleans makes the verdict depend on which one it
happens to check first. One function, one lane, disjoint by construction.

Two normalizations feed the classification:

*IPv6 transition addresses* carry an IPv4 destination in their low bits, and
``ipaddress`` classifies the wrapper rather than the destination:
``64:ff9b::a9fe:a9fe`` is ``is_global`` because ``64:ff9b::/96`` is global
unicast, even though a NAT64 gateway routes it to 169.254.169.254. The wrapper
is *replaced* by what it routes to — never merely added alongside, since
``64:ff9b::/96`` and ``::/96`` are themselves ``is_reserved`` and judging the
wrapper would refuse every NAT64 address including the ones DNS64 synthesizes
for ordinary IPv4-only websites.

*Vendor metadata prefixes* that the stdlib has no opinion about (AWS Nitro IMDS
over IPv6 at ``fd00:ec2::/32``) are ULA — ``is_private`` and nothing else — so
without an explicit entry they would land in the operator-approvable lane and a
home-lab opt-in would expose instance credentials.

Residual, deliberately not addressed: a NAT64 Network-Specific Prefix
(RFC 6052 §3.2) is built from the operator's own global prefix, so it is
indistinguishable from ordinary global unicast and carries no marker any
address-based check could key on.
"""

from __future__ import annotations

import enum
import ipaddress
import socket
from typing import TypeAlias

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address


class AddressLane(enum.IntEnum):
    """Which policy lane an address falls in. Ordered by severity.

    Ordering lets a caller fold several resolved addresses with ``max()``: a
    hostname is only as safe as the worst address it resolves to.
    """

    PUBLIC = 0
    """Globally routable. Every guard allows it."""

    PRIVATE = 1
    """LAN, ULA, CGNAT, loopback. Refused unless the operator opted in."""

    NEVER = 2
    """Refused regardless of any opt-in — link-local (cloud metadata at
    169.254.169.254 is the canonical SSRF target), multicast, unspecified,
    reserved, and known vendor metadata prefixes. No legitimate IdP, home-lab
    dashboard, or media server lives in these."""


# Translation prefixes carrying an embedded IPv4:
#   64:ff9b::/96    RFC 6052 §3.1 well-known prefix. A compliant NAT64 gateway
#                   MUST NOT use it for non-global IPv4, but a misconfigured
#                   one will — so decode and re-classify rather than trusting
#                   the RFC to hold.
#   64:ff9b:1::/48  RFC 8215 local-use translation prefix.
#   ::/96           RFC 4291 §2.5.5.1 IPv4-compatible (deprecated). ``::`` and
#                   ``::1`` sit inside it but are the unspecified and loopback
#                   addresses, not wrappers — the floor below excludes them
#                   along with every other embedding no host would route.
_LOWEST_ROUTABLE_V4 = ipaddress.IPv4Address("1.0.0.0")

# Metadata endpoints the stdlib does not flag. The 169.254.169.254 most vendors
# use is caught by ``is_link_local``; these are not. Add new vendor prefixes
# here — this is the single list, shared by every guard.
_VENDOR_METADATA: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("fd00:ec2::/32"),  # AWS Nitro IMDS / ECS task metadata
    # Alibaba Cloud ECS metadata. Sits in CGNAT space, so it is neither private
    # nor global nor link-local — without this entry it lands in the
    # operator-approvable lane and the opt-in hands out RAM-role STS credentials.
    ipaddress.IPv4Network("100.100.100.200/32"),
)

# RFC 6052 §2.2 embeds the IPv4 at a position that depends on the translation
# prefix length, and requires bits 64-71 (the "u" octet) to be zero. The
# well-known prefix is /96 by definition (RFC 6052 §3.1); the local-use prefix
# is a /48 that a gateway may subnet at any RFC 6052 length, and the address
# does not say which. Every length is therefore decoded and the WORST result
# classified: guessing one layout would let a mis-decode of an internal target
# read as an unrelated public address. Only lengths at or below the prefix are
# possible — RFC 8215 defines the local-use prefix as a /48, so a /32 or /40
# "layout" would overlap the prefix bits and decode pure garbage.
_RFC6052_LOCAL_USE_LENGTHS = (48, 56, 64, 96)
_TRANSLATION_PREFIXES: tuple[tuple[ipaddress.IPv6Network, tuple[int, ...]], ...] = (
    (ipaddress.IPv6Network("64:ff9b::/96"), (96,)),
    (ipaddress.IPv6Network("64:ff9b:1::/48"), _RFC6052_LOCAL_USE_LENGTHS),
    (ipaddress.IPv6Network("::/96"), (96,)),
)


def _rfc6052_ipv4(addr: ipaddress.IPv6Address, prefix_len: int) -> ipaddress.IPv4Address | None:
    """Decode the IPv4 an RFC 6052 translation address carries, or None.

    The 32 IPv4 bits start at *prefix_len* and skip bits 64-71, which the RFC
    reserves as a zero "u" octet — a non-zero value there means this is not a
    translation address of that length, so the layout does not apply.
    """
    value = int(addr)
    if prefix_len == 96:
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if (value >> 56) & 0xFF:
        return None
    high_bits = min(64 - prefix_len, 32)
    low_bits = 32 - high_bits
    high = (value >> (128 - prefix_len - high_bits)) & ((1 << high_bits) - 1)
    low = ((value >> (56 - low_bits)) & ((1 << low_bits) - 1)) if low_bits else 0
    return ipaddress.IPv4Address((high << low_bits) | low)


def embedded_ipv4(addr: IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    """Return the IPv4 addresses *addr* routes through, if it is a wrapper.

    Empty for a plain address. Teredo yields both the server and the client
    address, since either being unsafe makes the wrapper unsafe.
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return ()
    if addr.ipv4_mapped is not None:
        return (addr.ipv4_mapped,)
    if addr.sixtofour is not None:
        return (addr.sixtofour,)
    teredo = addr.teredo
    if teredo is not None:
        return teredo
    # ``ipaddress`` exposes no accessor for the translation prefixes, so decode
    # by hand per RFC 6052 §2.2.
    for network, lengths in _TRANSLATION_PREFIXES:
        if addr not in network:
            continue
        decoded = tuple(
            item
            for item in (_rfc6052_ipv4(addr, length) for length in lengths)
            if item is not None and item >= _LOWEST_ROUTABLE_V4
        )
        # Several layouts can decode at once; the worst-lane fold in
        # ``classify_address`` then judges the address by all of them.
        return tuple(dict.fromkeys(decoded))
    return ()


def effective_addresses(addr: IPAddress) -> tuple[IPAddress, ...]:
    """Return the addresses that decide how *addr* should be classified.

    For a transition wrapper that is the IPv4 it routes to; the wrapper's own
    classification describes the transition prefix, not the destination. For
    anything else it is the address itself.
    """
    return embedded_ipv4(addr) or (addr,)


def _classify_one(addr: IPAddress) -> AddressLane:
    """Classify a single already-unwrapped address into exactly one lane."""
    # Loopback first: ``::1`` is inside ``::/8``, which CPython lists as
    # reserved, so a bare ``is_reserved`` test would drag IPv6 loopback into
    # NEVER and lock a self-hoster out of their own dev server — while the
    # identical 127.0.0.1 stayed approvable.
    if addr.is_loopback:
        return AddressLane.PRIVATE
    if (
        addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
        or any(addr in net for net in _VENDOR_METADATA)
    ):
        return AddressLane.NEVER
    # Deprecated IPv6 site-local (RFC 3879). CPython reports is_global True and
    # is_private False for fec0::/10, so without this it would read as ordinary
    # public unicast and skip the opt-in entirely — legacy and embedded gear
    # still carries it.
    if isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local:
        return AddressLane.PRIVATE
    if addr.is_global:
        return AddressLane.PUBLIC
    return AddressLane.PRIVATE


def classify_address(addr: IPAddress) -> AddressLane:
    """Return the single lane *addr* falls in, after unwrapping transitions.

    A wrapper is only as safe as what it routes to, so the worst lane among the
    effective addresses wins.
    """
    return max(_classify_one(item) for item in effective_addresses(addr))


def reaches_only_loopback(addr: IPAddress) -> bool:
    """True when every address *addr* routes to is loopback.

    One spelling of the predicate, because two spellings is how the cleartext
    gate and the localhost development lane ended up disagreeing: a Teredo
    address carries two IPv4s, so ``any`` and ``all`` give opposite answers for
    a wrapper whose server half is loopback and whose client half is not.
    """
    return all(item.is_loopback for item in effective_addresses(addr))


class ResolutionError(Exception):
    """A hostname could not be resolved to any address the guards can judge."""


def resolve_and_classify(hostname: str, port: int = 0) -> list[tuple[AddressLane, IPAddress]]:
    """Resolve *hostname* and classify every address it answers with.

    The single resolution path for every guard in the tree. Each guard used to
    hand-roll ``getaddrinfo`` → parse → classify, and the copies had already
    drifted on the one thing the loop must get right: which failures are
    caught. ``getaddrinfo`` raises ``UnicodeError`` (a ``ValueError``, not an
    ``OSError``) out of the IDNA encoder for an over-long label, so a guard
    catching only ``socket.gaierror`` lets that escape into its caller.

    Raises :class:`ResolutionError` when the name yields nothing usable —
    including an empty answer list, which a caller looping over results would
    otherwise treat as "no objections found".
    """
    try:
        infos = socket.getaddrinfo(hostname, port or None, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError) as exc:
        raise ResolutionError(f"hostname cannot be resolved ({hostname})") from exc

    classified: list[tuple[AddressLane, IPAddress]] = []
    for info in infos:
        raw = str(info[4][0])
        try:
            addr = parse_resolved_address(raw)
        except ValueError as exc:
            raise ResolutionError(f"unable to parse resolved address ({raw})") from exc
        classified.append((classify_address(addr), addr))
    if not classified:
        raise ResolutionError(f"hostname cannot be resolved ({hostname})")
    return classified


def describe_address(addr: IPAddress) -> str:
    """Render *addr* for an operator-facing refusal, naming what it reaches."""
    embedded = embedded_ipv4(addr)
    if not embedded:
        return str(addr)
    return f"{addr} (routes to {', '.join(str(item) for item in embedded)})"


def parse_resolved_address(raw: str) -> IPAddress:
    """Parse an address as returned by ``getaddrinfo``, dropping any zone id.

    ``getaddrinfo`` renders a link-local result as ``fe80::1%eth0``. The stdlib
    parses that fine, but a scoped address does not compare equal to its
    unscoped form, so keeping the zone would make every address the guards
    resolve depend on which interface answered — and would leak the interface
    name into operator-facing refusal text. The classification is identical
    either way, so the zone is dropped at the single point every guard parses.
    """
    return ipaddress.ip_address(raw.partition("%")[0])


__all__ = [
    "AddressLane",
    "IPAddress",
    "ResolutionError",
    "classify_address",
    "describe_address",
    "effective_addresses",
    "embedded_ipv4",
    "parse_resolved_address",
    "reaches_only_loopback",
    "resolve_and_classify",
]

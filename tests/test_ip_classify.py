"""Cross-guard regression tests for SSRF address classification.

Three guards screen outbound URLs — :func:`turnstone.core.oauth_ssrf.validate_url_no_ssrf`
(OAuth/OIDC endpoints), :func:`turnstone.core.web.screen_url` (the ``web_fetch`` /
``open_preview`` tools) and ``turnstone.channels._formatter._is_safe_image_url``
(inline images). Each once hand-rolled its own normalization and its own policy
tests, so each had a different hole: NAT64 and IPv4-compatible walked through the
first two, 6to4 walked through the third, and CGNAT walked through the denylist.

These tests pin what no single guard's own file would catch:

1. A transition address is judged by the IPv4 it routes to, in EVERY guard, for
   EVERY wrapper form.
2. Every address lands in exactly ONE lane, and each guard honours the lane
   rather than re-deriving it. A previous revision exposed two overlapping
   booleans, so lane assignment depended on which one a caller tested first —
   ``64:ff9b:1::a9fe:a9fe`` was both "public" and "never allowed", and the OAuth
   guard accepted it while the web guard refused it.
3. The operator's ``allow_private_network`` opt-in still admits the whole home
   lab (v4 and v6), and still never admits the NEVER lane.
4. Unwrapping did not become a blanket rejection of transition prefixes: DNS64
   on an IPv6-only node synthesizes ``64:ff9b::<public-v4>`` for every IPv4-only
   website, so refusing the prefix would black-hole ordinary browsing.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.channels._formatter import _is_safe_image_url
from turnstone.core.ip_classify import (
    AddressLane,
    classify_address,
    describe_address,
    effective_addresses,
    embedded_ipv4,
    parse_resolved_address,
)
from turnstone.core.oauth_ssrf import OAuthSSRFError, validate_url_no_ssrf
from turnstone.core.web import screen_url

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

# ---------------------------------------------------------------------------
# Wrapper constructors — build each transition form around an IPv4 target.
# ---------------------------------------------------------------------------


def _plain(v4: str) -> str:
    return v4


def _nat64_wkp(v4: str) -> str:
    """RFC 6052 §3.1 well-known prefix: IPv4 in the low 32 bits of 64:ff9b::/96."""
    base = int(ipaddress.IPv6Address("64:ff9b::"))
    return str(ipaddress.IPv6Address(base | int(ipaddress.IPv4Address(v4))))


def _nat64_local_use(v4: str) -> str:
    """RFC 8215 local-use translation prefix 64:ff9b:1::/48."""
    base = int(ipaddress.IPv6Address("64:ff9b:1::"))
    return str(ipaddress.IPv6Address(base | int(ipaddress.IPv4Address(v4))))


def _sixtofour(v4: str) -> str:
    """RFC 3056: IPv4 in bits 16-47 of 2002::/16."""
    base = int(ipaddress.IPv6Address("2002::"))
    return str(ipaddress.IPv6Address(base | (int(ipaddress.IPv4Address(v4)) << 80)))


def _ipv4_compatible(v4: str) -> str:
    """RFC 4291 §2.5.5.1 (deprecated): IPv4 in the low 32 bits of ::/96."""
    return str(ipaddress.IPv6Address(int(ipaddress.IPv4Address(v4))))


def _ipv4_mapped(v4: str) -> str:
    return str(ipaddress.IPv6Address("::ffff:" + v4))


_TEREDO_SERVER = "8.8.8.8"


def _teredo(v4: str) -> str:
    """RFC 4380: 2001:0::/32, server in bits 32-63, obfuscated client in the low 32.

    The target goes in the CLIENT field over a public server, so the wrapper's
    lane is the target's lane — either field being unsafe must condemn it.
    """
    value = int(ipaddress.IPv6Address("2001::"))
    value |= int(ipaddress.IPv4Address(_TEREDO_SERVER)) << 64
    value |= (~int(ipaddress.IPv4Address(v4))) & 0xFFFFFFFF
    return str(ipaddress.IPv6Address(value))


WRAPPERS: dict[str, Callable[[str], str]] = {
    "plain": _plain,
    "nat64-wkp": _nat64_wkp,
    "nat64-local-use": _nat64_local_use,
    "6to4": _sixtofour,
    "ipv4-compatible": _ipv4_compatible,
    "ipv4-mapped": _ipv4_mapped,
    "teredo": _teredo,
}
WRAPPER_IDS = sorted(WRAPPERS)

PUBLIC_CAPABLE_WRAPPER_IDS = WRAPPER_IDS

METADATA = "169.254.169.254"
PUBLIC = "93.184.216.34"

# Everything an operator may opt in to reaching.
PRIVATE_TARGETS = ["127.0.0.1", "192.168.1.5", "10.0.0.1", "100.64.0.1"]
PRIVATE_V6_TARGETS = ["::1", "fd00::1", "fc00::1"]

# Everything refused regardless of the opt-in.
NEVER_TARGETS = [METADATA, "224.0.0.1", "0.0.0.0", "240.0.0.1", "255.255.255.255"]
NEVER_V6_TARGETS = ["fe80::1", "::", "ff02::1", "fd00:ec2::254", "fd00:ec2::23"]


# ---------------------------------------------------------------------------
# Guard runners.
# ---------------------------------------------------------------------------


def _resolving_to(*addrs: str) -> AbstractContextManager[MagicMock]:
    infos: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
    for a in addrs:
        parsed = ipaddress.ip_address(a)
        if parsed.version == 6:
            infos.append(
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, 0, 0, 0))
            )
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, 0)))
    return patch("socket.getaddrinfo", return_value=infos)


def _oauth_allows(addr: str, *, allow_private: bool = False) -> bool:
    with _resolving_to(addr):
        try:
            validate_url_no_ssrf(
                "https://idp.example.com/x", allow_http=False, allow_private=allow_private
            )
        except OAuthSSRFError:
            return False
    return True


def _web_lane(*addrs: str) -> AddressLane:
    with _resolving_to(*addrs):
        return screen_url("https://site.example/x").lane


def _image_allows(addr: str) -> bool:
    async def _run() -> bool:
        with _resolving_to(addr):
            return await _is_safe_image_url("https://cdn.example/i.png")

    return asyncio.run(_run())


def _screen_tool(*addrs: str, allow_private_network: bool) -> tuple[str | None, bool, bool]:
    """Drive the REAL consumer of the lane contract, not just the guard."""
    from turnstone.core.session import _screen_tool_url

    with _resolving_to(*addrs):
        return _screen_tool_url("http://target.example/x", allow_private_network)


# ---------------------------------------------------------------------------
# Normalization.
# ---------------------------------------------------------------------------


class TestEmbeddedIPv4:
    def test_unwraps_every_transition_form(self) -> None:
        for name, wrap in WRAPPERS.items():
            if name in ("plain", "teredo"):
                continue
            addr = ipaddress.ip_address(wrap(PUBLIC))
            assert embedded_ipv4(addr) == (ipaddress.IPv4Address(PUBLIC),), name

    def test_plain_addresses_are_not_wrappers(self) -> None:
        for a in ("93.184.216.34", "2606:4700:4700::1111", "fd00::1", "fe80::1"):
            assert embedded_ipv4(ipaddress.ip_address(a)) == (), a

    def test_loopback_and_unspecified_are_not_ipv4_compatible_wrappers(self) -> None:
        """``::`` and ``::1`` sit inside ``::/96`` but are not IPv4 wrappers."""
        assert embedded_ipv4(ipaddress.ip_address("::1")) == ()
        assert embedded_ipv4(ipaddress.ip_address("::")) == ()

    def test_floor_declines_to_unwrap_unroutable_embeddings(self) -> None:
        """The 0.0.0.0/8 floor applies inside real wrapper prefixes too.

        Without this, ``64:ff9b::1`` would unwrap to 0.0.0.1 and be judged on a
        meaningless address; with it, the wrapper itself is classified.
        """
        for a in ("64:ff9b::1", "::5", "64:ff9b:1::1"):
            assert embedded_ipv4(ipaddress.ip_address(a)) == (), a
            assert classify_address(ipaddress.ip_address(a)) is AddressLane.NEVER, a

    def test_teredo_yields_server_and_client(self) -> None:
        addr = ipaddress.ip_address(_teredo(METADATA))
        assert embedded_ipv4(addr) == (
            ipaddress.IPv4Address(_TEREDO_SERVER),
            ipaddress.IPv4Address(METADATA),
        )

    def test_zone_identifier_is_stripped(self) -> None:
        assert parse_resolved_address("fe80::1%eth0") == ipaddress.ip_address("fe80::1")
        assert parse_resolved_address("10.0.0.1") == ipaddress.ip_address("10.0.0.1")


class TestEffectiveAddresses:
    def test_wrapper_is_replaced_not_augmented(self) -> None:
        """The wrapper's own class describes the prefix, not the destination.

        ``64:ff9b::/96`` and ``::/96`` are both ``is_reserved``; consulting the
        wrapper as well would refuse every NAT64 address, including the ones
        DNS64 synthesizes for ordinary IPv4-only websites.
        """
        addr = ipaddress.ip_address(_nat64_wkp(PUBLIC))
        assert addr.is_reserved, "precondition: the WKP is reserved"
        assert effective_addresses(addr) == (ipaddress.IPv4Address(PUBLIC),)
        assert classify_address(addr) is AddressLane.PUBLIC

    def test_plain_address_is_its_own_effective_address(self) -> None:
        addr = ipaddress.ip_address(PUBLIC)
        assert effective_addresses(addr) == (addr,)


class TestDescribeAddress:
    def test_names_what_a_wrapper_reaches(self) -> None:
        assert METADATA in describe_address(ipaddress.ip_address(_nat64_wkp(METADATA)))

    def test_plain_address_renders_bare(self) -> None:
        assert describe_address(ipaddress.ip_address(PUBLIC)) == PUBLIC


# ---------------------------------------------------------------------------
# Lanes are disjoint and total — the property the two-boolean design broke.
# ---------------------------------------------------------------------------


class TestLaneAssignment:
    @pytest.mark.parametrize("target", NEVER_TARGETS + NEVER_V6_TARGETS)
    def test_never_lane(self, target: str) -> None:
        assert classify_address(ipaddress.ip_address(target)) is AddressLane.NEVER

    @pytest.mark.parametrize("target", PRIVATE_TARGETS + PRIVATE_V6_TARGETS)
    def test_private_lane(self, target: str) -> None:
        assert classify_address(ipaddress.ip_address(target)) is AddressLane.PRIVATE

    @pytest.mark.parametrize("target", [PUBLIC, "2606:4700:4700::1111", "1.1.1.1"])
    def test_public_lane(self, target: str) -> None:
        assert classify_address(ipaddress.ip_address(target)) is AddressLane.PUBLIC

    def test_ipv6_loopback_shares_the_ipv4_loopback_lane(self) -> None:
        """``::1`` is inside ``::/8`` which CPython lists as reserved.

        Folding ``is_reserved`` in blindly put IPv6 loopback in NEVER while
        127.0.0.1 stayed approvable, so ``http://localhost:8080/`` succeeded or
        failed depending on getaddrinfo ordering on a dual-stack host.
        """
        assert ipaddress.ip_address("::1").is_reserved, "precondition"
        assert classify_address(ipaddress.ip_address("::1")) is AddressLane.PRIVATE
        assert classify_address(ipaddress.ip_address("127.0.0.1")) is AddressLane.PRIVATE

    def test_vendor_metadata_is_never_despite_being_ula(self) -> None:
        addr = ipaddress.ip_address("fd00:ec2::254")
        assert addr.is_private and not addr.is_reserved, "precondition: only ULA"
        assert classify_address(addr) is AddressLane.NEVER


# ---------------------------------------------------------------------------
# Cross-guard matrix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wrapper", WRAPPER_IDS)
class TestMetadataIsNeverReachable:
    def test_oauth_guard_blocks(self, wrapper: str) -> None:
        assert not _oauth_allows(WRAPPERS[wrapper](METADATA))

    def test_oauth_guard_blocks_even_with_private_opt_in(self, wrapper: str) -> None:
        assert not _oauth_allows(WRAPPERS[wrapper](METADATA), allow_private=True)

    def test_web_guard_assigns_the_never_lane(self, wrapper: str) -> None:
        assert _web_lane(WRAPPERS[wrapper](METADATA)) is AddressLane.NEVER

    def test_tool_screen_refuses_even_with_the_opt_in(self, wrapper: str) -> None:
        err, private_origin, _block = _screen_tool(
            WRAPPERS[wrapper](METADATA), allow_private_network=True
        )
        assert err is not None
        assert private_origin is False

    def test_image_guard_blocks(self, wrapper: str) -> None:
        assert not _image_allows(WRAPPERS[wrapper](METADATA))


@pytest.mark.parametrize("wrapper", WRAPPER_IDS)
@pytest.mark.parametrize("target", PRIVATE_TARGETS)
class TestPrivateTargetsAreOperatorGated:
    def test_oauth_guard_blocks_by_default(self, wrapper: str, target: str) -> None:
        assert not _oauth_allows(WRAPPERS[wrapper](target))

    def test_oauth_guard_allows_under_opt_in(self, wrapper: str, target: str) -> None:
        """The home-lab lane must survive unwrapping, wrapped or bare."""
        assert _oauth_allows(WRAPPERS[wrapper](target), allow_private=True)

    def test_web_guard_assigns_the_private_lane(self, wrapper: str, target: str) -> None:
        assert _web_lane(WRAPPERS[wrapper](target)) is AddressLane.PRIVATE

    def test_tool_screen_offers_the_opt_in(self, wrapper: str, target: str) -> None:
        addr = WRAPPERS[wrapper](target)
        err, _, _block = _screen_tool(addr, allow_private_network=False)
        assert err is not None, "refused without the opt-in"
        err, private_origin, _block = _screen_tool(addr, allow_private_network=True)
        assert err is None and private_origin is True, "admitted with it"


@pytest.mark.parametrize("target", PRIVATE_V6_TARGETS)
class TestIPv6HomeLabIsReachableUnderTheOptIn:
    def test_oauth_guard(self, target: str) -> None:
        assert not _oauth_allows(target)
        assert _oauth_allows(target, allow_private=True)

    def test_tool_screen(self, target: str) -> None:
        err, private_origin, _block = _screen_tool(target, allow_private_network=True)
        assert err is None and private_origin is True


@pytest.mark.parametrize("target", NEVER_V6_TARGETS)
class TestIPv6NeverTargetsStayRefused:
    def test_oauth_guard_even_with_opt_in(self, target: str) -> None:
        assert not _oauth_allows(target, allow_private=True)

    def test_tool_screen_even_with_opt_in(self, target: str) -> None:
        err, private_origin, _block = _screen_tool(target, allow_private_network=True)
        assert err is not None and private_origin is False


@pytest.mark.parametrize("wrapper", PUBLIC_CAPABLE_WRAPPER_IDS)
class TestPublicTargetsStayReachable:
    """False-negative guard: unwrapping must not black-hole legitimate traffic."""

    def test_oauth_guard_allows(self, wrapper: str) -> None:
        assert _oauth_allows(WRAPPERS[wrapper](PUBLIC))

    def test_web_guard_allows(self, wrapper: str) -> None:
        assert _web_lane(WRAPPERS[wrapper](PUBLIC)) is AddressLane.PUBLIC

    def test_image_guard_allows(self, wrapper: str) -> None:
        assert _image_allows(WRAPPERS[wrapper](PUBLIC))

    def test_tool_screen_allows_without_marking_it_private(self, wrapper: str) -> None:
        err, private_origin, _block = _screen_tool(
            WRAPPERS[wrapper](PUBLIC), allow_private_network=True
        )
        assert err is None and private_origin is False


def _nat64_local_use_48(v4: str) -> str:
    """RFC 6052 §2.2 /48 layout: IPv4 across bits 48-63 and 72-87, u octet zero."""
    value = int(ipaddress.IPv6Address("64:ff9b:1::"))
    packed = int(ipaddress.IPv4Address(v4))
    value |= (packed >> 16) << 64
    value |= (packed & 0xFFFF) << 40
    return str(ipaddress.IPv6Address(value))


class TestLocalUseNAT64:
    """RFC 8215 64:ff9b:1::/48 — a fixed prefix, not the §3.2 NSP residual.

    RFC 6052 §2.2 puts the embedded IPv4 at a position that depends on the
    prefix length, and deployments carve /96s out of this /48, so both layouts
    are decoded. Getting this wrong in either direction is costly: too strict
    and an IPv6-only node cannot browse at all, too loose and a NAT64 gateway
    translates the guard's blessing straight to the metadata service.
    """

    def test_96_layout_wrapping_metadata_is_never(self) -> None:
        assert _web_lane(_nat64_local_use(METADATA)) is AddressLane.NEVER
        assert not _oauth_allows(_nat64_local_use(METADATA), allow_private=True)

    def test_48_layout_wrapping_metadata_is_never(self) -> None:
        assert _web_lane(_nat64_local_use_48(METADATA)) is AddressLane.NEVER
        assert not _oauth_allows(_nat64_local_use_48(METADATA), allow_private=True)

    def test_96_layout_wrapping_public_stays_public(self) -> None:
        """A DNS64 node must reach IPv4-only sites without any opt-in."""
        addr = _nat64_local_use(PUBLIC)
        assert _web_lane(addr) is AddressLane.PUBLIC
        assert _oauth_allows(addr)

    def test_48_layout_wrapping_public_stays_public(self) -> None:
        addr = _nat64_local_use_48(PUBLIC)
        # The address does not say which RFC 6052 length its gateway uses, so
        # every possible layout is decoded and the worst wins. The correct
        # decode must be among them; the others are judged too.
        assert ipaddress.IPv4Address(PUBLIC) in embedded_ipv4(ipaddress.ip_address(addr))
        assert _web_lane(addr) is AddressLane.PUBLIC
        assert _oauth_allows(addr)

    def test_64_layout_wrapping_an_internal_host_is_not_public(self) -> None:
        """A layout the decoder does not try would mis-read as an unrelated public IPv4.

        64:ff9b:1:100:a:0:700:0 carries 10.0.0.7 at the /64 layout. Decoding
        only /48 and /96 yielded 1.0.10.0 and 7.0.0.0 — both global — and
        promoted an internal target to PUBLIC, below even what HEAD refused.
        """
        addr = "64:ff9b:1:100:a:0:700:0"
        assert ipaddress.IPv4Address("10.0.0.7") in embedded_ipv4(ipaddress.ip_address(addr))
        assert _web_lane(addr) is AddressLane.PRIVATE
        assert not _oauth_allows(addr)

    def test_non_zero_u_octet_is_not_a_48_layout(self) -> None:
        """RFC 6052 reserves bits 64-71; a non-zero value there rules the layout out."""
        value = int(ipaddress.IPv6Address(_nat64_local_use_48(PUBLIC))) | (0xFF << 56)
        assert _rfc6052_rejects_u_octet(str(ipaddress.IPv6Address(value)))


def _rfc6052_rejects_u_octet(addr: str) -> bool:
    from turnstone.core.ip_classify import _rfc6052_ipv4

    return _rfc6052_ipv4(ipaddress.IPv6Address(addr), 48) is None


class TestMultipleResolvedAddresses:
    """A hostname is only as safe as the WORST address it resolves to."""

    def test_private_record_does_not_mask_a_later_never_record(self) -> None:
        assert _web_lane("10.0.0.7", METADATA) is AddressLane.NEVER

    def test_order_does_not_matter(self) -> None:
        assert _web_lane(METADATA, "10.0.0.7") is AddressLane.NEVER

    def test_public_record_does_not_mask_a_private_one(self) -> None:
        assert _web_lane(PUBLIC, "10.0.0.7") is AddressLane.PRIVATE

    def test_opt_in_does_not_admit_a_masked_never_record(self) -> None:
        """The exploit the worst-lane fold exists to prevent."""
        err, private_origin, _block = _screen_tool("10.0.0.7", METADATA, allow_private_network=True)
        assert err is not None
        assert private_origin is False


class TestResolutionFailureFailsClosed:
    """A guard that cannot resolve must refuse, not pass.

    The fetch resolves again, so an authority answering the guard's query with
    SERVFAIL and the fetch's query with an internal address would otherwise
    switch the guard off for that hop.
    """

    def test_web_guard_refuses(self) -> None:
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("SERVFAIL")):
            screen = screen_url("http://evil.example/x")
        assert screen.lane is AddressLane.NEVER
        assert screen.error is not None

    def test_tool_screen_refuses_even_with_the_opt_in(self) -> None:
        from turnstone.core.session import _screen_tool_url

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("SERVFAIL")):
            err, private_origin, _block = _screen_tool_url("http://evil.example/x", True)
        assert err is not None and private_origin is False


class TestMixedRecordsAreNotAPrivateOrigin:
    """``private_origin`` needs EVERY record private, not just the worst one.

    The worst-lane fold is right for refusing and wrong for deciding that a
    chain sits inside the operator's network: the connection may land on the
    public record, so the approval would describe somewhere the fetch is not.
    """

    def test_dual_record_host_is_fetchable_but_grants_no_chain_permission(self) -> None:
        """Split-horizon and hairpin DNS are ordinary self-hosting, not an attack.

        The host is approved like any other private target — refusing it would
        make that setup permanently unreachable with no operator remedy. The
        chain stays safe because ``fetch_with_ssrf_guard`` revokes private-hop
        permission after any hop that is not wholly private, so a mixed origin
        buys exactly one hop.
        """
        err, private_origin, block = _screen_tool("10.0.0.1", "1.2.3.4", allow_private_network=True)
        assert err is None and private_origin is True and block is True
        with _resolving_to("10.0.0.1", "1.2.3.4"):
            assert screen_url("http://dual.example/x").all_private is False

    def test_wholly_private_host_still_qualifies(self) -> None:
        err, private_origin, _block = _screen_tool(
            "10.0.0.1", "192.168.1.5", allow_private_network=True
        )
        assert err is None and private_origin is True


class TestSiteLocalAndVendorMetadata:
    @pytest.mark.parametrize(
        "addr,who",
        [
            ("fd00:ec2::254", "AWS Nitro IMDS over IPv6"),
            ("100.100.100.200", "Alibaba Cloud ECS"),
            ("168.63.129.16", "Azure host agent / wire server"),
            ("192.0.0.192", "Oracle Cloud"),
        ],
    )
    def test_vendor_metadata_is_never(self, addr: str, who: str) -> None:
        """These sit in ordinary unicast space, so the stdlib calls them routable.

        Without an explicit entry they are reachable with NO opt-in at all —
        a worse position than the RFC 1918 host next to them — and the docs
        and settings help promise the opposite.
        """
        assert classify_address(ipaddress.ip_address(addr)) is AddressLane.NEVER, who
        err, private_origin, _block = _screen_tool(addr, allow_private_network=True)
        assert err is not None and private_origin is False

    def test_deprecated_site_local_is_not_public(self) -> None:
        """CPython reports fec0::/10 as is_global, so it needs an explicit rule."""
        assert ipaddress.ip_address("fec0::1").is_global, "precondition"
        assert classify_address(ipaddress.ip_address("fec0::1")) is AddressLane.PRIVATE
        assert not _oauth_allows("fec0::1")
        assert _oauth_allows("fec0::1", allow_private=True)


class TestCleartextRequiresProvenLoopback:
    """``http://`` is for a real local dev server, and only that.

    A hostname is not proof: accepting one would put an OIDC token exchange —
    client_secret and authorization code included — on the wire in the clear.
    """

    def test_localhost_name_resolving_public_is_refused(self) -> None:
        with _resolving_to(PUBLIC), pytest.raises(OAuthSSRFError, match="HTTPS"):
            validate_url_no_ssrf("http://evil.localhost/token", allow_http=True)

    def test_localhost_name_resolving_private_is_refused(self) -> None:
        with _resolving_to("10.0.0.1"), pytest.raises(OAuthSSRFError):
            validate_url_no_ssrf("http://evil.localhost/token", allow_http=True)

    def test_genuine_loopback_is_allowed(self) -> None:
        for addr in ("127.0.0.1", "::1"):
            with _resolving_to(addr):
                validate_url_no_ssrf("http://localhost:8080/x", allow_http=True)

    def test_transition_wrapper_of_loopback_counts_as_loopback(self) -> None:
        """``::7f00:1`` reaches 127.0.0.1, so the classifier and this lane must agree."""
        assert not ipaddress.ip_address("::7f00:1").is_loopback, "precondition"
        with _resolving_to("::7f00:1"):
            validate_url_no_ssrf("https://localhost/x", allow_http=False)


class TestLocalhostNameIsNotEvidence:
    """``*.localhost`` is ordinary DNS a hostile authority answers at will.

    The dev lane is gated on the RESOLVED address being loopback, not on the
    hostname. A PRIVATE target is what discriminates here: a NEVER target is
    refused by the lane check regardless, so it cannot detect the name being
    trusted on its own.
    """

    def _validate(self, hostname: str, addr: str, *, allow_http: bool = False) -> bool:
        with _resolving_to(addr):
            try:
                validate_url_no_ssrf(
                    f"http://{hostname}/x" if allow_http else f"https://{hostname}/x",
                    allow_http=allow_http,
                )
            except OAuthSSRFError:
                return False
        return True

    def test_localhost_name_resolving_to_a_private_address_is_refused(self) -> None:
        assert not self._validate("evil.localhost", "10.0.0.1")

    def test_localhost_name_resolving_to_metadata_is_refused(self) -> None:
        assert not self._validate("evil.localhost", METADATA)

    def test_genuine_loopback_still_works(self) -> None:
        """The dev lane must survive: real localhost resolves to loopback."""
        assert self._validate("localhost", "127.0.0.1", allow_http=True)
        assert self._validate("localhost", "::1", allow_http=True)


class TestMalformedInput:
    def test_out_of_range_port_returns_instead_of_raising(self) -> None:
        """``urlsplit.port`` parses lazily and raises; the contract is str|None."""
        assert screen_url("http://example.com:99999/x").error is not None

    def test_missing_hostname(self) -> None:
        assert screen_url("http:///nohost").error is not None


class TestCGNATIsNotPublic:
    """RFC 6598 shared address space is neither ``is_private`` nor ``is_global``.

    A denylist built on ``is_private`` missed it entirely, which let
    ``web_fetch`` reach hosts on an overlay VPN — a common place to find
    100.64.0.0/10 — with no transition gateway involved at all.
    """

    def test_stdlib_classifies_cgnat_as_neither(self) -> None:
        addr = ipaddress.ip_address("100.64.0.1")
        assert not addr.is_private, "precondition: a denylist on is_private misses CGNAT"
        assert not addr.is_global

    def test_cgnat_is_operator_gated(self) -> None:
        assert classify_address(ipaddress.ip_address("100.64.0.1")) is AddressLane.PRIVATE
        assert not _oauth_allows("100.64.0.1")
        assert _oauth_allows("100.64.0.1", allow_private=True)

"""Message formatting utilities for channel adapters.

Handles chunking long messages for platforms with character limits, formatting
tool-approval requests, plan-review prompts, and rich media embeds for
platforms that support them (e.g. Discord).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx


def chunk_message(text: str, max_length: int = 2000) -> list[str]:
    """Split *text* into chunks that fit within *max_length*.

    Respects code-block boundaries: if a fenced code block (````` ```)
    spans a chunk boundary the current chunk is closed with ````` ``` ``
    and the next chunk reopens it.  Prefers splitting at newline
    boundaries, then word boundaries, then hard-splits.
    """
    if len(text) <= max_length:
        return [text]

    # Fast path: plain text with no code fences.  Skips per-iteration
    # fence bookkeeping for the common streaming-response case.
    if "```" not in text:
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break
            candidate = remaining[:max_length]
            split_idx = candidate.rfind("\n")
            if split_idx <= 0:
                split_idx = candidate.rfind(" ")
            if split_idx <= 0:
                split_idx = max_length
            chunks.append(remaining[:split_idx])
            remaining = remaining[split_idx:].lstrip("\n")
        return chunks

    chunks = []
    remaining = text
    in_code_block = False

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Reserve space for a closing ``` if we're inside a code block.
        limit = max_length - 4 if in_code_block else max_length
        limit = max(limit, 1)

        candidate = remaining[:limit]

        # Prefer a newline boundary.
        split_idx = candidate.rfind("\n")
        if split_idx <= 0:
            # Fall back to a word boundary.
            split_idx = candidate.rfind(" ")
        if split_idx <= 0:
            # Hard split.
            split_idx = limit

        chunk = remaining[:split_idx]
        remaining = remaining[split_idx:].lstrip("\n")

        # Track code-block fences in this chunk.
        fence_count = chunk.count("```")
        block_open = in_code_block

        if fence_count % 2 != 0:
            in_code_block = not in_code_block

        # If we end inside a code block, close it in this chunk and
        # reopen in the next.
        if in_code_block:
            chunk += "\n```"
            remaining = "```\n" + remaining
            in_code_block = False
        elif block_open and fence_count % 2 != 0:
            # We were inside a code block and the chunk closed it
            # properly -- nothing extra needed.
            pass

        chunks.append(chunk)

    return chunks


def format_approval_request(items: list[dict[str, Any]]) -> str:
    """Format tool-approval *items* into a human-readable message.

    Items use the server's SSE format: ``func_name``, ``preview``,
    ``approval_label``, ``header``.  Falls back to the nested
    ``function.name`` format for compatibility.
    """
    lines: list[str] = ["**Tool approval required:**"]
    for item in items:
        # Server SSE format: top-level func_name / preview
        name = item.get("func_name") or item.get("approval_label", "")
        if not name:
            # Fallback: nested function.name (SDK / older format)
            func = item.get("function", {})
            name = func.get("name", "unknown")
        preview = item.get("preview", "")
        if not preview:
            args = item.get("function", {}).get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            preview = str(args)
        preview = truncate(preview)
        header = item.get("header", "")
        if header:
            lines.append(f"\u2022 `{name}`: {header}")
        elif preview:
            lines.append(f"\u2022 `{name}`: {preview}")
        else:
            lines.append(f"\u2022 `{name}`")
    return "\n".join(lines)


def format_verdict(verdict: dict[str, Any]) -> str:
    """Format an intent verdict for display in a channel message.

    Accepts either a raw heuristic verdict dict (from ``_heuristic_verdict``
    in approval items) or an :class:`IntentVerdictEvent`-like dict with the
    same field names.  Returns Markdown text suitable for a Discord embed
    field.
    """
    risk = (verdict.get("risk_level") or "medium").upper()
    rec = verdict.get("recommendation", "review")
    raw_conf = verdict.get("confidence")
    conf = int((raw_conf if raw_conf is not None else 0.5) * 100)
    summary = verdict.get("intent_summary", "")
    tier = verdict.get("tier", "")

    emoji_map = {
        "LOW": "\U0001f7e2",
        "MEDIUM": "\U0001f7e1",
        "HIGH": "\U0001f534",
        "CRITICAL": "\u26d4",
    }
    emoji = emoji_map.get(risk, "\u2753")

    label = f"{tier.upper()} " if tier else ""
    parts = [f"{emoji} **{label}Risk: {risk}** ({conf}%) \u2014 {rec}"]
    if summary:
        parts.append(f"_{summary}_")
    return "\n".join(parts)


def format_tool_result(output: str) -> str:
    """Format a tool result into a compact code-block summary.

    Truncates to the first 10 lines (plus an ellipsis line if trimmed) or
    500 characters, whichever is shorter.
    """
    # Truncate to 10 lines.
    lines = output.split("\n", 10)
    if len(lines) > 10:
        lines = lines[:10]
        lines.append("\u2026")
    trimmed = "\n".join(lines)
    # Escape triple backticks to prevent code-block breakout.
    trimmed = trimmed.replace("```", "` ` `")
    # Truncate to 500 chars (after escaping, which can expand the string).
    if len(trimmed) > 500:
        trimmed = trimmed[:497] + "\u2026"
    return f"```\n{trimmed}\n```"


def truncate(text: str, max_length: int = 200) -> str:
    """Truncate *text* to *max_length*, appending an ellipsis if trimmed."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Rich media embed helpers (Discord)
# ---------------------------------------------------------------------------


def try_parse_media(output: str) -> dict[str, Any] | None:
    """Attempt to parse tool output as a media result.

    Returns the parsed dict when the output looks like structured media
    (single item, search results, or session list), otherwise ``None``.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # Single item with stream URL or detailed metadata.
    if "stream_url" in data or ("name" in data and "type" in data and "id" in data):
        return data
    # Search results.
    if "results" in data and isinstance(data["results"], list) and data["results"]:
        return data
    # Active sessions.
    if "sessions" in data and isinstance(data["sessions"], list):
        return data
    return None


_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})

# Cloud-metadata deny-list applied *before* the `is_private` allowance so
# ULA-hosted vendor metadata endpoints don't slip through the "private IPs
# are fine, we trust the LAN" exception.  IPv4 169.254.169.254 is caught
# by `is_link_local`; IPv6 ULA metadata (AWS Nitro IMDS at fd00:ec2::254,
# ECS task metadata at fd00:ec2::23) is `is_private` and needs explicit
# blocking.  Add new vendor prefixes here as they're published.
_BLOCKED_IP_NETWORKS: tuple[str, ...] = (
    "fd00:ec2::/32",  # AWS Nitro IMDS / ECS task metadata over IPv6
)


async def _is_safe_image_url(url: str) -> bool:
    """Validate that *url* uses http(s), has no embedded credentials, and does
    not target loopback, link-local (incl. cloud metadata 169.254.169.254),
    or reserved ranges — even after DNS resolution.

    Resolves the hostname and checks every returned address so a DNS
    rebinding attack cannot swap a safe-looking public IP for an
    internal one between validation and fetch.  Private/LAN IPs are
    still allowed (media servers typically live on the local network),
    so only loopback + link-local + multicast + reserved are rejected.

    NOTE: there is a residual TOCTOU gap because httpx resolves the
    hostname again when it actually issues the GET.  A 0-TTL rebinding
    resolver could still slip an internal IP in between validation and
    fetch.  Fully closing the gap requires pinning the validated IP on
    the connection (a custom httpx transport) — out of scope for this
    backfill pass.
    """
    import asyncio
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname in _BLOCKED_HOSTNAMES:
        return False

    # Collect candidate IPs: either an IP literal in the URL, or every
    # A/AAAA record the resolver returns for a hostname.
    candidates: list[str] = []
    try:
        ipaddress.ip_address(hostname)
        candidates.append(hostname)
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, socket.AF_UNSPEC)
        except socket.gaierror:
            return False
        # Strip IPv6 zone IDs (e.g. ``fe80::1%eth0``) before parsing —
        # ipaddress.ip_address would raise on them and we'd drop the host
        # on unrelated metadata.
        candidates = [str(info[4][0]).partition("%")[0] for info in infos]
        if not candidates:
            return False

    blocked_networks = [ipaddress.ip_network(cidr) for cidr in _BLOCKED_IP_NETWORKS]
    for raw in candidates:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        if any(ip in net for net in blocked_networks):
            return False
    return True


async def _fetch_thumbnail(
    http: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 5.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> tuple[bytes, str] | None:
    """Fetch a thumbnail image, returning ``(bytes, filename)`` or ``None``.

    Never raises — a failed image fetch must not break tool result
    rendering.  Private/LAN URLs are intentionally allowed (media servers
    are typically on the local network), but scheme is restricted to
    http(s) and userinfo is rejected.
    """
    if not await _is_safe_image_url(url):
        return None
    try:
        async with http.stream("GET", url, timeout=timeout) as resp:
            if resp.status_code != 200:
                return None
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                return None
            content_type = resp.headers.get("content-type", "image/jpeg").lower()
            if not content_type.startswith("image/"):
                return None
            ext = "jpg"
            if "png" in content_type:
                ext = "png"
            elif "webp" in content_type:
                ext = "webp"
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None
            return bytes(data), f"poster.{ext}"
    except Exception:  # noqa: BLE001
        return None


async def try_build_media_embed(
    tool_name: str,
    output: str,
    *,
    http: httpx.AsyncClient,
) -> tuple[Any, Any | None] | None:
    """Attempt to build a rich Discord embed from media tool output.

    Returns ``(embed, optional_file)`` if the output is parseable as media,
    or ``None`` to fall through to the default code-block formatter.

    The ``discord`` library is imported lazily since this module is shared
    across adapters and ``discord.py`` is an optional dependency.
    """
    data = try_parse_media(output)
    if data is None:
        return None

    import io

    import discord

    # Dispatch on result shape.
    if "results" in data and isinstance(data["results"], list):
        embed = _build_search_results_embed(data)
    elif "sessions" in data and isinstance(data["sessions"], list):
        embed = _build_sessions_embed(data)
    else:
        embed = _build_single_media_embed(data, tool_name)

    # Proxy thumbnail image.
    thumbnail_url = data.get("thumbnail_url") or data.get("image_url")
    if not thumbnail_url and data.get("results"):
        first = data["results"][0]
        thumbnail_url = first.get("thumbnail_url") or first.get("image_url")

    file: discord.File | None = None
    if thumbnail_url:
        fetched = await _fetch_thumbnail(http, thumbnail_url)
        if fetched:
            image_bytes, filename = fetched
            file = discord.File(io.BytesIO(image_bytes), filename=filename)
            embed.set_thumbnail(url=f"attachment://{filename}")

    return embed, file


# -- Private embed builders ------------------------------------------------


def _build_single_media_embed(data: dict[str, Any], tool_name: str) -> Any:
    """Build a Discord embed for a single media item."""
    import discord

    title = data.get("name", "Unknown")
    if data.get("year"):
        title += f" ({data['year']})"

    embed = discord.Embed(
        title=title,
        url=data.get("web_url"),  # safe link — NOT stream_url
        description=truncate(data.get("overview", ""), 200),
        color=discord.Color.teal(),
    )

    # Metadata fields (inline).
    meta_parts: list[str] = []
    if data.get("type"):
        meta_parts.append(data["type"])
    if data.get("official_rating"):
        meta_parts.append(data["official_rating"])
    if data.get("runtime_minutes"):
        hours = int(data["runtime_minutes"] // 60)
        mins = int(data["runtime_minutes"] % 60)
        meta_parts.append(f"{hours}h {mins}m" if hours else f"{mins}m")
    if meta_parts:
        embed.add_field(name="Info", value=" \u00b7 ".join(meta_parts), inline=True)

    if data.get("genres"):
        embed.add_field(name="Genres", value=", ".join(data["genres"][:5]), inline=True)

    if data.get("community_rating"):
        embed.add_field(
            name="Rating",
            value=f"{data['community_rating']:.1f}/10",
            inline=True,
        )

    # Extract server name from tool_name (mcp__servername__toolname).
    parts = tool_name.split("__")
    if len(parts) >= 3:
        embed.set_footer(text=parts[1])

    return embed


def _build_search_results_embed(data: dict[str, Any]) -> Any:
    """Build a Discord embed for a list of search results."""
    import discord

    results = data.get("results", [])
    total = data.get("total_count", len(results))

    lines: list[str] = []
    char_count = 0
    for i, r in enumerate(results[:10], 1):
        line = f"**{i}.** {r.get('name', '?')}"
        if r.get("year"):
            line += f" ({r['year']})"
        meta: list[str] = []
        if r.get("type"):
            meta.append(r["type"])
        if r.get("series_name"):
            meta.append(r["series_name"])
            if r.get("season_number") is not None and r.get("episode_number") is not None:
                meta.append(f"S{int(r['season_number']):02d}E{int(r['episode_number']):02d}")
        if r.get("runtime_minutes"):
            mins = r["runtime_minutes"]
            meta.append(f"{int(mins // 60)}h {int(mins % 60)}m" if mins >= 60 else f"{int(mins)}m")
        if meta:
            line += "  \u00b7  " + " \u00b7 ".join(meta)
        if char_count + len(line) + 1 > 4000:
            break
        lines.append(line)
        char_count += len(line) + 1

    embed = discord.Embed(
        title="Search results",
        description="\n".join(lines),
        color=discord.Color.teal(),
    )
    embed.set_footer(text=f"showing {len(lines)} of {total}")
    return embed


def _build_sessions_embed(data: dict[str, Any]) -> Any:
    """Build a Discord embed for active playback sessions."""
    import discord

    sessions = data.get("sessions", [])
    if not sessions:
        embed = discord.Embed(
            title="Now Playing",
            description="No active sessions.",
            color=discord.Color.light_grey(),
        )
        return embed

    lines: list[str] = []
    has_active = False
    for s in sessions:
        np = s.get("now_playing")
        device = s.get("device_name", "Unknown device")
        user = s.get("user_name", "")
        if np:
            has_active = True
            title = np.get("name", "Unknown")
            if np.get("year"):
                title += f" ({np['year']})"
            ps = s.get("play_state", {}) or {}
            pos = ps.get("position_seconds")
            runtime_min = np.get("runtime_minutes")
            time_str = ""
            if pos is not None and runtime_min:
                total_sec = int(runtime_min * 60)
                pos_i = int(pos)
                time_str = (
                    f" {pos_i // 3600}:{pos_i % 3600 // 60:02d}:{pos_i % 60:02d}"
                    f" / {total_sec // 3600}:{total_sec % 3600 // 60:02d}:{total_sec % 60:02d}"
                )
            paused = ps.get("is_paused", False)
            icon = "\u23f8" if paused else "\u25b6"
            line = f"**{title}** on {device}\n{icon}{time_str}"
            if user:
                line += f" \u00b7 {user}"
            lines.append(line)
        else:
            line = f"*{device}* \u2014 idle"
            if user:
                line += f" ({user})"
            lines.append(line)

    embed = discord.Embed(
        title="Now Playing",
        description="\n\n".join(lines),
        color=discord.Color.green() if has_active else discord.Color.light_grey(),
    )
    return embed

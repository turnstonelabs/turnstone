"""Command safety guards — soft guardrails against destructive commands."""

# Soft guardrail — catches common accidental destructive commands but is
# trivially bypassable (e.g. extra spaces, shell variable expansion).
# The user approval prompt is the primary security boundary.
BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "of=/dev/sd",
    "of=/dev/nvme",
    "of=/dev/vd",
    "of=/dev/xvd",
    "of=/dev/hd",
    "of=/dev/dm-",
    "of=/dev/md",
    "of=/dev/loop",
    "of=/dev/disk/",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sd",
    "> /dev/nvme",
    "> /dev/vd",
    "> /dev/xvd",
    "> /dev/hd",
    "> /dev/dm-",
    "> /dev/md",
    "> /dev/disk/",
    "mv / ",
    "chmod -R 777 /",
    "chown -R ",
]


def sanitize_command(cmd: str) -> str:
    """Replace common unicode look-alikes that break the shell."""
    return (
        cmd.replace("\u2018", "'")  # left single curly quote
        .replace("\u2019", "'")  # right single curly quote
        .replace("\u201c", '"')  # left double curly quote
        .replace("\u201d", '"')  # right double curly quote
        .replace("\u2013", "-")  # en dash
        .replace("\u2014", "-")  # em dash
    )


def is_command_blocked(cmd: str) -> str | None:
    """Return reason string if command is blocked, None otherwise."""
    cmd_stripped = cmd.strip()
    for pattern in BLOCKED_PATTERNS:
        if pattern in cmd_stripped:
            return f"Blocked: command matches dangerous pattern '{pattern}'"
    return None

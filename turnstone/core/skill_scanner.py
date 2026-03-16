"""Skill content scanner — production risk evaluation for skill install/update.

Evaluates skill content (SKILL.md text) and declared capabilities for security
risk signals.  Four axes scored independently, combined into a composite tier:
content risk, supply chain risk, vulnerability risk, and capability risk.

Patterns calibrated against 25K public agent skill security audits across
three independent auditors.

Performance target: <50ms synchronous.  Dependencies: stdlib only (re).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Included in ScanResult.to_dict() so stored reports carry their version.
# Bump when rules change.  Version-based re-scan (comparing stored version
# against current SCANNER_VERSION on load) is not yet implemented — this is
# infrastructure for future use by the rule update service.
SCANNER_VERSION = "1"

_TIERS = ("safe", "low", "medium", "high", "critical")
_THRESHOLDS = ((2.8, "critical"), (2.0, "high"), (1.2, "medium"), (0.5, "low"))


def _tier_from_composite(score: float) -> str:
    for threshold, label in _THRESHOLDS:
        if score >= threshold:
            return label
    return "safe"


# -- Frontmatter helpers ---------------------------------------------------

_RE_FM_FULL = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_RE_FM_PARTIAL = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter block, return body only."""
    m = _RE_FM_FULL.match(text) or _RE_FM_PARTIAL.match(text)
    return text[m.end() :] if m else text


# -- Calibrated regex patterns (from research scorer) ----------------------

# Intentionally includes an empty alternative (trailing |) so that untagged
# fenced code blocks (bare ```) count as shell blocks.  In SKILL.md files,
# untagged code blocks are most commonly shell commands.  This is calibrated
# against empirical distribution in public agent skill repositories.
_RE_SHELL_BLOCK_OPEN = re.compile(
    r"```(?:bash|sh|shell|zsh|fish|powershell|ps1|)\n",
    re.IGNORECASE,
)
_RE_PIPE_TO_SHELL = re.compile(
    r"(?:curl|wget|fetch)\s[^\n|]{0,200}\|\s*(?:ba)?sh\b",
    re.IGNORECASE,
)
_RE_EVAL_EXEC = re.compile(
    r"\beval\s*[(`\"'\$]|\bexec\s*[(\"`]|\beval\s+\$|\$\(\s*(?:curl|wget)\b",
    re.IGNORECASE,
)
_RE_SUBPROCESS = re.compile(
    r"subprocess\.(?:run|call|Popen|check_output)"
    r"|os\.(?:system|popen|exec[lv]p?)|shell=True",
    re.IGNORECASE,
)
_RE_SUDO = re.compile(r"\bsudo\s+\S+", re.IGNORECASE)
_RE_PKG_INSTALL = re.compile(
    r"\bpip\d*\s+install\b|\bnpm\s+(?:install|i)\b|\byarn\s+add\b"
    r"|\bpnpm\s+(?:install|add)\b|\bnpx\s+(?!-{1,2}\w)\S+"
    r"|\bapt(?:-get)?\s+install\b|\bbrew\s+install\b"
    r"|\bcargo\s+(?:add|install)\b|\bgo\s+(?:get|install)\b"
    r"|\bpoetry\s+add\b|\buv\s+(?:add|pip\s+install)\b",
    re.IGNORECASE,
)
_RE_SCRIPT_EXEC = re.compile(
    r"\bpython\d*\s+\S+\.py\b|\bnode\s+\S+\.(?:js|mjs|cjs)\b"
    r"|\bbash\s+\S+\.sh\b|\bRscript\s+\S+\.R\b",
    re.IGNORECASE,
)
_RE_CURL_WGET = re.compile(r"\b(?:curl|wget)\s", re.IGNORECASE)
_RE_CLOUD_CLI = re.compile(
    r"\b(?:az|gcloud|aws|terraform|kubectl|helm|ansible|docker|podman|vault)\s+\S+",
    re.IGNORECASE,
)
_RE_BROWSER_AUTO = re.compile(
    r"\b(?:playwright|puppeteer|selenium|pyppeteer|mechanize|browserless"
    r"|chromium|headless\s+(?:chrome|chromium|browser))\b",
    re.IGNORECASE,
)
_RE_HARDCODED_CREDS = re.compile(
    r"(?:password|passwd|token|secret|key)\s*[=:]\s*[\"'][^\"']{4,}[\"']"
    r"|echo\s+[\"'][^\"']{4,}[\"']\s*\|\s*\S+\s+(?:auth|login|pass)",
    re.IGNORECASE,
)
_RE_EXFIL = re.compile(
    r"\bngrok\b|\bcloudflared\b|\bpagekite\b"
    r"|\btunnel\b.*(?:expose|forward|proxy|cloudflare|ngrok)"
    r"|expose.*(?:localhost|port\s+\d)"
    r"|\bcookies?\s+(?:export|sync|dump|steal)\b"
    r"|\bsession[_\-]?token\b.*\bplaintext\b"
    r"|(?:export|sync)\s+(?:browser\s+)?(?:cookie|session|credential|profile)\b",
    re.IGNORECASE,
)
_RE_AUTH_ACCESS = re.compile(
    r"\b(?:oauth|jwt|bearer\s+token|api[_\s\-]?key|access[_\s\-]?token"
    r"|authenticate|authorization|login|logout)\b",
    re.IGNORECASE,
)
_RE_CRED_ENV = re.compile(
    r"\$(?:[A-Z][A-Z_]{2,})\b|os\.environ\[|process\.env\.",
    re.IGNORECASE,
)
_RE_CREDS = re.compile(
    r"\b(?:api[_\-]?key|apikey|secret[_\-]?key|access[_\-]?token"
    r"|auth[_\-]?token|bearer[_\-]?token|password|passwd"
    r"|private[_\-]?key|client[_\-]?secret|credentials?"
    r"|\.env\b|\.netrc\b|\.aws/credentials|keyring"
    r"|OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN"
    r"|session[_\-]?token|cookie)\b",
    re.IGNORECASE,
)
_RE_TRANSITIVE_INSTALL = re.compile(
    r"npx\s+skills\s+add\b|\bskills\s+add\s+\S+"
    r"|install\s+from\s+(?:github|registry|third.party|external)\b"
    r"|add\s+from\s+(?:github|registry|untrusted)\b",
    re.IGNORECASE,
)
_RE_OBFUSCATION = re.compile(
    r"[A-Za-z0-9+/]{60,}={0,2}"
    r"|\\x[0-9a-fA-F]{2}(?:\\x[0-9a-fA-F]{2}){10,}"
    r"|unescape\(|fromCharCode\(|atob\(\s*[\"']",
    re.IGNORECASE,
)
_RE_DOWNLOAD_EXEC = re.compile(
    r"(?:curl|wget)\s+-[^\n]{5,}\n[^\n]{0,60}(?:chmod\s+\+x|sh\s|bash\s|exec\s)\b",
    re.IGNORECASE | re.MULTILINE,
)
_RE_EXEC_URL_RAW = re.compile(
    r"https?://[^\s\"'<>)]{5,}\.(?:sh|bash|ps1|exe|msi|dmg|pkg|deb|rpm|run|bin)\b",
    re.IGNORECASE,
)
_RE_RAW_URL = re.compile(
    r"https?://(?:raw\.githubusercontent\.com|gist\.github\.com"
    r"|pastebin\.com|paste\.ee|hastebin\.com)/[^\s\"'<>)]+",
    re.IGNORECASE,
)
_TRUSTED_EXEC_DOMAINS = re.compile(
    r"https?://(?:aka\.ms/"
    r"|(?:[\w-]+\.)?microsoft\.com/"
    r"|(?:[\w-]+\.)?github\.com/"
    r"|raw\.githubusercontent\.com/"
    r"|(?:docs|learn)\.microsoft\.com/"
    r"|docs\.github\.com/"
    r"|docs\.docker\.com/"
    r"|get\.docker\.com|brew\.sh|npmjs\.com|pypi\.org"
    r"|install\.python-poetry\.org|sh\.rustup\.rs|bootstrap\.pypa\.io)",
    re.IGNORECASE,
)
_RE_E004 = re.compile(
    r"(?<!\w)IGNORE\s+(?:any\s+)?(?:prior\s+|previous\s+)?"
    r"(?:training|instruction|context|rules?)\b(?!\s+delimiter)"
    r"|MANDATORY\s+COMPLIANCE\b"
    r"|(?:^|\.\s+|\n)(?:MUST|SHALL)\s+supersede\s+(?:all\s+)?(?:other\s+)?"
    r"(?:source|instruction|training)"
    r"|override\s+(?:your\s+)?(?:training|system\s+prompt|all\s+(?:previous\s+)?instruction)"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|other)\s+instruction"
    r"|forget\s+(?:all\s+)?(?:previous|prior)\s+instruction"
    r"|(?:instructions?\s+designed\s+to|intended\s+to)\s+override\s+(?:the\s+)?agent"
    r"|override\s+(?:the\s+)?agent.s\s+general\s+knowledge"
    r"|authoritative\s+instructions?\s+designed\s+to\s+(?:supersede|override|replace)",
    re.IGNORECASE | re.MULTILINE,
)
_RE_E004_NEGATION = re.compile(
    r"(?:does\s+not\s+(?:use|include|utilize)|without|absent|no\b|lack)"
    r"\s.{0,60}(?:ignore|override|supersede)",
    re.IGNORECASE,
)
_RE_E005_RAW = re.compile(
    r"https?://[^\s\"'<>)]{5,}\.(?:sh|bash|ps1|exe|msi|run|bin)\b"
    r"|malicious\.com\b|evil\.com\b",
    re.IGNORECASE,
)
_RE_W007_POSITIVE = re.compile(
    r"echo\s+[\"'][^\"']{4,}[\"']\s*\|"
    r"|password\s*=\s*[\"'][^\"']{4,}[\"']"
    r"|(?:fill|type|enter)\s+\S+\s+\"[^\"]{4,}\""
    r"|session\s+(?:token|state)\s+(?:in\s+)?plaintext"
    r"|state\s+files?\s+(?:can\s+)?contain\s+session\s+tokens"
    r"|(?:store|save|write)\s+(?:secret|token|password|key)\s+in\s+plaintext"
    r"|tokens?\s+in\s+plaintext\b"
    r"|\bcookies?\s+(?:export|sync|steal|dump)\b"
    r"|(?:export|sync)\s+(?:browser\s+)?(?:cookie|session|credential|profile)\b",
    re.IGNORECASE,
)
_RE_W007_AMBIGUOUS = re.compile(
    r"hardcode[d]?\s+(?:credential|password|secret|token|key)"
    r"|plaintext\s+(?:password|credential|key)"
    r"|inline\s+(?:secret|credential|token)\s+in\s+(?:code|script|command)",
    re.IGNORECASE,
)
_RE_NEGATION_CONTEXT = re.compile(
    r"(?:avoid|don.t|do\s+not|never|against|discourage|prohibit"
    r"|recommend\s+against|advising\s+against|warns?\s+against"
    r"|moving\s+away\s+from|instead\s+of|over\s+hardcoded|over\s+plaintext)"
    r"\s?.{0,80}(?:hardcode|inline|plaintext|secret|credential)"
    r"|(?:hardcode|inline|plaintext|secret).{0,80}"
    r"(?:should\s+(?:not|never)|must\s+not|is\s+(?:insecure|unsafe|bad|dangerous)"
    r"|are\s+(?:insecure|unsafe))"
    r"|(?:X\s+over|instead\s+of|rather\s+than|prefer\s+\S+\s+(?:over|to))\s+hardcoded"
    r"|hardcoded\s+(?:secret|credential|password)\s+(?:or|and)\s+(?:use|prefer|recommend)"
    r"|\bover\s+hardcoded\s+(?:secret|credential|password|key|token)"
    r"|\binstead\s+of\s+hardcoded\s+(?:secret|credential|password|key|token)",
    re.IGNORECASE,
)
_RE_W011 = re.compile(
    r"fetch.*untrusted\b"
    r"|ingest.*external\s+(?:instruction|command|rule|control)\b"
    r"|process.*third.party\s+(?:instruction|content.*agent|code)\b"
    r"|web\s+content.*agent\b|agent.*web\s+content\b"
    r"|(?:fetch|retrieve|download)\s[^\n]{0,80}(?:instruction|command|rule)\b"
    r"|apply\s+(?:all\s+)?rules?\s+from\s+(?:the\s+)?fetched\b"
    r"|(?:act|execute)\s+on\s+(?:fetched|retrieved|external)\s+(?:content|instruction)"
    r"|allow.*override\s+(?:system|context|instruction)",
    re.IGNORECASE,
)
_RE_W011_NO_SANITIZE = re.compile(
    r"(?:no\s+(?:explicit\s+)?(?:boundary|delimiter|sanitiz)"
    r"|(?:sanitiz|escap).*absent|absent.*(?:sanitiz|escap)"
    r"|without\s+(?:validation|sanitiz|escaping|boundary))",
    re.IGNORECASE,
)
_RE_W011_FETCH_INSTRUCTION = re.compile(
    r"(?:fetch|retrieve|load|apply)\s[^\n]{0,80}(?:instruction|rule|command|guideline)",
    re.IGNORECASE,
)
_RE_W012 = re.compile(
    r"(?:fetch|download|load|execute)\s[^\n]{0,80}(?:url|endpoint|remote)"
    r"[^\n]{0,80}(?:instruction|rule|command|control)"
    r"|remote\s+url\s+(?:that\s+)?(?:control|alter|change|influence)\s+agent"
    r"|external\s+(?:url|source)\s+.*(?:alter|control)\s+(?:agent|behavior)",
    re.IGNORECASE,
)
_RE_RCE_RAW = re.compile(
    r"\bremote\s+(?:code\s+exec|exec(?:ution)?)\b"
    r"|\barbitrary\s+(?:python|javascript|code|script)\b",
    re.IGNORECASE,
)
_RE_RCE_NEGATION = re.compile(
    r"\b(?:no|not|without|absent|none|zero|prevent|mitigat|block)\b"
    r".{0,40}(?:remote\s+code|arbitrary\s+code|rce\b)",
    re.IGNORECASE,
)
_RE_CRED_FILE = re.compile(
    r"[\./~][^\s]*(?:\.pem|\.key|\.p12|\.pfx|id_rsa|id_ecdsa|\.kubeconfig)\b"
    r"|\~/\.(?:aws|gcp|azure|kube|ssh)/"
    r"|\.env(?:\.local|\.production|\.development)?\b",
    re.IGNORECASE,
)
_RE_ALL_URLS = re.compile(r"https?://[^\s\"'<>)]{4,}", re.IGNORECASE)
_RE_PACKAGES = re.compile(
    r"(?:npm|pip|gem|cargo|go\s+get|brew\s+install"
    r"|apt(?:-get)?\s+install|dnf\s+install|yum\s+install)\s+\S+",
    re.IGNORECASE,
)

# -- Negation-aware count helpers ------------------------------------------


def _count_e004(text: str) -> int:
    """Count E004 (prompt injection) hits, filtering negation context."""
    count = 0
    for m in _RE_E004.finditer(text):
        start = max(0, m.start() - 100)
        end = min(len(text), m.end() + 50)
        if not _RE_E004_NEGATION.search(text[start:end]):
            count += 1
    return count


def _count_e005(text: str) -> int:
    """Count E005 (suspicious executable URL) hits, excluding trusted domains."""
    return sum(1 for m in _RE_E005_RAW.finditer(text) if not _TRUSTED_EXEC_DOMAINS.match(m.group()))


def _count_w007(text: str) -> int:
    """Count W007 (insecure credential handling) hits with negation filtering."""
    count = len(_RE_W007_POSITIVE.findall(text))
    for m in _RE_W007_AMBIGUOUS.finditer(text):
        start = max(0, m.start() - 200)
        end = min(len(text), m.end() + 200)
        if not _RE_NEGATION_CONTEXT.search(text[start:end]):
            count += 1
    return count


def _count_w011(text: str) -> int:
    """Count W011 (third-party content exposure) with compound logic."""
    count = len(_RE_W011.findall(text))
    for m in _RE_W011_NO_SANITIZE.finditer(text):
        start = max(0, m.start() - 400)
        end = min(len(text), m.end() + 400)
        if _RE_W011_FETCH_INSTRUCTION.search(text[start:end]):
            count += 1
    return count


def _count_rce(text: str) -> int:
    """Count RCE pattern hits, filtering negations."""
    count = 0
    for m in _RE_RCE_RAW.finditer(text):
        start = max(0, m.start() - 80)
        if not _RE_RCE_NEGATION.search(text[start : m.end()]):
            count += 1
    return count


def _count_exec_urls(text: str) -> int:
    """Count executable URL hits, excluding trusted vendor domains."""
    return sum(
        1 for m in _RE_EXEC_URL_RAW.finditer(text) if not _TRUSTED_EXEC_DOMAINS.match(m.group())
    )


# -- Feature extraction ----------------------------------------------------


@dataclass
class _Features:
    """Raw feature counts extracted from skill content."""

    shell_block_count: int = 0
    pipe_to_shell: int = 0
    eval_exec: int = 0
    subprocess_calls: int = 0
    sudo_usage: int = 0
    pkg_install: int = 0
    script_exec: int = 0
    curl_wget: int = 0
    exec_urls: int = 0
    raw_script_urls: int = 0
    cloud_cli: int = 0
    browser_auto: int = 0
    hardcoded_creds: int = 0
    exfil_patterns: int = 0
    rce_patterns: int = 0
    auth_access: int = 0
    cred_mentions: int = 0
    cred_env: int = 0
    transitive_install: int = 0
    obfuscation: int = 0
    download_exec: int = 0
    e004_prompt_injection: int = 0
    e005_suspicious_url: int = 0
    w007_insecure_creds: int = 0
    w011_third_party_content: int = 0
    w012_unverifiable_dep: int = 0
    cred_file_access: int = 0
    url_count: int = 0
    package_refs: int = 0


def _extract_features(body: str) -> _Features:
    """Extract all risk features from skill body text (frontmatter stripped)."""
    f = _Features()
    f.shell_block_count = len(_RE_SHELL_BLOCK_OPEN.findall(body))
    f.pipe_to_shell = len(_RE_PIPE_TO_SHELL.findall(body))
    f.eval_exec = len(_RE_EVAL_EXEC.findall(body))
    f.subprocess_calls = len(_RE_SUBPROCESS.findall(body))
    f.sudo_usage = len(_RE_SUDO.findall(body))
    f.pkg_install = len(_RE_PKG_INSTALL.findall(body))
    f.script_exec = len(_RE_SCRIPT_EXEC.findall(body))
    f.curl_wget = len(_RE_CURL_WGET.findall(body))
    f.exec_urls = _count_exec_urls(body)
    f.raw_script_urls = len(_RE_RAW_URL.findall(body))
    f.cloud_cli = len(_RE_CLOUD_CLI.findall(body))
    f.browser_auto = len(_RE_BROWSER_AUTO.findall(body))
    f.hardcoded_creds = len(_RE_HARDCODED_CREDS.findall(body))
    f.exfil_patterns = len(_RE_EXFIL.findall(body))
    f.rce_patterns = _count_rce(body)
    f.auth_access = len(_RE_AUTH_ACCESS.findall(body))
    f.cred_mentions = len(_RE_CREDS.findall(body))
    f.cred_env = len(_RE_CRED_ENV.findall(body))
    f.transitive_install = len(_RE_TRANSITIVE_INSTALL.findall(body))
    f.obfuscation = len(_RE_OBFUSCATION.findall(body))
    f.download_exec = len(_RE_DOWNLOAD_EXEC.findall(body))
    f.e004_prompt_injection = _count_e004(body)
    f.e005_suspicious_url = _count_e005(body)
    f.w007_insecure_creds = _count_w007(body)
    f.w011_third_party_content = _count_w011(body)
    f.w012_unverifiable_dep = len(_RE_W012.findall(body))
    f.cred_file_access = len(_RE_CRED_FILE.findall(body))
    f.url_count = len(_RE_ALL_URLS.findall(body))
    f.package_refs = len(_RE_PACKAGES.findall(body))
    return f


# -- Axis scoring ----------------------------------------------------------


def _score_content_risk(f: _Features) -> tuple[float, list[str]]:
    """Content risk: 0=no shell, 1=shell, 2=operational, 2.5=auth, 3=eval, 3.5=pipe-to-shell."""
    score = 0.0
    flags: list[str] = []

    if f.pipe_to_shell > 0:
        score = max(score, 3.5)
        flags.append("pipe_to_shell")
    if f.exfil_patterns > 0 and f.sudo_usage > 0:
        score = max(score, 3.5)
        flags.append("exfil_with_sudo")
    elif f.exfil_patterns > 0:
        score = max(score, 2.5)
        flags.append("data_exfiltration")
    if f.eval_exec > 0:
        score = max(score, 3.0)
        flags.append("eval_exec")
    if f.exec_urls > 0:
        score = max(score, 3.0)
        flags.append("executable_urls")
    if f.download_exec > 0:
        score = max(score, 3.0)
        flags.append("download_exec_chain")

    has_ops = (
        f.pkg_install > 0
        or f.script_exec > 0
        or f.curl_wget > 0
        or f.browser_auto > 0
        or f.cloud_cli > 0
    )
    if f.sudo_usage > 0 and "exfil_with_sudo" not in flags:
        score = max(score, 2.5)
        flags.append("sudo_usage")
    if f.hardcoded_creds > 0:
        score = max(score, 2.5)
        flags.append("hardcoded_credentials")

    if f.shell_block_count >= 2 and has_ops:
        if f.auth_access > 0 or f.cred_mentions >= 3:
            score = max(score, 2.5)
            if "auth_credential_access" not in flags:
                flags.append("auth_credential_access")
        else:
            score = max(score, 2.0)
        if "operational_shell" not in flags:
            flags.append("operational_shell")
    elif (
        f.shell_block_count >= 5
        or (f.shell_block_count >= 1 and has_ops)
        or (f.shell_block_count >= 1 and (f.auth_access > 0 or f.cloud_cli > 0))
    ):
        score = max(score, 2.0)
        if "operational_shell" not in flags:
            flags.append("operational_shell")
    elif f.shell_block_count >= 1:
        score = max(score, 1.0)
        flags.append("shell_blocks")

    if f.raw_script_urls > 0 and score < 2.5:
        score = max(score, 2.0)
        flags.append("raw_script_urls")
    if f.rce_patterns > 0:
        score = max(score, 2.0)
        if "rce_language" not in flags:
            flags.append("rce_language")
    if f.subprocess_calls > 0 and score < 2.0:
        score = max(score, 1.5)
        flags.append("subprocess_calls")

    return min(4.0, score), flags


def _score_supply_chain_risk(f: _Features) -> tuple[float, list[str]]:
    """Supply chain: 0=none, 2=obfuscation, 3=exec URLs, 4=pipe-to-shell/transitive."""
    score = 0.0
    flags: list[str] = []

    if f.pipe_to_shell > 0:
        score = max(score, 4.0)
        flags.append("pipe_to_shell")
    if f.transitive_install > 0:
        score = max(score, 4.0)
        flags.append("transitive_install")
    if f.download_exec > 0:
        score = max(score, 3.0)
        flags.append("download_exec_chain")
    if f.e005_suspicious_url > 0:
        score = max(score, 3.0)
        flags.append("suspicious_executable_url")
    if f.exec_urls > 0:
        score = max(score, 3.0)
        flags.append("untrusted_executable_url")
    if f.raw_script_urls > 0:
        score = max(score, 3.0)
        flags.append("raw_script_url")
    if f.eval_exec > 0 and f.exfil_patterns > 0:
        score = max(score, 3.0)
        flags.append("eval_exfil_combo")
    if f.obfuscation > 0:
        score = max(score, 2.0)
        flags.append("obfuscation")

    return min(4.0, score), flags


def _score_vuln_risk(f: _Features) -> tuple[float, list[str]]:
    """Vulnerability: 0=none, 1.5=operational floor, 2=W011, 3=W007, 4=E004/E005."""
    score = 0.0
    flags: list[str] = []

    # Critical
    if f.e004_prompt_injection > 0:
        score = max(score, 4.0)
        flags.append("prompt_injection_override")
    if f.e005_suspicious_url > 0:
        score = max(score, 4.0)
        flags.append("suspicious_executable_url")
    if f.transitive_install > 0:
        score = max(score, 4.0)
        flags.append("transitive_install")
    if f.eval_exec > 0 and f.exfil_patterns > 0:
        score = max(score, 4.0)
        flags.append("eval_exfil_combo")

    # High: credential patterns
    if f.w007_insecure_creds > 0:
        score = max(score, 3.0)
        flags.append("insecure_credential_handling")
    if f.hardcoded_creds > 0:
        score = max(score, 3.0)
        flags.append("hardcoded_credentials")
    if (f.auth_access > 0 and f.cred_mentions >= 2) or (f.cred_mentions >= 5 and f.cred_env > 0):
        score = max(score, 3.0)
        flags.append("auth_credential_density")
    elif (f.cred_mentions >= 3 and (f.cred_env > 0 or f.url_count >= 3)) or f.auth_access > 0:
        score = max(score, 2.5)
        if "auth_access" not in flags:
            flags.append("auth_access")
    elif f.cred_mentions >= 2 and f.cred_env > 0:
        score = max(score, 2.0)
    elif f.cred_file_access > 0 and f.cred_mentions >= 2:
        score = max(score, 2.5)
        flags.append("credential_file_access")

    # Medium: third-party content
    if f.w011_third_party_content >= 2 or (
        f.w011_third_party_content == 1 and (f.raw_script_urls > 0 or f.transitive_install > 0)
    ):
        score = max(score, 2.0)
        flags.append("third_party_content_exposure")
    elif f.w011_third_party_content == 1 and (f.url_count > 10 or f.package_refs > 3):
        score = max(score, 1.5)
        flags.append("third_party_content_weak")
    if f.w012_unverifiable_dep > 0:
        score = max(score, 2.0)
        flags.append("unverifiable_dependency")
    if f.raw_script_urls > 0 and score < 2.0:
        score = max(score, 2.0)
        flags.append("raw_script_url")
    if f.browser_auto > 0 and score < 2.0:
        score = max(score, 2.0)
        flags.append("browser_automation")
    if f.url_count >= 5 and score < 2.0:
        score = max(score, 1.5)
    if f.pkg_install > 0 and score < 1.5:
        score = max(score, 1.5)
        flags.append("package_install")

    # Operational floor
    has_ops = (
        f.pkg_install > 0
        or f.script_exec > 0
        or f.curl_wget > 0
        or f.browser_auto > 0
        or f.cloud_cli > 0
    )
    if f.shell_block_count >= 2 and has_ops and score < 1.5:
        score = max(score, 1.5)

    return min(4.0, score), flags


# -- Capability risk from allowed_tools ------------------------------------

_READ_ONLY_TOOLS = frozenset(
    {
        "read",
        "grep",
        "glob",
        "websearch",
        "web_search",
        "ls",
        "lsp",
        "taskget",
        "tasklist",
    }
)
_WRITE_TOOLS = frozenset({"write", "edit", "notebookedit", "notebook_edit"})

_BASH_SAFE = re.compile(
    r"^(?:git|ls|cat|head|tail|wc|diff|find|grep|rg|echo|pwd|date|whoami)(?::\*)?$",
    re.IGNORECASE,
)
_BASH_BROAD = re.compile(
    r"^(?:npm|docker|podman|kubectl|helm|terraform|ansible|vagrant"
    r"|aws|gcloud|az|pip|cargo|go|make|cmake)(?::\*)?$",
    re.IGNORECASE,
)
_BASH_DESTRUCTIVE = re.compile(
    r"^(?:rm|rmdir|dd|mkfs|fdisk|shred|kill|pkill|shutdown|reboot"
    r"|chmod|chown|iptables|systemctl)(?::\*)?$",
    re.IGNORECASE,
)


def _score_capability_risk(allowed_tools: list[str] | None) -> tuple[float, list[str]]:
    """Capability: 0=none, 0.5=read, 1.5=write/safe bash, 2=MCP, 2.5=broad, 3.5=unrestricted, 4=destructive."""
    if not allowed_tools:
        return 0.0, []

    flags: list[str] = []
    tool_scores: list[float] = []

    for tool in allowed_tools:
        ts = tool.strip()
        tl = ts.lower()

        bash_m = re.match(r"^bash\s*(?:\(([^)]*)\))?$", ts, re.IGNORECASE)
        if bash_m:
            constraint = bash_m.group(1)
            if constraint is None or constraint.strip() in ("", "*"):
                tool_scores.append(3.5)
                flags.append("bash_unrestricted")
            else:
                cc = constraint.strip()
                if _BASH_DESTRUCTIVE.match(cc):
                    tool_scores.append(4.0)
                    flags.append(f"bash_destructive({cc})")
                elif _BASH_BROAD.match(cc):
                    tool_scores.append(2.5)
                    flags.append(f"bash_broad({cc})")
                elif _BASH_SAFE.match(cc):
                    tool_scores.append(1.5)
                    flags.append(f"bash_safe({cc})")
                else:
                    tool_scores.append(2.5)
                    flags.append(f"bash_unknown({cc})")
            continue

        if tl.startswith("mcp__"):
            tool_scores.append(2.0)
            flags.append(f"mcp_tool({ts})")
            continue

        name_only = tl.split("(")[0].strip()
        if name_only in _WRITE_TOOLS:
            tool_scores.append(1.5)
            flags.append(f"write_tool({ts})")
        elif name_only in _READ_ONLY_TOOLS:
            tool_scores.append(0.5)
        else:
            tool_scores.append(1.0)
            flags.append(f"unknown_tool({ts})")

    if not tool_scores:
        return 0.0, []

    # Max score + 0.5 per additional high-risk tool (>=2.0), capped at 4.0
    tool_scores.sort(reverse=True)
    high_risk_extra = sum(1 for s in tool_scores[1:] if s >= 2.0)
    return min(4.0, tool_scores[0] + high_risk_extra * 0.5), flags


# -- Public API ------------------------------------------------------------


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning skill content for security risk signals."""

    tier: str  # "safe" | "low" | "medium" | "high" | "critical"
    composite: float  # 0.0-4.0
    content_risk: float  # 0.0-4.0
    supply_chain_risk: float
    vuln_risk: float
    capability_risk: float
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage and API transport."""
        return {
            "tier": self.tier,
            "composite": round(self.composite, 3),
            "content_risk": round(self.content_risk, 3),
            "supply_chain_risk": round(self.supply_chain_risk, 3),
            "vuln_risk": round(self.vuln_risk, 3),
            "capability_risk": round(self.capability_risk, 3),
            "flags": list(self.flags),
            "details": dict(self.details),
            "scanner_version": SCANNER_VERSION,
        }


def scan_skill(
    content: str,
    allowed_tools: list[str] | None = None,
) -> ScanResult:
    """Evaluate skill content for risk signals.

    Scans the content text (typically a SKILL.md body) for patterns indicating
    security risk across four axes: content, supply chain, vulnerability, and
    declared capability.

    Args:
        content: Raw skill content text (may include YAML frontmatter).
        allowed_tools: Optional declared tool permissions (e.g. ["Bash(*)", "Read"]).

    Returns:
        Frozen ScanResult with tier, composite score, per-axis scores, flags,
        and a details dict for structured storage.
    """
    body = _strip_frontmatter(content)
    features = _extract_features(body)

    content_score, content_flags = _score_content_risk(features)
    supply_score, supply_flags = _score_supply_chain_risk(features)
    vuln_score, vuln_flags = _score_vuln_risk(features)
    cap_score, cap_flags = _score_capability_risk(allowed_tools)

    composite = content_score * 0.25 + supply_score * 0.25 + vuln_score * 0.25 + cap_score * 0.25

    # Floor rule: if any single axis is critical (4.0), composite tier is at
    # least "medium".  A skill with "IGNORE all prior instructions" (vuln=4.0)
    # but no other signals should not be classified as merely "low".
    max_axis = max(content_score, supply_score, vuln_score, cap_score)
    if max_axis >= 4.0:
        composite = max(composite, 1.2)  # medium threshold

    tier = _tier_from_composite(composite)

    # Deduplicate flags preserving order
    all_flags = content_flags + supply_flags + vuln_flags + cap_flags
    seen: set[str] = set()
    unique_flags: list[str] = []
    for flag in all_flags:
        if flag not in seen:
            seen.add(flag)
            unique_flags.append(flag)

    details: dict[str, Any] = {
        "content": {
            "score": round(content_score, 3),
            "flags": content_flags,
            "shell_blocks": features.shell_block_count,
            "pipe_to_shell": features.pipe_to_shell,
            "eval_exec": features.eval_exec,
            "sudo": features.sudo_usage,
            "exfil": features.exfil_patterns,
            "hardcoded_creds": features.hardcoded_creds,
        },
        "supply_chain": {
            "score": round(supply_score, 3),
            "flags": supply_flags,
            "transitive_install": features.transitive_install,
            "obfuscation": features.obfuscation,
            "download_exec": features.download_exec,
            "exec_urls": features.exec_urls,
            "raw_script_urls": features.raw_script_urls,
        },
        "vulnerability": {
            "score": round(vuln_score, 3),
            "flags": vuln_flags,
            "e004_prompt_injection": features.e004_prompt_injection,
            "e005_suspicious_url": features.e005_suspicious_url,
            "w007_insecure_creds": features.w007_insecure_creds,
            "w011_third_party": features.w011_third_party_content,
            "w012_unverifiable_dep": features.w012_unverifiable_dep,
        },
        "capability": {
            "score": round(cap_score, 3),
            "flags": cap_flags,
            "allowed_tools": list(allowed_tools) if allowed_tools else [],
        },
    }

    return ScanResult(
        tier=tier,
        composite=composite,
        content_risk=content_score,
        supply_chain_risk=supply_score,
        vuln_risk=vuln_score,
        capability_risk=cap_score,
        flags=unique_flags,
        details=details,
    )

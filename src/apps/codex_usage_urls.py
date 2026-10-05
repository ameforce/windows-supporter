"""Shared navigation and landing-page contract for current Codex usage."""

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


CURRENT_CODEX_USAGE_URL = "https://chatgpt.com/settings/usage?tab=overview"
CODEX_USAGE_CANONICAL_PATH = "/settings/usage"
CODEX_USAGE_PAGE_PATHS = (
    "/codex/settings/usage",
    "/codex/cloud/settings/usage",
    "/codex/settings/analytics",
    "/codex/cloud/settings/analytics",
)


def _chatgpt_url(value: str):
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() != "chatgpt.com":
        return None
    return parsed


def is_legacy_usage_dialog_url(value: str) -> bool:
    parsed = _chatgpt_url(value)
    if parsed is None or parsed.path not in ("", "/"):
        return False
    tabs = [val for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key == "tab"]
    return parsed.fragment.lower() == "settings/usage" and tabs in ([], ["overview"])


def canonicalize_codex_usage_url(value: str) -> str:
    """Migrate a saved collection target, including the old analytics redirect."""
    text = str(value or "").strip()
    if not text:
        return CURRENT_CODEX_USAGE_URL
    parsed = _chatgpt_url(text)
    if parsed is None:
        return text
    if parsed.path.rstrip("/") not in ("", CODEX_USAGE_CANONICAL_PATH, *CODEX_USAGE_PAGE_PATHS):
        return text
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key != "tab"]
    query.append(("tab", "overview"))
    return urlunsplit(("https", "chatgpt.com", CODEX_USAGE_CANONICAL_PATH, urlencode(query), ""))


def is_codex_usage_url(value: str) -> bool:
    """Only current-value pages qualify; history is never a fresh snapshot."""
    parsed = _chatgpt_url(value)
    if parsed is None:
        return False
    if is_legacy_usage_dialog_url(value):
        return True
    path = parsed.path.rstrip("/")
    if path == CODEX_USAGE_CANONICAL_PATH:
        tabs = [val for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key == "tab"]
        return tabs in ([], ["overview"])
    if path in CODEX_USAGE_PAGE_PATHS:
        return not path.endswith("analytics") or parsed.fragment.lower() in ("", "usage")
    return False


def are_equivalent_codex_usage_urls(left: str, right: str) -> bool:
    left, right = str(left or "").strip(), str(right or "").strip()
    if is_codex_usage_url(left) and is_codex_usage_url(right):
        return canonicalize_codex_usage_url(left) == canonicalize_codex_usage_url(right)
    return left == right


def build_codex_login_entry_url(usage_url: str) -> str:
    normalized = canonicalize_codex_usage_url(usage_url)
    parsed = _chatgpt_url(normalized) or urlsplit(CURRENT_CODEX_USAGE_URL)
    target = urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    return f"https://chatgpt.com/auth/login?next={quote(target, safe='/')}"

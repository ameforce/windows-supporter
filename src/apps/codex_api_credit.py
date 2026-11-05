"""OpenAI Platform API credit balance: page address, in-page probe and parsing.

The prepaid API credit balance is shown on the organization billing overview page
of the OpenAI Platform. It is read from the browser profile that already collects
Codex usage, never through an API key. The driver, the monitor and the tests share
this one definition. Nothing here logs or stores the amount; callers decide that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any
from urllib.parse import urlsplit


API_CREDIT_BILLING_URL = "https://platform.openai.com/settings/organization/billing/overview"
API_CREDIT_HOST = "platform.openai.com"
_API_CREDIT_BILLING_PATH = "/settings/organization/billing/overview"
# Signed in on the billing page, but the balance stayed unreadable across several reads.
API_CREDIT_UNREADABLE_REASON = "balance_unreadable"

# The page hosts a single-page app: the text is read from the rendered body only,
# so hidden panels and unrendered templates never count as the current balance.
API_CREDIT_PAGE_PROBE_SCRIPT = r"""
() => {
  const body = document.body;
  const rendered = (node) => {
    if (!node || !node.getClientRects().length) return '';
    if (['hidden', 'collapse'].includes(getComputedStyle(node).visibility)) return '';
    return typeof node.innerText === 'string' ? node.innerText : (node.textContent || '');
  };
  return {
    url: location.href,
    title: document.title || '',
    text: String(rendered(body)).slice(0, 30000),
  };
}
"""


@unique
class ApiCreditStatus(StrEnum):
    OK = "ok"
    LOGIN_REQUIRED = "login_required"
    UNAVAILABLE = "unavailable"


API_CREDIT_STATUS_TEXT: dict[str, str] = {
    ApiCreditStatus.OK.value: "정상",
    ApiCreditStatus.LOGIN_REQUIRED.value: "로그인 필요",
    ApiCreditStatus.UNAVAILABLE.value: "조회 불가",
}

# Reasons that may still resolve while the single-page app finishes rendering or
# a challenge page clears itself. Everything else ends the wait immediately.
_TRANSIENT_REASONS = frozenset(
    {"loading", "balance_not_found", "unexpected_page", "challenge", "probe_invalid"}
)

# The page's own title. When it is present, the synonyms below are ignored so a
# grant or another credit figure printed earlier can never pass as the balance.
_PRIMARY_BALANCE_LABELS = frozenset(
    {"credit balance", "credits balance", "크레딧 잔액", "크레딧 잔고"}
)
# Longest first: "available credits" must win over its prefix "available credit".
_BALANCE_LABELS = tuple(
    sorted(
        (
            *_PRIMARY_BALANCE_LABELS,
            "available credit",
            "available credits",
            "credit available",
            "remaining credit",
            "remaining credits",
            "prepaid balance",
            "사용 가능한 크레딧",
            "남은 크레딧",
            "잔여 크레딧",
        ),
        key=len,
        reverse=True,
    )
)

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SIGN = r"[-−–]"
_AMOUNT_LEADING = (
    rf"(?P<sign1>{_SIGN}\s*)?(?:US\s*)?(?:\$|USD\s*)\s*(?P<sign2>{_SIGN}\s*)?(?P<number1>{_NUMBER})"
)
_AMOUNT_TRAILING = rf"(?P<sign3>{_SIGN}\s*)?(?P<number2>{_NUMBER})\s*USD\b"
_AMOUNT_ONLY_LINE = re.compile(
    rf"^\s*(?:{_AMOUNT_LEADING}|{_AMOUNT_TRAILING})\s*(?:USD)?\s*$", re.IGNORECASE
)
_AMOUNT_AFTER_LABEL = re.compile(
    # A dash separates only when a space follows; "-$5.00" keeps its sign. Nothing
    # number-like may follow the amount, so "$1,23" is unreadable rather than "$1".
    rf"^\s*(?:[:：]|[\-–]\s)?\s*(?:{_AMOUNT_LEADING}|{_AMOUNT_TRAILING})(?!\d|[.,]\d)", re.IGNORECASE
)
_LEADING_SYMBOLS = re.compile(r"^[\W_]+")
_LABEL_SEPARATORS = " :：-–"
# Action buttons sit between a card title and its amount ("Add to credit balance").
# A closed list of whole lines: the amount under anything else (a budget, a recharge
# threshold) is not the balance, and an unknown button only leaves the balance unread.
_ACTION_LINE = re.compile(
    r"(?:"
    r"(?:add|buy|purchase|top[\s-]?up|recharge)"
    r"(?:(?:\s+to)?(?:\s+(?:your|more|credit|credits|balance|funds)){1,3})?"
    r"|(?:modify|edit|manage|enable|disable|set\s?up|turn\s+(?:on|off))"
    r"(?:\s+your)?\s+(?:auto[\s-]?recharge|recharge|billing|credits?|balance|payment\s+methods?)"
    r"|(?:(?:API\s*)?크레딧|잔액|자동\s*충전|결제(?:\s*(?:수단|정보))?)?\s*"
    r"(?:충전|추가|구매|수정|관리|변경|설정)(?:하기)?"
    r")[\W_]*",
    re.IGNORECASE,
)
_ACTION_LINES_BEFORE_AMOUNT = 2

_LOGIN_PATHS = ("/login", "/log-in", "/signup", "/sign-up", "/auth")
_LOGIN_HOST_SUFFIXES = (
    "auth.openai.com",
    "auth0.openai.com",
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "appleid.apple.com",
)
_LOGIN_TEXT_MARKERS = (
    "sign up or login with an openai account",
    "continue with google",
    "continue with microsoft",
    "continue with apple",
    "continue with phone",
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies to continue",
    "challenge-platform",
    "challenge-error-text",
)


@dataclass(frozen=True, slots=True)
class ApiCreditReading:
    status: ApiCreditStatus
    balance: str = ""
    reason: str = ""

    @property
    def is_final(self) -> bool:
        """True when waiting longer on the same page cannot change the answer."""
        if self.status is ApiCreditStatus.UNAVAILABLE:
            return self.reason not in _TRANSIENT_REASONS
        return True

    def to_payload(self) -> dict[str, str]:
        payload = {"status": self.status.value}
        if self.balance:
            payload["balance"] = self.balance
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        cleaned = " ".join(raw.split())
        if cleaned:
            lines.append(cleaned)
    return lines


def _format_amount(match: re.Match[str]) -> str:
    number = match.group("number1") or match.group("number2") or ""
    try:
        float(number.replace(",", ""))
    except ValueError:
        return ""
    negative = bool(match.group("sign1") or match.group("sign2") or match.group("sign3"))
    return f"{'-' if negative else ''}${number}"


def normalize_api_credit_balance(value: Any) -> str:
    """Canonical ``$12.34`` text for one amount, or an empty string for anything else."""
    match = _AMOUNT_ONLY_LINE.match(" ".join(_text(value).split()))
    return _format_amount(match) if match is not None else ""


def normalize_api_credit_status(value: Any) -> str:
    text = _text(value).strip().lower()
    return text if text in API_CREDIT_STATUS_TEXT else ""


def format_api_credit_text(balance: Any, status: Any) -> str:
    """One display line: the balance, qualified when the latest read did not succeed.

    Empty means there is nothing to show yet (no balance and no failed attempt).
    """
    amount = normalize_api_credit_balance(balance)
    state = normalize_api_credit_status(status)
    if amount and state in ("", ApiCreditStatus.OK.value):
        return amount
    if amount:
        return f"{amount} (이전 값, {API_CREDIT_STATUS_TEXT[state]})"
    if state and state != ApiCreditStatus.OK.value:
        return API_CREDIT_STATUS_TEXT[state]
    return ""


def _split_label(line: str) -> tuple[str, str] | None:
    """The balance label a line starts with and the text after it."""
    stripped = _LEADING_SYMBOLS.sub("", line)
    for label in _BALANCE_LABELS:
        if stripped[: len(label)].casefold() == label:
            return label, stripped[len(label):]
    return None


def _is_action_line(line: str) -> bool:
    return _ACTION_LINE.fullmatch(_LEADING_SYMBOLS.sub("", line)) is not None


def extract_credit_balance(lines: list[str], *, allow_synonyms: bool = True) -> str:
    """The amount printed on the label line or right below it.

    A wrong balance misleads more than a missing one, so only a label at the start
    of a line counts, and the amount must sit on the same line or be an entire line
    that follows the label directly or after at most two action-button lines such as
    "Add to credit balance". Amounts elsewhere on the page (budgets, thresholds,
    prices) and amounts above the label never qualify. Synonym labels count only on
    a page without the primary "Credit balance" title.
    """
    labelled = [
        (index, split)
        for index, split in ((index, _split_label(line)) for index, line in enumerate(lines))
        if split is not None
    ]
    if not allow_synonyms or any(
        split[0] in _PRIMARY_BALANCE_LABELS for _index, split in labelled
    ):
        labelled = [item for item in labelled if item[1][0] in _PRIMARY_BALANCE_LABELS]
    for index, split in labelled:
        remainder = split[1]
        same_line = _AMOUNT_AFTER_LABEL.match(remainder)
        if same_line is not None:
            amount = _format_amount(same_line)
            if amount:
                return amount
        if remainder.strip(_LABEL_SEPARATORS):
            # The label starts a sentence or a longer text, not a heading.
            continue
        for offset in range(1, _ACTION_LINES_BEFORE_AMOUNT + 2):
            if index + offset >= len(lines):
                break
            candidate = lines[index + offset]
            following = _AMOUNT_ONLY_LINE.match(candidate)
            if following is not None:
                amount = _format_amount(following)
                if amount:
                    return amount
                break
            if not _is_action_line(candidate):
                break
    return ""


def _is_login_page(host: str, path: str) -> bool:
    if host.endswith(_LOGIN_HOST_SUFFIXES):
        return True
    if host == API_CREDIT_HOST:
        return any(path == item or path.startswith(item + "/") for item in _LOGIN_PATHS)
    return False


def parse_api_credit_probe(raw: Any) -> ApiCreditReading:
    """Classify what ``API_CREDIT_PAGE_PROBE_SCRIPT`` returned."""
    if not isinstance(raw, dict):
        return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="probe_invalid")
    parts = urlsplit(_text(raw.get("url")).strip())
    host = parts.netloc.lower().split(":", 1)[0]
    path = parts.path.lower().rstrip("/")
    lines = _lines(_text(raw.get("text")))
    login_page = _is_login_page(host, path)
    if host == API_CREDIT_HOST and not login_page:
        # Other platform pages may print other credit figures under a synonym.
        balance = extract_credit_balance(
            lines,
            allow_synonyms=path == _API_CREDIT_BILLING_PATH,
        )
        if balance:
            return ApiCreditReading(ApiCreditStatus.OK, balance=balance)
    if login_page:
        return ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_page")
    folded = " ".join([_text(raw.get("title")), *lines]).casefold()
    if any(marker in folded for marker in _CHALLENGE_MARKERS):
        return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="challenge")
    if host != API_CREDIT_HOST or path != _API_CREDIT_BILLING_PATH:
        if host == API_CREDIT_HOST and any(marker in folded for marker in _LOGIN_TEXT_MARKERS):
            return ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_text")
        return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="unexpected_page")
    if any(marker in folded for marker in _LOGIN_TEXT_MARKERS):
        return ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_text")
    if not lines:
        return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="loading")
    return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="balance_not_found")


def reading_from_payload(raw: Any) -> ApiCreditReading | None:
    """Rebuild a reading from the payload that crossed the worker boundary."""
    if not isinstance(raw, dict):
        return None
    status_text = normalize_api_credit_status(raw.get("status"))
    if not status_text:
        return None
    status = ApiCreditStatus(status_text)
    reason = " ".join(_text(raw.get("reason")).split())[:60]
    if status is not ApiCreditStatus.OK:
        return ApiCreditReading(status, reason=reason)
    balance = normalize_api_credit_balance(raw.get("balance"))
    if not balance:
        return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="balance_invalid")
    return ApiCreditReading(status, balance=balance, reason=reason)


def _mask_shape_line(line: str) -> str:
    return re.sub(r"\d", "#", re.sub(r"\S*@\S*", "<email>", line))[:48]


def describe_api_credit_shape(raw: Any) -> str:
    """Amount-free shape of a page that was reached but yielded no balance.

    Digits are masked and only a few lines around the label are kept, so a log line
    helps repair a changed page layout without recording what the account holds.
    Other hosts (company sign-in tenants among them) are never named, and path parts
    holding digits (project ids) are masked.
    """
    if not isinstance(raw, dict):
        return "probe=invalid"
    parts = urlsplit(_text(raw.get("url")).strip())
    if parts.netloc.lower().split(":", 1)[0] != API_CREDIT_HOST:
        return "host=other"
    path = "/".join(
        "#" if re.search(r"\d", part) else part
        for part in parts.path.lower().rstrip("/").split("/")
    )
    lines = _lines(_text(raw.get("text")))
    label_index = next((i for i, line in enumerate(lines) if _split_label(line)), -1)
    shape = (
        f"host=platform path={path or '/'} "
        f"lines={len(lines)} label_line={label_index}"
    )
    if label_index >= 0:
        window = lines[label_index: label_index + 3]
        shape += " around_label=" + " | ".join(_mask_shape_line(line) for line in window)
    return shape

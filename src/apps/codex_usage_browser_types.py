from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, TypeAlias


@unique
class BrowserState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    HEADLESS_READY = "headless_ready"
    HEADED_LOGIN = "headed_login"
    RECOVERING = "recovering"
    PROFILE_IN_USE = "profile_in_use"
    FAILED = "failed"


@unique
class BrowserErrorCode(StrEnum):
    PROFILE_IN_USE = "profile_in_use"
    BROWSER_CHANNEL_UNAVAILABLE = "browser_channel_unavailable"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    COLLECT_FAILED = "collect_failed"
    LOGIN_WINDOW_CLOSED = "login_window_closed"
    LOGIN_REQUIRED = "login_required"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"


class MetricBlockPayload(TypedDict):
    metric_key: str
    label_text: NotRequired[str]
    block_text: NotRequired[str]
    heading_text: NotRequired[str]
    value_candidates: NotRequired[list[str]]
    reset_candidates: NotRequired[list[str]]
    reset_at_candidates: NotRequired[list[str]]
    boundary_tag: NotRequired[str]
    boundary_role: NotRequired[str]


class UsageProbePayload(TypedDict):
    url: str
    title: NotRequired[str]
    mainText: NotRequired[str]
    profileName: NotRequired[str]
    accountId: NotRequired[str]
    planType: NotRequired[str]
    metricBlocks: list[MetricBlockPayload]


class PageProtocol(Protocol):
    @property
    def url(self) -> str: ...

    def reload(self, *, timeout: int, wait_until: str) -> None: ...
    def goto(self, url: str, *, timeout: int, wait_until: str) -> None: ...
    def evaluate(self, expression: str) -> str | UsageProbePayload: ...
    def is_closed(self) -> bool: ...
    def close(self) -> None: ...


class ContextProtocol(Protocol):
    @property
    def pages(self) -> Sequence[PageProtocol]: ...

    def new_page(self) -> PageProtocol: ...
    def close(self) -> None: ...


class ChromiumProtocol(Protocol):
    def launch_persistent_context(
        self,
        user_data_dir: str,
        *,
        channel: str,
        headless: bool,
        chromium_sandbox: bool,
        args: list[str],
        user_agent: str | None,
        timeout: float,
    ) -> ContextProtocol: ...


class PlaywrightProtocol(Protocol):
    @property
    def chromium(self) -> ChromiumProtocol: ...
    def stop(self) -> None: ...


if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Playwright
else:
    BrowserContext = ContextProtocol
    Page = PageProtocol
    Playwright = PlaywrightProtocol


PageLike: TypeAlias = PageProtocol | Page
ContextLike: TypeAlias = ContextProtocol | BrowserContext
PlaywrightLike: TypeAlias = PlaywrightProtocol | Playwright
PlaywrightStarter = Callable[[], PlaywrightLike]
LogSink = Callable[[str], None]
JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


def _string_list(value: JsonValue) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


def parse_usage_probe(value: JsonValue | UsageProbePayload) -> UsageProbePayload | None:
    if not isinstance(value, dict):
        return None
    raw_blocks = value.get("metricBlocks")
    if not isinstance(raw_blocks, list):
        return None
    blocks: list[MetricBlockPayload] = []
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        metric_key = raw_block.get("metric_key")
        if not isinstance(metric_key, str):
            continue
        block: MetricBlockPayload = {"metric_key": metric_key}
        for key in ("label_text", "block_text", "heading_text", "boundary_tag", "boundary_role"):
            field = raw_block.get(key)
            if isinstance(field, str):
                block[key] = field
        for key in ("value_candidates", "reset_candidates", "reset_at_candidates"):
            candidates = _string_list(raw_block.get(key))
            if candidates is not None:
                block[key] = candidates
        blocks.append(block)
    raw_url = value.get("url")
    probe: UsageProbePayload = {
        "url": raw_url if isinstance(raw_url, str) else "",
        "metricBlocks": blocks,
    }
    for key in ("title", "mainText", "profileName", "accountId", "planType"):
        field = value.get(key)
        if isinstance(field, str):
            probe[key] = field
    return probe


@dataclass(frozen=True, slots=True)
class PlaywrightSessionConfig:
    profile_dir: str
    usage_url: str
    probe_script: str
    navigation_timeout_ms: int = 30_000
    command_timeout_sec: float = 45.0


@dataclass(frozen=True, slots=True)
class BrowserOperationResult:
    probe: UsageProbePayload | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserRuntimeStatus:
    state: BrowserState
    login_window_open: bool
    last_error: str


@dataclass(frozen=True, slots=True)
class CollectCommand:
    pass


@dataclass(frozen=True, slots=True)
class OpenLoginCommand:
    pass


@dataclass(frozen=True, slots=True)
class PollLoginCommand:
    pass


@dataclass(frozen=True, slots=True)
class CloseSessionCommand:
    pass


@dataclass(frozen=True, slots=True)
class ShutdownCommand:
    pass


BrowserCommand: TypeAlias = (
    CollectCommand
    | OpenLoginCommand
    | PollLoginCommand
    | CloseSessionCommand
    | ShutdownCommand
)

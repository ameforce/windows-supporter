from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import traceback
import urllib.request
from datetime import timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

try:
    import websocket
except Exception:  # pragma: no cover - optional runtime bridge
    websocket = None

from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip


USAGE_METRIC_KEYS = (
    "five_hour_limit",
    "weekly_limit",
    "code_review",
    "remaining_credit",
)

USAGE_METRIC_LABELS: dict[str, str] = {
    "five_hour_limit": "5시간 사용 한도",
    "weekly_limit": "주간 사용 한도",
    "code_review": "코드 검토",
    "remaining_credit": "남은 크레딧",
}

CURRENT_CODEX_USAGE_URL = "https://chatgpt.com/codex/cloud/settings/usage"
CODEX_USAGE_PAGE_PATHS = (
    "/codex/settings/usage",
    "/codex/cloud/settings/usage",
)
RAW_CDP_COMMAND_TIMEOUT_SEC = 8.0


class _FallbackWebSocketClient:
    _ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(
        self,
        url: str,
        timeout: float | None = None,
        suppress_origin: bool = True,
    ) -> None:
        _ = suppress_origin
        self._url = str(url or "")
        self._timeout = float(timeout or RAW_CDP_COMMAND_TIMEOUT_SEC)
        if self._timeout <= 0.0:
            self._timeout = float(RAW_CDP_COMMAND_TIMEOUT_SEC)
        self._sock: socket.socket | None = None
        self._recv_buffer = bytearray()
        self._connect()

    def _connect(self) -> None:
        parsed = urlsplit(self._url)
        if str(parsed.scheme or "").lower() != "ws":
            raise ValueError("only ws:// raw CDP websocket endpoints are supported")
        host = str(parsed.hostname or "").strip()
        if not host:
            raise ValueError("raw CDP websocket host is required")
        port = int(parsed.port or 80)
        path = str(parsed.path or "/")
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        sock = socket.create_connection((host, port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        sock.sendall(request)
        response = self._recv_http_headers(sock)
        header_blob, _, tail = response.partition(b"\r\n\r\n")
        status_line, _, header_lines = header_blob.partition(b"\r\n")
        if b" 101 " not in status_line:
            sock.close()
            raise ConnectionError(
                f"raw CDP websocket upgrade failed: {status_line.decode('utf-8', 'replace')}"
            )
        headers: dict[str, str] = {}
        for raw_line in header_lines.split(b"\r\n"):
            if b":" not in raw_line:
                continue
            key_bytes, value_bytes = raw_line.split(b":", 1)
            headers[key_bytes.decode("utf-8", "replace").strip().lower()] = (
                value_bytes.decode("utf-8", "replace").strip()
            )
        expected_accept = base64.b64encode(
            hashlib.sha1(f"{key}{self._ACCEPT_GUID}".encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept", "") != expected_accept:
            sock.close()
            raise ConnectionError("raw CDP websocket handshake validation failed")
        self._sock = sock
        if tail:
            self._recv_buffer.extend(tail)

    def _recv_http_headers(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def settimeout(self, timeout: float) -> None:
        self._timeout = float(timeout or RAW_CDP_COMMAND_TIMEOUT_SEC)
        if self._timeout <= 0.0:
            self._timeout = float(RAW_CDP_COMMAND_TIMEOUT_SEC)
        if self._sock is not None:
            self._sock.settimeout(self._timeout)

    def _recv_exact(self, count: int) -> bytes:
        while len(self._recv_buffer) < int(count):
            if self._sock is None:
                raise EOFError("raw CDP websocket is closed")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise EOFError("raw CDP websocket closed while receiving frame")
            self._recv_buffer.extend(chunk)
        data = bytes(self._recv_buffer[:count])
        del self._recv_buffer[:count]
        return data

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        if self._sock is None:
            raise EOFError("raw CDP websocket is closed")
        payload_bytes = payload or b""
        header = bytearray()
        header.append(0x80 | (int(opcode) & 0x0F))
        payload_len = len(payload_bytes)
        if payload_len < 126:
            header.append(0x80 | payload_len)
        elif payload_len < (1 << 16):
            header.append(0x80 | 126)
            header.extend(payload_len.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(payload_len.to_bytes(8, "big"))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(
            value ^ mask[idx % 4] for idx, value in enumerate(payload_bytes)
        )
        self._sock.sendall(bytes(header) + masked)

    def send(self, payload: str) -> None:
        self._send_frame(str(payload or "").encode("utf-8"), opcode=0x1)

    def recv(self) -> str:
        chunks: list[bytes] = []
        message_opcode = 0
        while True:
            first = self._recv_exact(2)
            fin = bool(first[0] & 0x80)
            opcode = int(first[0] & 0x0F)
            masked = bool(first[1] & 0x80)
            payload_len = int(first[1] & 0x7F)
            if payload_len == 126:
                payload_len = int.from_bytes(self._recv_exact(2), "big")
            elif payload_len == 127:
                payload_len = int.from_bytes(self._recv_exact(8), "big")
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(payload_len) if payload_len > 0 else b""
            if masked and mask:
                payload = bytes(
                    value ^ mask[idx % 4] for idx, value in enumerate(payload)
                )
            if opcode == 0x8:
                self.close()
                raise EOFError("raw CDP websocket received close frame")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            if opcode != 0x0:
                message_opcode = opcode
            chunks.append(payload)
            if not fin:
                continue
            message = b"".join(chunks)
            if message_opcode == 0x2:
                return message.decode("utf-8", "replace")
            return message.decode("utf-8", "replace")

    def close(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        self._sock = None
        try:
            sock.close()
        except Exception:
            pass


def _create_raw_websocket_connection(url: str, timeout: float) -> Any:
    if websocket is not None:
        return websocket.create_connection(
            str(url),
            timeout=float(timeout),
            suppress_origin=True,
        )
    return _FallbackWebSocketClient(
        str(url),
        timeout=float(timeout),
        suppress_origin=True,
    )

USAGE_PAGE_PROBE_SCRIPT = r"""
() => {
  const normalize = (value) =>
    String(value || '')
      .replace(/\r/g, '\n')
      .split('\n')
      .map((line) => line.trim().replace(/\s+/g, ' '))
      .filter(Boolean)
      .join(' ')
      .trim();
  const normalizeToken = (value) =>
    normalize(value).toLowerCase().replace(/[\s:：\-_|\t]/g, '');
  const valuePattern = /(\d+(?:\.\d+)?\s*\/\s*\d+(?:\.\d+)?)|(\d+(?:\.\d+)?\s*%)/;
  const headingTags = new Set(['H1', 'H2', 'H3', 'H4', 'H5', 'H6']);
  const aliases = {
    five_hour_limit: ['5시간 사용 한도', '5시간한도', '5-hour usage limit', '5 hour usage limit', '5h usage limit'],
    weekly_limit: ['주간 사용 한도', '주간한도', 'weekly usage limit', 'weekly limit'],
    code_review: ['코드 검토', '코드리뷰', 'code review', 'reviews'],
    remaining_credit: ['남은 크레딧', '잔여 크레딧', 'remaining credit', 'credits remaining'],
  };
  const scope = document.querySelector('main') || document.body;
  if (!scope) {
    return { url: location.href, title: document.title, mainText: '', metricBlocks: [] };
  }
  const metricKeys = Object.keys(aliases);
  const getMetricKey = (text) => {
    const token = normalizeToken(text);
    if (!token) return '';
    for (const key of metricKeys) {
      const candidates = aliases[key] || [];
      for (const alias of candidates) {
        if (normalizeToken(alias) && token.includes(normalizeToken(alias))) {
          return key;
        }
      }
    }
    return '';
  };
  const collectValueCandidates = (boundary, labelText) => {
    const values = [];
    const seen = new Set();
    const nodes = [boundary, ...Array.from(boundary.querySelectorAll('*'))];
    for (const node of nodes) {
      const text = normalize(node.innerText || node.textContent || '');
      if (!text || text === normalize(labelText) || text.length > 80) continue;
      if (!/[0-9]/.test(text)) continue;
      if (!seen.has(text)) {
        seen.add(text);
        values.push(text);
      }
    }
    return values;
  };
  const findBoundary = (labelEl) => {
    let boundary = labelEl;
    let current = labelEl;
    while (current && current !== scope) {
      const text = normalize(current.innerText || current.textContent || '');
      if (text && text.length <= 260 && valuePattern.test(text)) {
        const labelsInside = Array.from(current.querySelectorAll('*'))
          .map((el) => getMetricKey(el.innerText || el.textContent || ''))
          .filter(Boolean);
        if (labelsInside.length <= 2) {
          boundary = current;
          break;
        }
      }
      current = current.parentElement;
    }
    return boundary;
  };
  const findHeading = (boundary) => {
    let current = boundary;
    while (current && current !== scope) {
      const heading = Array.from(current.children || []).find((child) => headingTags.has(child.tagName));
      if (heading) return normalize(heading.innerText || heading.textContent || '');
      current = current.parentElement;
    }
    return '';
  };
  const metricBlocks = [];
  const seen = new Set();
  const elements = [scope, ...Array.from(scope.querySelectorAll('*'))];
  for (const element of elements) {
    const text = normalize(element.innerText || element.textContent || '');
    if (!text || text.length > 120) continue;
    const metricKey = getMetricKey(text);
    if (!metricKey) continue;
    const boundary = findBoundary(element);
    const blockText = normalize(boundary.innerText || boundary.textContent || '');
    if (!blockText) continue;
    const dedupeKey = `${metricKey}::${blockText}`;
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    metricBlocks.push({
      metric_key: metricKey,
      label_text: text,
      block_text: blockText,
      heading_text: findHeading(boundary),
      value_candidates: collectValueCandidates(boundary, text),
      boundary_tag: boundary.tagName || '',
      boundary_role: boundary.getAttribute ? (boundary.getAttribute('role') || '') : '',
    });
  }
  return {
    url: location.href,
    title: document.title,
    mainText: normalize(scope.innerText || scope.textContent || ''),
    metricBlocks,
  };
}
"""

USAGE_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "five_hour_limit": (
        "5시간 사용 한도",
        "5시간한도",
        "5-hour usage limit",
        "5 hour usage limit",
        "5h usage limit",
    ),
    "weekly_limit": (
        "주간 사용 한도",
        "주간한도",
        "weekly usage limit",
        "weekly limit",
    ),
    "code_review": (
        "코드 검토",
        "코드리뷰",
        "code review",
        "reviews",
    ),
    "remaining_credit": (
        "남은 크레딧",
        "잔여 크레딧",
        "remaining credit",
        "credits remaining",
    ),
}

def normalize_usage_value(value: str) -> str:
    text = str(value or "").replace("\r", "\n")
    parts: list[str] = []
    for line in text.split("\n"):
        cleaned = " ".join(str(line).strip().split())
        if cleaned:
            parts.append(cleaned)
    return " ".join(parts).strip()


def _normalize_match_token(text: str) -> str:
    raw = normalize_usage_value(text).lower()
    for token in (" ", ":", "：", "-", "_", "|", "\t"):
        raw = raw.replace(token, "")
    return raw


def _find_alias_in_line(line: str, aliases: tuple[str, ...]) -> tuple[str | None, int]:
    line_text = str(line or "")
    line_match = _normalize_match_token(line_text)
    if not line_match:
        return None, -1

    for alias in sorted(aliases, key=len, reverse=True):
        alias_text = str(alias or "").strip()
        if not alias_text:
            continue
        alias_match = _normalize_match_token(alias_text)
        if not alias_match:
            continue
        if alias_match in line_match:
            try:
                idx = line_text.lower().find(alias_text.lower())
            except Exception:
                idx = line_text.find(alias_text)
            return alias_text, idx
    return None, -1


def _line_contains_any_usage_label(line: str) -> bool:
    normalized = _normalize_match_token(line)
    if not normalized:
        return False
    for aliases in USAGE_METRIC_ALIASES.values():
        for alias in aliases:
            alias_token = _normalize_match_token(alias)
            if alias_token and alias_token in normalized:
                return True
    return False


def _normalize_metric_candidate(key: str, value: str) -> str:
    text = normalize_usage_value(value)
    if not text:
        return ""
    try:
        import re
    except Exception:
        return ""

    if key in {"five_hour_limit", "weekly_limit", "code_review"}:
        ratio = re.search(r"(\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?)", text)
        if ratio:
            return normalize_usage_value(ratio.group(1))
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if percent:
            return f"{percent.group(1)}%"
        return ""

    if key == "remaining_credit":
        if "%" in text or "/" in text:
            return ""
        number = re.search(r"\d[\d,]*", text)
        if not number:
            return ""
        return number.group(0).replace(",", "")

    return text


def parse_usage_metrics_from_text(raw_text: str) -> dict[str, str]:
    text = str(raw_text or "")
    if not text.strip():
        return {}

    lines: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        norm = normalize_usage_value(line)
        if norm:
            lines.append(norm)

    if not lines:
        return {}

    parsed: dict[str, str] = {}

    for idx, line in enumerate(lines):
        for key in USAGE_METRIC_KEYS:
            if key in parsed:
                continue
            aliases = USAGE_METRIC_ALIASES.get(key, ())
            alias, start_idx = _find_alias_in_line(line, aliases)
            if alias is None:
                continue

            value = ""
            if start_idx >= 0:
                cut = start_idx + len(alias)
                inline_candidate = line[cut:].strip(" :：-|")
                value = _normalize_metric_candidate(key, inline_candidate)
            if not value:
                j = idx + 1
                while j < len(lines):
                    candidate = normalize_usage_value(lines[j])
                    if not candidate:
                        j += 1
                        continue
                    if _line_contains_any_usage_label(candidate):
                        break
                    candidate_value = _normalize_metric_candidate(key, candidate)
                    if candidate_value:
                        value = candidate_value
                        break
                    j += 1
            value = _normalize_metric_candidate(key, value)
            if value:
                parsed[key] = value

    # Fallback: robust colon parsing over the full flattened text.
    if len(parsed) < len(USAGE_METRIC_KEYS):
        merged = "\n".join(lines)
        try:
            import re

            for key in USAGE_METRIC_KEYS:
                if key in parsed:
                    continue
                aliases = USAGE_METRIC_ALIASES.get(key, ())
                for alias in aliases:
                    pat = re.compile(
                        rf"{re.escape(str(alias))}\s*[:：-]\s*([^\n]+)",
                        re.IGNORECASE,
                    )
                    m = pat.search(merged)
                    if not m:
                        continue
                    value = _normalize_metric_candidate(key, m.group(1))
                    if value:
                        parsed[key] = value
                        break
        except Exception:
            pass

    return parsed


def canonicalize_codex_usage_url(value: str) -> str:
    text = normalize_usage_value(value)
    if not text:
        return CURRENT_CODEX_USAGE_URL
    try:
        parsed = urlsplit(text)
    except Exception:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    path = str(parsed.path or "").rstrip("/")
    if path == "/codex/settings/usage":
        path = "/codex/cloud/settings/usage"
    elif path == "":
        path = str(parsed.path or "")
    if not path:
        path = "/codex/cloud/settings/usage"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def build_codex_login_entry_url(usage_url: str) -> str:
    normalized = canonicalize_codex_usage_url(usage_url)
    try:
        parsed = urlsplit(normalized)
    except Exception:
        return "https://chatgpt.com/auth/login?next=/codex/cloud/settings/usage"
    path = str(parsed.path or "").rstrip("/")
    if not path:
        path = "/codex/cloud/settings/usage"
    next_target = path
    query = str(parsed.query or "").strip()
    if query:
        next_target = f"{next_target}?{query}"
    return f"https://chatgpt.com/auth/login?next={quote(next_target, safe='/?=&')}"


def is_codex_usage_url(value: str) -> bool:
    text = normalize_usage_value(value)
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except Exception:
        return False
    if str(parsed.netloc or "").lower() != "chatgpt.com":
        return False
    path = str(parsed.path or "").rstrip("/")
    return path in CODEX_USAGE_PAGE_PATHS


def are_equivalent_codex_usage_urls(left: str, right: str) -> bool:
    left_text = normalize_usage_value(left)
    right_text = normalize_usage_value(right)
    if not left_text or not right_text:
        return left_text == right_text
    if is_codex_usage_url(left_text) and is_codex_usage_url(right_text):
        return canonicalize_codex_usage_url(left_text) == canonicalize_codex_usage_url(right_text)
    return left_text == right_text


def _find_metric_key_for_label(text: str) -> str | None:
    line = normalize_usage_value(text)
    if not line:
        return None
    for key, aliases in USAGE_METRIC_ALIASES.items():
        alias, _ = _find_alias_in_line(line, aliases)
        if alias is not None:
            return key
    return None


def _normalize_value_candidates(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        values = raw_value
    elif raw_value is None:
        values = []
    else:
        values = [raw_value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = normalize_usage_value(str(item or ""))
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def extract_usage_metrics_from_semantic_blocks(raw_blocks: Any) -> dict[str, str]:
    if not isinstance(raw_blocks, list):
        return {}
    parsed: dict[str, str] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        key = normalize_usage_value(raw_block.get("metric_key", ""))
        if not key:
            key = str(_find_metric_key_for_label(raw_block.get("label_text", "")) or "")
        if key not in USAGE_METRIC_KEYS:
            continue
        if key in parsed:
            continue
        candidates = _normalize_value_candidates(raw_block.get("value_candidates", []))
        if not candidates:
            block_text = normalize_usage_value(raw_block.get("block_text", ""))
            if block_text:
                candidates = [block_text]
        value = ""
        for candidate in candidates:
            value = _normalize_metric_candidate(key, candidate)
            if value:
                break
        if value:
            parsed[key] = value
    return parsed


@dataclass
class UsageSnapshot:
    five_hour_limit: str = ""
    weekly_limit: str = ""
    code_review: str = ""
    remaining_credit: str = ""
    captured_at: str = ""

    @classmethod
    def from_metrics(
        cls,
        metrics: dict[str, str] | None,
        captured_at: str = "",
    ) -> "UsageSnapshot":
        data = metrics or {}
        return cls(
            five_hour_limit=normalize_usage_value(data.get("five_hour_limit", "")),
            weekly_limit=normalize_usage_value(data.get("weekly_limit", "")),
            code_review=normalize_usage_value(data.get("code_review", "")),
            remaining_credit=normalize_usage_value(data.get("remaining_credit", "")),
            captured_at=normalize_usage_value(captured_at),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UsageSnapshot":
        if not isinstance(data, dict):
            return cls()
        return cls(
            five_hour_limit=normalize_usage_value(data.get("five_hour_limit", "")),
            weekly_limit=normalize_usage_value(data.get("weekly_limit", "")),
            code_review=normalize_usage_value(data.get("code_review", "")),
            remaining_credit=normalize_usage_value(data.get("remaining_credit", "")),
            captured_at=normalize_usage_value(data.get("captured_at", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "five_hour_limit": normalize_usage_value(self.five_hour_limit),
            "weekly_limit": normalize_usage_value(self.weekly_limit),
            "code_review": normalize_usage_value(self.code_review),
            "remaining_credit": normalize_usage_value(self.remaining_credit),
            "captured_at": normalize_usage_value(self.captured_at),
        }

    def metrics(self) -> dict[str, str]:
        payload = self.to_dict()
        payload.pop("captured_at", None)
        return payload

    def has_any_metric(self) -> bool:
        return any(bool(v) for v in self.metrics().values())


@dataclass
class UsageChange:
    key: str
    label: str
    before: str
    after: str


def merge_snapshot_with_previous(
    current: UsageSnapshot,
    previous: UsageSnapshot | None,
) -> UsageSnapshot:
    prev = previous if isinstance(previous, UsageSnapshot) else None
    if prev is None:
        return current
    merged = current.to_dict()
    prev_payload = prev.to_dict()
    for key in USAGE_METRIC_KEYS:
        if not merged.get(key):
            merged[key] = prev_payload.get(key, "")
    if not merged.get("captured_at"):
        merged["captured_at"] = prev_payload.get("captured_at", "")
    return UsageSnapshot.from_dict(merged)


def compute_usage_changes(
    previous: UsageSnapshot | None,
    current: UsageSnapshot,
) -> list[UsageChange]:
    if previous is None or not previous.has_any_metric():
        return []
    changes: list[UsageChange] = []
    prev_payload = previous.to_dict()
    curr_payload = current.to_dict()
    for key in USAGE_METRIC_KEYS:
        before = normalize_usage_value(prev_payload.get(key, ""))
        after = normalize_usage_value(curr_payload.get(key, ""))
        if before == after:
            continue
        if not after:
            # Missing parse is treated conservatively as no change.
            continue
        changes.append(
            UsageChange(
                key=key,
                label=USAGE_METRIC_LABELS.get(key, key),
                before=before,
                after=after,
            )
        )
    return changes


class CodexUsageMonitor:
    def __init__(
        self,
        config_dir: str | None = None,
        profile_dir: str | None = None,
    ) -> None:
        self.__lib = LibConnector()
        self.__root = None
        self.__event_queue = None

        self.__monitor_after_id = None
        self.__monitor_running = False
        self.__startup_warmup_running = False
        self.__worker_epoch = 0
        self.__active_tooltip = None
        self.__failure_count = 0
        self.__collect_inflight = False
        self.__collect_inflight_source = ""
        self.__collect_started_ts = 0.0
        self.__next_collect_due_ts = 0.0
        self.__manual_query_waiting_result = False
        self.__manual_query_state_lock = threading.Lock()
        self.__monitor_state = "idle"
        self.__session_state = "logged_in"
        self.__logout_in_progress = False
        self.__collect_cancel_event = threading.Event()
        self.__release_wait_timeout_sec = 8.0
        self.__release_poll_interval_sec = 0.1
        self.__playwright_checked = False
        self.__playwright_available = False
        self.__collection_mode = "playwright"
        self.__playwright_launch_retry_count = 2
        self.__last_login_notice_ts = 0.0
        self.__login_notice_cooldown_sec = 600.0
        self.__last_playwright_notice_ts = 0.0
        self.__playwright_notice_cooldown_sec = 1800.0
        self.__last_profile_in_use_notice_ts = 0.0
        self.__profile_in_use_notice_cooldown_sec = 600.0
        self.__profile_in_use_detected = False
        self.__last_interactive_login_ts = 0.0
        self.__interactive_login_cooldown_sec = 600.0
        self.__manual_interactive_reopen_cooldown_sec = 3.0
        self.__collect_lock = threading.Lock()
        self.__hidden_cdp_proc = None
        self.__hidden_cdp_port = 0
        self.__pending_hidden_cdp_clear = False
        self.__last_successful_cdp_port = 0
        self.__cdp_port_attempt_limit = 6
        self.__cdp_total_launch_timeout_sec = 28.0
        self.__cdp_connect_timeout_ms = 3000
        self.__api_failure_count = 0
        self.__api_failover_threshold = 2
        self.__api_request_timeout_sec = 12.0

        self.__settings_version = 1
        self.__enabled = True
        self.__interval_sec = 90.0
        self.__min_interval_sec = 10.0
        self.__tooltip_duration_ms = 7000
        self.__usage_url = CURRENT_CODEX_USAGE_URL
        self.__login_entry_url = build_codex_login_entry_url(self.__usage_url)
        self.__navigation_timeout_ms = 30000
        self.__login_timeout_sec = 180.0
        self.__headless_wait_timeout_sec = 10.0
        self.__background_cloudflare_grace_sec = 6.0
        self.__korea_tz = timezone(timedelta(hours=9), name="KST")

        self.__last_snapshot = UsageSnapshot()

        base_dir = self.__lib.os.getenv("APPDATA")
        if not base_dir:
            base_dir = self.__lib.os.getenv("LOCALAPPDATA")
        if not base_dir:
            base_dir = self.__lib.os.path.expanduser("~")
        local_base = self.__lib.os.getenv("LOCALAPPDATA") or base_dir

        normalized_config_dir = str(config_dir or "").strip()
        if normalized_config_dir:
            self.__config_dir = normalized_config_dir
        else:
            self.__config_dir = self.__lib.os.path.join(base_dir, "windows-supporter")
        self.__settings_path = self.__lib.os.path.join(
            self.__config_dir,
            "codex_usage_settings.json",
        )
        self.__state_path = self.__lib.os.path.join(
            self.__config_dir,
            "codex_usage_state.json",
        )
        self.__log_path = self.__lib.os.path.join(self.__config_dir, "codex_usage.log")
        normalized_profile_dir = str(profile_dir or "").strip()
        if normalized_profile_dir:
            self.__profile_dir = normalized_profile_dir
        else:
            self.__profile_dir = self.__lib.os.path.join(
                local_base,
                "windows-supporter",
                "chatgpt-profile",
            )

        self.__load_settings()
        self.__load_state()
        self.__refresh_session_state_from_profile()
        return

    def attach(self, root, event_queue=None) -> None:
        self.__root = root
        self.__event_queue = event_queue
        self.__refresh_session_state_from_profile()
        self.__restart_monitor()
        return

    def __set_usage_url(self, value: str) -> None:
        self.__usage_url = canonicalize_codex_usage_url(value)
        self.__login_entry_url = build_codex_login_entry_url(self.__usage_url)
        return

    def get_settings_snapshot(self) -> dict[str, Any]:
        self.__force_playwright_mode()
        return {
            "enabled": bool(self.__enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "collection_mode": str(self.__collection_mode or "playwright"),
            "settings_path": str(self.__settings_path),
            "state_path": str(self.__state_path),
            "profile_dir": str(self.__profile_dir),
        }

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        enabled = bool(data.get("enabled", self.__enabled))
        usage_url = normalize_usage_value(data.get("usage_url", self.__usage_url))
        if not usage_url:
            usage_url = self.__usage_url
        try:
            interval_sec = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            return False, "interval"
        try:
            tooltip_ms = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            return False, "tooltip"
        min_interval = float(getattr(self, "_CodexUsageMonitor__min_interval_sec", 10.0) or 10.0)
        if interval_sec < min_interval:
            interval_sec = min_interval
        if tooltip_ms < 1200:
            tooltip_ms = 1200
        self.__enabled = enabled
        self.__set_usage_url(usage_url)
        self.__interval_sec = float(interval_sec)
        self.__tooltip_duration_ms = int(tooltip_ms)
        self.__force_playwright_mode()
        self.__refresh_session_state_from_profile()
        self.__save_settings()
        self.__restart_monitor()
        return True, None

    def release_profile_session(self) -> tuple[bool, str]:
        acquired = False
        self.__logout_in_progress = True
        self.__set_monitor_state("cancelling")
        self.__request_collect_cancel()
        self.__pause_background_monitor()
        try:
            self.__worker_epoch = int(self.__worker_epoch) + 1
        except Exception:
            self.__worker_epoch = 1
        wait_timeout = float(self.__release_wait_timeout_sec)
        if wait_timeout < 0.2:
            wait_timeout = 0.2
        poll_interval = float(self.__release_poll_interval_sec)
        if poll_interval <= 0.0:
            poll_interval = 0.05
        start_ts = 0.0
        try:
            start_ts = float(self.__lib.time.monotonic())
        except Exception:
            start_ts = 0.0

        while True:
            acquired = self.__acquire_collect_lock_non_blocking()
            if acquired:
                break
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = start_ts + wait_timeout + 1.0
            if (now - start_ts) >= wait_timeout:
                self.__set_monitor_state("idle")
                self.__logout_in_progress = False
                self.__clear_collect_cancel()
                return (
                    False,
                    "진행 중인 조회를 중단하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                )
            try:
                self.__lib.time.sleep(poll_interval)
            except Exception:
                pass

        try:
            self.__pending_hidden_cdp_clear = False
            self.__clear_hidden_cdp_process(terminate=True)
            self.__terminate_profile_remote_debugging_processes()
            self.__terminate_profile_chrome_processes()
            ok, message = self.__clear_profile_directory()
            if not ok:
                return False, message
            self.__last_snapshot = UsageSnapshot()
            self.__save_state()
            self.__playwright_checked = False
            self.__playwright_available = False
            self.__failure_count = 0
            self.__manual_query_waiting_result = False
            self.__set_session_state("logged_out")
            self.__pause_background_monitor()
            return (
                True,
                message or "로그아웃되었습니다. 다시 사용하려면 로그인 후 조회해 주세요.",
            )
        except Exception as exc:
            self.__log_exception("release profile session failed", exc)
            return False, "로그아웃 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        finally:
            if acquired:
                try:
                    self.__collect_lock.release()
                except Exception:
                    pass
            self.__logout_in_progress = False
            self.__set_monitor_state("idle")
            self.__clear_collect_cancel()

    def __clear_profile_directory(self) -> tuple[bool, str]:
        profile_dir = str(self.__profile_dir or "").strip()
        if not profile_dir:
            return False, "로그인 세션 경로를 확인하지 못했습니다."
        try:
            if not self.__lib.os.path.isdir(profile_dir):
                return True, "이미 로그아웃된 상태입니다."
        except Exception:
            return False, "로그인 세션 경로 확인 중 오류가 발생했습니다."
        try:
            shutil.rmtree(profile_dir)
            return True, "로그아웃되었습니다."
        except Exception as exc:
            self.__log_exception("profile directory delete failed", exc)

        stamp = "0"
        try:
            stamp = str(int(float(self.__lib.time.time())))
        except Exception:
            stamp = "0"
        moved_path = f"{profile_dir}.released-{stamp}"
        try:
            if self.__lib.os.path.exists(moved_path):
                shutil.rmtree(moved_path, ignore_errors=True)
        except Exception:
            pass
        try:
            self.__lib.os.replace(profile_dir, moved_path)
            try:
                shutil.rmtree(moved_path, ignore_errors=True)
            except Exception:
                pass
            return True, "로그아웃되었습니다."
        except Exception as exc:
            self.__log_exception("profile directory rename failed", exc)
            return (
                False,
                "로그인 세션 폴더가 사용 중입니다. 관련 창을 닫고 다시 시도해 주세요.",
            )

    def __force_playwright_mode(self) -> None:
        self.__collection_mode = "playwright"
        return

    def __set_monitor_state(self, state: str) -> None:
        normalized = normalize_usage_value(state).lower()
        if normalized not in {"idle", "running", "cancelling"}:
            normalized = "idle"
        self.__monitor_state = normalized
        return

    def __set_session_state(self, state: str) -> None:
        normalized = normalize_usage_value(state).lower()
        if normalized not in {"logged_in", "logged_out"}:
            normalized = "logged_out"
        self.__session_state = normalized
        return

    def __is_logged_in_session(self) -> bool:
        return str(self.__session_state) == "logged_in"

    def __has_profile_session(self) -> bool:
        profile_dir = str(self.__profile_dir or "").strip()
        if not profile_dir:
            return False
        try:
            if not self.__lib.os.path.isdir(profile_dir):
                return False
        except Exception:
            return False
        try:
            return bool(self.__lib.os.listdir(profile_dir))
        except Exception:
            return False

    def __refresh_session_state_from_profile(self) -> None:
        if self.__has_profile_session():
            self.__set_session_state("logged_in")
        else:
            self.__set_session_state("logged_out")
        return

    def __should_run_background_collection(self) -> bool:
        return bool(
            self.__enabled
            and self.__is_logged_in_session()
            and not bool(self.__logout_in_progress)
        )

    def __request_collect_cancel(self) -> None:
        try:
            self.__collect_cancel_event.set()
        except Exception:
            pass
        return

    def __clear_collect_cancel(self) -> None:
        try:
            self.__collect_cancel_event.clear()
        except Exception:
            pass
        return

    def __is_collect_cancel_requested(self) -> bool:
        if bool(self.__logout_in_progress):
            return True
        try:
            return bool(self.__collect_cancel_event.is_set())
        except Exception:
            return False

    def __acquire_collect_lock_non_blocking(self) -> bool:
        try:
            return bool(self.__collect_lock.acquire(blocking=False))
        except TypeError:
            try:
                return bool(self.__collect_lock.acquire(False))
            except Exception:
                return False
        except Exception:
            return False

    def __pause_background_monitor(self) -> None:
        root = self.__root
        if root is not None:
            try:
                if self.__monitor_after_id is not None:
                    root.after_cancel(self.__monitor_after_id)
            except Exception:
                pass
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        self.__monitor_running = False
        self.__startup_warmup_running = False
        self.__set_monitor_state("idle")
        return

    def __clear_monitor_schedule(self) -> None:
        root = self.__root
        if root is not None:
            try:
                if self.__monitor_after_id is not None:
                    root.after_cancel(self.__monitor_after_id)
            except Exception:
                pass
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        return

    def __pause_monitor_countdown_for_manual_query(self) -> None:
        self.__clear_monitor_schedule()
        return

    def __reset_monitor_countdown_after_manual_query(self) -> None:
        self.__clear_monitor_schedule()
        if not self.__should_run_background_collection():
            return
        if bool(self.__monitor_running or self.__startup_warmup_running):
            return
        self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
        return

    def __resume_background_monitor_if_needed(self) -> None:
        if not self.__should_run_background_collection():
            return
        if self.__monitor_after_id is not None:
            return
        if bool(self.__monitor_running or self.__startup_warmup_running):
            return
        self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
        return

    def get_last_snapshot(self) -> UsageSnapshot:
        return UsageSnapshot.from_dict(self.__last_snapshot.to_dict())

    def get_runtime_status(self) -> dict[str, Any]:
        self.__force_playwright_mode()
        now = 0.0
        try:
            now = float(self.__lib.time.monotonic())
        except Exception:
            now = 0.0
        remain: float | None = None
        estimated = False
        if (
            self.__should_run_background_collection()
            and not bool(self.__profile_in_use_detected)
            and not bool(self.__collect_inflight)
        ):
            due = float(self.__next_collect_due_ts or 0.0)
            if due > 0.0:
                remain = due - now
                if remain < 0.0:
                    remain = 0.0
        monitor_state = str(self.__monitor_state or "idle")
        if bool(self.__logout_in_progress):
            monitor_state = "cancelling"
        elif bool(self.__collect_inflight):
            monitor_state = "running"
        elif bool(self.__profile_in_use_detected):
            monitor_state = "paused_profile_in_use"
        can_login = bool(
            str(self.__session_state) == "logged_out"
            and not bool(self.__logout_in_progress)
            and not bool(self.__collect_inflight)
        )
        can_logout = bool(
            (str(self.__session_state) == "logged_in" or bool(self.__collect_inflight))
            and not bool(self.__logout_in_progress)
        )
        return {
            "enabled": bool(self.__enabled),
            "collect_inflight": bool(self.__collect_inflight),
            "collect_source": str(self.__collect_inflight_source or ""),
            "collection_mode": str(self.__collection_mode or "unknown"),
            "monitor_running": bool(self.__monitor_running),
            "startup_warmup_running": bool(self.__startup_warmup_running),
            "next_collect_in_sec": remain,
            "next_collect_estimated": bool(estimated),
            "failure_count": int(self.__failure_count),
            "api_failure_count": int(self.__api_failure_count),
            "session_state": str(self.__session_state or "logged_out"),
            "monitor_state": monitor_state,
            "logout_in_progress": bool(self.__logout_in_progress),
            "profile_in_use": bool(self.__profile_in_use_detected),
            "auto_monitoring_active": bool(self.__should_run_background_collection()),
            "can_login": can_login,
            "can_logout": can_logout,
        }

    def format_captured_at_for_display(self, value: str) -> str:
        return self.__format_timestamp_display(str(value or ""))

    def show_current_status(self, force_refresh: bool = True) -> None:
        root = self.__root
        if root is None:
            return
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            snapshot = None if bool(force_refresh) else self.get_last_snapshot()
            error = None
            try:
                if bool(self.__logout_in_progress):
                    self.__ui_post(
                        lambda: self.__show_tooltip(
                            "로그아웃 진행 중입니다. 완료 후 다시 시도해 주세요."
                        )
                    )
                    return
                if bool(force_refresh):
                    refreshed, error = self.__collect_snapshot_guarded(
                        source="manual_query",
                        on_acquired=lambda: self.__ui_post(
                            lambda: self.__show_tooltip(
                                "Codex 사용량 조회 중...",
                                duration_ms=0,
                            )
                        ),
                    )
                    if error == "collect_busy":
                        if bool(self.__profile_in_use_detected):
                            latest = self.get_last_snapshot()
                            if latest is not None and latest.has_any_metric():
                                self.__ui_post(
                                    lambda: self.__show_snapshot_tooltip(
                                        latest,
                                        title="Codex 최근 사용량 (자동 조회 일시중지)",
                                    )
                                )
                            else:
                                self.__ui_post(
                                    lambda: self.__show_tooltip(
                                        "다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다."
                                    )
                                )
                            return
                        self.__set_manual_query_pending_result()
                        self.__ui_post(self.__show_busy_collect_tooltip)
                        return
                    if error == "collect_cancelled":
                        self.__ui_post(lambda: self.__show_tooltip("조회가 취소되었습니다."))
                        return
                    self.__consume_manual_query_pending_result()
                    if error is not None and error != "profile_in_use":
                        self.__handle_collect_error(error, source="manual_query")
                    if refreshed is not None:
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        self.__set_session_state("logged_in")
                        merged = merge_snapshot_with_previous(
                            refreshed,
                            self.__last_snapshot if self.__last_snapshot.has_any_metric() else None,
                        )
                        self.__last_snapshot = merged
                        self.__save_state()
                        snapshot = merged
                        self.__profile_in_use_detected = False
                        self.__resume_background_monitor_if_needed()
                if error == "profile_in_use":
                    latest = self.get_last_snapshot()
                    if latest is not None and latest.has_any_metric():
                        self.__ui_post(
                            lambda: self.__show_snapshot_tooltip(
                                latest,
                                title="Codex 최근 사용량 (자동 조회 일시중지)",
                            )
                        )
                        return
                    self.__ui_post(
                        lambda: self.__show_tooltip(
                            "다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다."
                        )
                    )
                    return
                if snapshot is not None and snapshot.has_any_metric():
                    self.__ui_post(
                        lambda: self.__show_snapshot_tooltip(
                            snapshot,
                            title="Codex 현재 사용량",
                        )
                    )
                    return
                msg = (
                    "사용량 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."
                    if error is None
                    else f"사용량 조회 실패: {self.__describe_collect_error_for_user(error)}"
                )
                self.__ui_post(lambda: self.__show_tooltip(msg))
            except Exception as exc:
                self.__log_exception("manual status query failed", exc)
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        "사용량 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.__log_exception("manual status worker start failed", exc)
            self.__ui_post(
                lambda: self.__show_tooltip(
                    "사용량 조회 작업을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요."
                )
            )
        return

    def handle_snapshot(self, snapshot: UsageSnapshot) -> list[UsageChange]:
        prev = self.__last_snapshot if self.__last_snapshot.has_any_metric() else None
        merged = merge_snapshot_with_previous(snapshot, prev)
        if not merged.has_any_metric():
            return []
        self.__profile_in_use_detected = False
        changes = compute_usage_changes(prev, merged)
        self.__last_snapshot = merged
        self.__save_state()
        return changes

    def __restart_monitor(self) -> None:
        self.__pause_background_monitor()
        try:
            self.__worker_epoch = int(self.__worker_epoch) + 1
        except Exception:
            self.__worker_epoch = 1
        self.__clear_collect_cancel()
        if bool(self.__collect_inflight):
            self.__pending_hidden_cdp_clear = True
        else:
            self.__clear_hidden_cdp_process(terminate=True)
        if not self.__should_run_background_collection():
            return
        self.__start_startup_warmup()
        return

    def __start_startup_warmup(self) -> None:
        root = self.__root
        if root is None:
            return
        if not self.__should_run_background_collection():
            self.__set_monitor_state("idle")
            return
        if self.__startup_warmup_running:
            return
        self.__startup_warmup_running = True
        self.__monitor_running = True
        self.__set_monitor_state("running")
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            next_delay = float(self.__interval_sec)
            try:
                self.__log("startup warmup begin mode=headful-hidden-first")
                self.__profile_in_use_detected = False
                snapshot, error = self.__collect_snapshot_guarded(source="startup_warmup")
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("startup warmup stale result ignored")
                    return
                if error is not None:
                    if error == "collect_busy":
                        self.__log("startup warmup skipped reason=busy")
                        next_delay = min(self.__interval_sec, 5.0)
                        return
                    if error == "profile_in_use":
                        self.__log("startup warmup skipped reason=profile_in_use")
                        self.__profile_in_use_detected = True
                        next_delay = min(self.__interval_sec, 20.0)
                        self.__ui_post(
                            lambda snap=self.get_last_snapshot(): self.__show_pending_manual_result_if_needed(
                                snap if snap is not None and snap.has_any_metric() else None,
                                error="profile_in_use",
                            )
                        )
                        self.__handle_collect_error(error, source="startup_warmup")
                        return
                    if error == "collect_cancelled":
                        self.__log("startup warmup cancelled")
                        return
                    if error in {"parse_failed", "collect_failed"} and self.__has_manual_query_pending_result():
                        retry_snapshot, retry_error = self.__collect_snapshot_guarded(
                            source="startup_warmup_pending_retry"
                        )
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        if retry_error is None and retry_snapshot is not None:
                            error = None
                            snapshot = retry_snapshot
                        elif retry_error:
                            error = str(retry_error)
                    if error is None and snapshot is not None:
                        self.__failure_count = 0
                        changes = self.handle_snapshot(snapshot)
                        latest_snapshot = self.get_last_snapshot()
                        if changes:
                            self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                        self.__ui_post(
                            lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                                snap,
                                error=None,
                            )
                        )
                        self.__log("startup warmup end ok (pending retry)")
                        return
                    self.__ui_post(
                        lambda err=error: self.__show_pending_manual_result_if_needed(None, error=err)
                    )
                    self.__failure_count = min(self.__failure_count + 1, 8)
                    next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                    self.__handle_collect_error(error, source="startup_warmup")
                    self.__log(f"startup warmup end error={error}")
                    return
                self.__failure_count = 0
                if snapshot is not None:
                    changes = self.handle_snapshot(snapshot)
                    latest_snapshot = self.get_last_snapshot()
                    if changes:
                        self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                    self.__ui_post(
                        lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                            snap,
                            error=None,
                        )
                    )
                self.__log("startup warmup end ok")
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__failure_count = min(self.__failure_count + 1, 8)
                next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                self.__log_exception("startup warmup failed", exc)
            finally:
                self.__ui_post(
                    lambda: self.__on_worker_done(
                        next_delay,
                        worker_epoch=worker_epoch,
                        from_startup=True,
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            if worker_epoch == int(self.__worker_epoch):
                self.__startup_warmup_running = False
                self.__monitor_running = False
                self.__set_monitor_state("idle")
            self.__log_exception("startup warmup thread start failed", exc)
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 10.0))
        return

    def __schedule_monitor_tick(self, initial_delay_sec: float | None = None) -> None:
        if not self.__should_run_background_collection():
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
            return
        root = self.__root
        if root is None:
            return
        delay_sec = self.__interval_sec if initial_delay_sec is None else float(initial_delay_sec)
        if delay_sec < 1.0:
            delay_sec = 1.0
        delay_ms = int(delay_sec * 1000)
        try:
            self.__next_collect_due_ts = float(self.__lib.time.monotonic()) + float(delay_sec)
        except Exception:
            self.__next_collect_due_ts = 0.0
        try:
            self.__monitor_after_id = root.after(delay_ms, self.__monitor_tick)
        except Exception:
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
        return

    def __monitor_tick(self) -> None:
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        if not self.__should_run_background_collection():
            self.__set_monitor_state("idle")
            return
        if self.__monitor_running:
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 5.0))
            return
        self.__monitor_running = True
        self.__set_monitor_state("running")
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            next_delay = float(self.__interval_sec)
            try:
                self.__profile_in_use_detected = False
                snapshot, error = self.__collect_snapshot_guarded(source="monitor_tick")
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("monitor worker stale result ignored")
                    return
                if error is not None:
                    if error == "collect_busy":
                        self.__log("monitor tick skipped reason=busy")
                        next_delay = min(self.__interval_sec, 5.0)
                        return
                    if error == "profile_in_use":
                        self.__log("monitor tick skipped reason=profile_in_use")
                        self.__profile_in_use_detected = True
                        next_delay = min(self.__interval_sec, 20.0)
                        self.__ui_post(
                            lambda snap=self.get_last_snapshot(): self.__show_pending_manual_result_if_needed(
                                snap if snap is not None and snap.has_any_metric() else None,
                                error="profile_in_use",
                            )
                        )
                        self.__handle_collect_error(error, source="monitor_tick")
                        return
                    if error == "collect_cancelled":
                        self.__log("monitor tick cancelled")
                        return
                    if error in {"parse_failed", "collect_failed"} and self.__has_manual_query_pending_result():
                        retry_snapshot, retry_error = self.__collect_snapshot_guarded(
                            source="monitor_tick_pending_retry"
                        )
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        if retry_error is None and retry_snapshot is not None:
                            error = None
                            snapshot = retry_snapshot
                        elif retry_error:
                            error = str(retry_error)
                    if error is None and snapshot is not None:
                        self.__failure_count = 0
                        changes = self.handle_snapshot(snapshot)
                        latest_snapshot = self.get_last_snapshot()
                        if changes:
                            self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                        self.__ui_post(
                            lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                                snap,
                                error=None,
                            )
                        )
                        return
                    self.__ui_post(
                        lambda err=error: self.__show_pending_manual_result_if_needed(None, error=err)
                    )
                    self.__failure_count = min(self.__failure_count + 1, 8)
                    next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                    self.__handle_collect_error(error, source="monitor_tick")
                    return
                self.__failure_count = 0
                if snapshot is None:
                    return
                changes = self.handle_snapshot(snapshot)
                latest_snapshot = self.get_last_snapshot()
                if changes:
                    self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                self.__ui_post(
                    lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                        snap,
                        error=None,
                    )
                )
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__failure_count = min(self.__failure_count + 1, 8)
                next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                self.__log_exception("monitor worker failed", exc)
            finally:
                self.__ui_post(
                    lambda: self.__on_worker_done(
                        next_delay,
                        worker_epoch=worker_epoch,
                        from_startup=False,
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.__monitor_running = False
            self.__set_monitor_state("idle")
            self.__log_exception("monitor thread start failed", exc)
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 15.0))
        return

    def __on_worker_done(
        self,
        next_delay: float,
        worker_epoch: int | None = None,
        from_startup: bool = False,
    ) -> None:
        if not self.__is_worker_epoch_current(worker_epoch):
            return
        if from_startup:
            self.__startup_warmup_running = False
        self.__monitor_running = False
        self.__set_monitor_state("idle")
        if not self.__should_run_background_collection():
            self.__next_collect_due_ts = 0.0
            return
        self.__schedule_monitor_tick(initial_delay_sec=next_delay)
        return

    def __is_worker_epoch_current(self, worker_epoch: int | None) -> bool:
        if worker_epoch is None:
            return True
        try:
            return int(worker_epoch) == int(self.__worker_epoch)
        except Exception:
            return False

    def __collect_snapshot_guarded(
        self,
        source: str,
        on_acquired=None,
    ) -> tuple[UsageSnapshot | None, str | None]:
        source_key = normalize_usage_value(source).lower()
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        acquired = self.__acquire_collect_lock_non_blocking()
        if not acquired:
            self.__log(f"collect skip source={source} reason=busy")
            return None, "collect_busy"
        try:
            self.__collect_inflight = True
            self.__collect_inflight_source = str(source or "")
            self.__set_monitor_state("running")
            if source_key == "manual_query":
                self.__ui_post(self.__pause_monitor_countdown_for_manual_query)
            try:
                self.__collect_started_ts = float(self.__lib.time.monotonic())
            except Exception:
                self.__collect_started_ts = 0.0
            self.__log(f"collect start source={source}")
            if callable(on_acquired):
                try:
                    on_acquired()
                except Exception:
                    pass
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            snapshot, error = self.__collect_snapshot(source=str(source or ""))
            self.__log(f"collect end source={source} error={error or 'none'}")
            return snapshot, error
        finally:
            self.__collect_inflight = False
            self.__collect_inflight_source = ""
            self.__collect_started_ts = 0.0
            if not bool(self.__logout_in_progress):
                self.__set_monitor_state("idle")
            if source_key == "manual_query":
                self.__ui_post(self.__reset_monitor_countdown_after_manual_query)
            if bool(self.__pending_hidden_cdp_clear):
                self.__pending_hidden_cdp_clear = False
                self.__clear_hidden_cdp_process(terminate=True)
            try:
                self.__collect_lock.release()
            except Exception:
                pass

    def __ui_post(self, fn) -> None:
        queue_obj = self.__event_queue
        if queue_obj is not None:
            try:
                queue_obj.put(fn)
                return
            except Exception:
                pass
        root = self.__root
        if root is None:
            return
        try:
            root.after(0, fn)
        except Exception:
            return
        return

    def __show_change_tooltip(
        self,
        changes: list[UsageChange],
        snapshot: UsageSnapshot | None = None,
    ) -> None:
        root = self.__root
        if root is None or not changes:
            return
        current = snapshot if isinstance(snapshot, UsageSnapshot) else self.get_last_snapshot()
        metric_colors: dict[str, str] = {}
        for item in changes:
            color = self.__resolve_change_color(item)
            if color:
                metric_colors[str(item.key)] = color
        lines: list[tuple[str, str | None]] = [("Codex 현재 사용량", None)]
        lines.extend(self.__build_snapshot_lines(current, metric_colors=metric_colors))
        lines.append(("", None))
        lines.append(("변경 항목", None))
        for item in changes:
            before = item.before if item.before else "-"
            after = item.after if item.after else "-"
            lines.append(
                (
                    f"- {item.label}: {before} -> {after}",
                    self.__resolve_change_color(item),
                )
            )
        self.__show_tooltip("", lines=lines)
        return

    def __show_snapshot_tooltip(self, snapshot: UsageSnapshot, title: str) -> None:
        lines: list[tuple[str, str | None]] = [(str(title or "Codex 현재 사용량"), None)]
        lines.extend(self.__build_snapshot_lines(snapshot))
        self.__show_tooltip("", lines=lines)
        return

    def __show_busy_collect_tooltip(self) -> None:
        self.__show_tooltip(
            "이미 Codex 사용량 조회가 진행 중입니다. 완료되면 결과를 자동으로 표시합니다.",
            duration_ms=0,
        )
        return

    def __set_manual_query_pending_result(self) -> None:
        try:
            with self.__manual_query_state_lock:
                self.__manual_query_waiting_result = True
        except Exception:
            self.__manual_query_waiting_result = True
        return

    def __consume_manual_query_pending_result(self) -> bool:
        try:
            with self.__manual_query_state_lock:
                if not bool(self.__manual_query_waiting_result):
                    return False
                self.__manual_query_waiting_result = False
                return True
        except Exception:
            pending = bool(self.__manual_query_waiting_result)
            self.__manual_query_waiting_result = False
            return pending

    def __has_manual_query_pending_result(self) -> bool:
        try:
            with self.__manual_query_state_lock:
                return bool(self.__manual_query_waiting_result)
        except Exception:
            return bool(self.__manual_query_waiting_result)

    def __show_pending_manual_result_if_needed(
        self,
        snapshot: UsageSnapshot | None,
        error: str | None = None,
    ) -> None:
        if not self.__consume_manual_query_pending_result():
            return
        err = normalize_usage_value(str(error or ""))
        if err:
            if err == "profile_in_use":
                latest = snapshot if isinstance(snapshot, UsageSnapshot) else self.get_last_snapshot()
                if latest is not None and latest.has_any_metric():
                    self.__show_snapshot_tooltip(latest, title="Codex 최근 사용량 (자동 조회 일시중지)")
                    return
                self.__show_tooltip("다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다.")
                return
            self.__show_tooltip(
                f"진행 중이던 조회가 실패했습니다. {self.__describe_collect_error_for_user(err)}"
            )
            return
        if snapshot is None or not snapshot.has_any_metric():
            self.__show_tooltip("조회가 완료되었지만 사용량을 확인하지 못했습니다.")
            return
        self.__show_snapshot_tooltip(snapshot, title="Codex 현재 사용량")
        return

    def __describe_collect_error_for_user(self, error: str) -> str:
        key = normalize_usage_value(str(error or "")).lower()
        if not key:
            return "잠시 후 다시 시도해 주세요."
        mapping = {
            "parse_failed": "페이지에서 사용량을 읽지 못했습니다.",
            "collect_failed": "조회 작업 중 오류가 발생했습니다.",
            "playwright_unavailable": "브라우저 런타임을 확인해 주세요.",
            "login_required": "로그인이 필요합니다.",
            "cloudflare_challenge": "Cloudflare 인증이 필요합니다.",
            "collect_busy": "이미 조회가 진행 중입니다.",
            "collect_cancelled": "요청에 의해 조회가 취소되었습니다.",
            "profile_in_use": "다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다.",
        }
        return mapping.get(key, "잠시 후 다시 시도해 주세요.")

    def __build_snapshot_lines(
        self,
        snapshot: UsageSnapshot | None,
        section_title: str | None = None,
        metric_colors: dict[str, str] | None = None,
    ) -> list[tuple[str, str | None]]:
        payload = snapshot.to_dict() if isinstance(snapshot, UsageSnapshot) else {}
        lines: list[tuple[str, str | None]] = []
        if section_title:
            lines.append((str(section_title), None))
        for key in USAGE_METRIC_KEYS:
            label = USAGE_METRIC_LABELS.get(key, key)
            value = normalize_usage_value(payload.get(key, ""))
            if not value:
                value = "-"
            line_color: str | None = None
            if isinstance(metric_colors, dict):
                line_color = metric_colors.get(str(key))
            lines.append((f"{label}: {value}", line_color))
        captured_at = normalize_usage_value(payload.get("captured_at", ""))
        if captured_at:
            lines.append((f"확인 시각: {self.__format_timestamp_display(captured_at)}", None))
        return lines

    def __format_timestamp_display(self, value: str) -> str:
        text = normalize_usage_value(value)
        if not text:
            return ""
        candidate = text
        try:
            normalized = candidate.replace("Z", "+00:00")
            parsed = self.__lib.datetime.fromisoformat(normalized)
            if parsed.tzinfo is not None:
                localized = parsed.astimezone(self.__korea_tz)
            else:
                localized = parsed
            return str(localized.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            return candidate.replace("T", " ")

    def __resolve_change_color(self, item: UsageChange) -> str | None:
        before_score = self.__metric_score_for_compare(item.key, item.before)
        after_score = self.__metric_score_for_compare(item.key, item.after)
        if before_score is None or after_score is None:
            return None
        if after_score > before_score:
            return "#16A34A"
        if after_score < before_score:
            return "#DC2626"
        return None

    def __metric_score_for_compare(self, key: str, value: str) -> float | None:
        text = normalize_usage_value(value)
        if not text or text == "-":
            return None

        ratio = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
        if ratio is not None:
            try:
                left = float(ratio.group(1))
            except Exception:
                return None
            if key in {"five_hour_limit", "weekly_limit", "code_review"}:
                # Usage ratios are treated as "used/limit", so lower is better.
                return -left
            return left

        percent = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
        if percent is not None:
            try:
                return float(percent.group(1))
            except Exception:
                return None

        raw = text.replace(",", "")
        number = re.search(r"(-?\d+(?:\.\d+)?)", raw)
        if number is None:
            return None
        try:
            return float(number.group(1))
        except Exception:
            return None

    def __show_tooltip(
        self,
        text: str,
        lines: list[tuple[str, str | None]] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        root = self.__root
        if root is None:
            return
        auto_hide_ms: int | None
        if duration_ms is None:
            duration = int(self.__tooltip_duration_ms)
            if duration < 1200:
                duration = 1200
            auto_hide_ms = duration
        else:
            try:
                duration = int(duration_ms)
            except Exception:
                duration = int(self.__tooltip_duration_ms)
            if duration <= 0:
                auto_hide_ms = None
            else:
                if duration < 1200:
                    duration = 1200
                auto_hide_ms = duration
        current = self.__active_tooltip
        if current is not None:
            try:
                current.hide_tooltip()
            except Exception:
                pass
        tooltip = ToolTip(
            root,
            str(text or ""),
            bind_events=False,
            auto_hide_ms=auto_hide_ms,
            keep_on_hover=True,
            lines=lines,
        )
        self.__active_tooltip = tooltip
        try:
            tooltip.show_tooltip()
        except Exception:
            return
        return

    def __handle_collect_error(self, error: str, source: str = "") -> None:
        msg = str(error or "unknown_error")
        if msg in {"collect_busy", "collect_cancelled"}:
            return
        self.__log(f"collect error: {msg}")
        normalized_source = normalize_usage_value(source).lower()
        is_manual_query = normalized_source == "manual_query"

        if msg in {"login_required", "cloudflare_challenge"}:
            if msg == "login_required":
                self.__set_session_state("logged_out")
                self.__pause_background_monitor()
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_login_notice_ts)) >= float(self.__login_notice_cooldown_sec):
                self.__last_login_notice_ts = now
                if msg == "cloudflare_challenge":
                    if is_manual_query:
                        message = (
                            "Cloudflare 인증이 필요합니다. 인증 창이 자동으로 열리지 않으면 "
                            "잠시 후 Ctrl+Alt+C로 다시 조회해 주세요."
                        )
                    else:
                        message = (
                            "Cloudflare 인증이 필요합니다. Ctrl+Alt+C로 수동 조회를 실행하면 "
                            "인증 창을 열어 확인할 수 있습니다."
                        )
                else:
                    if is_manual_query:
                        message = (
                            "Codex 로그인이 필요합니다. 로그인 창이 자동으로 열리지 않으면 "
                            "잠시 후 Ctrl+Alt+C로 다시 조회해 주세요."
                        )
                    else:
                        message = (
                            "Codex 로그인이 필요합니다. Ctrl+Alt+C로 수동 조회를 실행하면 "
                            "로그인 창을 열 수 있습니다."
                        )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        elif msg == "profile_in_use":
            self.__profile_in_use_detected = True
            if is_manual_query:
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        "현재 Chrome에서 같은 프로필을 사용 중입니다. 자동 창 생성 대신 수동 조회만 허용됩니다."
                    )
                )
                return
            return
        elif msg == "playwright_unavailable":
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_playwright_notice_ts)) >= float(self.__playwright_notice_cooldown_sec):
                self.__last_playwright_notice_ts = now
                is_frozen = False
                try:
                    is_frozen = bool(getattr(self.__lib.sys, "frozen", False))
                except Exception:
                    is_frozen = False
                message = (
                    "Playwright 런타임 로드 실패: 빌드 포함 상태를 확인하세요."
                    if is_frozen
                    else "Playwright 런타임 로드 실패: 개발 환경 동기화 상태를 확인하세요."
                )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        return

    def __collect_snapshot(self, source: str = "") -> tuple[UsageSnapshot | None, str | None]:
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        self.__force_playwright_mode()
        self.__configure_playwright_env()
        if not self.__ensure_playwright_available():
            return None, "playwright_unavailable"
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None, "playwright_unavailable"

        retry_count = 1
        try:
            retry_count = int(getattr(self, "_CodexUsageMonitor__playwright_launch_retry_count", 1) or 1)
        except Exception:
            retry_count = 1
        if retry_count < 1:
            retry_count = 1

        for attempt in range(retry_count):
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            try:
                with sync_playwright() as playwright_obj:
                    return self.__collect_with_playwright_obj(playwright_obj, source=str(source or ""))
            except Exception as exc:
                self.__log_exception("collect snapshot failed", exc)
                if attempt >= (retry_count - 1):
                    return None, "collect_failed"
                self.__log(f"collect snapshot retry attempt={attempt + 2}")
        return None, "collect_failed"

    def __collect_with_playwright_obj(
        self,
        playwright_obj,
        source: str = "",
    ) -> tuple[UsageSnapshot | None, str | None]:
        normalized_source = normalize_usage_value(source).lower()
        is_manual_query = normalized_source == "manual_query"
        self.__log("collect strategy=headful-hidden-first step=hidden")
        snapshot, error = self.__collect_snapshot_once(
            playwright_obj,
            headless=False,
            allow_interactive_recovery=False,
            force_hidden=True,
            prefer_system_channel=True,
        )
        if error in {"profile_in_use", "collect_failed", "parse_failed", "cloudflare_challenge"}:
            raw_snapshot = self.__try_collect_snapshot_via_raw_external_cdp()
            if raw_snapshot is not None:
                self.__log("collect strategy=headful-hidden-first recovered=raw_cdp_external")
                return raw_snapshot, None
        if error == "collect_cancelled":
            return None, "collect_cancelled"
        if is_manual_query and error in {"login_required", "cloudflare_challenge"}:
            self.__log(
                f"collect strategy=headful-hidden-first pre-interactive-retry reason={error} "
                f"source={normalized_source}"
            )
            retry_snapshot, retry_error = self.__collect_snapshot_once(
                playwright_obj,
                headless=False,
                allow_interactive_recovery=False,
                force_hidden=True,
                prefer_system_channel=True,
            )
            if retry_error is None and retry_snapshot is not None:
                return retry_snapshot, None
            if retry_error is not None:
                if str(retry_error) == "collect_cancelled":
                    return None, "collect_cancelled"
                error = str(retry_error)
                snapshot = retry_snapshot
        if error not in {"login_required", "cloudflare_challenge"}:
            return snapshot, error
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        if not self.__should_open_interactive_recovery(source=source):
            self.__log(
                f"collect strategy=headful-hidden-first interactive=skip reason={error} "
                f"source={normalize_usage_value(source)}"
            )
            return None, error
        self.__log(
            f"collect strategy=headful-hidden-first interactive=open reason={error} "
            f"source={normalize_usage_value(source)}"
        )
        self.__prepare_interactive_recovery_launch(
            source=normalized_source,
            reason=str(error or ""),
        )
        if error == "login_required":
            notice = "Codex 로그인 창을 여는 중... 로그인 완료 후 자동으로 수집합니다."
        else:
            notice = "Cloudflare 인증 창을 여는 중... 인증 완료 후 자동으로 수집합니다."
        self.__ui_post(lambda: self.__show_tooltip(notice))
        return self.__collect_snapshot_once(
            playwright_obj,
            headless=False,
            allow_interactive_recovery=True,
            force_hidden=False,
            prefer_system_channel=True,
            initial_url=str(self.__login_entry_url),
        )

    def __should_open_interactive_recovery(self, source: str = "") -> bool:
        normalized_source = normalize_usage_value(source).lower()
        # Background collectors should never open visible auth windows.
        if normalized_source != "manual_query":
            return False
        if bool(self.__logout_in_progress) or self.__is_collect_cancel_requested():
            return False
        now = 0.0
        try:
            now = float(self.__lib.time.monotonic())
        except Exception:
            now = 0.0
        cooldown_sec = float(self.__manual_interactive_reopen_cooldown_sec)
        if cooldown_sec < 0.0:
            cooldown_sec = 0.0
        if (now - float(self.__last_interactive_login_ts)) < cooldown_sec:
            return False
        self.__last_interactive_login_ts = now
        return True

    def __prepare_interactive_recovery_launch(self, source: str = "", reason: str = "") -> None:
        normalized_source = normalize_usage_value(source)
        normalized_reason = normalize_usage_value(reason)
        self.__log(
            "interactive recovery prep "
            f"source={normalized_source or 'unknown'} "
            f"reason={normalized_reason or 'unknown'}"
        )
        # Interactive recovery must not attach to stale hidden CDP sessions.
        self.__pending_hidden_cdp_clear = False
        self.__clear_hidden_cdp_process(terminate=True)
        return

    def __collect_snapshot_once(
        self,
        playwright_obj,
        headless: bool,
        allow_interactive_recovery: bool = False,
        force_hidden: bool = False,
        prefer_system_channel: bool = False,
        initial_url: str | None = None,
    ) -> tuple[UsageSnapshot | None, str | None]:
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        context = None
        cdp_browser = None
        cdp_proc = None
        keep_hidden_cdp_process = False
        page = None
        close_collect_page = False
        usage_url = str(self.__usage_url)
        start_url = normalize_usage_value(initial_url)
        if not start_url:
            start_url = usage_url
        needs_usage_navigation = not are_equivalent_codex_usage_urls(str(start_url), usage_url)
        effective_headless = bool(headless)
        try:
            if (not bool(effective_headless)) and bool(prefer_system_channel):
                if bool(force_hidden) and not bool(allow_interactive_recovery):
                    (
                        context,
                        cdp_browser,
                        cdp_proc,
                        keep_hidden_cdp_process,
                    ) = self.__connect_hidden_cdp_context(
                        playwright_obj,
                        launch_url=start_url,
                    )
                    if context is None and self.__is_profile_locked_without_remote_debugging():
                        return None, "profile_in_use"
                else:
                    context, cdp_browser, cdp_proc = self.__launch_interactive_context_via_cdp(
                        playwright_obj,
                        start_hidden=False,
                        initial_url=start_url,
                    )
            if context is None:
                launch_headless = bool(effective_headless)
                if (not launch_headless) and bool(force_hidden):
                    launch_headless = True
                context = self.__launch_browser_context(
                    playwright_obj,
                    headless=bool(launch_headless),
                    prefer_system_channel=bool(prefer_system_channel),
                )
                effective_headless = bool(launch_headless)
            if context is None:
                return None, "collect_failed"
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            if bool(effective_headless):
                self.__apply_headless_fast_routes(context)
            is_external_cdp = self.__is_external_cdp_handle(cdp_proc)
            is_monitor_managed_cdp = self.__is_monitor_managed_cdp_handle(cdp_proc)
            should_hide_cdp_window = bool((not is_external_cdp) or is_monitor_managed_cdp)
            if cdp_proc is not None:
                if bool(force_hidden) and bool(should_hide_cdp_window):
                    self.__set_cdp_window_visibility(cdp_proc, visible=False, bring_to_front=False)
            if bool(is_external_cdp) and not bool(is_monitor_managed_cdp):
                try:
                    page = context.new_page()
                    close_collect_page = True
                except Exception:
                    page = self.__select_collect_page(
                        context,
                        preferred_url=start_url,
                        close_extra_blank_tabs=False,
                    )
            else:
                page = self.__select_collect_page(
                    context,
                    preferred_url=start_url,
                    close_extra_blank_tabs=not bool(effective_headless),
                )
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            page.goto(
                str(start_url),
                wait_until="domcontentloaded",
                timeout=int(self.__navigation_timeout_ms),
            )
            if (
                cdp_proc is not None
                and bool(force_hidden)
                and not bool(allow_interactive_recovery)
                and bool(should_hide_cdp_window)
            ):
                # Navigation can trigger profile popups; re-hide the window defensively.
                self.__set_cdp_window_visibility(cdp_proc, visible=False, bring_to_front=False)

            if self.__is_cloudflare_challenge(page):
                if bool(effective_headless):
                    return None, "cloudflare_challenge"
                if not bool(allow_interactive_recovery):
                    grace_sec = 0.0
                    try:
                        grace_sec = float(self.__background_cloudflare_grace_sec)
                    except Exception:
                        grace_sec = 0.0
                    if grace_sec <= 0.0:
                        return None, "cloudflare_challenge"
                    ok_cf = self.__wait_until_cloudflare_cleared(
                        page,
                        timeout_sec=grace_sec,
                    )
                    if not ok_cf:
                        if self.__is_collect_cancel_requested():
                            return None, "collect_cancelled"
                        return None, "cloudflare_challenge"
                else:
                    ok_cf = self.__wait_until_cloudflare_cleared(
                        page,
                        timeout_sec=max(float(self.__login_timeout_sec), 420.0),
                    )
                    if not ok_cf:
                        if self.__is_collect_cancel_requested():
                            return None, "collect_cancelled"
                        return None, "cloudflare_challenge"

            if self.__is_login_required(page):
                if bool(effective_headless) or not bool(allow_interactive_recovery):
                    self.__set_session_state("logged_out")
                    return None, "login_required"
                ok = self.__wait_until_logged_in(page, timeout_sec=self.__login_timeout_sec)
                if not ok:
                    if self.__is_collect_cancel_requested():
                        return None, "collect_cancelled"
                    self.__set_session_state("logged_out")
                    return None, "login_required"
            if needs_usage_navigation:
                try:
                    if self.__is_collect_cancel_requested():
                        return None, "collect_cancelled"
                    page.goto(
                        usage_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.__navigation_timeout_ms),
                    )
                except Exception as exc:
                    self.__log_exception("navigate usage after login failed", exc)
                    return None, "collect_failed"

            snapshot = self.__build_snapshot_from_page(page)
            if snapshot is not None:
                self.__set_session_state("logged_in")
                if (
                    not bool(effective_headless)
                    and bool(allow_interactive_recovery)
                    and cdp_proc is not None
                ):
                    if self.__promote_cdp_process_for_hidden_reuse(cdp_proc):
                        keep_hidden_cdp_process = True
                        self.__set_cdp_window_visibility(
                            cdp_proc,
                            visible=False,
                            bring_to_front=False,
                        )
                return snapshot, None
            if bool(effective_headless):
                return self.__wait_for_snapshot_ready(
                    page,
                    timeout_sec=min(float(self.__login_timeout_sec), float(self.__headless_wait_timeout_sec)),
                )
            if not bool(effective_headless) and bool(allow_interactive_recovery):
                waited_snapshot, waited_error = self.__wait_for_snapshot_ready(
                    page,
                    timeout_sec=self.__login_timeout_sec,
                )
                if waited_error is None and waited_snapshot is not None and cdp_proc is not None:
                    if self.__promote_cdp_process_for_hidden_reuse(cdp_proc):
                        keep_hidden_cdp_process = True
                        self.__set_cdp_window_visibility(
                            cdp_proc,
                            visible=False,
                            bring_to_front=False,
                        )
                return waited_snapshot, waited_error
            if not bool(effective_headless):
                return self.__wait_for_snapshot_ready(
                    page,
                    timeout_sec=float(self.__headless_wait_timeout_sec),
                )
            try:
                self.__log(
                    f"parse_failed url={str(page.url or '')} "
                    f"login={self.__is_login_required(page)} "
                    f"cloudflare={self.__is_cloudflare_challenge(page)}"
                )
            except Exception:
                pass
            return None, "parse_failed"
        except Exception as exc:
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            if bool(keep_hidden_cdp_process):
                self.__clear_hidden_cdp_process(terminate=True)
            self.__log_exception("collect snapshot once failed", exc)
            return None, "collect_failed"
        finally:
            if bool(close_collect_page) and page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None and not bool(keep_hidden_cdp_process):
                try:
                    context.close()
                except Exception:
                    pass
            if cdp_browser is not None:
                try:
                    cdp_browser.close()
                except Exception:
                    pass
            if cdp_proc is not None and not bool(keep_hidden_cdp_process):
                self.__terminate_spawned_process(cdp_proc, cleanup_orphans=False)

    def __build_snapshot_from_page(self, page) -> UsageSnapshot | None:
        probe = self.__probe_usage_page(page)
        return self.__build_snapshot_from_probe(probe)

    def __wait_for_snapshot_ready(self, page, timeout_sec: float) -> tuple[UsageSnapshot | None, str | None]:
        deadline = 0.0
        try:
            deadline = float(self.__lib.time.monotonic()) + float(timeout_sec)
        except Exception:
            deadline = 0.0
        next_home_recovery_ts = 0.0

        while True:
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            snapshot = self.__build_snapshot_from_page(page)
            if snapshot is not None:
                self.__set_session_state("logged_in")
                return snapshot, None

            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now >= float(next_home_recovery_ts):
                current_url = self.__get_page_url(page)
                if self.__is_chatgpt_home_url(current_url):
                    if self.__is_login_required(page):
                        try:
                            if self.__try_open_login_entry(page, force=False):
                                next_home_recovery_ts = now + 4.0
                                continue
                        except Exception:
                            pass
                    else:
                        try:
                            page.goto(
                                str(self.__usage_url),
                                wait_until="domcontentloaded",
                                timeout=int(self.__navigation_timeout_ms),
                            )
                            try:
                                page.wait_for_timeout(900)
                            except Exception:
                                pass
                            self.__log("usage retry navigation from chatgpt home")
                            next_home_recovery_ts = now + 4.0
                            continue
                        except Exception as exc:
                            self.__log_exception("usage retry from home failed", exc)
                    next_home_recovery_ts = now + 4.0
            if now > deadline:
                if self.__is_cloudflare_challenge(page):
                    return None, "cloudflare_challenge"
                if self.__is_login_required(page):
                    self.__set_session_state("logged_out")
                    return None, "login_required"
                return None, "parse_failed"
            try:
                page.wait_for_timeout(1500)
            except Exception:
                if self.__is_cloudflare_challenge(page):
                    return None, "cloudflare_challenge"
                if self.__is_login_required(page):
                    self.__set_session_state("logged_out")
                    return None, "login_required"
                return None, "parse_failed"

    def __get_page_url(self, page) -> str:
        try:
            return str(page.url or "")
        except Exception:
            return ""

    def __is_chatgpt_home_url(self, url: str) -> bool:
        lowered = str(url or "").strip().lower()
        if not lowered.startswith("https://chatgpt.com"):
            return False
        tail = lowered[len("https://chatgpt.com") :]
        if not tail:
            return True
        if tail == "/":
            return True
        if tail.startswith("/?") or tail.startswith("/#"):
            return True
        return False

    def __is_usage_page_url(self, url: str) -> bool:
        return bool(is_codex_usage_url(url))

    def __is_blank_page_url(self, url: str) -> bool:
        lowered = str(url or "").strip().lower()
        if not lowered:
            return True
        return lowered in {
            "about:blank",
            "chrome://newtab/",
            "chrome://newtab",
            "chrome://new-tab-page/",
            "chrome://new-tab-page",
            "edge://newtab/",
            "edge://newtab",
        }

    def __select_collect_page(self, context, preferred_url: str, close_extra_blank_tabs: bool = False):
        pages = []
        try:
            pages = list(context.pages or [])
        except Exception:
            pages = []

        preferred = normalize_usage_value(preferred_url)
        selected = None
        for candidate in pages:
            url = normalize_usage_value(self.__get_page_url(candidate))
            if preferred and are_equivalent_codex_usage_urls(url, preferred):
                selected = candidate
                break

        if selected is None:
            for candidate in pages:
                if not self.__is_blank_page_url(self.__get_page_url(candidate)):
                    selected = candidate
                    break

        if selected is None:
            selected = pages[0] if pages else context.new_page()

        if bool(close_extra_blank_tabs):
            for candidate in pages:
                if candidate is selected:
                    continue
                if not self.__is_blank_page_url(self.__get_page_url(candidate)):
                    continue
                try:
                    candidate.close()
                except Exception:
                    continue
        return selected

    def __launch_interactive_context_via_cdp(
        self,
        playwright_obj,
        start_hidden: bool = False,
        initial_url: str | None = None,
    ):
        chrome_path = self.__resolve_chrome_executable_path()
        if not chrome_path:
            return None, None, None
        try:
            self.__lib.os.makedirs(self.__profile_dir, exist_ok=True)
        except Exception:
            pass
        self.__prepare_profile_for_chrome_launch()

        last_error = None
        total_deadline = 0.0
        try:
            total_deadline = float(self.__lib.time.monotonic()) + float(self.__cdp_total_launch_timeout_sec)
        except Exception:
            total_deadline = 0.0
        for port in self.__iter_cdp_ports():
            if self.__is_collect_cancel_requested():
                return None, None, None
            if total_deadline > 0.0:
                now = 0.0
                try:
                    now = float(self.__lib.time.monotonic())
                except Exception:
                    now = total_deadline + 1.0
                if now >= total_deadline:
                    break
            proc = None
            browser = None
            try:
                existing_pid = self.__find_profile_remote_debugging_pid(int(port))
                if existing_pid > 0:
                    self.__log(
                        f"interactive cdp skip occupied profile port={int(port)} pid={int(existing_pid)}"
                    )
                    continue
                launch_url = normalize_usage_value(initial_url)
                if not launch_url:
                    launch_url = str(self.__usage_url)
                cmd = [
                    str(chrome_path),
                    f"--remote-debugging-port={int(port)}",
                    f"--user-data-dir={self.__profile_dir}",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
                if bool(start_hidden):
                    cmd.extend(
                        [
                            "--window-size=1280,720",
                            "--disable-extensions",
                            "--disable-notifications",
                        ]
                    )
                cmd.extend(["--new-window", str(launch_url)])
                popen_kwargs: dict[str, Any] = {}
                if bool(start_hidden):
                    try:
                        startupinfo = self.__lib.subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= int(
                            getattr(self.__lib.subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
                        )
                        startupinfo.wShowWindow = int(
                            getattr(self.__lib.subprocess, "SW_HIDE", 0)
                        )
                        popen_kwargs["startupinfo"] = startupinfo
                    except Exception:
                        pass
                proc = self.__lib.subprocess.Popen(cmd, **popen_kwargs)
                endpoint = f"http://127.0.0.1:{int(port)}"
                connect_deadline = 0.0
                try:
                    connect_deadline = float(self.__lib.time.monotonic()) + 15.0
                except Exception:
                    connect_deadline = 0.0
                if total_deadline > 0.0 and (connect_deadline <= 0.0 or connect_deadline > total_deadline):
                    connect_deadline = total_deadline
                while True:
                    if self.__is_collect_cancel_requested():
                        self.__terminate_spawned_process(proc, cleanup_orphans=False)
                        return None, None, None
                    try:
                        browser = self.__connect_browser_over_cdp(playwright_obj, endpoint)
                        break
                    except Exception as exc:
                        last_error = exc
                        now = 0.0
                        try:
                            now = float(self.__lib.time.monotonic())
                        except Exception:
                            now = connect_deadline + 1.0
                        if now > connect_deadline:
                            break
                        try:
                            self.__lib.time.sleep(0.35)
                        except Exception:
                            pass

                if browser is None:
                    self.__terminate_spawned_process(proc, cleanup_orphans=False)
                    continue
                contexts = []
                try:
                    contexts = list(browser.contexts or [])
                except Exception:
                    contexts = []
                if not contexts:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    self.__terminate_spawned_process(proc, cleanup_orphans=False)
                    last_error = RuntimeError("cdp browser has no context")
                    continue
                spawned_pid = 0
                try:
                    spawned_pid = int(getattr(proc, "pid", 0) or 0)
                except Exception:
                    spawned_pid = 0
                listener_pid = self.__find_profile_remote_debugging_pid(int(port))
                if spawned_pid > 0 and listener_pid > 0 and listener_pid != spawned_pid:
                    self.__log(
                        "interactive cdp listener remapped "
                        f"port={int(port)} spawned={int(spawned_pid)} listener={int(listener_pid)}"
                    )
                    try:
                        setattr(proc, "_ws_listener_pid", int(listener_pid))
                    except Exception:
                        pass
                if spawned_pid > 0 and (not self.__is_subprocess_running(proc)):
                    if listener_pid <= 0:
                        self.__log(
                            "interactive cdp process exited early "
                            f"port={int(port)} pid={int(spawned_pid)}"
                        )
                        try:
                            browser.close()
                        except Exception:
                            pass
                        self.__terminate_spawned_process(proc, cleanup_orphans=False)
                        last_error = RuntimeError("cdp spawned process exited")
                        continue
                    try:
                        setattr(proc, "_ws_listener_pid", int(listener_pid))
                    except Exception:
                        pass
                self.__log(
                    f"interactive cdp connected port={int(port)} pid={int(spawned_pid)}"
                )
                try:
                    setattr(proc, "_ws_cdp_port", int(port))
                except Exception:
                    pass
                self.__last_successful_cdp_port = int(port)
                return contexts[0], browser, proc
            except Exception as exc:
                last_error = exc
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if proc is not None:
                    self.__terminate_spawned_process(proc, cleanup_orphans=False)
                continue
        if last_error is not None:
            self.__log_exception("interactive cdp launch failed", last_error)
        return None, None, None

    def __iter_cdp_ports(self) -> list[int]:
        ports = list(range(9333, 9345))
        try:
            preferred = int(self.__last_successful_cdp_port or 0)
        except Exception:
            preferred = 0
        if preferred in ports:
            ports.remove(preferred)
            ports.insert(0, preferred)
        try:
            limit = int(self.__cdp_port_attempt_limit or len(ports))
        except Exception:
            limit = len(ports)
        if limit < 1:
            limit = len(ports)
        return ports[: min(limit, len(ports))]

    def __prepare_profile_for_chrome_launch(self) -> None:
        profile_dir = str(self.__profile_dir or "").strip()
        if not profile_dir:
            return
        targets = [
            self.__lib.os.path.join(profile_dir, "Default", "Preferences"),
            self.__lib.os.path.join(profile_dir, "Local State"),
        ]
        for target in targets:
            self.__patch_chrome_clean_exit_markers(target)
        return

    def __patch_chrome_clean_exit_markers(self, path: str) -> None:
        raw_path = str(path or "").strip()
        if not raw_path:
            return
        try:
            if not self.__lib.os.path.isfile(raw_path):
                return
        except Exception:
            return
        try:
            with open(raw_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        dirty = False
        if str(payload.get("exit_type", "") or "").lower() in {"crashed", "session_ended"}:
            payload["exit_type"] = "Normal"
            dirty = True
        if payload.get("exited_cleanly") is False:
            payload["exited_cleanly"] = True
            dirty = True
        profile = payload.get("profile")
        if isinstance(profile, dict):
            if str(profile.get("exit_type", "") or "").lower() in {"crashed", "session_ended"}:
                profile["exit_type"] = "Normal"
                dirty = True
            if profile.get("exited_cleanly") is False:
                profile["exited_cleanly"] = True
                dirty = True

        if not dirty:
            return
        try:
            with open(raw_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception:
            return
        return

    def __is_pid_alive(self, pid: int) -> bool:
        try:
            return bool(self.__lib.psutil.pid_exists(int(pid)))
        except Exception:
            return False

    def __is_subprocess_running(self, proc) -> bool:
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            pass
        try:
            listener_pid = int(getattr(proc, "_ws_listener_pid", 0) or 0)
        except Exception:
            listener_pid = 0
        if listener_pid > 0:
            return self.__is_pid_alive(listener_pid)
        return False

    def __clear_hidden_cdp_process(self, terminate: bool = False) -> None:
        proc = self.__hidden_cdp_proc
        self.__hidden_cdp_proc = None
        self.__hidden_cdp_port = 0
        if bool(terminate) and proc is not None:
            self.__terminate_spawned_process(proc, cleanup_orphans=True)
        return

    def __is_external_cdp_handle(self, proc) -> bool:
        if proc is None:
            return False
        try:
            return bool(getattr(proc, "_ws_external_cdp", False))
        except Exception:
            return False

    def __is_monitor_managed_cdp_handle(self, proc) -> bool:
        if proc is None:
            return False
        try:
            return bool(getattr(proc, "_ws_monitor_managed", False))
        except Exception:
            return False

    def __build_external_cdp_handle(self, pid: int, port: int, monitor_managed: bool = False):
        class _ExternalCdpHandle:
            pass

        handle = _ExternalCdpHandle()
        safe_pid = int(pid) if int(pid or 0) > 0 else 0
        safe_port = int(port) if int(port or 0) > 0 else 0
        setattr(handle, "pid", safe_pid)
        setattr(handle, "_ws_listener_pid", safe_pid)
        setattr(handle, "_ws_cdp_port", safe_port)
        setattr(handle, "_ws_external_cdp", True)
        setattr(handle, "_ws_monitor_managed", bool(monitor_managed))
        return handle

    def __iter_external_profile_remote_debugging_endpoints(self) -> list[tuple[int, int, bool]]:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return []
        owned_pids: set[int] = set()
        proc = self.__hidden_cdp_proc
        if proc is not None:
            for attr in ("pid", "_ws_listener_pid"):
                try:
                    candidate = int(getattr(proc, attr, 0) or 0)
                except Exception:
                    candidate = 0
                if candidate > 0:
                    owned_pids.add(candidate)
        try:
            owned_port = int(self.__hidden_cdp_port or 0)
        except Exception:
            owned_port = 0
        if owned_port > 0:
            pid_from_port = self.__find_profile_remote_debugging_pid(owned_port)
            if pid_from_port > 0:
                owned_pids.add(int(pid_from_port))

        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["pid", "name", "cmdline"])
        except Exception:
            return []

        items: list[tuple[int, int, bool]] = []
        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                pid = int((info or {}).get("pid") or 0)
                if pid > 0 and pid in owned_pids:
                    continue
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if f"--user-data-dir={target_profile}" not in cmd_text:
                    continue
                if "--type=" in cmd_text:
                    continue
                port = 0
                for token in cmdline:
                    text = str(token or "").strip().lower()
                    prefix = "--remote-debugging-port="
                    if not text.startswith(prefix):
                        continue
                    raw_port = str(text[len(prefix) :]).strip()
                    try:
                        parsed = int(raw_port)
                    except Exception:
                        parsed = 0
                    if parsed > 0:
                        port = int(parsed)
                        break
                if port <= 0:
                    continue
                managed_tokens = (
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-notifications",
                )
                managed_hit = 0
                for token in managed_tokens:
                    if token in cmd_text:
                        managed_hit += 1
                monitor_managed = managed_hit >= 4
                items.append((int(port), int(pid if pid > 0 else 0), bool(monitor_managed)))
            except Exception:
                continue

        dedup: list[tuple[int, int, bool]] = []
        seen_ports: set[int] = set()
        for port, pid, monitor_managed in items:
            if port in seen_ports:
                continue
            seen_ports.add(port)
            dedup.append((int(port), int(pid), bool(monitor_managed)))

        try:
            preferred = int(self.__last_successful_cdp_port or 0)
        except Exception:
            preferred = 0
        if preferred > 0:
            for idx, item in enumerate(dedup):
                if int(item[0]) != preferred:
                    continue
                dedup.insert(0, dedup.pop(idx))
                break
        return dedup

    def __connect_browser_over_cdp(self, playwright_obj, endpoint: str):
        timeout_ms = 3000
        try:
            timeout_ms = int(getattr(self, "_CodexUsageMonitor__cdp_connect_timeout_ms", 3000) or 3000)
        except Exception:
            timeout_ms = 3000
        if timeout_ms < 500:
            timeout_ms = 500
        return playwright_obj.chromium.connect_over_cdp(
            str(endpoint),
            timeout=int(timeout_ms),
        )

    def __connect_existing_profile_remote_debug_context(self, playwright_obj):
        last_error = None
        for port, pid, monitor_managed in self.__iter_external_profile_remote_debugging_endpoints():
            endpoint = f"http://127.0.0.1:{int(port)}"
            browser = None
            try:
                browser = self.__connect_browser_over_cdp(playwright_obj, endpoint)
                contexts = []
                try:
                    contexts = list(browser.contexts or [])
                except Exception:
                    contexts = []
                if not contexts:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    continue
                self.__log(
                    "hidden cdp attached existing process "
                    f"port={int(port)} pid={int(pid)} managed={bool(monitor_managed)}"
                )
                handle = self.__build_external_cdp_handle(
                    int(pid),
                    int(port),
                    monitor_managed=bool(monitor_managed),
                )
                return contexts[0], browser, handle, True
            except Exception as exc:
                last_error = exc
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                continue
        if last_error is not None:
            self.__log_exception("hidden cdp attach existing failed", last_error)
        return None, None, None, False

    def __try_collect_snapshot_via_raw_external_cdp(self) -> UsageSnapshot | None:
        for port, _pid, _managed in self.__iter_external_profile_remote_debugging_endpoints():
            snapshot = self.__collect_snapshot_via_raw_cdp_port(int(port))
            if snapshot is not None:
                return snapshot
        return None

    def __collect_snapshot_via_raw_cdp_port(self, port: int) -> UsageSnapshot | None:
        try:
            target_port = int(port)
        except Exception:
            return None
        if target_port <= 0:
            return None
        browser_ws = self.__fetch_raw_cdp_browser_websocket_url(target_port)
        if not browser_ws:
            return None
        socket_timeout_sec = float(RAW_CDP_COMMAND_TIMEOUT_SEC)
        ws = None
        target_id = ""
        try:
            ws = _create_raw_websocket_connection(browser_ws, socket_timeout_sec)
            target_id = self.__raw_cdp_create_target(ws)
            if not target_id:
                return None
            attach = self.__raw_cdp_send(
                ws,
                "Target.attachToTarget",
                {"targetId": str(target_id), "flatten": True},
            )
            session_id = str((attach.get("result") or {}).get("sessionId") or "")
            if not session_id:
                self.__raw_cdp_send(
                    ws,
                    "Target.closeTarget",
                    {"targetId": str(target_id)},
                )
                target_id = ""
                return None
            self.__raw_cdp_send(ws, "Runtime.enable", session_id=session_id)
            self.__raw_cdp_send(ws, "Page.enable", session_id=session_id)
            self.__raw_cdp_send(
                ws,
                "Page.navigate",
                {"url": str(self.__usage_url)},
                session_id=session_id,
            )
            deadline = 0.0
            try:
                deadline = float(self.__lib.time.monotonic()) + float(
                    self.__headless_wait_timeout_sec
                )
            except Exception:
                deadline = 0.0
            while True:
                if self.__is_collect_cancel_requested():
                    return None
                probe = self.__raw_cdp_probe_target(ws, session_id=session_id)
                snapshot = self.__build_snapshot_from_probe(probe)
                if snapshot is not None:
                    self.__set_session_state("logged_in")
                    self.__last_successful_cdp_port = int(target_port)
                    self.__log(f"raw cdp snapshot collected port={int(target_port)}")
                    return snapshot
                now = 0.0
                try:
                    now = float(self.__lib.time.monotonic())
                except Exception:
                    now = deadline + 1.0
                if deadline > 0.0 and now >= deadline:
                    break
                try:
                    self.__lib.time.sleep(0.75)
                except Exception:
                    break
        except Exception as exc:
            self.__log_exception("raw cdp snapshot failed", exc)
        finally:
            if ws is not None and target_id:
                try:
                    self.__raw_cdp_send(
                        ws,
                        "Target.closeTarget",
                        {"targetId": str(target_id)},
                    )
                except Exception:
                    pass
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        return None

    def __fetch_raw_cdp_browser_websocket_url(self, port: int) -> str:
        try:
            target_port = int(port)
        except Exception:
            return ""
        if target_port <= 0:
            return ""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{int(target_port)}/json/version",
                timeout=3,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self.__log_exception("raw cdp version fetch failed", exc)
            return ""
        if not isinstance(payload, dict):
            return ""
        return normalize_usage_value(payload.get("webSocketDebuggerUrl", ""))

    def __raw_cdp_create_target(self, ws) -> str:
        response = self.__raw_cdp_send(
            ws,
            "Target.createTarget",
            {"url": "about:blank", "newWindow": False, "background": True},
        )
        return str((response.get("result") or {}).get("targetId") or "")

    def __raw_cdp_probe_target(self, ws, session_id: str) -> dict[str, Any]:
        response = self.__raw_cdp_send(
            ws,
            "Runtime.evaluate",
            {"expression": f"({USAGE_PAGE_PROBE_SCRIPT})()", "returnByValue": True},
            session_id=session_id,
        )
        result = response.get("result") or {}
        value = ((result.get("result") or {}).get("value") or {})
        return self.__normalize_probe_payload(value, fallback_url=str(self.__usage_url))

    def __raw_cdp_send(
        self,
        ws,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        if ws is None:
            raise RuntimeError("raw cdp websocket unavailable")
        request_id = 1
        try:
            request_id = int(getattr(ws, "_ws_request_id", 0) or 0) + 1
        except Exception:
            request_id = 1
        try:
            setattr(ws, "_ws_request_id", int(request_id))
        except Exception:
            pass
        payload: dict[str, Any] = {
            "id": int(request_id),
            "method": str(method),
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = str(session_id)
        effective_timeout = float(timeout_sec or RAW_CDP_COMMAND_TIMEOUT_SEC)
        if effective_timeout <= 0.0:
            effective_timeout = float(RAW_CDP_COMMAND_TIMEOUT_SEC)
        try:
            ws.settimeout(effective_timeout)
        except Exception:
            pass
        ws.send(json.dumps(payload))
        while True:
            message = json.loads(ws.recv())
            if int(message.get("id") or 0) != int(request_id):
                continue
            if message.get("error"):
                raise RuntimeError(str(message.get("error")))
            return message

    def __connect_hidden_cdp_context(self, playwright_obj, launch_url: str | None = None):
        proc = self.__hidden_cdp_proc
        port = 0
        try:
            port = int(self.__hidden_cdp_port or 0)
        except Exception:
            port = 0

        if proc is not None:
            if (not self.__is_subprocess_running(proc)) or (port <= 0):
                self.__clear_hidden_cdp_process(terminate=True)
                proc = None
                port = 0

        if proc is not None and port > 0:
            endpoint = f"http://127.0.0.1:{int(port)}"
            reconnect_browser = None
            try:
                reconnect_browser = self.__connect_browser_over_cdp(playwright_obj, endpoint)
                contexts = []
                try:
                    contexts = list(reconnect_browser.contexts or [])
                except Exception:
                    contexts = []
                if contexts:
                    return contexts[0], reconnect_browser, proc, True
            except Exception as exc:
                self.__log_exception("hidden cdp reconnect failed", exc)
            if reconnect_browser is not None:
                try:
                    reconnect_browser.close()
                except Exception:
                    pass
            self.__clear_hidden_cdp_process(terminate=True)

        ext_context, ext_browser, ext_proc, ext_keep = (
            self.__connect_existing_profile_remote_debug_context(playwright_obj)
        )
        if ext_context is not None:
            return ext_context, ext_browser, ext_proc, ext_keep

        if self.__is_profile_locked_without_remote_debugging():
            self.__log("hidden cdp launch skipped reason=profile_locked_non_debug")
            return None, None, None, False

        context, browser, proc = self.__launch_interactive_context_via_cdp(
            playwright_obj,
            start_hidden=True,
            initial_url=launch_url,
        )
        if proc is None:
            return context, browser, proc, False
        try:
            port = int(getattr(proc, "_ws_cdp_port", 0) or 0)
        except Exception:
            port = 0
        if port > 0:
            self.__hidden_cdp_proc = proc
            self.__hidden_cdp_port = int(port)
            return context, browser, proc, True
        return context, browser, proc, False

    def __promote_cdp_process_for_hidden_reuse(self, proc) -> bool:
        if proc is None:
            return False
        if self.__is_external_cdp_handle(proc):
            return False
        try:
            port = int(getattr(proc, "_ws_cdp_port", 0) or 0)
        except Exception:
            port = 0
        if port <= 0:
            return False
        prev = self.__hidden_cdp_proc
        try:
            self.__hidden_cdp_proc = proc
            self.__hidden_cdp_port = int(port)
        except Exception:
            return False
        if prev is not None and prev is not proc:
            self.__terminate_spawned_process(prev, cleanup_orphans=True)
        return True

    def __set_cdp_window_visibility(
        self,
        proc,
        visible: bool,
        bring_to_front: bool = False,
        timeout_sec: float = 3.0,
    ) -> bool:
        if proc is None:
            return False
        pid_candidates: list[int] = []
        for attr in ("_ws_listener_pid", "pid"):
            try:
                candidate = int(getattr(proc, attr, 0) or 0)
            except Exception:
                candidate = 0
            if candidate > 0:
                pid_candidates.append(candidate)
        for alt_pid in self.__list_profile_chrome_pids():
            try:
                candidate = int(alt_pid)
            except Exception:
                continue
            if candidate > 0:
                pid_candidates.append(candidate)
        unique_pids: list[int] = []
        seen: set[int] = set()
        for pid in pid_candidates:
            if pid in seen:
                continue
            seen.add(pid)
            unique_pids.append(pid)
        if not unique_pids:
            return False

        if not bool(visible):
            hidden_any = False
            hide_timeout = float(timeout_sec)
            if hide_timeout > 1.0:
                hide_timeout = 1.0
            for candidate in unique_pids:
                if self.__set_windows_visibility_for_pid(
                    pid=candidate,
                    visible=False,
                    bring_to_front=False,
                    timeout_sec=hide_timeout,
                ):
                    hidden_any = True
            return hidden_any

        primary = self.__set_windows_visibility_for_pid(
            pid=unique_pids[0],
            visible=True,
            bring_to_front=bool(bring_to_front),
            timeout_sec=float(timeout_sec),
        )
        if primary:
            return True
        fallback_timeout = float(timeout_sec)
        if fallback_timeout > 1.0:
            fallback_timeout = 1.0
        for candidate in unique_pids[1:]:
            if self.__set_windows_visibility_for_pid(
                pid=candidate,
                visible=True,
                bring_to_front=bool(bring_to_front),
                timeout_sec=fallback_timeout,
            ):
                return True
        return False

    def __set_windows_visibility_for_pid(
        self,
        pid: int,
        visible: bool,
        bring_to_front: bool = False,
        timeout_sec: float = 3.0,
    ) -> bool:
        if int(pid) <= 0:
            return False
        try:
            if str(self.__lib.os.name).lower() != "nt":
                return False
        except Exception:
            return False

        now = 0.0
        deadline = 0.0
        try:
            now = float(self.__lib.time.monotonic())
            deadline = now + max(float(timeout_sec), 0.2)
        except Exception:
            deadline = 0.0

        while True:
            handles = self.__list_top_windows_for_pid(int(pid))
            if handles:
                changed = False
                for hwnd in handles:
                    try:
                        if bool(visible):
                            self.__lib.win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
                            if bool(bring_to_front):
                                try:
                                    self.__lib.win32gui.SetForegroundWindow(hwnd)
                                except Exception:
                                    pass
                        else:
                            self.__lib.win32gui.ShowWindow(hwnd, 0)  # SW_HIDE
                        changed = True
                    except Exception:
                        continue
                if changed:
                    return True
            if deadline <= 0.0:
                break
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now >= deadline:
                break
            try:
                self.__lib.time.sleep(0.15)
            except Exception:
                break
        return False

    def __list_top_windows_for_pid(self, pid: int) -> list[int]:
        handles: list[int] = []
        try:
            target_pid = int(pid)
        except Exception:
            target_pid = 0
        if target_pid <= 0:
            return handles

        def _collect(hwnd, _lparam):
            try:
                _, wnd_pid = self.__lib.win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return True
            if int(wnd_pid) != target_pid:
                return True
            try:
                parent = self.__lib.win32gui.GetParent(hwnd)
                if parent:
                    return True
            except Exception:
                pass
            handles.append(int(hwnd))
            return True

        try:
            self.__lib.win32gui.EnumWindows(_collect, 0)
        except Exception:
            return handles
        return handles

    def __list_profile_chrome_pids(self) -> list[int]:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return []
        pids: list[int] = []
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["pid", "name", "cmdline"])
        except Exception:
            return pids
        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if f"--user-data-dir={target_profile}" not in cmd_text:
                    continue
                pid = int((info or {}).get("pid") or 0)
                if pid > 0:
                    pids.append(pid)
            except Exception:
                continue
        seen: set[int] = set()
        ordered: list[int] = []
        for pid in pids:
            if pid in seen:
                continue
            seen.add(pid)
            ordered.append(pid)
        return ordered

    def __is_profile_locked_without_remote_debugging(self) -> bool:
        if self.__root is None:
            return False
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return False
        owned_pids: set[int] = set()
        proc = self.__hidden_cdp_proc
        if proc is not None:
            for attr in ("pid", "_ws_listener_pid"):
                try:
                    candidate = int(getattr(proc, attr, 0) or 0)
                except Exception:
                    candidate = 0
                if candidate > 0:
                    owned_pids.add(candidate)
        try:
            owned_port = int(self.__hidden_cdp_port or 0)
        except Exception:
            owned_port = 0
        if owned_port > 0:
            pid_from_port = self.__find_profile_remote_debugging_pid(owned_port)
            if pid_from_port > 0:
                owned_pids.add(int(pid_from_port))
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["pid", "name", "cmdline"])
        except Exception:
            return False
        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                pid = int((info or {}).get("pid") or 0)
                if pid > 0 and pid in owned_pids:
                    continue
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if f"--user-data-dir={target_profile}" not in cmd_text:
                    continue
                if "--type=" in cmd_text:
                    # Renderer/GPU helper processes inherit user-data-dir
                    # but should not be treated as profile lock owners.
                    continue
                if "--remote-debugging-pipe" in cmd_text:
                    continue
                return True
            except Exception:
                continue
        return False

    def __find_profile_remote_debugging_pid(self, port: int) -> int:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return 0
        try:
            target_port = int(port)
        except Exception:
            return 0
        if target_port <= 0:
            return 0

        port_token = f"--remote-debugging-port={target_port}"
        profile_token = f"--user-data-dir={target_profile}"
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["pid", "name", "cmdline"])
        except Exception:
            return 0
        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if port_token not in cmd_text:
                    continue
                if profile_token not in cmd_text:
                    continue
                pid = int((info or {}).get("pid") or 0)
                if pid > 0:
                    return pid
            except Exception:
                continue
        return 0

    def __resolve_chrome_executable_path(self) -> str:
        candidates = [
            self.__lib.os.path.join(
                self.__lib.os.getenv("PROGRAMFILES", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            self.__lib.os.path.join(
                self.__lib.os.getenv("PROGRAMFILES(X86)", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            self.__lib.os.path.join(
                self.__lib.os.getenv("LOCALAPPDATA", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
        ]
        for candidate in candidates:
            path = str(candidate or "").strip()
            if not path:
                continue
            try:
                if self.__lib.os.path.isfile(path):
                    return path
            except Exception:
                continue
        return ""

    def __terminate_spawned_process(self, proc, cleanup_orphans: bool = True) -> None:
        spawned_pid = 0
        listener_pid = 0
        try:
            spawned_pid = int(getattr(proc, "pid", 0) or 0)
        except Exception:
            spawned_pid = 0
        try:
            listener_pid = int(getattr(proc, "_ws_listener_pid", 0) or 0)
        except Exception:
            listener_pid = 0
        # 1) Stop the direct spawned process (if still alive).
        if proc is not None:
            running = False
            try:
                running = proc.poll() is None
            except Exception:
                running = False
            if running:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=6.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass

        # 2) When CDP listener was remapped to another pid, terminate that
        # listener explicitly to avoid leaving detached blank windows behind.
        if listener_pid > 0 and listener_pid != spawned_pid:
            self.__terminate_pid_tree(listener_pid)

        # 3) Cleanup orphaned Chrome instances attached to this monitor profile.
        # Some Chrome launches can detach from the original parent process,
        # leaving visible windows even after the collected snapshot returns.
        if bool(cleanup_orphans):
            self.__terminate_profile_remote_debugging_processes()
        return

    def __terminate_pid_tree(self, pid: int) -> None:
        try:
            target = self.__lib.psutil.Process(int(pid))
        except Exception:
            return
        try:
            children = target.children(recursive=True)
        except Exception:
            children = []
        for child in children:
            try:
                child.terminate()
            except Exception:
                continue
        try:
            target.terminate()
        except Exception:
            return
        try:
            target.wait(timeout=2.0)
        except Exception:
            try:
                target.kill()
            except Exception:
                pass
        return

    def __terminate_profile_remote_debugging_processes(self) -> None:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return

        to_kill: list[Any] = []
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["name", "cmdline"])
        except Exception:
            return

        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if f"--user-data-dir={target_profile}" not in cmd_text:
                    continue
                if "--remote-debugging-port=" not in cmd_text:
                    continue
                to_kill.append(item)
            except Exception:
                continue

        for item in to_kill:
            try:
                children = item.children(recursive=True)
            except Exception:
                children = []
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    continue
            try:
                item.terminate()
            except Exception:
                continue

        for item in to_kill:
            try:
                item.wait(timeout=2.0)
            except Exception:
                try:
                    item.kill()
                except Exception:
                    continue

    def __terminate_profile_chrome_processes(self) -> None:
        pids = self.__list_profile_chrome_pids()
        if not pids:
            return
        for pid in pids:
            try:
                proc = self.__lib.psutil.Process(int(pid))
            except Exception:
                continue
            try:
                children = proc.children(recursive=True)
            except Exception:
                children = []
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    continue
            try:
                proc.terminate()
            except Exception:
                continue
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def __apply_headless_fast_routes(self, context) -> None:
        try:
            blockers = (
                "google-analytics",
                "googletagmanager",
                "doubleclick",
                "hotjar",
                "segment.io",
                "sentry.io",
                "intercom",
            )

            def _route_handler(route, request):
                try:
                    rtype = str(getattr(request, "resource_type", "") or "").lower()
                except Exception:
                    rtype = ""
                if rtype in {"image", "font", "media"}:
                    try:
                        route.abort()
                    except Exception:
                        try:
                            route.continue_()
                        except Exception:
                            pass
                    return
                url = ""
                try:
                    url = str(getattr(request, "url", "") or "").lower()
                except Exception:
                    url = ""
                if url and any(token in url for token in blockers):
                    try:
                        route.abort()
                    except Exception:
                        try:
                            route.continue_()
                        except Exception:
                            pass
                    return
                try:
                    route.continue_()
                except Exception:
                    return

            context.route("**/*", _route_handler)
        except Exception:
            return
        return

    def __launch_browser_context(
        self,
        playwright_obj,
        headless: bool,
        prefer_system_channel: bool = False,
    ):
        channels: list[str | None] = [None]
        if bool(prefer_system_channel):
            if bool(headless):
                channels = ["chrome", None]
            else:
                channels = ["chrome"]
        last_error = None
        for channel in channels:
            # Keep browser sandbox enabled to avoid auth instability
            # from unsupported --no-sandbox launches.
            kwargs = {
                "headless": bool(headless),
                "chromium_sandbox": True,
            }
            if channel:
                kwargs["channel"] = channel
            if not bool(headless):
                kwargs["no_viewport"] = True
            try:
                return playwright_obj.chromium.launch_persistent_context(
                    self.__profile_dir,
                    **kwargs,
                )
            except Exception as exc:
                last_error = exc
                try:
                    self.__log(
                        f"launch context failed channel={channel or 'bundled'} err={exc!r}"
                    )
                except Exception:
                    pass
                continue
        if last_error is not None:
            self.__log_exception("all browser launch attempts failed", last_error)
        return None

    def __wait_until_logged_in(self, page, timeout_sec: float) -> bool:
        deadline = 0.0
        try:
            deadline = float(self.__lib.time.monotonic()) + float(timeout_sec)
        except Exception:
            deadline = 0.0
        attempted_open = False
        stagnant_count = 0
        last_url = ""
        while True:
            if self.__is_collect_cancel_requested():
                return False
            if self.__is_cloudflare_challenge(page):
                now_cf = 0.0
                try:
                    now_cf = float(self.__lib.time.monotonic())
                except Exception:
                    now_cf = deadline + 1.0
                remain = max(5.0, float(deadline) - float(now_cf))
                if not self.__wait_until_cloudflare_cleared(page, timeout_sec=min(60.0, remain)):
                    return False

            if not self.__is_login_required(page):
                self.__set_session_state("logged_in")
                return True

            did_open = self.__try_open_login_entry(
                page,
                force=(not attempted_open or stagnant_count >= 2),
            )
            if did_open:
                attempted_open = True

            current_url = ""
            try:
                current_url = str(page.url or "")
            except Exception:
                current_url = ""
            if current_url and current_url == last_url:
                stagnant_count += 1
            else:
                stagnant_count = 0
                last_url = current_url

            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now > deadline:
                self.__set_session_state("logged_out")
                return False
            try:
                page.wait_for_timeout(1000)
            except Exception:
                self.__set_session_state("logged_out")
                return False

    def __try_open_login_entry(self, page, force: bool = False) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        lowered = url.lower()
        if self.__is_auth_invalid_state(page):
            for selector in (
                "button:has-text('Try again')",
                "button:has-text('다시 시도')",
                "button[type='submit']",
            ):
                try:
                    locator = page.locator(selector)
                except Exception:
                    continue
                try:
                    if locator.count() <= 0:
                        continue
                except Exception:
                    continue
                try:
                    locator.first.click(timeout=1500)
                    self.__log(f"auth error recovery clicked selector={selector}")
                    return True
                except Exception:
                    continue
            if force:
                try:
                    page.goto(
                        str(self.__login_entry_url),
                        wait_until="domcontentloaded",
                        timeout=int(self.__navigation_timeout_ms),
                    )
                    self.__log("auth error recovery navigated login entry")
                    return True
                except Exception:
                    pass

        # Do not force-refresh login pages while the user is interacting
        # (e.g., Google OAuth), otherwise the auth flow keeps restarting.
        if (
            "/auth/login" in lowered
            or "/log-in" in lowered
            or "auth.openai.com" in lowered
        ):
            return False

        selectors = [
            "button:has-text('Log in')",
            "button:has-text('로그인')",
            "a:has-text('Log in')",
            "a:has-text('로그인')",
            "[data-testid*='login']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
            except Exception:
                continue
            try:
                if locator.count() <= 0:
                    continue
            except Exception:
                continue
            try:
                locator.first.click(timeout=1500)
                self.__log(f"login entry clicked selector={selector}")
                return True
            except Exception:
                continue

        if not force:
            return False

        for candidate in (
            str(self.__login_entry_url),
            "https://chatgpt.com/auth/login",
            "https://auth.openai.com/log-in-or-create-account",
        ):
            try:
                page.goto(
                    candidate,
                    wait_until="domcontentloaded",
                    timeout=int(self.__navigation_timeout_ms),
                )
                self.__log(f"login entry navigated url={candidate}")
                return True
            except Exception:
                continue
        return False

    def __is_auth_invalid_state(self, page) -> bool:
        body_text = ""
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            ).lower()
        except Exception:
            body_text = ""
        if not body_text:
            return False
        if "invalid_state" in body_text:
            return True
        if "route error" in body_text:
            return True
        if "invalid content type" in body_text:
            return True
        if "error occurred during authentication" in body_text:
            return True
        if "인증 중 오류" in body_text:
            return True
        return False

    def __is_cloudflare_challenge(self, page) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        lowered_url = url.lower()
        has_cloudflare_query_token = False
        if "challenges.cloudflare.com" in lowered_url:
            return True
        if "cdn-cgi/challenge" in lowered_url:
            return True
        if "__cf_chl_rt_tk=" in lowered_url:
            has_cloudflare_query_token = True
        elif "__cf_chl_" in lowered_url:
            has_cloudflare_query_token = True

        body_text = ""
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            ).lower()
        except Exception:
            body_text = ""
        has_usage_limit_metric = False
        if body_text:
            try:
                parsed = parse_usage_metrics_from_text(body_text)
                has_usage_limit_metric = any(
                    normalize_usage_value(parsed.get(k, ""))
                    for k in ("five_hour_limit", "weekly_limit", "code_review")
                )
            except Exception:
                has_usage_limit_metric = False
        if has_usage_limit_metric:
            return False
        if body_text:
            if "verify you are human" in body_text and "cloudflare" in body_text:
                return True
            if "checking your browser" in body_text and "cloudflare" in body_text:
                return True
        html_text = ""
        try:
            html_text = str(page.content() or "").lower()
        except Exception:
            html_text = ""
        if not html_text:
            return bool(has_cloudflare_query_token and not body_text)
        if "challenges.cloudflare.com" in html_text:
            return True
        if "cdn-cgi/challenge-platform" in html_text:
            return True
        if "cf-challenge" in html_text:
            return True
        if has_cloudflare_query_token and not body_text:
            return True
        return False

    def __wait_until_cloudflare_cleared(self, page, timeout_sec: float) -> bool:
        deadline = 0.0
        try:
            deadline = float(self.__lib.time.monotonic()) + float(timeout_sec)
        except Exception:
            deadline = 0.0
        while True:
            if self.__is_collect_cancel_requested():
                return False
            if not self.__is_cloudflare_challenge(page):
                return True
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now > deadline:
                return False
            try:
                page.wait_for_timeout(1500)
            except Exception:
                return False

    def __probe_usage_page(self, page) -> dict[str, Any]:
        try:
            payload = page.evaluate(USAGE_PAGE_PROBE_SCRIPT)
        except Exception:
            payload = {}
        return self.__normalize_probe_payload(payload, fallback_url=self.__get_page_url(page))

    def __is_usage_dom_ready_from_probe(self, probe: dict[str, Any] | None) -> bool:
        if not isinstance(probe, dict):
            return False
        main_text = normalize_usage_value(probe.get("mainText", ""))
        if not main_text:
            return False
        lowered = main_text.lower()
        if any(token in lowered for token in ("log in", "sign in", "로그인", "continue with google")):
            return False
        if isinstance(probe.get("metricBlocks"), list) and probe.get("metricBlocks"):
            return True
        readiness_markers = (
            "usage",
            "limit",
            "review",
            "credit",
            "사용",
            "한도",
            "검토",
            "크레딧",
        )
        return any(marker in lowered for marker in readiness_markers)

    def __normalize_probe_payload(
        self,
        payload: Any,
        fallback_url: str = "",
    ) -> dict[str, Any]:
        normalized_payload = payload if isinstance(payload, dict) else {}
        default_url = normalize_usage_value(fallback_url)
        normalized_payload.setdefault("url", default_url)
        normalized_payload["url"] = normalize_usage_value(
            normalized_payload.get("url", default_url)
        )
        normalized_payload["mainText"] = normalize_usage_value(
            normalized_payload.get("mainText", "")
        )
        metric_blocks = normalized_payload.get("metricBlocks", [])
        if not isinstance(metric_blocks, list):
            metric_blocks = []
        normalized_payload["metricBlocks"] = metric_blocks
        return normalized_payload

    def __build_snapshot_from_probe(self, probe: dict[str, Any] | None) -> UsageSnapshot | None:
        normalized_probe = self.__normalize_probe_payload(
            probe,
            fallback_url=str(self.__usage_url),
        )
        page_url = normalize_usage_value(normalized_probe.get("url", ""))
        if not self.__is_usage_page_url(page_url):
            return None
        if not self.__is_usage_dom_ready_from_probe(normalized_probe):
            return None
        captured_at = self.__now_iso()
        metrics = extract_usage_metrics_from_semantic_blocks(
            normalized_probe.get("metricBlocks", [])
        )
        if not metrics:
            return None
        limit_keys = ("five_hour_limit", "weekly_limit", "code_review")
        has_limit_metric = any(normalize_usage_value(metrics.get(k, "")) for k in limit_keys)
        if not has_limit_metric:
            return None
        snapshot = UsageSnapshot.from_metrics(metrics, captured_at=captured_at)
        if not snapshot.has_any_metric():
            return None
        return snapshot

    def __is_login_required(self, page) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        lowered = url.lower()
        if any(token in lowered for token in ("login", "signin", "auth")):
            return True
        try:
            if page.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            ).lower()
        except Exception:
            body_text = ""
        if not body_text:
            return False
        markers = (
            "log in",
            "sign in",
            "sign up",
            "로그인",
            "회원가입",
            "continue with google",
            "continue with email",
        )
        return any(marker in body_text for marker in markers)

    def __configure_playwright_env(self) -> None:
        try:
            is_frozen = bool(getattr(self.__lib.sys, "frozen", False))
        except Exception:
            is_frozen = False
        try:
            if is_frozen:
                self.__lib.os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
            else:
                self.__lib.os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        except Exception:
            return
        try:
            raw = str(self.__lib.os.environ.get("NODE_OPTIONS", "") or "").strip()
            tokens = [token for token in raw.split(" ") if token]
            if "--no-deprecation" not in tokens:
                tokens.append("--no-deprecation")
                self.__lib.os.environ["NODE_OPTIONS"] = " ".join(tokens).strip()
        except Exception:
            pass
        return

    def __ensure_playwright_available(self) -> bool:
        if self.__playwright_checked:
            return bool(self.__playwright_available)
        self.__playwright_checked = True
        self.__configure_playwright_env()
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401

            self.__playwright_available = True
            return True
        except Exception as exc:
            self.__playwright_available = False
            self.__log_exception("playwright import failed", exc)
            return False

    def __load_settings(self) -> None:
        self.__force_playwright_mode()
        data = self.__read_json_file(self.__settings_path)
        if not isinstance(data, dict):
            data = {}
        dirty = False
        try:
            self.__enabled = bool(data.get("enabled", self.__enabled))
        except Exception:
            self.__enabled = True
        try:
            interval = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            interval = self.__interval_sec
        min_interval = float(getattr(self, "_CodexUsageMonitor__min_interval_sec", 10.0) or 10.0)
        if interval < min_interval:
            interval = min_interval
            dirty = True
        self.__interval_sec = float(interval)
        try:
            tooltip = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            tooltip = self.__tooltip_duration_ms
        if tooltip < 1200:
            tooltip = 1200
            dirty = True
        self.__tooltip_duration_ms = int(tooltip)
        usage_url = normalize_usage_value(data.get("usage_url", self.__usage_url))
        if usage_url:
            canonical_usage_url = canonicalize_codex_usage_url(usage_url)
            if canonical_usage_url != usage_url:
                dirty = True
            self.__set_usage_url(canonical_usage_url)
        if bool(dirty):
            self.__save_settings()
        return

    def __save_settings(self) -> None:
        payload = {
            "settings_version": int(self.__settings_version),
            "enabled": bool(self.__enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
        }
        self.__write_json_file(self.__settings_path, payload)
        return

    def __load_state(self) -> None:
        data = self.__read_json_file(self.__state_path)
        if not isinstance(data, dict):
            self.__last_snapshot = UsageSnapshot()
            return
        snap = UsageSnapshot.from_dict(data.get("last_snapshot"))
        self.__last_snapshot = snap
        return

    def __save_state(self) -> None:
        payload = {
            "last_snapshot": self.__last_snapshot.to_dict(),
        }
        self.__write_json_file(self.__state_path, payload)
        return

    def __read_json_file(self, path: str) -> dict | None:
        if not path:
            return None
        try:
            if not self.__lib.os.path.isfile(path):
                return None
        except Exception:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = fp.read()
        except Exception:
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def __write_json_file(self, path: str, payload: dict) -> None:
        if not path:
            return
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
        except Exception:
            pass
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.__log_exception("json write failed", exc)
        return

    def __now_iso(self) -> str:
        try:
            utc_now = self.__lib.datetime.now(timezone.utc)
            local_now = utc_now.astimezone(self.__korea_tz)
            return str(local_now.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            return ""

    def __log(self, message: str) -> None:
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
        except Exception:
            return
        ts = self.__now_iso() or "time"
        line = f"[{ts}] {str(message)}\n"
        try:
            with open(self.__log_path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            return

    def __log_exception(self, title: str, exc: Exception) -> None:
        try:
            self.__log(f"{title}: {exc!r}")
            tb = traceback.format_exc()
            if tb:
                self.__log(tb.strip())
        except Exception:
            return

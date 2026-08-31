"""Privacy-safe Google Calendar OAuth and read-only vacation provider.

This module intentionally uses only the Python standard library.  Public
operations return closed result objects so credentials, authorization payloads,
and provider responses never appear in exceptions or result representations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic
from typing import Generic, TypeAlias, TypeVar, final

from src.apps.wrike_ical import VACATION_CALENDAR_SCHEMA_VERSION

GOOGLE_CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

_AUTH_ENDPOINTS = frozenset(
    {
        "https://accounts.google.com/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/v2/auth",
    }
)
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_CALENDAR_LIST_ENDPOINT = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
_EVENTS_ENDPOINT_PREFIX = "https://www.googleapis.com/calendar/v3/calendars/"
_CLIENT_CONFIG_LIMIT = 64 * 1024
_ENVELOPE_LIMIT = 64 * 1024
_TOKEN_RESPONSE_LIMIT = 128 * 1024
_API_RESPONSE_LIMIT = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_AUTH_TIMEOUT_SEC = 600.0
_MAX_API_TIMEOUT_SEC = 120.0
_MAX_PAGES = 100
_MAX_EVENTS = 10_000
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GoogleCalendarErrorCode(str, Enum):
    """Stable failures that contain no provider payload or secret."""

    CLIENT_CONFIG_INVALID = "client_config_invalid"
    BROWSER_LAUNCH_FAILED = "browser_launch_failed"
    CALLBACK_TIMEOUT = "callback_timeout"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_CANCELLED = "authorization_cancelled"
    STATE_MISMATCH = "state_mismatch"
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    TOKEN_REFRESH_FAILED = "token_refresh_failed"
    TOKEN_REVOCATION_FAILED = "token_revocation_failed"
    API_UNAUTHORIZED = "api_unauthorized"
    API_FORBIDDEN = "api_forbidden"
    API_RATE_LIMITED = "api_rate_limited"
    API_UNAVAILABLE = "api_unavailable"
    INVALID_RESPONSE = "invalid_response"
    CALENDAR_NOT_FOUND = "calendar_not_found"
    CALENDAR_AMBIGUOUS = "calendar_ambiguous"

    def __str__(self) -> str:
        return self.value


@final
@dataclass(frozen=True, slots=True)
class DesktopClientConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(default="", repr=False)


@final
@dataclass(frozen=True, slots=True)
class OAuthEnvelope:
    client_id: str = field(repr=False)
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    calendar_id: str = field(default="", repr=False)


_ResultValue = TypeVar("_ResultValue")


@final
@dataclass(frozen=True, slots=True)
class GoogleCalendarSuccess(Generic[_ResultValue]):
    value: _ResultValue = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class GoogleCalendarError:
    code: GoogleCalendarErrorCode


GoogleCalendarResult: TypeAlias = GoogleCalendarSuccess[_ResultValue] | GoogleCalendarError


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _strict_json_loads(text: str):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError

    return json.loads(
        text,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def _bounded_timeout(value, default: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not (parsed > 0.0):
        parsed = default
    return min(parsed, upper)


def _valid_secret_string(value, *, optional: bool = False, maximum: int = 8192) -> bool:
    if type(value) is not str:
        return False
    if not value:
        return optional
    if len(value) > maximum or value != value.strip():
        return False
    return not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _valid_client_id(value) -> bool:
    return (
        _valid_secret_string(value, maximum=2048)
        and value.endswith(".apps.googleusercontent.com")
    )


def _valid_loopback_redirect(value) -> bool:
    if type(value) is not str or not value or len(value) > 4096:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = str(parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme.lower() != "http" or not hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.query or parsed.fragment or port == 0:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def load_desktop_client_config(path) -> GoogleCalendarResult[DesktopClientConfig]:
    """Load and validate a bounded Google downloaded desktop client file."""

    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_CLIENT_CONFIG_LIMIT + 1)
    except (OSError, TypeError, ValueError):
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)
    if not raw or len(raw) > _CLIENT_CONFIG_LIMIT:
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)
    try:
        document = _strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)
    if type(document) is not dict or set(document) != {"installed"}:
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)
    installed = document.get("installed")
    if type(installed) is not dict:
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)

    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret", "")
    auth_uri = installed.get("auth_uri")
    token_uri = installed.get("token_uri")
    redirect_uris = installed.get("redirect_uris")
    valid = (
        _valid_client_id(client_id)
        and _valid_secret_string(client_secret, optional=True, maximum=4096)
        and type(auth_uri) is str
        and auth_uri in _AUTH_ENDPOINTS
        and type(token_uri) is str
        and token_uri == _TOKEN_ENDPOINT
        and type(redirect_uris) is list
        and 0 < len(redirect_uris) <= 16
        and all(_valid_loopback_redirect(uri) for uri in redirect_uris)
    )
    if not valid:
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)
    return GoogleCalendarSuccess(DesktopClientConfig(client_id, client_secret))


def _response_status(response) -> int | None:
    try:
        status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else None
        return int(status) if status is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _response_content_length(response) -> int | None:
    try:
        raw = response.headers.get("Content-Length")
        return int(raw) if raw is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _response_media_type(response) -> str | None:
    try:
        raw = response.headers.get("Content-Type")
    except (AttributeError, TypeError, ValueError):
        return ""
    if raw is None:
        return ""
    if type(raw) is not str:
        return None
    return raw.split(";", 1)[0].strip().lower()


def _close_response(response) -> None:
    try:
        response.close()
    except Exception:
        pass


def _open_fixed(request, timeout: float, urlopen):
    if urlopen is urllib.request.urlopen:
        return _NO_REDIRECT_OPENER.open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def _api_error_for_status(status: int | None) -> GoogleCalendarErrorCode:
    if status == 401:
        return GoogleCalendarErrorCode.API_UNAUTHORIZED
    if status == 403:
        return GoogleCalendarErrorCode.API_FORBIDDEN
    if status == 429:
        return GoogleCalendarErrorCode.API_RATE_LIMITED
    if status is not None and (status >= 500 or 300 <= status < 400):
        return GoogleCalendarErrorCode.API_UNAVAILABLE
    return GoogleCalendarErrorCode.INVALID_RESPONSE


def _read_json_response(response, expected_url: str, limit: int):
    try:
        geturl = getattr(response, "geturl", None)
        final_url = geturl() if callable(geturl) else None
    except Exception:
        final_url = None
    if final_url != expected_url:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    status = _response_status(response)
    if status is not None and not 200 <= status < 300:
        return GoogleCalendarError(_api_error_for_status(status))
    declared = _response_content_length(response)
    if declared is not None and (declared < 0 or declared > limit):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    media_type = _response_media_type(response)
    if media_type not in {"", "application/json"} and not (
        isinstance(media_type, str) and media_type.endswith("+json")
    ):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)

    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            chunk = response.read(min(_READ_CHUNK_BYTES, limit - size + 1))
        except Exception:
            return GoogleCalendarError(GoogleCalendarErrorCode.API_UNAVAILABLE)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        piece = bytes(chunk)
        if not piece:
            break
        size += len(piece)
        if size > limit:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        chunks.append(piece)
    if size == 0:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    try:
        document = _strict_json_loads(b"".join(chunks).decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    if type(document) is not dict:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    return GoogleCalendarSuccess((document, size))


def _request_json(
    request: urllib.request.Request,
    *,
    timeout: float,
    limit: int,
    urlopen,
):
    expected_url = request.full_url
    response = None
    try:
        response = _open_fixed(request, timeout, urlopen)
        return _read_json_response(response, expected_url, limit)
    except urllib.error.HTTPError as error:
        return GoogleCalendarError(_api_error_for_status(_response_status(error)))
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        return GoogleCalendarError(GoogleCalendarErrorCode.API_UNAVAILABLE)
    except Exception:
        return GoogleCalendarError(GoogleCalendarErrorCode.API_UNAVAILABLE)
    finally:
        if response is not None:
            _close_response(response)


def _token_request(form: dict[str, str], timeout: float, urlopen):
    request = urllib.request.Request(
        _TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "windows-supporter/google-calendar-oauth",
        },
        method="POST",
    )
    return _request_json(
        request,
        timeout=timeout,
        limit=_TOKEN_RESPONSE_LIMIT,
        urlopen=urlopen,
    )


def _authorization_url(config: DesktopClientConfig, redirect_uri: str, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    query = urllib.parse.urlencode(
        {
            "access_type": "offline",
            "client_id": config.client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_CALENDAR_READONLY_SCOPE,
            "state": state,
        }
    )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + query


def _write_browser_response(handler: BaseHTTPRequestHandler, success: bool) -> None:
    body = (
        b"Authorization completed. You may close this window."
        if success
        else b"Authorization failed. You may close this window."
    )
    try:
        handler.send_response(200 if success else 400)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        pass


def _make_callback_handler(callback_path: str, expected_state: str, captured: dict):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                parsed = urllib.parse.urlsplit(self.path)
            except Exception:
                parsed = None
            if parsed is None or parsed.path != callback_path or parsed.fragment:
                _write_browser_response(self, False)
                return
            try:
                values = urllib.parse.parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=16,
                )
            except (ValueError, TypeError):
                values = {}
            state_values = values.get("state", [])
            if (
                len(state_values) != 1
                or not hmac.compare_digest(state_values[0], expected_state)
            ):
                captured["error"] = GoogleCalendarErrorCode.STATE_MISMATCH
                _write_browser_response(self, False)
                return
            error_values = values.get("error", [])
            code_values = values.get("code", [])
            if len(error_values) == 1 and error_values[0] and not code_values:
                captured["error"] = GoogleCalendarErrorCode.AUTHORIZATION_DENIED
                _write_browser_response(self, False)
                return
            if (
                len(code_values) != 1
                or not code_values[0]
                or len(code_values[0]) > 8192
                or error_values
            ):
                captured["error"] = GoogleCalendarErrorCode.INVALID_RESPONSE
                _write_browser_response(self, False)
                return
            captured["code"] = code_values[0]
            _write_browser_response(self, True)

        def do_POST(self):
            _write_browser_response(self, False)

        def log_message(self, format, *args):
            return

    return CallbackHandler


def _api_get_json(url: str, access_token: str, timeout: float, remaining: int, urlopen):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + access_token,
            "User-Agent": "windows-supporter/google-calendar-readonly",
        },
        method="GET",
    )
    return _request_json(
        request,
        timeout=timeout,
        limit=min(_API_RESPONSE_LIMIT, remaining),
        urlopen=urlopen,
    )


def _select_calendar(access_token: str, expected_name: str, timeout: float, urlopen):
    matches: set[str] = set()
    seen_tokens: set[str] = set()
    next_token = ""
    remaining = _API_RESPONSE_LIMIT
    for _page in range(_MAX_PAGES):
        params = {
            "fields": "items(id,summary),nextPageToken",
            "maxResults": "250",
        }
        if next_token:
            params["pageToken"] = next_token
        url = _CALENDAR_LIST_ENDPOINT + "?" + urllib.parse.urlencode(params)
        response = _api_get_json(url, access_token, timeout, remaining, urlopen)
        if isinstance(response, GoogleCalendarError):
            return response
        document, consumed = response.value
        remaining -= consumed
        items = document.get("items", [])
        if type(items) is not list:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        for item in items:
            if type(item) is not dict:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
            calendar_id = item.get("id")
            summary = item.get("summary")
            if type(calendar_id) is not str or type(summary) is not str:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
            if summary == expected_name:
                if not _valid_secret_string(calendar_id, maximum=4096):
                    return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
                matches.add(calendar_id)
        raw_next = document.get("nextPageToken", "")
        if type(raw_next) is not str or len(raw_next) > 4096:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        next_token = raw_next
        if not next_token:
            break
        if next_token in seen_tokens or remaining <= 0:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        seen_tokens.add(next_token)
    else:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)

    if not matches:
        return GoogleCalendarError(GoogleCalendarErrorCode.CALENDAR_NOT_FOUND)
    if len(matches) != 1:
        return GoogleCalendarError(GoogleCalendarErrorCode.CALENDAR_AMBIGUOUS)
    return GoogleCalendarSuccess(next(iter(matches)))


def _is_cancelled(cancel_event) -> bool:
    if cancel_event is None:
        return False
    try:
        return bool(cancel_event.is_set())
    except Exception:
        return True


def revoke_refresh_token(
    envelope: OAuthEnvelope,
    timeout_sec=15,
    urlopen=urllib.request.urlopen,
) -> GoogleCalendarResult[None]:
    """Revoke one refresh token without exposing it in results or redirects."""

    if not _valid_envelope(envelope):
        return GoogleCalendarError(
            GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
        )
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/revoke",
        data=urllib.parse.urlencode(
            {"token": envelope.refresh_token}
        ).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "windows-supporter/google-calendar-oauth",
        },
        method="POST",
    )
    response = None
    try:
        response = _open_fixed(
            request,
            _bounded_timeout(timeout_sec, 15.0, _MAX_API_TIMEOUT_SEC),
            urlopen,
        )
        try:
            final_url = response.geturl()
        except Exception:
            final_url = None
        status = _response_status(response)
        if final_url != request.full_url or status != 200:
            return GoogleCalendarError(
                GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
            )
        declared = _response_content_length(response)
        if declared is not None and (declared < 0 or declared > 4096):
            return GoogleCalendarError(
                GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
            )
        try:
            body = response.read(4097)
        except Exception:
            return GoogleCalendarError(
                GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
            )
        if not isinstance(body, (bytes, bytearray, memoryview)) or len(body) > 4096:
            return GoogleCalendarError(
                GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
            )
        return GoogleCalendarSuccess(None)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            socket.timeout, ConnectionError, OSError):
        return GoogleCalendarError(
            GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
        )
    except Exception:
        return GoogleCalendarError(
            GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
        )
    finally:
        if response is not None:
            _close_response(response)


def authorize_desktop(
    config: DesktopClientConfig,
    expected_calendar_name,
    timeout_sec=180,
    browser_opener=webbrowser.open,
    urlopen=urllib.request.urlopen,
    cancel_event=None,
) -> GoogleCalendarResult[OAuthEnvelope]:
    """Authorize through a one-shot loopback callback and select one calendar."""

    if (
        not isinstance(config, DesktopClientConfig)
        or not _valid_client_id(config.client_id)
        or not _valid_secret_string(config.client_secret, optional=True, maximum=4096)
        or type(expected_calendar_name) is not str
        or not expected_calendar_name
        or len(expected_calendar_name) > 1024
    ):
        return GoogleCalendarError(GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID)

    if _is_cancelled(cancel_event):
        return GoogleCalendarError(
            GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
        )

    callback_path = "/oauth2/callback/" + secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    captured: dict[str, object] = {}
    handler = _make_callback_handler(callback_path, state, captured)
    server = None
    try:
        server = HTTPServer(("127.0.0.1", 0), handler)
        server.timeout = 0.25
        redirect_uri = f"http://127.0.0.1:{server.server_port}{callback_path}"
        authorization_url = _authorization_url(config, redirect_uri, state, verifier)
        try:
            opened = browser_opener(authorization_url)
        except Exception:
            opened = False
        if opened is False:
            return GoogleCalendarError(GoogleCalendarErrorCode.BROWSER_LAUNCH_FAILED)

        timeout = _bounded_timeout(timeout_sec, 180.0, _MAX_AUTH_TIMEOUT_SEC)
        deadline = monotonic() + timeout
        while (
            monotonic() < deadline
            and not captured
            and not _is_cancelled(cancel_event)
        ):
            server.timeout = min(0.25, max(0.01, deadline - monotonic()))
            try:
                server.handle_request()
            except (OSError, socket.error):
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        if _is_cancelled(cancel_event):
            return GoogleCalendarError(
                GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
            )
        if not captured:
            return GoogleCalendarError(GoogleCalendarErrorCode.CALLBACK_TIMEOUT)
        callback_error = captured.get("error")
        if isinstance(callback_error, GoogleCalendarErrorCode):
            return GoogleCalendarError(callback_error)
        code = captured.get("code")
        if type(code) is not str or not code:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    except (OSError, ValueError):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass

    token_form = {
        "client_id": config.client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if config.client_secret:
        token_form["client_secret"] = config.client_secret
    if _is_cancelled(cancel_event):
        return GoogleCalendarError(
            GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
        )
    token_result = _token_request(
        token_form,
        _bounded_timeout(timeout_sec, 15.0, _MAX_API_TIMEOUT_SEC),
        urlopen,
    )
    if isinstance(token_result, GoogleCalendarError):
        return GoogleCalendarError(GoogleCalendarErrorCode.TOKEN_EXCHANGE_FAILED)
    token_document, _consumed = token_result.value
    access_token = token_document.get("access_token")
    refresh_token = token_document.get("refresh_token")
    if not _valid_secret_string(access_token) or not _valid_secret_string(refresh_token):
        return GoogleCalendarError(GoogleCalendarErrorCode.TOKEN_EXCHANGE_FAILED)

    issued_envelope = OAuthEnvelope(
        client_id=config.client_id,
        client_secret=config.client_secret,
        refresh_token=refresh_token,
        calendar_id="pending",
    )
    if _is_cancelled(cancel_event):
        revoke_result = revoke_refresh_token(
            issued_envelope,
            timeout_sec=timeout_sec,
            urlopen=urlopen,
        )
        if isinstance(revoke_result, GoogleCalendarError):
            return revoke_result
        return GoogleCalendarError(
            GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
        )

    calendar_result = _select_calendar(
        access_token,
        expected_calendar_name,
        _bounded_timeout(timeout_sec, 15.0, _MAX_API_TIMEOUT_SEC),
        urlopen,
    )
    if isinstance(calendar_result, GoogleCalendarError):
        revoke_result = revoke_refresh_token(
            issued_envelope,
            timeout_sec=timeout_sec,
            urlopen=urlopen,
        )
        if isinstance(revoke_result, GoogleCalendarError):
            return revoke_result
        if _is_cancelled(cancel_event):
            return GoogleCalendarError(
                GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
            )
        return calendar_result
    envelope = OAuthEnvelope(
        client_id=config.client_id,
        client_secret=config.client_secret,
        refresh_token=refresh_token,
        calendar_id=calendar_result.value,
    )
    if _is_cancelled(cancel_event):
        revoke_result = revoke_refresh_token(
            envelope,
            timeout_sec=timeout_sec,
            urlopen=urlopen,
        )
        if isinstance(revoke_result, GoogleCalendarError):
            return revoke_result
        return GoogleCalendarError(
            GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
        )
    return GoogleCalendarSuccess(envelope)


def _refresh_access_token(envelope: OAuthEnvelope, timeout: float, urlopen):
    form = {
        "client_id": envelope.client_id,
        "grant_type": "refresh_token",
        "refresh_token": envelope.refresh_token,
    }
    if envelope.client_secret:
        form["client_secret"] = envelope.client_secret
    result = _token_request(form, timeout, urlopen)
    if isinstance(result, GoogleCalendarError):
        return GoogleCalendarError(GoogleCalendarErrorCode.TOKEN_REFRESH_FAILED)
    document, _consumed = result.value
    access_token = document.get("access_token")
    if not _valid_secret_string(access_token):
        return GoogleCalendarError(GoogleCalendarErrorCode.TOKEN_REFRESH_FAILED)
    return GoogleCalendarSuccess(access_token)


def _parse_google_time(start, end):
    if type(start) is not dict or type(end) is not dict:
        return None
    start_keys = {key for key in ("date", "dateTime") if key in start}
    end_keys = {key for key in ("date", "dateTime") if key in end}
    if start_keys == {"date"} and end_keys == {"date"}:
        raw_start = start.get("date")
        raw_end = end.get("date")
        if (
            type(raw_start) is not str
            or type(raw_end) is not str
            or not _DATE_RE.fullmatch(raw_start)
            or not _DATE_RE.fullmatch(raw_end)
        ):
            return None
        try:
            start_day = date.fromisoformat(raw_start)
            end_day = date.fromisoformat(raw_end)
        except ValueError:
            return None
        start_dt = datetime.combine(start_day, time.min)
        end_dt = datetime.combine(end_day, time.min)
        if end_dt <= start_dt:
            return None
        return start_dt, end_dt, True
    if start_keys != {"dateTime"} or end_keys != {"dateTime"}:
        return None
    raw_start = start.get("dateTime")
    raw_end = end.get("dateTime")
    if (
        type(raw_start) is not str
        or type(raw_end) is not str
        or not _RFC3339_RE.fullmatch(raw_start)
        or not _RFC3339_RE.fullmatch(raw_end)
    ):
        return None
    try:
        aware_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        aware_end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
        if aware_start.tzinfo is None or aware_end.tzinfo is None:
            return None
        start_dt = aware_start.astimezone().replace(tzinfo=None)
        end_dt = aware_end.astimezone().replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return None
    if end_dt <= start_dt:
        return None
    return start_dt, end_dt, False


def _normalized_vacation_match(summary: str) -> bool:
    normalized = unicodedata.normalize("NFKC", summary)
    return "휴가" in " ".join(normalized.split())


def _week_bounds(week_start):
    try:
        if isinstance(week_start, datetime):
            if week_start.tzinfo is not None:
                start_day = week_start.astimezone().date()
            else:
                start_day = week_start.date()
        elif isinstance(week_start, date):
            start_day = week_start
        else:
            return None
        local_start = datetime.combine(start_day, time.min).astimezone()
        local_end = datetime.combine(start_day + timedelta(days=7), time.min).astimezone()
        return local_start.isoformat(timespec="seconds"), local_end.isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def fetch_vacation_calendar(
    envelope: OAuthEnvelope,
    week_start,
    timeout_sec=15,
    urlopen=urllib.request.urlopen,
) -> GoogleCalendarResult[dict]:
    """Fetch one local week and return only compiled calculation data."""

    if (
        not isinstance(envelope, OAuthEnvelope)
        or not _valid_client_id(envelope.client_id)
        or not _valid_secret_string(envelope.client_secret, optional=True, maximum=4096)
        or not _valid_secret_string(envelope.refresh_token)
        or not _valid_secret_string(envelope.calendar_id, maximum=4096)
    ):
        return GoogleCalendarError(GoogleCalendarErrorCode.TOKEN_REFRESH_FAILED)
    bounds = _week_bounds(week_start)
    if bounds is None:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    time_min, time_max = bounds
    timeout = _bounded_timeout(timeout_sec, 15.0, _MAX_API_TIMEOUT_SEC)
    access_result = _refresh_access_token(envelope, timeout, urlopen)
    if isinstance(access_result, GoogleCalendarError):
        return access_result
    access_token = access_result.value

    endpoint = _EVENTS_ENDPOINT_PREFIX + urllib.parse.quote(
        envelope.calendar_id,
        safe="",
    ) + "/events"
    events: list[dict] = []
    seen_tokens: set[str] = set()
    next_token = ""
    remaining = _API_RESPONSE_LIMIT
    for _page in range(_MAX_PAGES):
        params = {
            "fields": "items(end,start,status,summary),nextPageToken",
            "maxResults": "2500",
            "showDeleted": "false",
            "singleEvents": "true",
            "timeMax": time_max,
            "timeMin": time_min,
        }
        if next_token:
            params["pageToken"] = next_token
        url = endpoint + "?" + urllib.parse.urlencode(params)
        response = _api_get_json(url, access_token, timeout, remaining, urlopen)
        if isinstance(response, GoogleCalendarError):
            return response
        document, consumed = response.value
        remaining -= consumed
        items = document.get("items", [])
        if type(items) is not list:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        for item in items:
            if type(item) is not dict:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
            status = item.get("status", "confirmed")
            if type(status) is not str:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
            if status == "cancelled":
                continue
            summary = item.get("summary", "")
            if type(summary) is not str or len(summary) > 32_768:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
            parsed_time = _parse_google_time(item.get("start"), item.get("end"))
            if parsed_time is None:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
            start_dt, end_dt, all_day = parsed_time
            events.append(
                {
                    "event_key": len(events) + 1,
                    "vacation_match": _normalized_vacation_match(summary),
                    "cancelled": False,
                    "recurrence_id": None,
                    "dtstart": start_dt,
                    "dtend": end_dt,
                    "all_day": all_day,
                    "rrule": {},
                    "exdates": [],
                }
            )
            if len(events) > _MAX_EVENTS:
                return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        raw_next = document.get("nextPageToken", "")
        if type(raw_next) is not str or len(raw_next) > 4096:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        next_token = raw_next
        if not next_token:
            break
        if next_token in seen_tokens or remaining <= 0:
            return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
        seen_tokens.add(next_token)
    else:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)

    return GoogleCalendarSuccess(
        {
            "vacation_schema_version": VACATION_CALENDAR_SCHEMA_VERSION,
            "calendar_matched": True,
            "events": events,
        }
    )


def _valid_envelope(envelope) -> bool:
    return (
        isinstance(envelope, OAuthEnvelope)
        and _valid_client_id(envelope.client_id)
        and _valid_secret_string(envelope.client_secret, optional=True, maximum=4096)
        and _valid_secret_string(envelope.refresh_token)
        and _valid_secret_string(envelope.calendar_id, maximum=4096)
    )


def serialize_envelope(envelope: OAuthEnvelope) -> GoogleCalendarResult[str]:
    """Serialize credentials into a bounded versioned JSON document."""

    if not _valid_envelope(envelope):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    try:
        text = json.dumps(
            {
                "schema_version": 1,
                "client_id": envelope.client_id,
                "client_secret": envelope.client_secret,
                "refresh_token": envelope.refresh_token,
                "calendar_id": envelope.calendar_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    if len(text.encode("utf-8")) > _ENVELOPE_LIMIT:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    return GoogleCalendarSuccess(text)


def deserialize_envelope(text) -> GoogleCalendarResult[OAuthEnvelope]:
    """Strictly deserialize schema version 1 without exposing its payload."""

    if type(text) is not str:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    if not encoded or len(encoded) > _ENVELOPE_LIMIT:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    try:
        document = _strict_json_loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    required = {
        "schema_version",
        "client_id",
        "client_secret",
        "refresh_token",
        "calendar_id",
    }
    if type(document) is not dict or set(document) != required:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    envelope = OAuthEnvelope(
        client_id=document.get("client_id"),
        client_secret=document.get("client_secret"),
        refresh_token=document.get("refresh_token"),
        calendar_id=document.get("calendar_id"),
    )
    if not _valid_envelope(envelope):
        return GoogleCalendarError(GoogleCalendarErrorCode.INVALID_RESPONSE)
    return GoogleCalendarSuccess(envelope)

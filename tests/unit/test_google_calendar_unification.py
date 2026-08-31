from __future__ import annotations

import base64
from datetime import date
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.apps.Wrike import Wrike
from src.apps.google_calendar_oauth import (
    CalendarCatalogEntry,
    GoogleCalendarSuccess,
    OAuthEnvelope,
    bind_calendar_role,
    deserialize_envelope,
    fetch_break_calendar,
    fetch_vacation_calendar,
    list_calendar_catalog,
    load_bundled_desktop_client_config,
    serialize_envelope,
)
from src.apps.wrike_ui import WrikeSettingsView
from src.utils.secret_store import SecretStore


class _JsonResponse:
    def __init__(self, url: str, document: dict) -> None:
        self._url = url
        self._body = io.BytesIO(
            json.dumps(document, ensure_ascii=False).encode("utf-8")
        )
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        return None


class _FakeVar:
    def __init__(self, value="") -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _FakeCombo:
    def __init__(self) -> None:
        self.configuration = {}

    def configure(self, **kwargs):
        self.configuration.update(kwargs)


class _FakeGoogleBackend:
    def __init__(self) -> None:
        self.begin_calls = 0
        self.bind_calls = []
        self.clear_calls = []
        self.status = {
            "configured": True,
            "secret_present": True,
            "state": "fresh",
            "error_code": "",
            "catalog_loading": False,
            "catalog": [{"handle": "opaque-break", "label": "Team Break"}],
            "break_role_configured": False,
            "vacation_role_configured": False,
            "break_role_handle": "",
            "vacation_role_handle": "",
        }

    def begin_google_calendar_oauth(self):
        self.begin_calls += 1
        return True, None

    def get_google_calendar_status_snapshot(self):
        return dict(self.status)

    def bind_google_calendar_role(self, role, handle):
        self.bind_calls.append((role, handle))
        self.status[f"{role}_role_configured"] = True
        self.status[f"{role}_role_handle"] = handle
        return True, None

    def clear_google_calendar_role(self, role):
        self.clear_calls.append(role)
        self.status[f"{role}_role_configured"] = False
        self.status[f"{role}_role_handle"] = ""
        return True, None


class GoogleCalendarUnificationTest(unittest.TestCase):
    def _envelope(self, **overrides) -> OAuthEnvelope:
        values = {
            "client_id": "unit-test.apps.googleusercontent.com",
            "client_secret": "unit-client-secret",
            "refresh_token": "unit-refresh-token",
            "break_calendar_id": "",
            "vacation_calendar_id": "",
        }
        values.update(overrides)
        return OAuthEnvelope(**values)

    @staticmethod
    def _urlopen_for(calendar_document=None, event_document=None):
        def urlopen(request, timeout=None):
            url = request.full_url
            if url == "https://oauth2.googleapis.com/token":
                return _JsonResponse(url, {"access_token": "unit-access-token"})
            if "/users/me/calendarList" in url:
                return _JsonResponse(url, calendar_document or {"items": []})
            if "/events?" in url:
                return _JsonResponse(url, event_document or {"items": []})
            raise AssertionError("unexpected endpoint")

        return urlopen

    def test_schema_v1_migrates_to_vacation_role_and_serializes_v2(self) -> None:
        legacy = json.dumps(
            {
                "schema_version": 1,
                "client_id": "unit-test.apps.googleusercontent.com",
                "client_secret": "unit-client-secret",
                "refresh_token": "unit-refresh-token",
                "calendar_id": "legacy-vacation-calendar",
            }
        )

        decoded = deserialize_envelope(legacy)
        self.assertIsInstance(decoded, GoogleCalendarSuccess)
        self.assertEqual(decoded.value.break_calendar_id, "")
        self.assertEqual(
            decoded.value.vacation_calendar_id,
            "legacy-vacation-calendar",
        )
        serialized = serialize_envelope(decoded.value)
        self.assertIsInstance(serialized, GoogleCalendarSuccess)
        document = json.loads(serialized.value)
        self.assertEqual(document["schema_version"], 2)
        self.assertNotIn("calendar_id", document)

    def test_bundled_client_and_catalog_keep_credentials_and_ids_out_of_repr(self) -> None:
        config = load_bundled_desktop_client_config()
        self.assertIsInstance(config, GoogleCalendarSuccess)
        self.assertNotIn(config.value.client_id, repr(config.value))
        if config.value.client_secret:
            self.assertNotIn(config.value.client_secret, repr(config.value))

        envelope = self._envelope()
        catalog = list_calendar_catalog(
            envelope,
            urlopen=self._urlopen_for(
                calendar_document={
                    "items": [
                        {"id": "private-break-id", "summary": "Break Calendar"},
                        {"id": "private-vacation-id", "summary": "Vacation Calendar"},
                    ]
                }
            ),
        )
        self.assertIsInstance(catalog, GoogleCalendarSuccess)
        self.assertEqual([item.label for item in catalog.value], ["Break Calendar", "Vacation Calendar"])
        self.assertNotIn("private-break-id", repr(catalog.value[0]))
        bound = bind_calendar_role(envelope, "break", catalog.value[0])
        self.assertIsInstance(bound, GoogleCalendarSuccess)
        self.assertEqual(bound.value.break_calendar_id, "private-break-id")
        self.assertEqual(bound.value.vacation_calendar_id, "")

    def test_explicit_role_fetches_preserve_separate_minimized_contracts(self) -> None:
        event = {
            "status": "confirmed",
            "summary": "Private Team 헬스 Session",
            "start": {"dateTime": "2026-04-06T09:00:00+09:00"},
            "end": {"dateTime": "2026-04-06T10:00:00+09:00"},
        }
        break_result = fetch_break_calendar(
            self._envelope(break_calendar_id="break-role-id"),
            date(2026, 4, 6),
            ["헬스"],
            urlopen=self._urlopen_for(event_document={"items": [event]}),
        )
        self.assertIsInstance(break_result, GoogleCalendarSuccess)
        self.assertEqual(break_result.value[0]["summary"], "헬스")
        self.assertNotIn("Private Team", repr(break_result.value))

        vacation_event = dict(event)
        vacation_event["summary"] = "오후 휴가"
        vacation_result = fetch_vacation_calendar(
            self._envelope(vacation_calendar_id="vacation-role-id"),
            date(2026, 4, 6),
            urlopen=self._urlopen_for(event_document={"items": [vacation_event]}),
        )
        self.assertIsInstance(vacation_result, GoogleCalendarSuccess)
        self.assertTrue(vacation_result.value["calendar_matched"])
        self.assertTrue(vacation_result.value["events"][0]["vacation_match"])
        self.assertNotIn("오후 휴가", repr(vacation_result.value))

    def test_settings_v8_legacy_key_and_v1_envelope_migrate_to_shared_v9(self) -> None:
        def protect(_store, value):
            encoded = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
            return "test-protected:" + encoded

        def unprotect(_store, value):
            raw = str(value or "")
            if not raw.startswith("test-protected:"):
                return ""
            return base64.b64decode(raw.split(":", 1)[1]).decode("utf-8")

        legacy_envelope = json.dumps(
            {
                "schema_version": 1,
                "client_id": "unit-test.apps.googleusercontent.com",
                "client_secret": "unit-client-secret",
                "refresh_token": "unit-refresh-token",
                "calendar_id": "legacy-vacation-calendar",
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as root, patch.object(
            SecretStore, "protect", autospec=True, side_effect=protect
        ), patch.object(
            SecretStore, "unprotect", autospec=True, side_effect=unprotect
        ), patch.dict(os.environ, {"APPDATA": root}, clear=False):
            config_dir = Path(root) / "windows-supporter"
            config_dir.mkdir(parents=True)
            settings_path = config_dir / "wrike_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": 8,
                        "vacation_calendar_provider": "google_oauth",
                        "vacation_google_oauth_protected": protect(None, legacy_envelope),
                        "vacation_google_oauth_delete_pending": False,
                    }
                ),
                encoding="utf-8",
            )

            wrike = Wrike()
            persisted_text = settings_path.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)
            self.assertEqual(persisted["settings_version"], 9)
            self.assertIn("google_calendar_oauth_protected", persisted)
            self.assertNotIn("vacation_google_oauth_protected", persisted)
            self.assertNotIn("legacy-vacation-calendar", persisted_text)
            migrated_text = unprotect(None, persisted["google_calendar_oauth_protected"])
            migrated = json.loads(migrated_text)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["break_calendar_id"], "")
            self.assertEqual(
                migrated["vacation_calendar_id"],
                "legacy-vacation-calendar",
            )
            self.assertEqual(
                wrike.get_google_calendar_status_snapshot()["vacation_role_configured"],
                True,
            )

    def test_ui_connect_is_argumentless_and_role_selection_uses_opaque_handle(self) -> None:
        source = inspect.getsource(WrikeSettingsView._on_connect_google_calendar_oauth)
        self.assertNotIn("filedialog", source)
        self.assertNotIn("askopenfilename", source)

        backend = _FakeGoogleBackend()
        view = WrikeSettingsView(None, backend)
        view._google_status_var = _FakeVar()
        view._google_action_var = _FakeVar()
        view._google_break_var = _FakeVar()
        view._google_vacation_var = _FakeVar()
        view._google_break_combo = _FakeCombo()
        view._google_vacation_combo = _FakeCombo()
        view._refresh_google_calendar_status(backend.status)

        view._on_connect_google_calendar_oauth()
        self.assertEqual(backend.begin_calls, 1)
        view._google_break_var.set("Team Break")
        view._on_google_role_selected("break")
        self.assertEqual(backend.bind_calls, [("break", "opaque-break")])
    def test_stale_catalog_result_cannot_cross_account_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"APPDATA": root}, clear=False
        ):
            wrike = Wrike()
        account_a = serialize_envelope(self._envelope(refresh_token="account-a-token"))
        account_b = serialize_envelope(self._envelope(refresh_token="account-b-token"))
        self.assertIsInstance(account_a, GoogleCalendarSuccess)
        self.assertIsInstance(account_b, GoogleCalendarSuccess)
        wrike._Wrike__google_calendar_oauth_session = account_a.value
        wrike._Wrike__google_calendar_oauth_protected = "protected-account-a"

        def delayed_catalog(_envelope):
            wrike._Wrike__google_calendar_oauth_session = account_b.value
            wrike._Wrike__google_calendar_oauth_protected = "protected-account-b"
            return GoogleCalendarSuccess(
                (CalendarCatalogEntry("account-a-calendar", "Account A"),)
            )

        with patch(
            "src.apps.Wrike.list_calendar_catalog",
            side_effect=delayed_catalog,
        ):
            ok, error = wrike.refresh_google_calendar_catalog()

        self.assertFalse(ok)
        self.assertEqual(error, "authorization_cancelled")
        self.assertEqual(wrike._Wrike__google_calendar_catalog, {})
        self.assertNotIn(
            "Account A",
            repr(wrike.get_google_calendar_status_snapshot()),
        )

    def test_catalog_admission_cannot_cross_disconnect_claim(self) -> None:
        class DeferredThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"APPDATA": root}, clear=False
        ):
            wrike = Wrike()
        serialized = serialize_envelope(self._envelope())
        self.assertIsInstance(serialized, GoogleCalendarSuccess)
        wrike._Wrike__google_calendar_oauth_session = serialized.value
        wrike._Wrike__google_calendar_oauth_protected = "protected-account"

        def claim_disconnect(_envelope):
            ok, error = wrike.disconnect_google_calendar_oauth()
            self.assertTrue(ok, error)
            return "pre-disconnect-fingerprint"

        with patch("src.apps.Wrike.threading.Thread", DeferredThread), patch.object(
            wrike,
            "_Wrike__google_calendar_envelope_fingerprint",
            side_effect=claim_disconnect,
        ), patch("src.apps.Wrike.list_calendar_catalog") as catalog:
            ok, error = wrike.refresh_google_calendar_catalog()

        self.assertFalse(ok)
        self.assertEqual(error, "secret_unavailable")
        catalog.assert_not_called()
        disconnect_owner = wrike._Wrike__google_calendar_disconnect_owner
        self.assertIsNotNone(disconnect_owner)
        self.assertIs(wrike._Wrike__vacation_ical_fetch_owner, disconnect_owner)
        self.assertIsNone(wrike._Wrike__google_calendar_catalog_owner)
        self.assertFalse(wrike._Wrike__google_calendar_catalog_loading)

    def test_connect_admission_cannot_replace_disconnect_claim(self) -> None:
        class DeferredThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"APPDATA": root}, clear=False
        ):
            wrike = Wrike()
        serialized = serialize_envelope(self._envelope())
        self.assertIsInstance(serialized, GoogleCalendarSuccess)
        wrike._Wrike__google_calendar_oauth_session = serialized.value
        wrike._Wrike__google_calendar_oauth_protected = "protected-account"
        wrike._Wrike__root = object()
        wrike._Wrike__background_active = True

        def load_after_disconnect_claim():
            ok, error = wrike.disconnect_google_calendar_oauth()
            self.assertTrue(ok, error)
            return GoogleCalendarSuccess({"installed": {}})

        with patch("src.apps.Wrike.threading.Thread", DeferredThread), patch(
            "src.apps.Wrike.load_bundled_desktop_client_config",
            side_effect=load_after_disconnect_claim,
        ), patch("src.apps.Wrike.authorize_desktop") as authorize:
            ok, error = wrike.begin_google_calendar_oauth()

        self.assertFalse(ok)
        self.assertEqual(error, "authorization_cancelled")
        authorize.assert_not_called()
        disconnect_owner = wrike._Wrike__google_calendar_disconnect_owner
        self.assertIsNotNone(disconnect_owner)
        self.assertIs(wrike._Wrike__vacation_ical_fetch_owner, disconnect_owner)

    def test_disconnect_owner_blocks_mutations_and_persists_revoke_before_delete(self) -> None:
        class DeferredThread:
            target = None

            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon
                self.__class__.target = target

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"APPDATA": root}, clear=False
        ):
            wrike = Wrike()
        serialized = serialize_envelope(self._envelope())
        self.assertIsInstance(serialized, GoogleCalendarSuccess)
        wrike._Wrike__google_calendar_oauth_session = serialized.value
        wrike._Wrike__google_calendar_oauth_protected = "protected-account"
        wrike._Wrike__google_calendar_catalog = {
            "opaque": CalendarCatalogEntry("private-calendar-id", "Calendar")
        }

        save = Mock(side_effect=[True, True])
        with patch("src.apps.Wrike.threading.Thread", DeferredThread), patch(
            "src.apps.Wrike.revoke_refresh_token",
            return_value=GoogleCalendarSuccess(None),
        ), patch.object(wrike, "_Wrike__save_settings", save):
            ok, error = wrike.disconnect_google_calendar_oauth()
            self.assertTrue(ok, error)
            self.assertIsNotNone(wrike._Wrike__google_calendar_disconnect_owner)
            self.assertEqual(
                wrike.get_google_calendar_status_snapshot()["state"],
                "disconnecting",
            )

            ok, error = wrike.bind_google_calendar_role("break", "opaque")
            self.assertFalse(ok)
            self.assertEqual(error, "authorization_cancelled")
            ok, error = wrike.clear_google_calendar_role("vacation")
            self.assertFalse(ok)
            self.assertEqual(error, "authorization_cancelled")
            ok, _error = wrike.refresh_google_calendar_catalog()
            self.assertFalse(ok)
            ok, error = wrike.begin_google_calendar_oauth()
            self.assertFalse(ok)
            self.assertEqual(error, "authorization_cancelled")
            disconnect_owner = wrike._Wrike__google_calendar_disconnect_owner
            ok, error = wrike.retry_vacation_ical()
            self.assertFalse(ok)
            self.assertEqual(error, "authorization_cancelled")
            self.assertIs(
                wrike._Wrike__google_calendar_disconnect_owner,
                disconnect_owner,
            )
            self.assertIs(
                wrike._Wrike__vacation_ical_fetch_owner,
                disconnect_owner,
            )

            self.assertIsNotNone(DeferredThread.target)
            DeferredThread.target()

        self.assertEqual(save.call_count, 2)
        self.assertEqual(wrike._Wrike__google_calendar_oauth_protected, "")
        self.assertEqual(wrike._Wrike__google_calendar_oauth_session, "")
        self.assertIsNone(wrike._Wrike__google_calendar_disconnect_owner)
        self.assertFalse(wrike._Wrike__google_calendar_oauth_delete_pending)

    def test_disconnect_local_delete_save_failure_is_retryable_without_revoke(self) -> None:
        class DeferredThread:
            target = None

            def __init__(self, target=None, daemon=None):
                self.__class__.target = target

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"APPDATA": root}, clear=False
        ):
            wrike = Wrike()
        serialized = serialize_envelope(self._envelope())
        self.assertIsInstance(serialized, GoogleCalendarSuccess)
        wrike._Wrike__google_calendar_oauth_session = serialized.value
        wrike._Wrike__google_calendar_oauth_protected = "protected-account"

        first_save = Mock(side_effect=[True, False])
        with patch("src.apps.Wrike.threading.Thread", DeferredThread), patch(
            "src.apps.Wrike.revoke_refresh_token",
            return_value=GoogleCalendarSuccess(None),
        ) as revoke, patch.object(wrike, "_Wrike__save_settings", first_save):
            ok, error = wrike.disconnect_google_calendar_oauth()
            self.assertTrue(ok, error)
            DeferredThread.target()

        self.assertEqual(revoke.call_count, 1)
        self.assertTrue(wrike._Wrike__google_calendar_oauth_delete_pending)
        self.assertTrue(wrike._Wrike__google_calendar_oauth_protected)
        self.assertIsNone(wrike._Wrike__google_calendar_disconnect_owner)

        retry_save = Mock(return_value=True)
        with patch.object(wrike, "_Wrike__save_settings", retry_save), patch(
            "src.apps.Wrike.revoke_refresh_token"
        ) as second_revoke:
            ok, error = wrike.disconnect_google_calendar_oauth()

        self.assertTrue(ok, error)
        second_revoke.assert_not_called()
        retry_save.assert_called_once()
        self.assertFalse(wrike._Wrike__google_calendar_oauth_delete_pending)
        self.assertEqual(wrike._Wrike__google_calendar_oauth_protected, "")

    def test_selector_display_labels_remain_globally_unique(self) -> None:
        backend = _FakeGoogleBackend()
        backend.status["catalog"] = [
            {"handle": "a", "label": "Team"},
            {"handle": "b", "label": "Team (2)"},
            {"handle": "c", "label": "Team"},
        ]
        view = WrikeSettingsView(None, backend)
        view._google_status_var = _FakeVar()
        view._google_action_var = _FakeVar()
        view._google_break_var = _FakeVar()
        view._google_vacation_var = _FakeVar()
        view._google_break_combo = _FakeCombo()
        view._google_vacation_combo = _FakeCombo()

        view._refresh_google_calendar_status(backend.status)

        self.assertEqual(len(view._google_handle_by_label), 3)
        self.assertEqual(
            set(view._google_handle_by_label.values()),
            {"a", "b", "c"},
        )

    def test_undecodable_legacy_ciphertext_is_renamed_without_loss(self) -> None:
        def unprotect(_store, _value):
            return ""

        with tempfile.TemporaryDirectory() as root, patch.object(
            SecretStore, "unprotect", autospec=True, side_effect=unprotect
        ), patch.dict(os.environ, {"APPDATA": root}, clear=False):
            config_dir = Path(root) / "windows-supporter"
            config_dir.mkdir(parents=True)
            settings_path = config_dir / "wrike_settings.json"
            opaque = "dpapi:opaque-undecodable-value"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": 8,
                        "vacation_google_oauth_protected": opaque,
                    }
                ),
                encoding="utf-8",
            )

            Wrike()
            migrated = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertEqual(migrated["settings_version"], 9)
        self.assertEqual(migrated["google_calendar_oauth_protected"], opaque)
        self.assertNotIn("vacation_google_oauth_protected", migrated)


if __name__ == "__main__":
    unittest.main()

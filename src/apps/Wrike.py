from datetime import datetime, timedelta
import copy
import hashlib
import json
import math
import os
import queue
import shutil
import socket
import ssl
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request

from src.utils.LibConnector import LibConnector
from src.utils.secret_store import SecretStore
from src.utils.subprocess_utils import build_python_module_command, is_frozen_runtime
from src.utils.ToolTip import ToolTip
from src.apps.google_calendar_oauth import (
    GoogleCalendarError,
    GoogleCalendarErrorCode,
    GoogleCalendarSuccess,
    authorize_desktop,
    deserialize_envelope,
    fetch_vacation_calendar,
    load_desktop_client_config,
    revoke_refresh_token,
    serialize_envelope,
)
from src.apps.wrike_ical import (
    DEFAULT_POLL_TIMEOUT_SEC,
    CalendarError,
    CalendarErrorCode,
    CalendarSuccess,
    compile_vacation_calendar,
    decode_calendar_response,
    fetch_calendar_text,
    matching_break_events,
    parse_calendar_document,
    parse_ics,
    vacation_events_for_day,
)
from src.apps.wrike_worktime import (
    BreakInterval,
    DEFAULT_LUNCH_END_MIN,
    DEFAULT_LUNCH_START_MIN,
    RefreshableLines,
    build_lunch_interval,
    build_workday_overview,
    clock_in_candidate,
    composed_vacation_credit_minutes,
)
from src.apps.wrike_worktime_state import WorktimeStateStore
from src.apps.wrike_timelog_snapshot import (
    TimelogDay,
    TimelogSnapshotState,
    WrikeTimelogSnapshotStore,
    apply_stale_threshold,
    error_from_last_good,
    loading_from_last_good,
    make_error_snapshot,
    make_fresh_snapshot,
    make_loading_snapshot,
    make_unconfigured_snapshot,
)
from src.apps.wrike_worktime_panel import (
    WorktimeActivityPrompt,
    WorktimePanelDayRow,
    WorktimePanelLine,
    WorktimePanelModel,
    WorktimeQuickPanel,
)
from src.apps.worktime_activity import (
    LastInputUnavailableError,
    WorktimeActivityWatcher,
)


VACATION_EXPECTED_CALENDAR_NAME = "김종인-ePapyrus"
VACATION_ERROR_FETCH_FAILED = "calendar_fetch_failed"
VACATION_ERROR_NAME_MISMATCH = "calendar_name_mismatch"
VACATION_ERROR_SECRET_UNAVAILABLE = "secret_unavailable"
VACATION_CALENDAR_PROVIDERS = frozenset({"private_ical", "google_oauth"})
VACATION_STATES = frozenset(
    {
        "unconfigured",
        "authorizing",
        "disconnecting",
        "loading",
        "fresh",
        "stale",
        "error",
    }
)
VACATION_STATUS_ERROR_CODES = frozenset(
    {
        *(code.value for code in CalendarErrorCode),
        *(code.value for code in GoogleCalendarErrorCode),
        VACATION_ERROR_FETCH_FAILED,
        VACATION_ERROR_NAME_MISMATCH,
        VACATION_ERROR_SECRET_UNAVAILABLE,
    }
)


class _VacationRedirectRejected(Exception):
    """Target-free marker for a rejected private-calendar redirect."""


class _VacationAuthenticationRequired(Exception):
    """Target-free marker for a redirect to an interactive login surface."""


class Wrike:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__form_url = 'https://www.wrike.com/workspace.htm?acc=469516#/forms?formid=2239448'
        self.__clipboard_timeout_sec = 0.7
        self.__clipboard_copy_retry = 6
        self.__form_nav_timeout_ms = 20000
        self.__form_title_timeout_ms = 7000
        self.__form_settle_timeout_ms = 1200
        self.__form_title_locator_wait_ms = 800
        self.__is_running = False
        self.__open_tab_running = False
        self.__time_log_running = False
        self.__form_playwright = None
        self.__form_context = None
        self.__form_page = None
        self.__form_browser_queue = queue.Queue()
        self.__form_browser_worker_thread = None
        self.__form_browser_worker_lock = threading.Lock()
        self.__form_browser_prewarm_requested = False
        self.__time_log_root_url = (
            'https://www.wrike.com/workspace.htm?acc=469516'
            '#/folder/1593118419/tableV2?showInfo=0&spaceId=1590111212&viewId=336617617'
        )
        self.__time_log_year_prefix = 'CS: '
        self.__time_log_month_prefix = 'CS: Kanban-'
        self.__time_log_month_names = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        self.__time_log_view_label = 'Timelog'
        self.__time_log_default_daily_minutes = 8 * 60
        self.__time_log_login_timeout_sec = 180.0
        self.__time_log_nav_timeout_ms = 20000
        self.__wrike_api_base = "https://www.wrike.com/api/v4"
        self.__wrike_api_token_env = "WRIKE_ACCESS_TOKEN"
        self.__wrike_api_timeout_sec = 15.0
        self.__wrike_api_page_size = 100
        self.__wrike_api_max_pages = 10
        self.__wrike_api_token_session = ""
        self.__wrike_api_last_error_code = 0
        self.__wrike_api_contact_id = ""
        self.__wrike_api_contact_name = ""
        self.__wrike_api_contact_token = ""
        self.__daily_target_minutes = int(self.__time_log_default_daily_minutes)
        self.__tooltip_duration_ms = 6000
        self.__monitor_enabled = True
        self.__monitor_interval_sec = 5.0
        self.__monitor_after_id = None
        self.__monitor_running = False
        self.__monitor_last_total_minutes = None
        self.__monitor_weekdays = [0, 1, 2, 3, 4]
        self.__monitor_folder_path: list[dict] = []
        self.__folder_cache: dict[str, list[dict]] = {}
        self.__root = None
        self.__ui_queue = queue.SimpleQueue()
        self.__ui_pump_started = False
        self.__ui_after_id = None
        self.__background_active = False
        self.__lifecycle_generation = 0
        self.__active_tooltip = None
        self.__worktime_panel = None
        self.__worktime_panel_root = None
        self.__activity_watcher = None
        self.__activity_prompt_surfaced_day = ""
        self.__settings_version = 8
        self.__playwright_checked = False
        self.__playwright_ready = False
        self.__time_log_weekday_labels = ['월', '화', '수', '목', '금', '토', '일']
        base_dir = self.__lib.os.getenv("APPDATA")
        if not base_dir:
            base_dir = self.__lib.os.getenv("LOCALAPPDATA")
        if not base_dir:
            base_dir = self.__lib.os.path.expanduser("~")
        self.__time_log_config_dir = self.__lib.os.path.join(base_dir, "windows-supporter")
        self.__time_log_log_path = self.__lib.os.path.join(self.__time_log_config_dir, "wrike.log")
        self.__time_log_token_path = self.__lib.os.path.join(self.__time_log_config_dir, "wrike_token.txt")
        self.__settings_path = self.__lib.os.path.join(self.__time_log_config_dir, "wrike_settings.json")
        self.__timelog_cache_path = self.__lib.os.path.join(
            self.__time_log_config_dir,
            "wrike_timelog_cache.json",
        )
        self.__timelog_snapshot_store = WrikeTimelogSnapshotStore(
            self.__timelog_cache_path
        )
        self.__timelog_snapshot_lock = threading.RLock()
        self.__timelog_refresh_generation = 0
        self.__timelog_refresh_running = False
        self.__timelog_refresh_running_generation = None
        self.__timelog_last_refresh_requested_at = None
        self.__timelog_snapshot = make_unconfigured_snapshot(generation=0)
        self.__timelog_last_good = None
        self.__secret_store = SecretStore("windows-supporter:wrike-api-token")
        self.__vacation_secret_store = SecretStore("windows-supporter:vacation-ical-url")
        self.__vacation_google_oauth_secret_store = SecretStore(
            "windows-supporter:google-calendar-oauth"
        )
        self.__wrike_api_token_secret_scope = "windows-supporter:wrike-api-token"
        self.__lunch_break_enabled = True
        self.__lunch_start_min = int(DEFAULT_LUNCH_START_MIN)
        self.__lunch_end_min = int(DEFAULT_LUNCH_END_MIN)
        self.__ical_url_protected = ""
        self.__ical_url_session = ""
        self.__ical_keywords = ["헬스", "운동", "gym", "fitness", "pt"]
        self.__ical_poll_interval_sec = 900.0
        self.__ical_after_id = None
        self.__ical_fetch_running = False
        self.__ical_last_success_ts = None
        self.__ical_last_error = ""
        self.__ical_events_for_date = ""
        self.__ical_parsed_events: list[dict] = []
        self.__ical_matched: list[dict] = []
        self.__vacation_calendar_provider = "private_ical"
        self.__vacation_ical_url_protected = ""
        self.__vacation_ical_url_session = ""
        self.__vacation_google_oauth_protected = ""
        self.__vacation_google_oauth_session = ""
        self.__vacation_google_oauth_week_start = ""
        self.__vacation_google_oauth_cancel_event = None
        self.__vacation_google_oauth_delete_pending = False
        self.__vacation_ical_poll_interval_sec = 900.0
        self.__vacation_ical_after_id = None
        self.__vacation_ical_fetch_owner = None
        self.__vacation_ical_last_success_ts = None
        self.__vacation_ical_last_error = ""
        self.__vacation_ical_state = "unconfigured"
        self.__vacation_ical_observed_calendar_name = ""
        self.__vacation_ical_generation = 0
        self.__vacation_ical_lock = threading.RLock()
        self.__vacation_ical_calendar: dict = {}
        self.__vacation_ical_events_for_date = ""
        self.__vacation_ical_day_result: dict = {}
        self.__vacation_ical_week_cache_calendar = None
        self.__vacation_ical_week_cache: dict[str, dict] = {}
        self.__worktime_state_path = self.__lib.os.path.join(
            self.__time_log_config_dir,
            "wrike_worktime_state.json",
        )

        self.__re_brackets = self.__lib.re.compile(r'\[([^\]]*)\]')
        self.__re_internal = self.__lib.re.compile(r'^없음\s*\((.+?)\)\s*$')
        self.__re_time_h = self.__lib.re.compile(r'(\d+(?:\.\d+)?)\s*h')
        self.__re_time_m = self.__lib.re.compile(r'(\d+(?:\.\d+)?)\s*m')
        self.__re_time_hhmm = self.__lib.re.compile(r'^\s*(\d+)\s*:\s*(\d{1,2})\s*$')
        self.__re_time_number = self.__lib.re.compile(r'^\s*\d+(?:\.\d+)?\s*$')
        self.__re_weekday_en = self.__lib.re.compile(r'\b(mon|tue|wed|thu|fri|sat|sun)\b', self.__lib.re.I)
        self.__re_date_num = self.__lib.re.compile(r'\b(\d{1,2})[./-](\d{1,2})\b')
        self.__load_settings()
        try:
            store_default_target = max(0, min(1440, int(self.__daily_target_minutes)))
        except Exception:
            store_default_target = int(self.__time_log_default_daily_minutes)
        self.__worktime_state_store = WorktimeStateStore(
            self.__worktime_state_path,
            default_target_minutes=store_default_target,
            now_provider=self.__lib.datetime.now,
        )
        configured_token = str(self.__wrike_api_token_session or "").strip()
        expected_cache_fingerprint = self.__timelog_token_fingerprint(
            configured_token
        )
        try:
            cached_snapshot = (
                self.__timelog_snapshot_store.load(
                    expected_account_fingerprint=expected_cache_fingerprint,
                    generation=int(self.__timelog_refresh_generation),
                )
                if expected_cache_fingerprint
                else None
            )
        except Exception as exc:
            cached_snapshot = None
            self.__log_exception("timelog cache load failed", exc)
        if cached_snapshot is not None:
            try:
                cached_snapshot = apply_stale_threshold(
                    cached_snapshot,
                    now=self.__lib.datetime.now(),
                    stale_after=timedelta(0),
                )
                self.__timelog_snapshot = cached_snapshot
                self.__timelog_last_good = cached_snapshot
            except Exception as exc:
                self.__log_exception("timelog cache classify failed", exc)
        elif str(self.__wrike_api_token_session or "").strip():
            self.__timelog_snapshot = make_loading_snapshot(
                generation=int(self.__timelog_refresh_generation)
            )
        return

    def is_wrike_active(self) -> bool:
        wrike_windows = [win for win in self.__lib.gw.getWindowsWithTitle('Wrike') if win.isActive]
        return bool(wrike_windows)

    def __show_tooltip(
        self,
        root,
        text: str,
        lines: list[tuple[str, str | None]] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        duration = int(duration_ms) if duration_ms is not None else int(self.__tooltip_duration_ms)
        if duration < 1200:
            duration = 1200
        try:
            current = self.__active_tooltip
        except Exception:
            current = None
        if current is not None:
            try:
                current.hide_tooltip()
            except Exception:
                pass
        tooltip = ToolTip(
            root,
            text,
            bind_events=False,
            auto_hide_ms=duration,
            keep_on_hover=True,
            lines=lines,
        )
        self.__active_tooltip = tooltip
        tooltip.show_tooltip()
        return

    def __safe_clipboard_paste(self) -> str:
        try:
            value = self.__lib.pyperclip.paste()
        except Exception:
            return ''
        if value is None:
            return ''
        return str(value)

    def __safe_clipboard_copy(self, text: str) -> bool:
        if text is None:
            return False
        value = str(text)
        for _ in range(self.__clipboard_copy_retry):
            try:
                self.__lib.pyperclip.copy(value)
                return True
            except Exception:
                self.__lib.time.sleep(0.02)
        return False

    def __wait_for_clipboard_update(self, before: str) -> str:
        deadline = self.__lib.time.monotonic() + self.__clipboard_timeout_sec
        while self.__lib.time.monotonic() < deadline:
            current = self.__safe_clipboard_paste()
            if current and current != before:
                return current
            self.__lib.time.sleep(0.02)
        return ''

    def __format_bracket_tokens(self, tokens: list[str]) -> str:
        formatted = ' '.join(('[]' if not t else f'[{t}]') for t in tokens)
        if formatted.endswith('[]'):
            return formatted + ' - '
        return formatted

    def __strip_leading_title_separator(self, text: str) -> str:
        value = str(text or '').strip()
        while value.startswith('-'):
            value = value[1:].strip()
        return value

    def transform_text(self, clipboard_content: str) -> str | None:
        if clipboard_content is None:
            return None

        text = str(clipboard_content).strip()
        if not text:
            return None

        text = ' '.join(text.split())

        bracket_matches = list(self.__re_brackets.finditer(text))
        if bracket_matches:
            tokens = [m.group(1).strip() for m in bracket_matches]
            remainder = text[bracket_matches[-1].end():].strip()
            if remainder:
                remainder = self.__strip_leading_title_separator(remainder)
            if remainder:
                tokens.append(remainder)
            while tokens and tokens[-1] == '':
                tokens.pop()
            tokens.append('')
            return self.__format_bracket_tokens(tokens)

        raw_parts = [p.strip() for p in text.split(' - ')]
        parts = [p for p in raw_parts if p]
        if not parts:
            return None

        payload = parts[2:] if len(parts) >= 4 else parts
        if not payload:
            return None

        company = payload[0]
        internal_match = self.__re_internal.match(company)
        if internal_match:
            company = internal_match.group(1).strip()

        if len(payload) == 1:
            return self.__format_bracket_tokens([company, ''])

        if len(payload) == 2:
            return self.__format_bracket_tokens([company, payload[1], ''])

        project = payload[1]
        description = ' - '.join(payload[2:])
        if project == description:
            return self.__format_bracket_tokens([company, project, ''])
        return self.__format_bracket_tokens([company, project, description, ''])

    def action(self, root) -> None:
        if self.__is_running:
            return

        self.__is_running = True
        try:
            threading.Thread(target=lambda: self.__action_worker(root), daemon=True).start()
        except Exception:
            self.__is_running = False
        return

    def __action_worker(self, root) -> None:
        def show_message(message: str) -> None:
            self.__ui_safe(root, lambda: self.__show_tooltip(root, message))

        try:
            self.__lib.pyautogui.click()
            self.__lib.time.sleep(0.02)
            self.__lib.pyautogui.hotkey('ctrl', 'a')
            self.__lib.time.sleep(0.02)
            before_clipboard = self.__safe_clipboard_paste()
            self.__lib.pyautogui.hotkey('ctrl', 'c')

            copied_text = self.__wait_for_clipboard_update(before_clipboard)
            if not copied_text:
                copied_text = self.__safe_clipboard_paste()

            if not copied_text:
                show_message("클립보드 복사 실패: 텍스트를 선택한 뒤 다시 시도하세요")
                return

            transformed_text = self.transform_text(copied_text)
            if not transformed_text:
                show_message("치환 실패: 텍스트 형식을 확인하세요")
                return

            error = self.__fill_wrike_form_on_browser_worker(root, transformed_text)
            if error:
                show_message(error)
                return
            show_message("Wrike Form 입력 완료")
            return
        finally:
            self.__is_running = False

    def run_action_async(self, root) -> None:
        self.action(root)
        return

    def open_in_separate_tab(self, root) -> None:
        if self.__open_tab_running:
            return
        self.__open_tab_running = True
        try:
            threading.Thread(target=lambda: self.__open_in_separate_tab_worker(root), daemon=True).start()
        except Exception:
            self.__open_tab_running = False
            return
        return

    def open_in_separate_tab_async(self, root) -> None:
        self.open_in_separate_tab(root)
        return

    def __open_in_separate_tab_worker(self, root) -> None:
        try:
            self.__lib.pyautogui.rightClick()
            self.__lib.pyautogui.moveRel(-20, 0, duration=0.1)
            self.__lib.pyautogui.hotkey('o')
            self.__lib.pyautogui.hotkey('enter')
        finally:
            self.__open_tab_running = False

        def show_done() -> None:
            tooltip = ToolTip(root, f"새로운 탭에서 열림", bind_events=False)
            tooltip.show_tooltip()
            root.after(1500, tooltip.hide_tooltip)

        self.__ui_safe(root, show_done)
        return

    def show_weekly_timelog_summary(self, root) -> None:
        if root is None:
            return
        if self.__root is None:
            self.attach(root)
        panel = self.__ensure_worktime_panel(root)
        if panel is None:
            return
        panel.toggle(activate=True)
        if panel.is_visible():
            self.__request_timelog_snapshot_refresh(force=True)
        return

    def __compose_live_worktime_rows(self) -> list[tuple[str, str | None]]:
        rows = self.__build_overview_rows([], int(self.__daily_target_minutes))
        if len(rows) != 5:
            raise ValueError("live worktime overview must contain exactly five rows")
        return rows

    def __build_live_worktime_lines(self) -> RefreshableLines:
        rows = self.__compose_live_worktime_rows()
        return RefreshableLines(rows, self.__compose_live_worktime_rows)

    def __show_live_worktime_summary(self, root) -> None:
        self.__show_tooltip(
            root,
            "근무시간 (실시간)",
            lines=self.__build_live_worktime_lines(),
        )
        return

    def __ui_safe(self, root, fn) -> bool:
        _ = root
        if fn is None:
            return False
        try:
            self.__ui_queue.put(fn)
            return True
        except Exception:
            return False

    def __start_ui_pump(self, root) -> None:
        if root is None or self.__ui_pump_started:
            return
        self.__ui_pump_started = True
        try:
            self.__ui_after_id = root.after(30, self.__drain_ui_queue)
        except Exception:
            self.__ui_after_id = None
            self.__ui_pump_started = False
        return

    def __drain_ui_queue(self) -> None:
        self.__ui_after_id = None
        root = self.__root
        processed = 0
        while processed < 16:
            try:
                fn = self.__ui_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            try:
                fn()
            except Exception:
                pass
            processed += 1
        if root is None:
            self.__ui_pump_started = False
            return
        try:
            self.__ui_after_id = root.after(
                1 if processed >= 16 else 30,
                self.__drain_ui_queue,
            )
        except Exception:
            self.__ui_after_id = None
            self.__ui_pump_started = False
        return

    def __open_settings_tab(self) -> None:
        root = self.__root
        if root is None:
            return
        try:
            ui = getattr(root, "_ws_main_ui", None)
        except Exception:
            ui = None
        if ui is None:
            return
        try:
            ui.show("wrike")
        except Exception:
            return
        return

    def attach(self, root) -> None:
        if root is None:
            return
        self.__root = root
        self.__start_ui_pump(root)
        self.start_background()
        self.__prewarm_wrike_form_browser_async()
        return

    def start_background(self) -> None:
        root = self.__root
        if root is None:
            return
        if not self.__background_active:
            self.__background_active = True
            self.__lifecycle_generation += 1
        self.__restart_monitor()
        self.__start_ical_polling()
        self.__start_vacation_ical_polling()
        self.__start_activity_watcher()
        return

    def __start_activity_watcher(self) -> None:
        root = self.__root
        if root is None or not self.__background_active:
            return
        watcher = self.__activity_watcher
        if watcher is None:
            try:
                watcher = WorktimeActivityWatcher(
                    root,
                    self.__on_worktime_activity,
                    now_provider=self.__lib.datetime.now,
                )
            except (LastInputUnavailableError, OSError, ValueError):
                return
            except Exception:
                return
            self.__activity_watcher = watcher
        try:
            watcher.start()
        except Exception:
            return
        return

    def __cancel_monitor_after(self) -> None:
        root = self.__root
        after_id = self.__monitor_after_id
        self.__monitor_after_id = None
        if root is None or after_id is None:
            return
        try:
            root.after_cancel(after_id)
        except Exception:
            pass
        return

    def stop_background(self) -> None:
        self.__background_active = False
        self.__lifecycle_generation += 1
        self.__cancel_monitor_after()
        self.__monitor_running = False
        self.__cancel_ical_after()
        self.__ical_fetch_running = False
        self.__cancel_vacation_ical_after()
        self.__cancel_vacation_google_oauth()
        self.__vacation_ical_generation += 1
        with self.__vacation_ical_lock:
            self.__vacation_ical_fetch_owner = None
        with self.__timelog_snapshot_lock:
            self.__timelog_refresh_generation += 1
            self.__timelog_refresh_running = False
            self.__timelog_refresh_running_generation = None
        watcher = self.__activity_watcher
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                pass
        panel = self.__worktime_panel
        if panel is not None:
            try:
                panel.hide()
            except Exception:
                pass
        return

    def on_session_unlock(self) -> None:
        watcher = self.__activity_watcher
        if watcher is not None:
            try:
                watcher.reset_baseline()
            except Exception:
                pass
        panel = self.__worktime_panel
        if panel is not None:
            try:
                if panel.is_visible():
                    self.__request_timelog_snapshot_refresh(force=False)
            except Exception:
                pass
        return

    def shutdown(self) -> None:
        self.stop_background()
        root = self.__root
        after_id = self.__ui_after_id
        self.__ui_after_id = None
        self.__ui_pump_started = False
        if root is not None and after_id is not None:
            try:
                root.after_cancel(after_id)
            except Exception:
                pass
        panel = self.__worktime_panel
        self.__worktime_panel = None
        self.__worktime_panel_root = None
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
        self.__activity_watcher = None
        self.__root = None
        return

    def __restart_monitor(self) -> None:
        root = self.__root
        if root is None:
            return
        self.__cancel_monitor_after()
        if not self.__background_active or not self.__monitor_enabled:
            return
        self.__schedule_monitor_tick(root, initial_delay_sec=2.0)
        return

    def __schedule_monitor_tick(self, root, initial_delay_sec: float | None = None) -> None:
        if (
            root is None
            or not self.__background_active
            or not self.__monitor_enabled
        ):
            return
        delay_sec = initial_delay_sec if initial_delay_sec is not None else self.__monitor_interval_sec
        try:
            delay_ms = int(max(5.0, float(delay_sec)) * 1000)
        except Exception:
            delay_ms = int(max(5.0, float(self.__monitor_interval_sec)) * 1000)
        try:
            self.__monitor_after_id = root.after(delay_ms, self.__monitor_tick)
        except Exception:
            self.__monitor_after_id = None
        return

    def __monitor_tick(self) -> None:
        root = self.__root
        if (
            root is None
            or not self.__background_active
            or not self.__monitor_enabled
        ):
            return
        if self.__monitor_running:
            self.__schedule_monitor_tick(root)
            return

        self.__monitor_running = True
        try:
            self.__request_timelog_snapshot_refresh(force=True)
        finally:
            self.__monitor_running = False
            if root is self.__root and self.__background_active:
                self.__schedule_monitor_tick(root)
        return

    def __update_monitor_from_snapshot(self, snapshot) -> None:
        root = self.__root
        if (
            root is None
            or not self.__background_active
            or not self.__monitor_enabled
            or snapshot.state is not TimelogSnapshotState.FRESH
            or not snapshot.has_last_good_data
        ):
            return
        total_minutes = snapshot.total_recorded_minutes
        if total_minutes is None:
            return
        current_total = int(total_minutes)
        previous_total = self.__monitor_last_total_minutes
        if previous_total is None or current_total <= int(previous_total):
            self.__monitor_last_total_minutes = current_total
            return
        try:
            if self.__show_activity_panel() is True:
                self.__monitor_last_total_minutes = current_total
        except Exception as exc:
            self.__log_exception("monitor snapshot notification failed", exc)
        return

    def __finish_monitor_worker(
        self,
        root,
        next_total_minutes: int | None,
        tooltip_lines: list[tuple[str, str | None]] | None,
        lifecycle_generation: int | None = None,
    ) -> None:
        target_generation = (
            int(self.__lifecycle_generation)
            if lifecycle_generation is None
            else int(lifecycle_generation)
        )
        if (
            target_generation != int(self.__lifecycle_generation)
            or not self.__background_active
            or root is not self.__root
        ):
            return
        if next_total_minutes is not None:
            self.__monitor_last_total_minutes = int(next_total_minutes)
        if tooltip_lines:
            self.__show_tooltip(root, "", lines=tooltip_lines)
        self.__monitor_running = False
        self.__schedule_monitor_tick(root)
        return

    # ------------------------------------------------------------------
    # Authoritative realtime snapshot and weekly quick panel
    # ------------------------------------------------------------------

    def __timelog_refresh_interval(self) -> timedelta:
        try:
            seconds = max(5.0, float(self.__monitor_interval_sec))
        except Exception:
            seconds = 5.0
        return timedelta(seconds=seconds)

    def __safe_timelog_error_code(self, value) -> str:
        code = str(value or "request_failed").strip().lower()
        allowed = {
            "api_token_missing",
            "auth_failed",
            "contact_not_found",
            "request_failed",
            "api_request_failed",
            "invalid_response",
            "pagination_cycle",
            "pagination_limit",
            "week_dates_empty",
        }
        return code if code in allowed else "request_failed"

    def get_timelog_snapshot(self):
        with self.__timelog_snapshot_lock:
            return self.__timelog_snapshot

    def __get_timelog_snapshot(self):
        return self.get_timelog_snapshot()

    def __snapshot_for_now(self, now):
        with self.__timelog_snapshot_lock:
            snapshot = self.__timelog_snapshot
            if (
                snapshot.state is TimelogSnapshotState.FRESH
                and snapshot.has_last_good_data
            ):
                try:
                    classified = apply_stale_threshold(
                        snapshot,
                        now=now,
                        stale_after=self.__timelog_refresh_interval(),
                    )
                except Exception:
                    classified = snapshot
                if classified is not snapshot:
                    self.__timelog_snapshot = classified
                    snapshot = classified
                    if self.__timelog_last_good is not None:
                        self.__timelog_last_good = classified
            return snapshot

    def __request_timelog_snapshot_refresh(self, force: bool = False):
        root = self.__root
        if root is None or not self.__background_active:
            return None
        now = self.__lib.datetime.now()
        interval = self.__timelog_refresh_interval()
        token = str(self.__wrike_api_token_session or "").strip()
        account_fingerprint = self.__timelog_token_fingerprint(token)
        week_dates = self.__get_week_dates()
        lifecycle_generation = int(self.__lifecycle_generation)

        with self.__timelog_snapshot_lock:
            if self.__timelog_refresh_running:
                return None
            previous_request = self.__timelog_last_refresh_requested_at
            if not force and previous_request is not None:
                try:
                    if timedelta(0) <= now - previous_request < interval:
                        return None
                except Exception:
                    pass
            self.__timelog_refresh_generation += 1
            generation = int(self.__timelog_refresh_generation)
            self.__timelog_last_refresh_requested_at = now
            last_good = self.__timelog_last_good
            if not token:
                error_code = "api_token_missing"
                if last_good is not None:
                    self.__timelog_snapshot = error_from_last_good(
                        last_good,
                        generation=generation,
                        error_code=error_code,
                    )
                else:
                    self.__timelog_snapshot = make_error_snapshot(
                        generation=generation,
                        error_code=error_code,
                    )
                return generation
            if last_good is not None:
                self.__timelog_snapshot = loading_from_last_good(
                    last_good,
                    generation=generation,
                )
            else:
                self.__timelog_snapshot = make_loading_snapshot(
                    generation=generation
                )
            self.__timelog_refresh_running = True
            self.__timelog_refresh_running_generation = generation

        try:
            thread = threading.Thread(
                target=lambda: self.__run_timelog_snapshot_refresh(
                    generation,
                    token,
                    account_fingerprint,
                    week_dates,
                    root,
                    lifecycle_generation,
                ),
                daemon=True,
            )
            thread.start()
        except Exception:
            self.__apply_timelog_snapshot_result(
                generation,
                account_fingerprint=account_fingerprint,
                error_code="request_failed",
            )
        return generation

    def __run_timelog_snapshot_refresh(
        self,
        generation: int,
        token: str,
        account_fingerprint: str,
        week_dates: list,
        root,
        lifecycle_generation: int,
    ) -> None:
        fresh_snapshot = None
        error_code = None
        try:
            contact_id, display_name, contact_error = self.__resolve_contact_identity(
                token
            )
            if contact_error or not contact_id:
                error_code = self.__safe_timelog_error_code(
                    contact_error or "contact_not_found"
                )
            else:
                timelogs, query_error = self.__query_authoritative_timelogs_week(
                    token,
                    contact_id,
                    week_dates,
                )
                if query_error or timelogs is None:
                    error_code = self.__safe_timelog_error_code(query_error)
                else:
                    aggregated = self.__aggregate_timelogs(timelogs, week_dates)
                    days = tuple(
                        TimelogDay(
                            date=item["date"].date(),
                            recorded_minutes=max(0, int(item.get("minutes", 0))),
                        )
                        for item in aggregated
                    )
                    fresh_snapshot = make_fresh_snapshot(
                        days=days,
                        display_name=str(display_name or "내 계정"),
                        fetched_at=self.__lib.datetime.now(),
                        generation=int(generation),
                        partial=False,
                    )
        except Exception as exc:
            self.__log_exception("authoritative timelog refresh failed", exc)
            error_code = "request_failed"

        def apply_result() -> None:
            if lifecycle_generation != int(self.__lifecycle_generation):
                return
            self.__apply_timelog_snapshot_result(
                generation,
                snapshot=fresh_snapshot,
                account_fingerprint=account_fingerprint,
                error_code=error_code,
            )

        if not self.__ui_safe(root, apply_result):
            with self.__timelog_snapshot_lock:
                if (
                    int(generation) == int(self.__timelog_refresh_generation)
                    and self.__timelog_refresh_running_generation
                    == int(generation)
                ):
                    self.__timelog_refresh_running = False
                    self.__timelog_refresh_running_generation = None
        return

    def __apply_timelog_snapshot_result(
        self,
        generation: int,
        snapshot=None,
        account_fingerprint: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        save_snapshot = None
        save_fingerprint = ""
        notify_snapshot = None
        with self.__timelog_snapshot_lock:
            if int(generation) != int(self.__timelog_refresh_generation):
                return False
            if self.__timelog_refresh_running_generation not in {None, int(generation)}:
                return False
            if snapshot is not None:
                captured_fingerprint = str(account_fingerprint or "").strip().lower()
                current_fingerprint = self.__timelog_token_fingerprint(
                    self.__wrike_api_token_session
                )
                if (
                    len(captured_fingerprint) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in captured_fingerprint
                    )
                    or captured_fingerprint != current_fingerprint
                ):
                    return False
            self.__timelog_refresh_running = False
            self.__timelog_refresh_running_generation = None
            if snapshot is not None:
                if (
                    snapshot.generation != int(generation)
                    or snapshot.state is not TimelogSnapshotState.FRESH
                    or not snapshot.has_last_good_data
                    or snapshot.partial
                ):
                    error_code = "invalid_response"
                    snapshot = None
            if snapshot is not None:
                self.__timelog_snapshot = snapshot
                self.__timelog_last_good = snapshot
                save_snapshot = snapshot
                save_fingerprint = str(account_fingerprint)
                notify_snapshot = snapshot
            else:
                safe_error = self.__safe_timelog_error_code(error_code)
                last_good = self.__timelog_last_good
                if last_good is not None:
                    self.__timelog_snapshot = error_from_last_good(
                        last_good,
                        generation=int(generation),
                        error_code=safe_error,
                    )
                else:
                    self.__timelog_snapshot = make_error_snapshot(
                        generation=int(generation),
                        error_code=safe_error,
                    )
        if save_snapshot is not None:
            try:
                self.__timelog_snapshot_store.save(
                    save_snapshot,
                    account_fingerprint=save_fingerprint,
                )
            except Exception as exc:
                self.__log_exception("timelog cache save failed", exc)
        if notify_snapshot is not None:
            self.__update_monitor_from_snapshot(notify_snapshot)
        return True

    def __refresh_visible_panel_if_due(self, now, snapshot) -> None:
        if not self.__background_active:
            return
        panel = self.__worktime_panel
        if panel is None:
            return
        try:
            if not panel.is_visible():
                return
        except Exception:
            return
        with self.__timelog_snapshot_lock:
            if self.__timelog_refresh_running:
                return
            requested_at = self.__timelog_last_refresh_requested_at
        references = [
            value
            for value in (snapshot.fetched_at, requested_at)
            if value is not None
        ]
        reference = max(references) if references else None
        due = reference is None
        if reference is not None:
            try:
                due = now - reference >= self.__timelog_refresh_interval()
            except Exception:
                due = True
        if due:
            self.__request_timelog_snapshot_refresh(force=False)
        return

    def __worktime_panel_idle_timeout_ms(self) -> int:
        try:
            return max(1200, int(self.__tooltip_duration_ms))
        except Exception:
            return 1200

    def __sync_worktime_panel_idle_timeout(self, panel=None) -> None:
        target = self.__worktime_panel if panel is None else panel
        setter = getattr(target, "set_idle_timeout_ms", None)
        if not callable(setter):
            return
        try:
            setter(self.__worktime_panel_idle_timeout_ms())
        except Exception:
            pass
        return

    def __ensure_worktime_panel(self, root):
        if root is None:
            return None
        panel = self.__worktime_panel
        if panel is not None and self.__worktime_panel_root is root:
            self.__sync_worktime_panel_idle_timeout(panel)
            return panel
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
        try:
            panel = WorktimeQuickPanel(
                root,
                self.__build_worktime_panel_model,
                refresh=self.__panel_refresh,
                clock_in_now=self.__panel_clock_in_now,
                edit_clock_in=self.__panel_edit_clock_in,
                edit_plan=self.__panel_save_target_minutes,
                toggle_break=self.__panel_toggle_break,
                open_settings=self.__open_settings_tab,
                prompt_accept=self.__panel_prompt_accept,
                prompt_edit=self.__panel_prompt_edit,
                prompt_snooze=self.__panel_prompt_snooze,
                prompt_skip=self.__panel_prompt_skip,
                idle_timeout_ms=self.__worktime_panel_idle_timeout_ms(),
            )
        except Exception:
            return None
        self.__worktime_panel = panel
        self.__worktime_panel_root = root
        return panel

    def __plan_for_date(self, target_day) -> dict:
        try:
            plan = self.get_workday_plan(target_day)
        except Exception:
            plan = {
                "date": target_day.isoformat(),
                "target_net_minutes": self.__default_workday_target_minutes(),
                "clock_in": None,
                "explicit": False,
            }
        explicit = bool(plan.get("explicit", False))
        if explicit:
            try:
                target = max(0, min(1440, int(plan.get("target_net_minutes", 0))))
            except Exception:
                target = 0
        else:
            target = (
                self.__default_workday_target_minutes()
                if int(target_day.weekday()) < 5
                else 0
            )
        return {
            "date": target_day.isoformat(),
            "target_net_minutes": int(target),
            "clock_in": plan.get("clock_in"),
            "explicit": explicit,
        }

    @staticmethod
    def __calculation_only_vacation_result(result) -> dict:
        source = result if isinstance(result, dict) else {}
        try:
            event_count = max(0, int(source.get("event_count", 0)))
        except Exception:
            event_count = 0
        intervals = []
        for span in source.get("intervals") or []:
            try:
                start, end = span
            except Exception:
                continue
            if isinstance(start, datetime) and isinstance(end, datetime):
                intervals.append((start, end))
        return {
            "calendar_matched": source.get("calendar_matched") is True,
            "all_day": source.get("all_day") is True,
            "intervals": intervals,
            "event_count": event_count,
        }

    def __vacation_result_for_date(self, target_day) -> dict:
        try:
            cache_key = target_day.isoformat()
        except Exception:
            return {
                "availability_state": "error",
                "available": False,
                "automatic_prompt_allowed": False,
                "using_last_good": False,
                "error_code": VACATION_ERROR_FETCH_FAILED,
            }
        provider, secret_present, configuration, _fingerprint = (
            self.__active_vacation_configuration()
        )
        with self.__vacation_ical_lock:
            state = str(self.__vacation_ical_state or "error").strip().lower()
            if state not in VACATION_STATES:
                state = "error"
            calendar = self.__vacation_ical_calendar
            last_error = str(self.__vacation_ical_last_error or "").strip()
            if self.__vacation_ical_week_cache_calendar is not calendar:
                self.__vacation_ical_week_cache_calendar = calendar
                self.__vacation_ical_week_cache = {}
            cached = self.__vacation_ical_week_cache.get(cache_key)

        authorizing = provider == "google_oauth" and state == "authorizing"
        if not secret_present and not authorizing:
            state = "unconfigured"
        elif secret_present and configuration is None and not authorizing:
            state = "error"
            last_error = VACATION_ERROR_SECRET_UNAVAILABLE
        has_last_good = self.__vacation_calendar_has_last_good(target_day)
        available = state == "unconfigured" or has_last_good
        metadata = {
            "availability_state": state,
            "available": available,
            "automatic_prompt_allowed": state in {"unconfigured", "fresh"},
            "using_last_good": bool(has_last_good and state in {"stale", "error"}),
            "error_code": last_error if state == "error" else "",
        }
        if state == "unconfigured":
            return {
                **metadata,
                "calendar_matched": True,
                "all_day": False,
                "intervals": [],
                "event_count": 0,
            }
        if not has_last_good:
            return metadata
        if isinstance(cached, dict):
            return {**cached, **metadata}
        try:
            result = vacation_events_for_day(
                calendar,
                VACATION_EXPECTED_CALENDAR_NAME,
                target_day,
            )
        except Exception:
            return {
                **metadata,
                "availability_state": "error",
                "available": False,
                "automatic_prompt_allowed": False,
                "using_last_good": False,
                "error_code": VACATION_ERROR_FETCH_FAILED,
            }
        normalized = self.__calculation_only_vacation_result(result)
        with self.__vacation_ical_lock:
            if calendar is not self.__vacation_ical_calendar:
                return {
                    "availability_state": "loading",
                    "available": False,
                    "automatic_prompt_allowed": False,
                    "using_last_good": False,
                    "error_code": "",
                }
            if self.__vacation_ical_week_cache_calendar is not calendar:
                self.__vacation_ical_week_cache_calendar = calendar
                self.__vacation_ical_week_cache = {}
            self.__vacation_ical_week_cache[cache_key] = dict(normalized)
        return {**normalized, **metadata}

    def __vacation_break_intervals(self, vacation) -> list[BreakInterval]:
        if not isinstance(vacation, dict) or bool(vacation.get("all_day")):
            return []
        intervals: list[BreakInterval] = []
        for span in vacation.get("intervals") or []:
            try:
                start, end = span
                if not isinstance(start, datetime) or not isinstance(end, datetime):
                    continue
                if start.tzinfo is not None:
                    start = start.astimezone().replace(tzinfo=None)
                if end.tzinfo is not None:
                    end = end.astimezone().replace(tzinfo=None)
                if end <= start:
                    continue
                intervals.append(BreakInterval(start, end, "휴가"))
            except Exception:
                continue
        return intervals

    def __today_overview(self, now, snapshot=None):
        current_snapshot = snapshot or self.__get_timelog_snapshot()
        plan = self.__plan_for_date(now.date())
        target_minutes = int(plan["target_net_minutes"])
        clock_in = self.__clock_in_from_plan(plan)
        intervals = self.__collect_break_intervals(now)
        vacation = self.__vacation_result_for_date(now.date())
        vacation_available = vacation.get("available", True) is True
        vacation_intervals = (
            self.__vacation_break_intervals(vacation)
            if vacation_available
            else []
        )
        recorded_minutes = current_snapshot.recorded_minutes_for(now.date())
        return build_workday_overview(
            now=now,
            clock_in=clock_in,
            target_minutes=target_minutes,
            intervals=intervals,
            vacation_intervals=vacation_intervals,
            vacation_all_day=bool(vacation.get("all_day")),
            vacation_available=vacation_available,
            vacation_state=str(vacation.get("availability_state") or "error"),
            recorded_minutes=recorded_minutes,
        )

    def __delta_text(self, delta) -> tuple[str, str]:
        if delta is None:
            return "조회 불가", "#6B7280"
        value = int(delta)
        if value < 0:
            return f"부족 {self.__format_minutes(-value)}", "#DC2626"
        if value > 0:
            return f"초과 {self.__format_minutes(value)}", "#059669"
        return "딱 맞음", "#059669"

    def __snapshot_sync_text(self, snapshot, now) -> str:
        pieces: list[str] = []
        if snapshot.fetched_at is not None:
            pieces.append(snapshot.fetched_at.strftime("%Y-%m-%d %H:%M:%S"))
            try:
                age_seconds = max(
                    0,
                    int((now - snapshot.fetched_at).total_seconds()),
                )
                if age_seconds < 60:
                    pieces.append("방금")
                else:
                    pieces.append(f"{age_seconds // 60}분 전")
            except Exception:
                pass
        state = snapshot.state.value
        pieces.append(state)
        if snapshot.error_code:
            pieces.append(self.__safe_timelog_error_code(snapshot.error_code))
        if snapshot.partial:
            pieces.append("partial")
        return " · ".join(pieces)

    def __live_activity_prompt_detected_at(
        self,
        now,
        day_plan=None,
        expected_detected_time: str | None = None,
    ) -> datetime | None:
        if not isinstance(now, datetime) or now.tzinfo is not None:
            return None
        try:
            resolved_plan = (
                day_plan
                if isinstance(day_plan, dict)
                else self.__plan_for_date(now.date())
            )
            if resolved_plan.get("clock_in"):
                return None
            prompt = self.__worktime_state_store.get_activity_prompt(now.date())
        except Exception:
            return None
        if not isinstance(prompt, dict) or prompt.get("status") != "pending":
            return None
        try:
            detected = datetime.fromisoformat(str(prompt.get("detected_at") or ""))
        except Exception:
            return None
        if detected.tzinfo is not None or detected.date() != now.date():
            return None
        if (
            expected_detected_time is not None
            and detected.strftime("%H:%M") != str(expected_detected_time).strip()
        ):
            return None
        try:
            vacation = self.__vacation_result_for_date(now.date())
        except Exception:
            return None
        if vacation.get("automatic_prompt_allowed") is not True:
            return None
        if vacation.get("all_day") is True:
            return None
        return detected

    def __visible_activity_prompt(self, now, day_plan=None):
        detected = self.__live_activity_prompt_detected_at(now, day_plan)
        if detected is None:
            return None
        return WorktimeActivityPrompt(detected.strftime("%H:%M"))

    def __build_worktime_panel_model(self) -> WorktimePanelModel:
        now = self.__lib.datetime.now()
        snapshot = self.__snapshot_for_now(now)
        self.__refresh_visible_panel_if_due(now, snapshot)
        week_start = now.date() - timedelta(days=now.weekday())
        week_days = [week_start + timedelta(days=index) for index in range(7)]
        overview = self.__today_overview(now, snapshot)
        delta_text, delta_color = self.__delta_text(
            overview.realtime_delta_minutes
        )
        recorded_text = (
            self.__format_minutes(overview.recorded_minutes)
            if overview.recorded_available and overview.recorded_minutes is not None
            else "조회 불가"
        )
        expected_text = (
            self.__format_minutes(overview.expected_now_minutes)
            if overview.expected_available
            else "조회 불가"
        )
        provisional = not overview.vacation_available
        expected_display = expected_text + (" (임시)" if provisional else "")
        clock_text = (
            overview.clock_in.strftime("%H:%M")
            if overview.clock_in is not None
            else "-"
        )
        quit_text = (
            overview.projected_quit.strftime("%H:%M")
            if overview.projected_quit is not None
            else "-"
        )
        sync_text = self.__snapshot_sync_text(snapshot, now)
        today_lines = (
            WorktimePanelLine(
                f"Wrike 기록 {recorded_text} · 현재 기대 {expected_display}",
                "#2563EB"
                if (
                    overview.recorded_available
                    and overview.expected_available
                    and not provisional
                )
                else "#6B7280",
            ),
            WorktimePanelLine(
                f"현재 기준 {delta_text}" + (" (임시)" if provisional else ""),
                delta_color,
            ),
            WorktimePanelLine(
                f"출근 {clock_text} · 예상 퇴근 {quit_text}"
                + (" (임시)" if provisional else ""),
                "#111827",
            ),
            WorktimePanelLine(
                f"병합 휴게 {self.__format_minutes(overview.break_total_minutes)}"
                + (" · 진행 중" if overview.manual_break_active else ""),
                "#6B7280",
            ),
            WorktimePanelLine(
                (
                    f"휴가 차감 {self.__format_minutes(overview.vacation_minutes)} · "
                    f"적용 목표 {self.__format_minutes(overview.effective_target_minutes)}"
                    if overview.vacation_available
                    else (
                        f"휴가 미확정 ({overview.vacation_state}) · "
                        "휴가 미반영 임시 목표 "
                        f"{self.__format_minutes(overview.effective_target_minutes)} (임시)"
                    )
                ),
                "#6B7280",
            ),
        )

        rows = []
        for index, target_day in enumerate(week_days):
            plan = self.__plan_for_date(target_day)
            target = int(plan["target_net_minutes"])
            vacation = self.__vacation_result_for_date(target_day)
            vacation_available = vacation.get("available", True) is True
            is_today = target_day == now.date()
            if not vacation_available:
                vacation_minutes = 0
            elif is_today:
                vacation_minutes = overview.vacation_minutes
            else:
                vacation_intervals = self.__vacation_break_intervals(vacation)
                vacation_all_day = bool(vacation.get("all_day"))
                if vacation_all_day or vacation_intervals:
                    break_intervals = self.__collect_break_intervals_for_day(
                        target_day,
                        now,
                    )
                    vacation_minutes = composed_vacation_credit_minutes(
                        target,
                        break_intervals,
                        vacation_intervals,
                        now,
                        all_day=vacation_all_day,
                    )
                else:
                    vacation_minutes = 0
            effective_target = max(0, target - vacation_minutes)
            recorded = snapshot.recorded_minutes_for(target_day)
            vacation_state = str(
                vacation.get("availability_state") or "error"
            )
            if target_day > now.date():
                if not vacation_available:
                    if target <= 0:
                        summary = (
                            f"휴무 · 휴가 미확정 ({vacation_state}) (임시)"
                        )
                    else:
                        summary = (
                            "휴가 미반영 임시 목표 "
                            f"{self.__format_minutes(target)} · "
                            f"휴가 미확정 ({vacation_state}) (임시)"
                        )
                elif target <= 0:
                    summary = "휴무"
                elif vacation_minutes > 0:
                    summary = (
                        f"목표 {self.__format_minutes(target)} · 휴가 "
                        f"{self.__format_minutes(vacation_minutes)} · 적용 "
                        f"{self.__format_minutes(effective_target)}"
                    )
                else:
                    summary = f"목표 {self.__format_minutes(target)}"
                color = "#6B7280"
            elif is_today:
                if overview.recorded_available and overview.expected_available:
                    status, color = self.__delta_text(
                        overview.realtime_delta_minutes
                    )
                    summary = (
                        f"Wrike {recorded_text} · 현재 기대 {expected_display} · {status}"
                    )
                    if provisional:
                        summary += f" · 휴가 미확정 ({overview.vacation_state})"
                elif not overview.expected_available:
                    summary = (
                        f"Wrike {recorded_text} · 현재 기대 조회 불가 · 휴가 미확정 "
                        f"({overview.vacation_state})"
                    )
                    color = "#6B7280"
                else:
                    summary = (
                        "Wrike 조회 불가 · 현재 기대 "
                        f"{expected_display}"
                    )
                    if provisional:
                        summary += f" · 휴가 미확정 ({overview.vacation_state})"
                    color = "#6B7280"
            elif not vacation_available:
                if recorded is None:
                    if target <= 0:
                        summary = (
                            f"Wrike 조회 불가 · 휴무 · 휴가 미확정 "
                            f"({vacation_state}) (임시)"
                        )
                    else:
                        summary = (
                            "Wrike 조회 불가 · 휴가 미반영 임시 목표 "
                            f"{self.__format_minutes(target)} · "
                            f"휴가 미확정 ({vacation_state}) (임시)"
                        )
                    color = "#6B7280"
                else:
                    delta = int(recorded) - int(target)
                    status, color = self.__delta_text(delta)
                    summary = (
                        f"Wrike {self.__format_minutes(recorded)} · "
                        "휴가 미반영 임시 목표 "
                        f"{self.__format_minutes(target)} · {status} · "
                        f"휴가 미확정 ({vacation_state}) (임시)"
                    )
            elif recorded is None:
                if effective_target <= 0:
                    summary = "휴무"
                else:
                    summary = (
                        f"Wrike 조회 불가 · 목표 "
                        f"{self.__format_minutes(effective_target)}"
                    )
                color = "#6B7280"
            else:
                delta = int(recorded) - int(effective_target)
                status, color = self.__delta_text(delta)
                summary = (
                    f"Wrike {self.__format_minutes(recorded)} · 목표 "
                    f"{self.__format_minutes(effective_target)} · {status}"
                )
            rows.append(
                WorktimePanelDayRow(
                    weekday=self.__time_log_weekday_labels[index],
                    date=target_day.strftime("%m/%d"),
                    date_key=target_day.isoformat(),
                    target_minutes=target,
                    summary=summary,
                    today=is_today,
                    color=color,
                )
            )
        break_state = self.__worktime_state_store.get_manual_break_state(now=now)
        today_plan = self.__plan_for_date(now.date())
        clock_in_time = str(today_plan.get("clock_in") or "").strip() or None
        return WorktimePanelModel(
            week_range=(
                f"{week_days[0].isoformat()} - {week_days[-1].isoformat()}"
            ),
            sync_text=sync_text,
            sync_state=snapshot.state.value,
            today_lines=today_lines,
            target_minutes=int(today_plan.get("target_net_minutes", 0)),
            clock_in_time=clock_in_time,
            break_active=bool(break_state.get("active")),
            rows=tuple(rows),
            prompt=self.__visible_activity_prompt(now, today_plan),
        )

    def __show_panel_action_error(self, message: str) -> None:
        root = self.__root
        if root is None:
            return
        try:
            self.__show_tooltip(root, str(message or "근무시간 저장 실패"))
        except Exception:
            pass
        return

    def __save_clock_in(
        self,
        clock_value: str,
        *,
        report_error: bool = True,
    ) -> bool:
        now = self.__lib.datetime.now()
        try:
            parsed = datetime.strptime(str(clock_value), "%H:%M")
            if parsed.strftime("%H:%M") != str(clock_value):
                raise ValueError("invalid clock")
        except Exception:
            if report_error:
                self.__show_panel_action_error(
                    "출근 시간은 HH:MM 형식이어야 합니다"
                )
            return False
        try:
            plan = self.__plan_for_date(now.date())
            target = max(0, min(1440, int(plan.get("target_net_minutes", 0))))
            ok, _error = self.update_workday_plan(
                now.date(),
                target,
                str(clock_value),
            )
        except Exception:
            ok = False
        if not ok:
            if report_error:
                self.__show_panel_action_error("출근 시간을 저장하지 못했습니다")
            return False
        return True

    def __panel_refresh(self) -> None:
        self.__request_timelog_snapshot_refresh(force=True)
        return

    def __panel_clock_in_now(self) -> None:
        self.__save_clock_in(self.__lib.datetime.now().strftime("%H:%M"))
        return

    def __panel_edit_clock_in(self, clock_value: str) -> bool:
        return self.__save_clock_in(
            str(clock_value).strip(),
            report_error=False,
        )

    def __panel_save_target_minutes(
        self,
        date_key: str,
        target_minutes: int,
    ) -> bool:
        if type(date_key) is not str or type(target_minutes) is not int:
            return False
        if not 0 <= target_minutes <= 1440:
            return False
        try:
            target_day = datetime.strptime(date_key, "%Y-%m-%d").date()
            if target_day.isoformat() != date_key:
                return False
            plan = self.__plan_for_date(target_day)
            clock_in = plan.get("clock_in")
            ok, _error = self.update_workday_plan(
                target_day,
                target_minutes,
                clock_in,
            )
        except Exception:
            ok = False
        return bool(ok)

    def __panel_toggle_break(self) -> None:
        try:
            state = self.toggle_manual_break()
        except Exception:
            state = {"ok": False}
        if not bool(state.get("ok", False)):
            self.__show_panel_action_error("휴게 상태를 저장하지 못했습니다")
        return

    def __panel_prompt_accept(self, detected_time: str) -> None:
        now = self.__lib.datetime.now()
        if self.__live_activity_prompt_detected_at(
            now,
            expected_detected_time=detected_time,
        ) is None:
            return
        self.__save_clock_in(str(detected_time).strip())
        return

    def __panel_prompt_edit(
        self,
        detected_time: str,
        edited_time: str,
    ) -> bool:
        now = self.__lib.datetime.now()
        if self.__live_activity_prompt_detected_at(
            now,
            expected_detected_time=detected_time,
        ) is None:
            return False
        return self.__save_clock_in(
            str(edited_time).strip(),
            report_error=False,
        )

    def __panel_prompt_snooze(self) -> None:
        now = self.__lib.datetime.now()
        if self.__live_activity_prompt_detected_at(now) is None:
            return
        try:
            ok, _error = self.__worktime_state_store.snooze_activity_prompt(
                now.date(),
                now + timedelta(minutes=30),
            )
        except Exception:
            ok = False
        if not ok:
            self.__show_panel_action_error("30분 후 알림을 저장하지 못했습니다")
            return
        day_key = now.date().isoformat()
        if self.__activity_prompt_surfaced_day == day_key:
            self.__activity_prompt_surfaced_day = ""
        return

    def __panel_prompt_skip(self) -> None:
        now = self.__lib.datetime.now()
        if self.__live_activity_prompt_detected_at(now) is None:
            return
        try:
            ok, _error = self.__worktime_state_store.skip_activity_prompt(
                now.date()
            )
        except Exception:
            ok = False
        if not ok:
            self.__show_panel_action_error("오늘 건너뛰기를 저장하지 못했습니다")
        return

    def __show_activity_panel(self) -> bool:
        root = self.__root
        if root is None:
            return False
        panel = self.__ensure_worktime_panel(root)
        if panel is None:
            return False
        try:
            shown = panel.show(activate=False)
            mapped = panel.is_visible()
        except Exception:
            return False
        if shown is not True or not mapped:
            return False
        self.__request_timelog_snapshot_refresh(force=False)
        return True

    def __surface_activity_panel(self, target_day) -> bool:
        try:
            day_key = target_day.isoformat()
        except Exception:
            return False
        if self.__activity_prompt_surfaced_day == day_key:
            return False
        if not self.__show_activity_panel():
            return False
        self.__activity_prompt_surfaced_day = day_key
        return True

    def __on_worktime_activity(self, detected_at) -> None:
        if not isinstance(detected_at, datetime) or detected_at.tzinfo is not None:
            return
        if (detected_at.hour, detected_at.minute) < (8, 0):
            return
        plan = self.__plan_for_date(detected_at.date())
        explicit = bool(plan.get("explicit", False))
        if plan.get("clock_in"):
            return
        if not explicit and detected_at.weekday() >= 5:
            return
        if int(plan.get("target_net_minutes", 0)) <= 0:
            return
        vacation = self.__vacation_result_for_date(detected_at.date())
        if vacation.get("automatic_prompt_allowed") is not True:
            return
        if bool(vacation.get("all_day")):
            return
        try:
            prompt = self.__worktime_state_store.get_activity_prompt(
                detected_at.date()
            )
        except Exception:
            prompt = None
        if isinstance(prompt, dict):
            status = str(prompt.get("status") or "")
            if status == "skipped":
                return
            if status == "pending":
                self.__surface_activity_panel(detected_at.date())
                return
            if status == "snoozed":
                try:
                    snooze_until = datetime.fromisoformat(
                        str(prompt.get("snooze_until") or "")
                    )
                except Exception:
                    return
                if snooze_until.tzinfo is not None or detected_at < snooze_until:
                    return
        try:
            ok, _error = self.__worktime_state_store.record_activity_prompt_pending(
                detected_at.date(),
                detected_at,
            )
        except Exception:
            ok = False
        if not ok:
            self.__show_panel_action_error("활동 알림을 저장하지 못했습니다")
            return
        self.__surface_activity_panel(detected_at.date())
        return

    # ------------------------------------------------------------------
    # Workday overview: persisted plan, break sources, calendar cache
    # ------------------------------------------------------------------

    def __record_daily_first_seen(self) -> None:
        """Legacy no-op: clock-in is now always an explicit persisted plan."""
        return

    def __default_workday_target_minutes(self) -> int:
        try:
            return max(0, min(1440, int(self.__daily_target_minutes)))
        except Exception:
            return int(self.__time_log_default_daily_minutes)

    def get_workday_plan(self, day=None) -> dict:
        return self.__worktime_state_store.get_day_plan(
            day,
            default_target_minutes=self.__default_workday_target_minutes(),
        )

    def update_workday_plan(
        self,
        day,
        target_minutes,
        clock_in,
    ) -> tuple[bool, str | None]:
        return self.__worktime_state_store.update_day_plan(
            day,
            target_minutes,
            clock_in,
        )

    def clear_workday_plan(self, day=None) -> tuple[bool, str | None]:
        return self.__worktime_state_store.clear_day_plan(day)

    def __clock_in_from_plan(self, plan: dict):
        if not isinstance(plan, dict):
            return None
        day_value = str(plan.get("date") or "").strip()
        clock_value = str(plan.get("clock_in") or "").strip()
        if not day_value or not clock_value:
            return None
        try:
            return datetime.strptime(
                f"{day_value}T{clock_value}",
                "%Y-%m-%dT%H:%M",
            )
        except Exception:
            return None

    def __resolve_clock_in_today(self, days: list[dict], today_key: str):
        _ = days
        try:
            plan = self.get_workday_plan(today_key)
        except Exception:
            return None
        return self.__clock_in_from_plan(plan)

    def __ensure_ical_day_cache(self, now) -> list[dict]:
        try:
            key = now.strftime("%Y-%m-%d")
        except Exception:
            return []
        if self.__ical_events_for_date == key and self.__ical_matched is not None:
            return self.__ical_matched
        try:
            matched = matching_break_events(
                self.__ical_parsed_events,
                list(self.__ical_keywords),
                now.date(),
            )
        except Exception:
            matched = []
        self.__ical_matched = matched
        self.__ical_events_for_date = key
        return matched

    def __collect_break_intervals(self, now) -> list[BreakInterval]:
        return self.__collect_break_intervals_for_day(now.date(), now)

    def __collect_break_intervals_for_day(
        self,
        target_day,
        now,
    ) -> list[BreakInterval]:
        intervals: list[BreakInterval] = []
        day_marker = datetime.combine(target_day, datetime.min.time())
        lunch = build_lunch_interval(
            day_marker,
            self.__lunch_break_enabled,
            self.__lunch_start_min,
            self.__lunch_end_min,
        )
        if lunch is not None:
            intervals.append(lunch)
        for entry in self.__ensure_ical_day_cache(day_marker):
            # Private calendar SUMMARY values are matching inputs only; never
            # expose them through live tooltip rows or logs.
            label = "캘린더"
            for span_start, span_end in entry.get("intervals") or []:
                try:
                    start_dt = (
                        span_start.replace(tzinfo=None)
                        if getattr(span_start, "tzinfo", None)
                        else span_start
                    )
                    end_dt = (
                        span_end.replace(tzinfo=None)
                        if (span_end is not None and getattr(span_end, "tzinfo", None))
                        else span_end
                    )
                    intervals.append(BreakInterval(start_dt, end_dt, label))
                except Exception:
                    continue
        try:
            intervals.extend(
                self.__worktime_state_store.break_intervals_for_day(
                    target_day,
                    now=now,
                )
            )
        except Exception as exc:
            self.__log_exception("manual break intervals failed", exc)
        return intervals

    def get_manual_break_state(self) -> dict:
        return self.__worktime_state_store.get_manual_break_state()

    def toggle_manual_break(self) -> dict:
        state = self.__worktime_state_store.toggle_manual_break()
        message = str(state.get("message") or "")
        root = self.__root
        if root is not None and message:
            self.__ui_safe(
                root,
                lambda message=message: self.__show_tooltip(root, message),
            )
        return state

    # ------------------------------------------------------------------
    # Google Calendar private-iCal polling and overview composition
    # ------------------------------------------------------------------

    def __mask_ical_url(self, url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        tail = raw[-18:] if len(raw) > 24 else raw
        return f"****{tail}"

    def __decode_ical_url(self) -> str:
        session_url = str(self.__ical_url_session or "").strip()
        if session_url:
            return session_url
        protected = str(self.__ical_url_protected or "").strip()
        if not protected:
            return ""
        try:
            decoded = self.__secret_store.unprotect(protected)
        except Exception:
            return ""
        cleaned = str(decoded or "").strip()
        if cleaned:
            self.__ical_url_session = cleaned
        return cleaned

    def __cancel_ical_after(self) -> None:
        root = self.__root
        after_id = self.__ical_after_id
        self.__ical_after_id = None
        if root is None or after_id is None:
            return
        try:
            root.after_cancel(after_id)
        except Exception:
            pass
        return

    def __schedule_ical_tick(self, initial_delay_sec: float = 2.0) -> None:
        root = self.__root
        if root is None or not self.__background_active:
            return
        if not str(self.__decode_ical_url() or "").strip():
            return
        interval_sec = max(60.0, float(self.__ical_poll_interval_sec or 900.0))
        delay_sec = max(1.0, float(initial_delay_sec)) if initial_delay_sec == 2.0 else min(
            max(1.0, float(initial_delay_sec)), interval_sec
        )
        try:
            delay_ms = int(delay_sec * 1000)
            self.__ical_after_id = root.after(delay_ms, self.__ical_tick)
        except Exception:
            self.__ical_after_id = None
        return

    def __start_ical_polling(self) -> None:
        self.__cancel_ical_after()
        if not self.__background_active:
            return
        if not str(self.__decode_ical_url() or "").strip():
            return
        self.__schedule_ical_tick(initial_delay_sec=2.0)
        return

    def __ical_tick(self) -> None:
        root = self.__root
        if root is None or not self.__background_active:
            return
        lifecycle_generation = int(self.__lifecycle_generation)
        if self.__ical_fetch_running:
            self.__schedule_ical_tick(initial_delay_sec=self.__ical_poll_interval_sec)
            return
        url = str(self.__decode_ical_url() or "").strip()
        if not url:
            return
        self.__ical_fetch_running = True

        def worker() -> None:
            text = fetch_calendar_text(url, float(DEFAULT_POLL_TIMEOUT_SEC))
            parsed_events = None
            if text:
                try:
                    parsed_events = parse_ics(text)
                except Exception:
                    parsed_events = None

            def apply_result() -> None:
                if (
                    lifecycle_generation != int(self.__lifecycle_generation)
                    or not self.__background_active
                    or root is not self.__root
                ):
                    return
                self.__ical_fetch_running = False
                if parsed_events is None:
                    self.__ical_last_error = "calendar_fetch_failed"
                    self.__log("ical fetch failed")
                else:
                    self.__ical_last_error = ""
                    self.__ical_last_success_ts = self.__lib.datetime.now().isoformat(timespec="seconds")
                    self.__ical_parsed_events = parsed_events
                    self.__ical_events_for_date = ""
                    self.__ical_matched = []
                    self.__log(f"ical events cached: {len(parsed_events)}")
                self.__schedule_ical_tick(initial_delay_sec=self.__ical_poll_interval_sec)

            self.__ui_safe(root, apply_result)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.__ical_fetch_running = False
            if lifecycle_generation == int(self.__lifecycle_generation):
                self.__schedule_ical_tick(initial_delay_sec=self.__ical_poll_interval_sec)
        return

    def __is_allowed_vacation_ical_url(self, url: str) -> bool:
        raw = str(url or "").strip()
        if not raw:
            return False
        try:
            parsed = urllib.parse.urlparse(raw)
            host = str(parsed.hostname or "").strip().lower().rstrip(".")
            port = parsed.port
        except Exception:
            return False
        if str(parsed.scheme or "").lower() != "https":
            return False
        if parsed.username or parsed.password or port not in (None, 443):
            return False
        return host == "calendar.google.com" or host.endswith(".googleusercontent.com")

    def __decode_vacation_ical_url(self) -> str:
        with self.__vacation_ical_lock:
            session_url = str(self.__vacation_ical_url_session or "").strip()
            protected = str(self.__vacation_ical_url_protected or "").strip()
        if session_url:
            return session_url if self.__is_allowed_vacation_ical_url(session_url) else ""
        if not protected:
            return ""
        try:
            decoded = self.__vacation_secret_store.unprotect(protected)
        except Exception:
            return ""
        cleaned = str(decoded or "").strip()
        if not self.__is_allowed_vacation_ical_url(cleaned):
            return ""
        with self.__vacation_ical_lock:
            if protected == str(self.__vacation_ical_url_protected or "").strip():
                self.__vacation_ical_url_session = cleaned
        return cleaned

    def __normalized_vacation_provider(self, value=None) -> str:
        provider = str(
            self.__vacation_calendar_provider if value is None else value
        ).strip()
        return provider if provider in VACATION_CALENDAR_PROVIDERS else "private_ical"

    def __cancel_vacation_google_oauth(self) -> None:
        with self.__vacation_ical_lock:
            cancel_event = self.__vacation_google_oauth_cancel_event
            self.__vacation_google_oauth_cancel_event = None
        if cancel_event is not None:
            try:
                cancel_event.set()
            except Exception:
                pass
        return

    def __revoke_uncommitted_google_oauth(self, envelope) -> bool:
        try:
            result = revoke_refresh_token(envelope)
        except Exception:
            result = GoogleCalendarError(
                GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
            )
        if isinstance(result, GoogleCalendarError):
            self.__log(
                "vacation oauth rollback failed: token_revocation_failed"
            )
            return False
        self.__log("vacation oauth uncommitted grant revoked")
        return True

    def __revoke_uncommitted_google_oauth_async(self, envelope) -> bool:
        try:
            threading.Thread(
                target=lambda: self.__revoke_uncommitted_google_oauth(envelope),
                daemon=True,
            ).start()
            return True
        except Exception:
            self.__log("vacation oauth rollback worker failed")
            return False

    def __decode_vacation_google_oauth(self):
        with self.__vacation_ical_lock:
            session = str(self.__vacation_google_oauth_session or "").strip()
            protected = str(self.__vacation_google_oauth_protected or "").strip()
        serialized = session
        if not serialized and protected:
            try:
                serialized = str(
                    self.__vacation_google_oauth_secret_store.unprotect(protected)
                    or ""
                ).strip()
            except Exception:
                serialized = ""
        if not serialized:
            return None
        result = deserialize_envelope(serialized)
        if not isinstance(result, GoogleCalendarSuccess):
            return None
        with self.__vacation_ical_lock:
            if (
                not session
                and protected
                == str(self.__vacation_google_oauth_protected or "").strip()
            ):
                self.__vacation_google_oauth_session = serialized
        return result.value

    def __active_vacation_configuration(self):
        provider = self.__normalized_vacation_provider()
        if provider == "private_ical":
            with self.__vacation_ical_lock:
                secret_present = bool(
                    str(self.__vacation_ical_url_protected or "").strip()
                    or str(self.__vacation_ical_url_session or "").strip()
                )
            url = self.__decode_vacation_ical_url()
            fingerprint = (
                hashlib.sha256((provider + "\0" + url).encode("utf-8")).hexdigest()
                if url
                else ""
            )
            return provider, secret_present, url or None, fingerprint

        with self.__vacation_ical_lock:
            secret_present = bool(
                str(self.__vacation_google_oauth_protected or "").strip()
                or str(self.__vacation_google_oauth_session or "").strip()
            )
            delete_pending = bool(
                self.__vacation_google_oauth_delete_pending
            )
        if delete_pending:
            return provider, secret_present, None, ""
        envelope = self.__decode_vacation_google_oauth()
        if envelope is None:
            return provider, secret_present, None, ""
        serialized_result = serialize_envelope(envelope)
        if not isinstance(serialized_result, GoogleCalendarSuccess):
            return provider, secret_present, None, ""
        fingerprint = hashlib.sha256(
            (provider + "\0" + serialized_result.value).encode("utf-8")
        ).hexdigest()
        return provider, secret_present, envelope, fingerprint

    def __current_vacation_week_start(self) -> str:
        try:
            today = self.__lib.datetime.now().date()
            return (today - timedelta(days=today.weekday())).isoformat()
        except Exception:
            return ""

    def __vacation_calendar_has_last_good(self, target_day=None) -> bool:
        with self.__vacation_ical_lock:
            has_calendar = bool(self.__vacation_ical_calendar)
            provider = self.__normalized_vacation_provider()
            covered_week = str(self.__vacation_google_oauth_week_start or "")
        if not has_calendar:
            return False
        if provider != "google_oauth":
            return True
        try:
            day = target_day or self.__lib.datetime.now().date()
            expected_week = (day - timedelta(days=day.weekday())).isoformat()
        except Exception:
            return False
        return bool(covered_week and covered_week == expected_week)

    def __clear_vacation_ical_cache(self) -> None:
        _provider, secret_present, configured, _fingerprint = (
            self.__active_vacation_configuration()
        )
        with self.__vacation_ical_lock:
            self.__vacation_ical_calendar = {}
            self.__vacation_ical_events_for_date = ""
            self.__vacation_ical_day_result = {}
            self.__vacation_ical_week_cache_calendar = None
            self.__vacation_ical_week_cache = {}
            self.__vacation_google_oauth_week_start = ""
            self.__vacation_ical_last_success_ts = None
            self.__vacation_ical_last_error = ""
            self.__vacation_ical_state = (
                "loading" if secret_present and configured is not None else "unconfigured"
            )
        return

    def get_vacation_ical_status_snapshot(self) -> dict:
        provider, secret_present, configuration, _fingerprint = (
            self.__active_vacation_configuration()
        )
        oauth_configured = (
            not bool(self.__vacation_google_oauth_delete_pending)
            and self.__decode_vacation_google_oauth() is not None
        )
        with self.__vacation_ical_lock:
            last_success = self.__vacation_ical_last_success_ts
            last_error = str(self.__vacation_ical_last_error or "").strip()
            fetch_running = self.__vacation_ical_fetch_owner is not None
            state = str(self.__vacation_ical_state or "error").strip().lower()
        configured = configuration is not None
        has_last_good = self.__vacation_calendar_has_last_good()
        error_code = last_error
        authorizing = (
            provider == "google_oauth"
            and state == "authorizing"
            and fetch_running
        )
        if authorizing:
            error_code = ""
        elif not secret_present:
            state = "unconfigured"
            error_code = ""
        elif not configured:
            state = "error"
            error_code = VACATION_ERROR_SECRET_UNAVAILABLE
        elif state not in VACATION_STATES:
            state = "error"
            error_code = error_code or VACATION_ERROR_FETCH_FAILED
        if state == "error" and not error_code:
            error_code = VACATION_ERROR_FETCH_FAILED
        elif error_code and error_code not in VACATION_STATUS_ERROR_CODES:
            error_code = VACATION_ERROR_FETCH_FAILED
        return {
            "provider": provider,
            "oauth_configured": oauth_configured,
            "secret_present": secret_present,
            "configured": configured,
            "expected_calendar_name": "",
            "observed_calendar_name": "",
            "state": state,
            "last_success_ts": last_success,
            "error_code": error_code,
            "fetch_running": fetch_running,
            "has_last_good": has_last_good,
            "automatic_prompt_allowed": state in {"unconfigured", "fresh"},
        }

    def __fetch_vacation_calendar_text(
        self,
        url: str,
        timeout_sec: float = DEFAULT_POLL_TIMEOUT_SEC,
    ) -> CalendarSuccess[str] | CalendarError:
        cleaned = str(url or "").strip()
        if not self.__is_allowed_vacation_ical_url(cleaned):
            return CalendarError(CalendarErrorCode.INVALID_ENDPOINT)
        validator = self.__is_allowed_vacation_ical_url

        class VacationRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(
                handler_self,
                request,
                fp,
                code,
                message,
                headers,
                new_url,
            ):
                absolute_url = urllib.parse.urljoin(request.full_url, new_url)
                if not validator(absolute_url):
                    try:
                        login_host = (
                            urllib.parse.urlparse(absolute_url).hostname or ""
                        ).rstrip(".").lower()
                    except Exception:
                        login_host = ""
                    if login_host in {"accounts.google.com", "login.microsoftonline.com"}:
                        raise _VacationAuthenticationRequired() from None
                    raise _VacationRedirectRejected() from None
                return super().redirect_request(
                    request,
                    fp,
                    code,
                    message,
                    headers,
                    absolute_url,
                )

        def http_error_for(status) -> CalendarError | None:
            try:
                code = int(status)
            except Exception:
                return None
            if code in {401, 403}:
                return CalendarError(CalendarErrorCode.AUTHENTICATION_REQUIRED)
            if 400 <= code <= 499:
                return CalendarError(CalendarErrorCode.HTTP_4XX)
            if 500 <= code <= 599:
                return CalendarError(CalendarErrorCode.HTTP_5XX)
            if 300 <= code <= 399:
                return CalendarError(CalendarErrorCode.REDIRECT_REJECTED)
            return None

        try:
            request = urllib.request.Request(
                cleaned,
                headers={
                    "User-Agent": "windows-supporter/vacation-ical",
                    "Accept": "text/calendar",
                    "Accept-Encoding": "gzip, identity",
                },
            )
            opener = urllib.request.build_opener(VacationRedirectHandler())
            with opener.open(
                request,
                timeout=max(5.0, float(timeout_sec)),
            ) as response:
                final_url = str(response.geturl() or "").strip()
                if not self.__is_allowed_vacation_ical_url(final_url):
                    return CalendarError(CalendarErrorCode.REDIRECT_REJECTED)
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if callable(getcode) else None
                http_error = http_error_for(status)
                if http_error is not None:
                    return http_error
                return decode_calendar_response(response)
        except _VacationAuthenticationRequired:
            return CalendarError(CalendarErrorCode.AUTHENTICATION_REQUIRED)
        except _VacationRedirectRejected:
            return CalendarError(CalendarErrorCode.REDIRECT_REJECTED)
        except urllib.error.HTTPError as exc:
            return http_error_for(getattr(exc, "code", None)) or CalendarError(
                CalendarErrorCode.DNS_OR_CONNECT
            )
        except (TimeoutError, socket.timeout):
            return CalendarError(CalendarErrorCode.TIMEOUT)
        except (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError):
            return CalendarError(CalendarErrorCode.TLS_VALIDATION)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return CalendarError(CalendarErrorCode.TIMEOUT)
            if isinstance(
                reason,
                (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError),
            ):
                return CalendarError(CalendarErrorCode.TLS_VALIDATION)
            return CalendarError(CalendarErrorCode.DNS_OR_CONNECT)
        except (ConnectionError, socket.gaierror, OSError):
            return CalendarError(CalendarErrorCode.DNS_OR_CONNECT)
        except Exception:
            return CalendarError(CalendarErrorCode.DNS_OR_CONNECT)

    def __cancel_vacation_ical_after(self) -> None:
        root = self.__root
        after_id = self.__vacation_ical_after_id
        self.__vacation_ical_after_id = None
        if root is None or after_id is None:
            return
        try:
            root.after_cancel(after_id)
        except Exception:
            pass
        return

    def begin_google_calendar_oauth(
        self,
        client_config_path,
    ) -> tuple[bool, str | None]:
        config_result = load_desktop_client_config(client_config_path)
        if isinstance(config_result, GoogleCalendarError):
            return False, config_result.code.value
        if not isinstance(config_result, GoogleCalendarSuccess):
            return False, GoogleCalendarErrorCode.CLIENT_CONFIG_INVALID.value
        root = self.__root
        if root is None or not self.__background_active:
            return False, VACATION_ERROR_FETCH_FAILED

        self.__cancel_vacation_ical_after()
        self.__cancel_vacation_google_oauth()
        self.__vacation_ical_generation += 1
        with self.__vacation_ical_lock:
            self.__vacation_ical_fetch_owner = None

        previous_runtime_state = self.__capture_settings_runtime_state()
        self.__vacation_calendar_provider = "google_oauth"
        with self.__vacation_ical_lock:
            self.__vacation_ical_calendar = {}
            self.__vacation_ical_events_for_date = ""
            self.__vacation_ical_day_result = {}
            self.__vacation_ical_week_cache_calendar = None
            self.__vacation_ical_week_cache = {}
            self.__vacation_google_oauth_week_start = ""
            self.__vacation_ical_last_success_ts = None
            self.__vacation_ical_last_error = ""
            self.__vacation_ical_state = "unconfigured"
        if not self.__save_settings():
            self.__restore_settings_runtime_state(previous_runtime_state)
            self.__start_vacation_ical_polling()
            return False, VACATION_ERROR_SECRET_UNAVAILABLE

        generation = int(self.__vacation_ical_generation)
        cancel_event = threading.Event()
        with self.__vacation_ical_lock:
            auth_owner = (generation, object())
            self.__vacation_google_oauth_cancel_event = cancel_event
            self.__vacation_ical_fetch_owner = auth_owner
            self.__vacation_ical_state = "authorizing"
            self.__vacation_ical_last_error = ""

        def worker() -> None:
            try:
                auth_result = authorize_desktop(
                    config_result.value,
                    VACATION_EXPECTED_CALENDAR_NAME,
                    cancel_event=cancel_event,
                )
            except Exception:
                auth_result = GoogleCalendarError(
                    GoogleCalendarErrorCode.INVALID_RESPONSE
                )
            grant_envelope = (
                auth_result.value
                if isinstance(auth_result, GoogleCalendarSuccess)
                else None
            )
            grant_settled = threading.Event()

            def rollback_sync() -> bool:
                if grant_envelope is None or grant_settled.is_set():
                    return True
                grant_settled.set()
                return self.__revoke_uncommitted_google_oauth(grant_envelope)

            def rollback_async() -> bool:
                if grant_envelope is None or grant_settled.is_set():
                    return True
                if self.__revoke_uncommitted_google_oauth_async(
                    grant_envelope
                ):
                    grant_settled.set()
                    return True
                return rollback_sync()

            if grant_envelope is not None and cancel_event.is_set():
                revoked = rollback_sync()
                auth_result = GoogleCalendarError(
                    GoogleCalendarErrorCode.AUTHORIZATION_CANCELLED
                    if revoked
                    else GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
                )
            serialized = ""
            protected = ""
            failure_code = VACATION_ERROR_FETCH_FAILED
            if isinstance(auth_result, GoogleCalendarError):
                failure_code = auth_result.code.value
            elif isinstance(auth_result, GoogleCalendarSuccess):
                serialized_result = serialize_envelope(auth_result.value)
                if isinstance(serialized_result, GoogleCalendarSuccess):
                    serialized = serialized_result.value
                    try:
                        protected = str(
                            self.__vacation_google_oauth_secret_store.protect(
                                serialized
                            )
                            or ""
                        ).strip()
                    except Exception:
                        protected = ""
                    failure_code = (
                        "" if protected else VACATION_ERROR_SECRET_UNAVAILABLE
                    )
                else:
                    failure_code = VACATION_ERROR_SECRET_UNAVAILABLE

            if failure_code and grant_envelope is not None:
                if not rollback_sync():
                    failure_code = (
                        GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED.value
                    )

            apply_started = threading.Event()
            apply_done = threading.Event()

            def apply_result_body() -> None:
                with self.__vacation_ical_lock:
                    if self.__vacation_ical_fetch_owner is not auth_owner:
                        rollback_async()
                        return
                    if self.__vacation_google_oauth_cancel_event is cancel_event:
                        self.__vacation_google_oauth_cancel_event = None
                    if (
                        cancel_event.is_set()
                        or generation != int(self.__vacation_ical_generation)
                        or self.__normalized_vacation_provider()
                        != "google_oauth"
                        or root is not self.__root
                        or not self.__background_active
                    ):
                        self.__vacation_ical_fetch_owner = None
                        rollback_async()
                        return
                    if failure_code:
                        self.__vacation_ical_fetch_owner = None
                        stable_code = (
                            failure_code
                            if failure_code in VACATION_STATUS_ERROR_CODES
                            else VACATION_ERROR_FETCH_FAILED
                        )
                        self.__vacation_ical_state = "error"
                        self.__vacation_ical_last_error = stable_code
                        self.__log(
                            f"vacation oauth authorization failed: {stable_code}"
                        )
                        return
                    previous_protected = self.__vacation_google_oauth_protected
                    previous_session = self.__vacation_google_oauth_session
                    previous_delete_pending = (
                        self.__vacation_google_oauth_delete_pending
                    )
                    self.__vacation_google_oauth_protected = protected
                    self.__vacation_google_oauth_session = serialized
                    self.__vacation_google_oauth_delete_pending = False
                if not self.__save_settings():
                    rollback_async()
                    with self.__vacation_ical_lock:
                        if self.__vacation_ical_fetch_owner is not auth_owner:
                            return
                        self.__vacation_google_oauth_protected = previous_protected
                        self.__vacation_google_oauth_session = previous_session
                        self.__vacation_google_oauth_delete_pending = (
                            previous_delete_pending
                        )
                        self.__vacation_ical_fetch_owner = None
                        self.__vacation_ical_state = "error"
                        self.__vacation_ical_last_error = (
                            VACATION_ERROR_SECRET_UNAVAILABLE
                        )
                    self.__log(
                        "vacation oauth authorization failed: secret_unavailable"
                    )
                    return
                owner_lost = False
                restore_needed = False
                with self.__vacation_ical_lock:
                    if self.__vacation_ical_fetch_owner is not auth_owner:
                        owner_lost = True
                        if (
                            self.__vacation_google_oauth_protected == protected
                            and self.__vacation_google_oauth_session == serialized
                        ):
                            self.__vacation_google_oauth_protected = (
                                previous_protected
                            )
                            self.__vacation_google_oauth_session = previous_session
                            self.__vacation_google_oauth_delete_pending = (
                                previous_delete_pending
                            )
                            restore_needed = True
                    else:
                        grant_settled.set()
                        self.__vacation_google_oauth_delete_pending = False
                        self.__vacation_ical_fetch_owner = None
                        self.__vacation_ical_calendar = {}
                        self.__vacation_ical_events_for_date = ""
                        self.__vacation_ical_day_result = {}
                        self.__vacation_ical_week_cache_calendar = None
                        self.__vacation_ical_week_cache = {}
                        self.__vacation_google_oauth_week_start = ""
                        self.__vacation_ical_last_success_ts = None
                        self.__vacation_ical_state = "loading"
                        self.__vacation_ical_last_error = ""
                if owner_lost:
                    if restore_needed:
                        self.__save_settings()
                    rollback_async()
                    return
                self.__log("vacation oauth authorization completed")
                self.__vacation_ical_tick(generation)

            def apply_result() -> None:
                apply_started.set()
                try:
                    apply_result_body()
                finally:
                    apply_done.set()

            queued = self.__ui_safe(root, apply_result)
            if not queued:
                rollback_sync()
                with self.__vacation_ical_lock:
                    if self.__vacation_ical_fetch_owner is auth_owner:
                        self.__vacation_ical_fetch_owner = None
                        self.__vacation_ical_state = "error"
                        self.__vacation_ical_last_error = (
                            VACATION_ERROR_FETCH_FAILED
                        )
            elif not apply_done.wait(5.0):
                if not apply_started.is_set():
                    with self.__vacation_ical_lock:
                        if self.__vacation_ical_fetch_owner is auth_owner:
                            self.__vacation_ical_fetch_owner = None
                            self.__vacation_ical_state = "error"
                            self.__vacation_ical_last_error = (
                                VACATION_ERROR_FETCH_FAILED
                            )
                    rollback_sync()
                else:
                    apply_done.wait()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            with self.__vacation_ical_lock:
                if self.__vacation_ical_fetch_owner is auth_owner:
                    self.__vacation_ical_fetch_owner = None
                    if self.__vacation_google_oauth_cancel_event is cancel_event:
                        self.__vacation_google_oauth_cancel_event = None
                    self.__vacation_ical_state = "error"
                    self.__vacation_ical_last_error = VACATION_ERROR_FETCH_FAILED
            self.__log("vacation oauth authorization worker failed")
            return False, VACATION_ERROR_FETCH_FAILED
        return True, None

    def disconnect_google_calendar_oauth(self) -> tuple[bool, str | None]:
        with self.__vacation_ical_lock:
            secret_present = bool(
                str(self.__vacation_google_oauth_protected or "").strip()
                or str(self.__vacation_google_oauth_session or "").strip()
            )
            delete_pending = bool(
                self.__vacation_google_oauth_delete_pending
            )
        envelope = self.__decode_vacation_google_oauth()
        with self.__vacation_ical_lock:
            stored_protected = str(
                self.__vacation_google_oauth_protected or ""
            ).strip()
            stored_session = str(
                self.__vacation_google_oauth_session or ""
            ).strip()
        self.__cancel_vacation_ical_after()
        self.__cancel_vacation_google_oauth()
        self.__vacation_ical_generation += 1
        generation = int(self.__vacation_ical_generation)
        self.__vacation_calendar_provider = "google_oauth"
        with self.__vacation_ical_lock:
            self.__vacation_ical_fetch_owner = None

        def complete_local_delete(revoked: bool) -> bool:
            previous_runtime_state = self.__capture_settings_runtime_state()
            with self.__vacation_ical_lock:
                self.__vacation_google_oauth_delete_pending = False
                self.__vacation_google_oauth_protected = ""
                self.__vacation_google_oauth_session = ""
                self.__vacation_google_oauth_week_start = ""
                self.__vacation_ical_calendar = {}
                self.__vacation_ical_events_for_date = ""
                self.__vacation_ical_day_result = {}
                self.__vacation_ical_week_cache_calendar = None
                self.__vacation_ical_week_cache = {}
                self.__vacation_ical_last_success_ts = None
                self.__vacation_ical_last_error = ""
                self.__vacation_ical_state = "disconnecting"
            if not self.__save_settings():
                self.__restore_settings_runtime_state(previous_runtime_state)
                with self.__vacation_ical_lock:
                    self.__vacation_google_oauth_delete_pending = bool(
                        revoked
                    )
                    self.__vacation_ical_state = "error"
                    self.__vacation_ical_last_error = (
                        VACATION_ERROR_SECRET_UNAVAILABLE
                    )
                self.__log("vacation oauth disconnect local save failed")
                return False
            with self.__vacation_ical_lock:
                self.__vacation_google_oauth_delete_pending = False
                self.__vacation_ical_state = "unconfigured"
                self.__vacation_ical_last_error = ""
            return True

        def record_revoked_pending() -> bool:
            with self.__vacation_ical_lock:
                if (
                    str(self.__vacation_google_oauth_protected or "").strip()
                    != stored_protected
                    or str(self.__vacation_google_oauth_session or "").strip()
                    != stored_session
                ):
                    return False
                self.__vacation_google_oauth_delete_pending = True
                self.__vacation_ical_state = "disconnecting"
                self.__vacation_ical_last_error = ""
            if self.__save_settings():
                return True
            with self.__vacation_ical_lock:
                self.__vacation_ical_state = "error"
                self.__vacation_ical_last_error = (
                    VACATION_ERROR_SECRET_UNAVAILABLE
                )
            self.__log("vacation oauth revoke receipt save failed")
            return False

        if delete_pending:
            if complete_local_delete(True):
                self.__log("vacation oauth revoked grant removed locally")
                return True, None
            return False, VACATION_ERROR_SECRET_UNAVAILABLE

        if envelope is None:
            if secret_present:
                with self.__vacation_ical_lock:
                    self.__vacation_ical_state = "error"
                    self.__vacation_ical_last_error = (
                        VACATION_ERROR_SECRET_UNAVAILABLE
                    )
                return False, VACATION_ERROR_SECRET_UNAVAILABLE
            if complete_local_delete(False):
                return True, None
            return False, VACATION_ERROR_SECRET_UNAVAILABLE

        with self.__vacation_ical_lock:
            revoke_owner = (generation, object())
            self.__vacation_ical_fetch_owner = revoke_owner
            self.__vacation_ical_state = "disconnecting"
            self.__vacation_ical_last_error = ""

        def worker() -> None:
            try:
                revoke_result = revoke_refresh_token(envelope)
            except Exception:
                revoke_result = GoogleCalendarError(
                    GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED
                )

            completion_lock = threading.Lock()
            completion_claimed = False
            apply_done = threading.Event()

            def apply_result_body() -> None:
                with self.__vacation_ical_lock:
                    owner_matches = (
                        self.__vacation_ical_fetch_owner is revoke_owner
                    )
                    if owner_matches:
                        self.__vacation_ical_fetch_owner = None
                    if isinstance(revoke_result, GoogleCalendarError):
                        if owner_matches:
                            self.__vacation_ical_state = "error"
                            self.__vacation_ical_last_error = (
                                GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED.value
                            )
                        self.__log(
                            "vacation oauth disconnect failed: "
                            "token_revocation_failed"
                        )
                        return
                if not record_revoked_pending():
                    return
                if not complete_local_delete(True):
                    return
                self.__log("vacation oauth disconnected and grant revoked")

            def apply_result() -> None:
                nonlocal completion_claimed
                with completion_lock:
                    if completion_claimed:
                        return
                    completion_claimed = True
                try:
                    apply_result_body()
                finally:
                    apply_done.set()

            queued = self.__ui_safe(self.__root, apply_result)
            if not queued:
                apply_result()
            elif not apply_done.wait(5.0):
                apply_result()
                apply_done.wait()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            with self.__vacation_ical_lock:
                if self.__vacation_ical_fetch_owner is revoke_owner:
                    self.__vacation_ical_fetch_owner = None
                    self.__vacation_ical_state = "error"
                    self.__vacation_ical_last_error = (
                        GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED.value
                    )
            return False, GoogleCalendarErrorCode.TOKEN_REVOCATION_FAILED.value
        return True, None

    def retry_vacation_ical(self) -> tuple[bool, str | None]:
        """Immediately recheck the selected provider without exposing secrets."""

        _provider, secret_present, configuration, _fingerprint = (
            self.__active_vacation_configuration()
        )
        has_last_good = self.__vacation_calendar_has_last_good()
        if not secret_present or configuration is None:
            with self.__vacation_ical_lock:
                if secret_present:
                    self.__vacation_ical_state = "error"
                    self.__vacation_ical_last_error = (
                        VACATION_ERROR_SECRET_UNAVAILABLE
                    )
            return False, VACATION_ERROR_SECRET_UNAVAILABLE
        if self.__root is None or not self.__background_active:
            return False, VACATION_ERROR_FETCH_FAILED

        self.__cancel_vacation_ical_after()
        self.__vacation_ical_generation += 1
        generation = int(self.__vacation_ical_generation)
        with self.__vacation_ical_lock:
            self.__vacation_ical_fetch_owner = None
            self.__vacation_ical_state = "stale" if has_last_good else "loading"
            self.__vacation_ical_last_error = ""
        self.__vacation_ical_tick(generation)
        return True, None

    def __schedule_vacation_ical_tick(
        self,
        initial_delay_sec: float = 2.0,
        generation: int | None = None,
    ) -> None:
        current_generation = int(self.__vacation_ical_generation)
        target_generation = (
            current_generation if generation is None else int(generation)
        )
        if target_generation != current_generation:
            return
        root = self.__root
        _provider, _secret_present, configuration, _fingerprint = (
            self.__active_vacation_configuration()
        )
        if root is None or not self.__background_active or configuration is None:
            return
        interval_sec = max(
            60.0,
            float(self.__vacation_ical_poll_interval_sec or 900.0),
        )
        delay_sec = (
            max(1.0, float(initial_delay_sec))
            if initial_delay_sec == 2.0
            else min(max(1.0, float(initial_delay_sec)), interval_sec)
        )
        self.__cancel_vacation_ical_after()
        try:
            self.__vacation_ical_after_id = root.after(
                int(delay_sec * 1000),
                lambda generation=target_generation: self.__vacation_ical_tick(
                    generation
                ),
            )
        except Exception:
            self.__vacation_ical_after_id = None
        return

    def __start_vacation_ical_polling(self) -> None:
        self.__cancel_vacation_ical_after()
        self.__vacation_ical_generation += 1
        generation = int(self.__vacation_ical_generation)
        provider, secret_present, configuration, _fingerprint = (
            self.__active_vacation_configuration()
        )
        has_last_good = self.__vacation_calendar_has_last_good()
        with self.__vacation_ical_lock:
            self.__vacation_ical_fetch_owner = None
            self.__vacation_ical_observed_calendar_name = ""
            if not secret_present:
                self.__vacation_ical_state = "unconfigured"
                self.__vacation_ical_last_error = ""
            elif configuration is None:
                self.__vacation_ical_state = "error"
                self.__vacation_ical_last_error = (
                    VACATION_ERROR_SECRET_UNAVAILABLE
                )
            else:
                self.__vacation_ical_state = (
                    "stale" if has_last_good else "loading"
                )
                if provider == "google_oauth" and not has_last_good:
                    self.__vacation_google_oauth_week_start = ""
        if not self.__background_active or configuration is None:
            return
        self.__schedule_vacation_ical_tick(
            initial_delay_sec=2.0,
            generation=generation,
        )
        return

    def __vacation_ical_tick(self, generation: int | None = None) -> None:
        current_generation = int(self.__vacation_ical_generation)
        target_generation = (
            current_generation if generation is None else int(generation)
        )
        if target_generation != current_generation or not self.__background_active:
            return
        root = self.__root
        if root is None:
            return
        self.__cancel_vacation_ical_after()
        with self.__vacation_ical_lock:
            fetch_running = self.__vacation_ical_fetch_owner is not None
        if fetch_running:
            self.__schedule_vacation_ical_tick(
                initial_delay_sec=2.0,
                generation=target_generation,
            )
            return
        provider, _secret_present, configuration, configuration_fingerprint = (
            self.__active_vacation_configuration()
        )
        if configuration is None or not configuration_fingerprint:
            return
        expected_name = VACATION_EXPECTED_CALENDAR_NAME
        try:
            now_day = self.__lib.datetime.now().date()
            week_start = now_day - timedelta(days=now_day.weekday())
            week_start_key = week_start.isoformat()
        except Exception:
            return
        with self.__vacation_ical_lock:
            fetch_owner = (target_generation, object())
            self.__vacation_ical_fetch_owner = fetch_owner
            self.__vacation_ical_state = (
                "stale" if self.__vacation_calendar_has_last_good() else "loading"
            )

        def worker() -> None:
            compiled_calendar = None
            parse_succeeded = False
            calendar_matches = False
            failure_code = VACATION_ERROR_FETCH_FAILED
            if provider == "google_oauth":
                try:
                    fetch_result = fetch_vacation_calendar(
                        configuration,
                        week_start,
                        float(DEFAULT_POLL_TIMEOUT_SEC),
                    )
                except Exception:
                    fetch_result = GoogleCalendarError(
                        GoogleCalendarErrorCode.API_UNAVAILABLE
                    )
                if isinstance(fetch_result, GoogleCalendarError):
                    failure_code = fetch_result.code.value
                elif (
                    isinstance(fetch_result, GoogleCalendarSuccess)
                    and isinstance(fetch_result.value, dict)
                    and fetch_result.value.get("calendar_matched") is True
                ):
                    compiled_calendar = fetch_result.value
                    parse_succeeded = True
                    calendar_matches = True
                    failure_code = ""
                else:
                    failure_code = GoogleCalendarErrorCode.INVALID_RESPONSE.value
            else:
                fetch_result = self.__fetch_vacation_calendar_text(
                    configuration,
                    float(DEFAULT_POLL_TIMEOUT_SEC),
                )
                parsed_calendar = None
                if isinstance(fetch_result, CalendarError):
                    failure_code = fetch_result.code.value
                elif isinstance(fetch_result, CalendarSuccess):
                    try:
                        parse_result = parse_calendar_document(fetch_result.value)
                    except Exception:
                        parse_result = CalendarError(CalendarErrorCode.INVALID_ICAL)
                    if isinstance(parse_result, CalendarSuccess) and isinstance(
                        parse_result.value,
                        dict,
                    ):
                        parsed_calendar = parse_result.value
                        parse_succeeded = True
                        failure_code = ""
                    elif isinstance(parse_result, CalendarError):
                        failure_code = parse_result.code.value
                    else:
                        failure_code = CalendarErrorCode.INVALID_ICAL.value
                observed_name = ""
                if isinstance(parsed_calendar, dict):
                    observed_name = str(
                        parsed_calendar.get("calendar_name") or ""
                    ).strip()
                calendar_matches = bool(
                    parse_succeeded and observed_name == expected_name
                )
                if calendar_matches:
                    try:
                        candidate = compile_vacation_calendar(
                            parsed_calendar,
                            expected_name,
                        )
                    except Exception:
                        candidate = None
                    if (
                        isinstance(candidate, dict)
                        and candidate.get("calendar_matched") is True
                    ):
                        compiled_calendar = candidate
                    else:
                        parse_succeeded = False
                        failure_code = CalendarErrorCode.INVALID_ICAL.value
                parsed_calendar = None
                observed_name = ""

            def apply_result() -> None:
                with self.__vacation_ical_lock:
                    if self.__vacation_ical_fetch_owner is not fetch_owner:
                        return
                    self.__vacation_ical_fetch_owner = None
                if target_generation != int(self.__vacation_ical_generation):
                    return
                (
                    current_provider,
                    _current_secret_present,
                    current_configuration,
                    current_fingerprint,
                ) = self.__active_vacation_configuration()
                if (
                    current_provider != provider
                    or current_configuration is None
                    or current_fingerprint != configuration_fingerprint
                ):
                    self.__schedule_vacation_ical_tick(
                        initial_delay_sec=2.0,
                        generation=target_generation,
                    )
                    return

                log_message = ""
                with self.__vacation_ical_lock:
                    self.__vacation_ical_observed_calendar_name = ""
                    if not parse_succeeded:
                        stable_code = (
                            failure_code
                            if failure_code in VACATION_STATUS_ERROR_CODES
                            else VACATION_ERROR_FETCH_FAILED
                        )
                        self.__vacation_ical_state = "error"
                        self.__vacation_ical_last_error = stable_code
                        log_message = (
                            f"vacation calendar failed: {stable_code}; "
                            "last-good retained"
                        )
                    elif not calendar_matches:
                        self.__vacation_ical_state = "error"
                        self.__vacation_ical_last_error = (
                            VACATION_ERROR_NAME_MISMATCH
                        )
                        log_message = (
                            "vacation calendar failed: calendar_name_mismatch; "
                            "last-good retained"
                        )
                    else:
                        self.__vacation_ical_state = "fresh"
                        self.__vacation_ical_last_error = ""
                        self.__vacation_ical_last_success_ts = (
                            self.__lib.datetime.now().isoformat(
                                timespec="seconds"
                            )
                        )
                        self.__vacation_ical_calendar = compiled_calendar
                        self.__vacation_google_oauth_week_start = (
                            week_start_key if provider == "google_oauth" else ""
                        )
                        self.__vacation_ical_events_for_date = ""
                        self.__vacation_ical_day_result = {}
                        self.__vacation_ical_week_cache_calendar = None
                        self.__vacation_ical_week_cache = {}
                        log_message = "vacation calendar cached"
                if log_message:
                    self.__log(log_message)
                self.__schedule_vacation_ical_tick(
                    initial_delay_sec=self.__vacation_ical_poll_interval_sec,
                    generation=target_generation,
                )

            if not self.__ui_safe(root, apply_result):
                with self.__vacation_ical_lock:
                    if self.__vacation_ical_fetch_owner is fetch_owner:
                        self.__vacation_ical_fetch_owner = None
                        self.__vacation_ical_state = "error"
                        self.__vacation_ical_last_error = (
                            VACATION_ERROR_FETCH_FAILED
                        )

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            with self.__vacation_ical_lock:
                if self.__vacation_ical_fetch_owner is not fetch_owner:
                    return
                self.__vacation_ical_fetch_owner = None
            if (
                target_generation != int(self.__vacation_ical_generation)
                or not self.__background_active
            ):
                return
            with self.__vacation_ical_lock:
                self.__vacation_ical_state = "error"
                self.__vacation_ical_last_error = VACATION_ERROR_FETCH_FAILED
            self.__log("vacation calendar fetch worker failed; last-good retained")
            self.__schedule_vacation_ical_tick(
                initial_delay_sec=self.__vacation_ical_poll_interval_sec,
                generation=target_generation,
            )
        return

    def __ensure_vacation_ical_day_cache(self, now) -> dict:
        try:
            key = now.strftime("%Y-%m-%d")
            target_day = now.date()
        except Exception:
            return {}
        with self.__vacation_ical_lock:
            if self.__vacation_ical_events_for_date == key:
                return dict(self.__vacation_ical_day_result)
            calendar = self.__vacation_ical_calendar
        try:
            result = vacation_events_for_day(
                calendar,
                VACATION_EXPECTED_CALENDAR_NAME,
                target_day,
            )
        except Exception:
            result = {}
        result = self.__calculation_only_vacation_result(result)
        with self.__vacation_ical_lock:
            if calendar is not self.__vacation_ical_calendar:
                return {}
            self.__vacation_ical_events_for_date = key
            self.__vacation_ical_day_result = result
            return dict(result)

    def __build_overview_rows(
        self,
        days: list[dict],
        daily_target_minutes: int,
        snapshot=None,
    ) -> list[tuple[str, str]]:
        _ = (days, daily_target_minutes)
        now = self.__lib.datetime.now()
        authoritative_snapshot = snapshot or self.__get_timelog_snapshot()
        overview = self.__today_overview(now, authoritative_snapshot)
        return overview.as_lines(now)

    def __count_target_days(self, week_dates: list) -> int:
        if not week_dates:
            return 0
        weekdays = set(int(x) for x in self.__monitor_weekdays)
        count = 0
        for dt in week_dates:
            try:
                if int(dt.weekday()) in weekdays:
                    count += 1
            except Exception:
                continue
        return int(count)

    def __build_monitor_summary(self, display_name: str, days: list[dict], total_minutes: int) -> str:
        week_start, week_end = self.__extract_week_range(days)
        month_label = self.__format_month_label(week_start, week_end)
        week_dates = self.__get_week_dates()
        target_days = self.__count_target_days(week_dates)
        weekly_target = int(self.__daily_target_minutes) * int(target_days if target_days > 0 else 5)
        remain = int(weekly_target) - int(total_minutes)
        lines = [f"Wrike 기록 업데이트 - {display_name}"]
        if month_label:
            lines.append(f"조회 기준 월: {month_label}")
        if week_start and week_end:
            lines.append(
                f"조회 주간: {week_start.strftime('%Y-%m-%d')} ~ {week_end.strftime('%Y-%m-%d')}"
            )
        lines.append(f"이번 주 합계: {self.__format_minutes(total_minutes)}")
        lines.append(f"주간 목표: {self.__format_minutes(weekly_target)}")
        if remain > 0:
            lines.append(f"남은 시간: {self.__format_minutes(remain)}")
        elif remain < 0:
            lines.append(f"초과: {self.__format_minutes(-remain)}")
        else:
            lines.append("목표 달성")
        return "\n".join(lines)

    def __parse_time_to_minutes(self, text: str) -> int:
        if text is None:
            return 0
        raw = str(text).strip().lower()
        if not raw:
            return 0
        raw = raw.replace('시간', 'h').replace('분', 'm')

        hhmm = self.__re_time_hhmm.match(raw)
        if hhmm:
            hours = int(hhmm.group(1))
            minutes = int(hhmm.group(2))
            return max(0, hours * 60 + minutes)

        hours_total = 0.0
        minutes_total = 0.0
        for match in self.__re_time_h.finditer(raw):
            try:
                hours_total += float(match.group(1))
            except Exception:
                continue
        for match in self.__re_time_m.finditer(raw):
            try:
                minutes_total += float(match.group(1))
            except Exception:
                continue
        if hours_total or minutes_total:
            return max(0, int(round(hours_total * 60 + minutes_total)))

        if self.__re_time_number.match(raw):
            try:
                return max(0, int(round(float(raw) * 60)))
            except Exception:
                return 0
        return 0

    def __format_minutes(self, minutes: int) -> str:
        minutes = int(minutes)
        if minutes <= 0:
            return "0분"
        hours = minutes // 60
        remain = minutes % 60
        if hours and remain:
            return f"{hours}시간 {remain}분"
        if hours:
            return f"{hours}시간"
        return f"{remain}분"

    def __format_minutes_to_hours(self, minutes: int) -> str:
        minutes = int(minutes)
        if minutes <= 0:
            return "0"
        hours = minutes / 60.0
        if abs(hours - int(hours)) < 1e-6:
            return str(int(hours))
        return f"{hours:.2f}".rstrip("0").rstrip(".")

    def __snapshot_days(self, snapshot) -> list[dict]:
        return [
            {
                "date": datetime.combine(item.date, datetime.min.time()),
                "minutes": int(item.recorded_minutes),
                "raw": "",
                "first_dt": None,
            }
            for item in snapshot.days
        ]

    def __build_timelog_summary_lines(self, snapshot) -> RefreshableLines:
        rows = self.__compose_timelog_summary_rows(snapshot)

        def _refresh_rows():
            return self.__compose_timelog_summary_rows(snapshot)

        return RefreshableLines(rows, _refresh_rows)

    def __compose_timelog_summary_rows(
        self,
        snapshot,
    ) -> list[tuple[str, str | None]]:
        days = self.__snapshot_days(snapshot)
        display_name = str(snapshot.display_name or "내 계정")
        week_start, week_end = self.__extract_week_range(days)
        month_label = self.__format_month_label(week_start, week_end)
        lines: list[tuple[str, str | None]] = [
            (f"Wrike 타임로그 (이번 주) - {display_name}", None),
            (
                f"범위: 내 전체 타임로그 · snapshot generation "
                f"{int(snapshot.generation)}",
                None,
            ),
        ]
        if month_label:
            lines.append((f"조회 기준 월: {month_label}", None))
        if week_start and week_end:
            lines.append(
                (f"조회 주간: {week_start.strftime('%Y-%m-%d')} ~ {week_end.strftime('%Y-%m-%d')}", None)
            )
        overview_rows = self.__build_overview_rows(
            days,
            int(self.__daily_target_minutes),
            snapshot=snapshot,
        )
        if len(overview_rows) != 5:
            raise ValueError("workday overview must contain exactly five rows")
        lines.extend(overview_rows)

        for idx, day in enumerate(days):
            date_value = day.get("date")
            raw_minutes = int(day.get("minutes", 0))
            label = self.__format_day_label(date_value, idx)
            display = self.__format_minutes(raw_minutes)
            lines.append((f"{label}: Wrike 기록 {display}", None))
        return lines

    def __format_day_label(self, date_value, index: int) -> str:
        if date_value is None:
            return f"Day {index + 1}"
        try:
            date_text = date_value.strftime("%Y-%m-%d")
        except Exception:
            return f"Day {index + 1}"
        label = self.__time_log_weekday_labels[index] if index < len(self.__time_log_weekday_labels) else ""
        if label:
            return f"{date_text} ({label})"
        return date_text

    def __extract_contact_email(self, contact: dict) -> str:
        profiles = contact.get("profiles")
        if isinstance(profiles, list):
            for prof in profiles:
                email = str((prof or {}).get("email") or "").strip()
                if email:
                    return email
        return ""

    def __cache_contact_identity(self, token: str, contact: dict) -> tuple[str | None, str]:
        contact_id = str(contact.get("id") or "").strip()
        name = str(contact.get("name") or "").strip()
        if not name:
            first = str(contact.get("firstName") or "").strip()
            last = str(contact.get("lastName") or "").strip()
            name = f"{first} {last}".strip()
        if not name:
            name = self.__extract_contact_email(contact)
        if not name:
            name = "내 계정"
        if contact_id:
            self.__wrike_api_contact_id = contact_id
            self.__wrike_api_contact_name = name
            self.__wrike_api_contact_token = str(token or "").strip()
        return contact_id or None, name

    def __resolve_contact_identity(self, token: str) -> tuple[str | None, str | None, str | None]:
        token = str(token or "").strip()
        if not token:
            return None, None, "api_token_missing"
        if self.__wrike_api_contact_id and self.__wrike_api_contact_token == token:
            name = str(self.__wrike_api_contact_name or "내 계정")
            return self.__wrike_api_contact_id, name, None
        self.__wrike_api_last_error_code = 0
        me_url = f"{self.__wrike_api_base}/contacts?me=true"
        me_data = self.__api_get_json(me_url, token)
        contact = None
        if isinstance(me_data, dict):
            data_items = me_data.get("data")
            if isinstance(data_items, list) and data_items:
                contact = data_items[0]
            else:
                return None, None, "contact_not_found"
        if not isinstance(contact, dict):
            if self.__wrike_api_last_error_code in {401, 403}:
                return None, None, "auth_failed"
            return None, None, "api_request_failed"
        contact_id, name = self.__cache_contact_identity(token, contact)
        if not contact_id:
            return None, None, "contact_not_found"
        return contact_id, name, None

    def __fetch_weekly_timelog(self, contact_id: str, token: str) -> tuple[list[dict] | None, str | None]:
        token = str(token or "").strip()
        if not token:
            self.__log("timelog api token missing")
            return None, self.__error_with_log("Wrike API 키가 필요합니다")

        contact_id = str(contact_id or "").strip()
        if not contact_id:
            self.__log("timelog api contact id missing")
            return None, self.__error_with_log("Wrike 사용자 정보를 찾지 못했습니다")

        self.__log(f"timelog api start: contact_id={contact_id!r}")
        api_days, api_error = self.__fetch_weekly_timelog_via_api(token, contact_id)
        if api_days is not None:
            self.__log("timelog api success")
            return api_days, None
        if api_error == "auth_failed":
            return None, self.__error_with_log("Wrike API 키 인증 실패")
        if api_error == "contact_not_found":
            return None, self.__error_with_log("Wrike 사용자 정보를 찾지 못했습니다")
        if api_error == "api_request_failed":
            return None, self.__error_with_log("Wrike API 조회 실패")
        if api_error:
            self.__log(f"timelog api failed: {api_error}")
        return None, self.__error_with_log("Wrike API 조회 실패")

    def __ensure_wrike_profile_dir(self) -> str:
        base_dir = self.__lib.os.getenv("LOCALAPPDATA")
        if not base_dir:
            base_dir = self.__lib.os.getenv("APPDATA")
        if not base_dir:
            base_dir = self.__lib.os.path.expanduser("~")
        profile_dir = self.__lib.os.path.join(base_dir, "windows-supporter", "wrike-profile")
        try:
            self.__lib.os.makedirs(profile_dir, exist_ok=True)
        except Exception:
            pass
        return profile_dir

    def __prewarm_wrike_form_browser_async(self) -> None:
        if self.__form_browser_prewarm_requested:
            return
        if not self.__ensure_wrike_form_browser_worker_started():
            return
        self.__form_browser_prewarm_requested = True
        try:
            self.__form_browser_queue.put(("prewarm", None, None, None))
        except Exception:
            self.__form_browser_prewarm_requested = False
        return

    def __fill_wrike_form_on_browser_worker(self, root, title: str) -> str | None:
        if not self.__ensure_wrike_form_browser_worker_started():
            return "Wrike Form 자동화 시작 실패"

        response_queue = queue.Queue(maxsize=1)
        try:
            self.__form_browser_queue.put(("fill", root, title, response_queue))
        except Exception:
            return "Wrike Form 자동화 시작 실패"

        timeout_sec = float(self.__time_log_login_timeout_sec) + 45.0
        try:
            ok, result = response_queue.get(timeout=timeout_sec)
        except queue.Empty:
            return "Wrike Form 자동화 시간 초과: 열린 브라우저 상태를 확인하세요"
        except Exception:
            return "Wrike Form 자동화 실패: 잠시 후 다시 시도하세요"

        if ok:
            return result
        self.__log_exception("wrike form browser worker failed", result)
        return "Wrike Form 자동화 실패: 잠시 후 다시 시도하세요"

    def __ensure_wrike_form_browser_worker_started(self) -> bool:
        with self.__form_browser_worker_lock:
            thread = self.__form_browser_worker_thread
            if self.__is_thread_alive(thread):
                return True
            self.__form_browser_queue = queue.Queue()
            self.__form_browser_prewarm_requested = False
            try:
                thread = threading.Thread(target=self.__wrike_form_browser_worker_loop, daemon=True)
                self.__form_browser_worker_thread = thread
                thread.start()
                return True
            except Exception as exc:
                self.__log_exception("wrike form browser worker start failed", exc)
                self.__form_browser_worker_thread = None
                return False

    def __is_thread_alive(self, thread) -> bool:
        if thread is None:
            return False
        try:
            return bool(thread.is_alive())
        except Exception:
            return True

    def __wrike_form_browser_worker_loop(self) -> None:
        while True:
            try:
                kind, root, title, response_queue = self.__form_browser_queue.get()
            except Exception:
                return

            if kind == "prewarm":
                self.__prewarm_wrike_form_browser()
                continue

            if kind != "fill":
                continue

            try:
                result = self.__fill_wrike_form_with_playwright(root, title)
            except Exception as exc:
                self.__log_exception("wrike form fill worker exception", exc)
                try:
                    response_queue.put((False, exc))
                except Exception:
                    pass
                continue

            try:
                response_queue.put((True, result))
            except Exception:
                pass
        return

    def __prewarm_wrike_form_browser(self) -> None:
        if not self.__ensure_playwright_ready():
            self.__playwright_checked = False
            return
        self.__ensure_wrike_form_playwright_started()
        return

    def __ensure_wrike_logged_in(self, page, root, return_url: str | None = None) -> str | None:
        current_url = str(page.url or "")
        if self.__requires_login(page):
            self.__log(f"login required url={current_url}")
            try:
                page.bring_to_front()
            except Exception:
                pass
            self.__ui_safe(
                root,
                lambda: self.__show_tooltip(
                    root,
                    "Wrike 로그인 필요: 열린 브라우저에서 로그인 후 대기하세요",
                ),
            )
            try:
                page.wait_for_url("**/workspace.htm*", timeout=int(self.__time_log_login_timeout_sec * 1000))
            except Exception:
                return self.__error_with_log("Wrike 로그인 시간 초과")
            page.goto(return_url or self.__time_log_root_url, wait_until="domcontentloaded")
        return None

    def __fill_wrike_form_with_playwright(self, root, title: str) -> str | None:
        if not self.__ensure_playwright_ready():
            return "Wrike Form 자동화 준비 실패: Playwright를 사용할 수 없습니다"

        page, error = self.__get_wrike_form_page()
        if error:
            return error
        if page is None:
            return "Wrike Form 브라우저를 열지 못했습니다"

        try:
            page.bring_to_front()
        except Exception:
            pass

        navigation_error = None
        if not self.__is_current_wrike_form_page(page):
            navigation_error = self.__goto_wrike_form(page)
        if navigation_error:
            if not self.__is_stale_playwright_error(navigation_error):
                self.__log_exception("wrike form navigation failed", navigation_error)
                return "Wrike Form 이동 실패: 잠시 후 다시 시도하세요"
            self.__log_exception("wrike form stale browser handle", navigation_error)
            self.__reset_wrike_form_browser_handles()
            page, error = self.__get_wrike_form_page()
            if error:
                return error
            if page is None:
                return "Wrike Form 브라우저를 열지 못했습니다"
            try:
                page.bring_to_front()
            except Exception:
                pass
            navigation_error = self.__goto_wrike_form(page)
            if navigation_error:
                self.__log_exception("wrike form navigation retry failed", navigation_error)
                return "Wrike Form 이동 실패: 잠시 후 다시 시도하세요"

        login_error = self.__ensure_wrike_logged_in(page, root, return_url=self.__form_url)
        if login_error:
            return login_error

        try:
            if not self.__is_current_wrike_form_page(page):
                page.goto(
                    self.__form_url,
                    wait_until="domcontentloaded",
                    timeout=self.__form_nav_timeout_ms,
                )
        except Exception as exc:
            self.__log_exception("wrike form post-login navigation failed", exc)
            return "Wrike Form 이동 실패: 잠시 후 다시 시도하세요"

        login_error = self.__ensure_wrike_logged_in(page, root, return_url=self.__form_url)
        if login_error:
            return login_error

        if not self.__fill_wrike_form_title(page, title):
            return "Wrike Form 제목 입력칸을 찾지 못했습니다"
        return None

    def __goto_wrike_form(self, page) -> Exception | None:
        try:
            page.goto(
                self.__form_url,
                wait_until="domcontentloaded",
                timeout=self.__form_nav_timeout_ms,
            )
            return None
        except Exception as exc:
            return exc

    def __is_current_wrike_form_page(self, page) -> bool:
        try:
            current_url = str(page.url or "")
        except Exception:
            return False
        return (
            current_url.startswith("https://www.wrike.com/workspace.htm")
            and "#/forms?formid=2239448" in current_url
        )

    def __is_stale_playwright_error(self, exc: Exception) -> bool:
        msg = str(exc or "").lower()
        stale_markers = (
            "different thread",
            "thread",
            "target closed",
            "page closed",
            "browser has been closed",
            "context closed",
            "connection closed",
        )
        return any(marker in msg for marker in stale_markers)

    def __reset_wrike_form_browser_handles(self) -> None:
        context = self.__form_context
        playwright_obj = self.__form_playwright
        self.__form_page = None
        self.__form_context = None
        self.__form_playwright = None
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if playwright_obj is not None:
                playwright_obj.stop()
        except Exception:
            pass

    def __get_wrike_form_page(self):
        last_error = None
        for attempt in range(2):
            try:
                if self.__is_playwright_page_open(self.__form_page):
                    return self.__form_page, None
            except Exception:
                self.__form_page = None

            if self.__form_playwright is None:
                if not self.__ensure_wrike_form_playwright_started():
                    return None, "Wrike Form 자동화 시작 실패"

            if self.__form_context is None:
                profile_dir = self.__ensure_wrike_profile_dir()
                self.__form_context = self.__launch_playwright_context(self.__form_playwright, profile_dir)
                if self.__form_context is None:
                    return None, "Wrike Form 브라우저 실행 실패"

            try:
                pages = list(getattr(self.__form_context, "pages", []) or [])
            except Exception as exc:
                last_error = exc
                if attempt == 0 and self.__is_stale_playwright_error(exc):
                    self.__log_exception("wrike form cached context closed", exc)
                    self.__reset_wrike_form_browser_handles()
                    continue
                pages = []

            for page in pages:
                if self.__is_playwright_page_open(page):
                    self.__form_page = page
                    return page, None

            try:
                self.__form_page = self.__form_context.new_page()
                return self.__form_page, None
            except Exception as exc:
                last_error = exc
                if attempt == 0 and self.__is_stale_playwright_error(exc):
                    self.__log_exception("wrike form cached context new_page failed", exc)
                    self.__reset_wrike_form_browser_handles()
                    continue
                self.__log_exception("wrike form page create failed", exc)
                return None, "Wrike Form 브라우저 탭 생성 실패"

        if last_error is not None:
            self.__log_exception("wrike form page recover failed", last_error)
        return None, "Wrike Form 브라우저 탭 생성 실패"

    def __ensure_wrike_form_playwright_started(self) -> bool:
        if self.__form_playwright is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright

            self.__form_playwright = sync_playwright().start()
            return True
        except Exception as exc:
            self.__log_exception("playwright start failed", exc)
            self.__form_playwright = None
            return False

    def __is_playwright_page_open(self, page) -> bool:
        if page is None:
            return False
        try:
            return not bool(page.is_closed())
        except Exception:
            return True

    def __fill_wrike_form_title(self, page, title: str) -> bool:
        self.__dismiss_wrike_form_draft_prompt(page, wait_ms=0)
        if self.__try_fill_wrike_form_title_field(page, title, wait_ms=0):
            return True

        dismissed = self.__dismiss_wrike_form_draft_prompt(page, wait_ms=500)
        if dismissed and self.__try_fill_wrike_form_title_field(
            page,
            title,
            wait_ms=self.__form_title_locator_wait_ms,
        ):
            return True

        return self.__try_fill_wrike_form_title_field(
            page,
            title,
            wait_ms=self.__form_title_locator_wait_ms,
        )

    def __try_fill_wrike_form_title_field(self, page, title: str, wait_ms: int = 0) -> bool:
        field = self.__find_wrike_form_title_field(page, wait_ms=wait_ms)
        if field is None:
            return False
        try:
            field.fill(title, timeout=self.__form_title_timeout_ms)
        except Exception as exc:
            self.__log_exception("wrike form title fill failed", exc)
            return False

        try:
            for _ in range(4):
                field.press("ArrowLeft", timeout=500)
        except Exception:
            pass
        return True

    def __dismiss_wrike_form_draft_prompt(self, page, wait_ms: int = 0) -> bool:
        locators = self.__wrike_form_draft_prompt_locators(page)
        for locator in locators:
            target = self.__first_available_locator(locator)
            if target is not None and self.__click_wrike_draft_prompt(target, page):
                return True

        if int(wait_ms) > 0 and locators:
            target = self.__first_available_locator(locators[0], wait_ms=wait_ms)
            if target is not None and self.__click_wrike_draft_prompt(target, page):
                return True
        return False

    def __wrike_form_draft_prompt_locators(self, page) -> list:
        locators = []
        labels = ("Start new", "새로 시작", "새로 만들기")
        for label in labels:
            try:
                locators.append(page.get_by_role("button", name=label, exact=True))
            except Exception:
                pass
        try:
            label_pattern = self.__lib.re.compile(
                r"^\s*(Start\s+new|새로\s*시작|새로\s*만들기)\s*$",
                self.__lib.re.IGNORECASE,
            )
            locators.append(page.get_by_role("button", name=label_pattern))
        except Exception:
            pass
        for label in labels:
            try:
                locators.append(page.get_by_text(label, exact=True))
            except Exception:
                pass
        for label in labels:
            escaped = label.replace('"', '\\"')
            for selector in (
                f'button:has-text("{escaped}")',
                f'[role="button"]:has-text("{escaped}")',
            ):
                try:
                    locators.append(page.locator(selector))
                except Exception:
                    pass
        return locators

    def __click_wrike_draft_prompt(self, target, page) -> bool:
        try:
            target.click(timeout=1500)
            try:
                page.wait_for_load_state(state="domcontentloaded", timeout=3000)
            except Exception:
                pass
            return True
        except Exception as exc:
            self.__log_exception("wrike form draft prompt click failed", exc)
            return False

    def __find_wrike_form_title_field(self, page, wait_ms: int = 0):
        candidates = []
        for label in ("제목", "Title"):
            candidates.append(lambda label=label: page.get_by_label(label, exact=False))
            candidates.append(lambda label=label: page.get_by_placeholder(label, exact=False))
        for selector in (
            "textarea[aria-label*='제목'], input[aria-label*='제목']",
            "textarea[placeholder*='제목'], input[placeholder*='제목']",
            "textarea[aria-label*='Title'], input[aria-label*='Title']",
            "textarea[placeholder*='Title'], input[placeholder*='Title']",
            "textarea[name*='title' i], input[name*='title' i]",
        ):
            candidates.append(lambda selector=selector: page.locator(selector))

        for candidate in candidates:
            try:
                locator = candidate()
            except Exception:
                continue
            target = self.__first_available_locator(locator)
            if target is not None:
                return target
        if int(wait_ms) <= 0:
            return None
        for candidate in candidates:
            try:
                locator = candidate()
            except Exception:
                continue
            target = self.__first_available_locator(locator, wait_ms=wait_ms)
            if target is not None:
                return target
        return None

    def __first_available_locator(self, locator, wait_ms: int = 0):
        try:
            count = int(locator.count())
        except Exception:
            return None
        if count <= 0 and int(wait_ms) > 0:
            try:
                locator.wait_for(state="visible", timeout=int(wait_ms))
                count = int(locator.count())
            except Exception:
                return None
        if count <= 0:
            return None
        if count == 1:
            return locator
        try:
            return locator.first
        except Exception:
            return locator

    def __is_login_url(self, url: str) -> bool:
        lowered = str(url or "").lower()
        return "login" in lowered or "signin" in lowered or "sso" in lowered or "auth" in lowered

    def __requires_login(self, page) -> bool:
        url = str(page.url or "")
        if self.__is_login_url(url):
            return True
        try:
            title = str(page.title() or "").strip().lower()
            if "wrike" in title and ("sign in" in title or "log in" in title or "login" in title):
                return True
        except Exception:
            pass
        try:
            if page.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
        for label in (
            "Log in",
            "Sign in",
            "Sign in to your Wrike account",
            "Forgot password?",
            "Log in with One-Time Password",
            "로그인",
            "SSO",
        ):
            try:
                if page.get_by_text(label, exact=False).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def __open_year_month(self, page) -> bool:
        now = self.__lib.datetime.now()
        year_label = f"{self.__time_log_year_prefix}{now.year}"
        month_index = max(1, min(12, int(now.month)))
        month_label = f"{self.__time_log_month_prefix}{self.__time_log_month_names[month_index - 1]}"

        try:
            year_locator = page.get_by_text(year_label, exact=True)
            year_locator.wait_for(state="visible", timeout=self.__time_log_nav_timeout_ms)
            year_locator.click()
        except Exception:
            return False

        try:
            month_locator = page.get_by_text(month_label, exact=True)
            month_locator.wait_for(state="visible", timeout=self.__time_log_nav_timeout_ms)
            month_locator.click()
        except Exception:
            return False

        try:
            page.wait_for_url("**/folder/**", timeout=self.__time_log_nav_timeout_ms)
        except Exception:
            pass
        return True

    def __resolve_timelog_url(self, page) -> str:
        current_url = str(page.url or "")
        if "timelog" in current_url:
            return current_url

        try:
            page.get_by_text(self.__time_log_view_label, exact=False).first.click(timeout=800)
        except Exception:
            pass

        try:
            page.wait_for_url("**/timelog**", timeout=1500)
            return str(page.url or "")
        except Exception:
            pass

        href = self.__find_timelog_href(page)
        if href:
            return self.__normalize_wrike_href(current_url, href)
        return self.__synthesize_timelog_url(current_url)

    def __find_timelog_href(self, page) -> str | None:
        try:
            locator = page.locator("a[href*='timelog']")
            if locator.count() <= 0:
                return None
            href = locator.first.get_attribute("href")
            if href:
                return str(href)
        except Exception:
            return None
        return None

    def __normalize_wrike_href(self, current_url: str, href: str) -> str:
        raw = str(href or "")
        if raw.startswith("http"):
            return raw
        if raw.startswith("#"):
            base = str(current_url or "").split("#", maxsplit=1)[0]
            return f"{base}{raw}"
        return raw

    def __synthesize_timelog_url(self, current_url: str) -> str:
        parsed = urllib.parse.urlparse(current_url)
        fragment = parsed.fragment or ""
        fragment_path, _, fragment_query = fragment.partition("?")
        match = self.__lib.re.search(r"/folder/(\d+)", fragment_path)
        folder_id = match.group(1) if match else ""
        if not folder_id:
            return ""
        query = urllib.parse.parse_qs(fragment_query)
        space_id = ""
        space_values = query.get("spaceId")
        if space_values:
            space_id = space_values[0]
        overlay = "overlayFullScreen=0&showInfo=0"
        if space_id:
            overlay = f"{overlay}&spaceId={space_id}"
        base = str(current_url or "").split("#", maxsplit=1)[0]
        return f"{base}#/folder/{folder_id}/timelog?{overlay}"

    def __try_select_this_week(self, page) -> None:
        for label in ("This week", "이번 주"):
            try:
                locator = page.get_by_text(label, exact=False)
                locator.first.click(timeout=600)
                return
            except Exception:
                continue
        return

    def __extract_timelog_grid_data(self, page, person: str) -> dict:
        script = """
        (name) => {
            const normalize = (value) => (value || "").toString().trim();
            const toLower = (value) => normalize(value).toLowerCase();
            const target = toLower(name);

            const grid = document.querySelector('[role="grid"]') || document.body;
            const rows = Array.from(grid.querySelectorAll('[role="row"]'));
            let targetRow = null;
            const allCells = Array.from(grid.querySelectorAll('[role="gridcell"]'));
            for (const cell of allCells) {
                const text = toLower(cell.innerText);
                if (text && text.includes(target)) {
                    const row = cell.closest('[role="row"]');
                    if (row) {
                        targetRow = row;
                        break;
                    }
                }
            }
            if (!targetRow) {
                for (const row of rows) {
                    const text = toLower(row.innerText);
                    if (text && text.includes(target)) {
                        targetRow = row;
                        break;
                    }
                }
            }

            let headerRow = null;
            for (const row of rows) {
                if (row.querySelector('[role="columnheader"]')) {
                    headerRow = row;
                    break;
                }
            }

            const collectData = (el) => {
                const data = {};
                if (el && el.dataset) {
                    for (const key of Object.keys(el.dataset)) {
                        data[key] = el.dataset[key];
                    }
                }
                return data;
            };

            const headers = headerRow
                ? Array.from(headerRow.querySelectorAll('[role="columnheader"]')).map((cell) => ({
                      col: cell.getAttribute("aria-colindex") || "",
                      text: normalize(cell.innerText || cell.textContent),
                      data: collectData(cell),
                  }))
                : [];

            if (!targetRow) {
                const debug = {
                    rowCount: rows.length,
                    headerCount: headers.length,
                    sampleRows: rows.slice(0, 3).map((row) => normalize(row.innerText).slice(0, 120)),
                };
                return { error: "row_not_found", debug };
            }

            const cells = Array.from(targetRow.querySelectorAll('[role="gridcell"]')).map((cell) => ({
                col: cell.getAttribute("aria-colindex") || "",
                text: normalize(cell.innerText || cell.textContent || cell.getAttribute("title")),
                data: collectData(cell),
            }));

            return { headers, cells };
        }
        """
        try:
            return page.evaluate(script, person)
        except Exception:
            return {}

    def __get_week_dates(self) -> list:
        now = self.__lib.datetime.now()
        week_start = now - timedelta(days=now.weekday())
        return [week_start + timedelta(days=i) for i in range(7)]

    def __extract_week_range(self, days: list[dict]):
        dates = []
        for day in days:
            val = day.get("date")
            if val is None:
                continue
            try:
                dates.append(val.date())
            except Exception:
                continue
        if not dates:
            return None, None
        dates.sort()
        return dates[0], dates[-1]

    def __format_month_label(self, start_date, end_date) -> str:
        if not start_date:
            return ""
        if not end_date:
            try:
                return start_date.strftime("%Y-%m")
            except Exception:
                return ""
        try:
            if start_date.year == end_date.year and start_date.month == end_date.month:
                return start_date.strftime("%Y-%m")
            return f"{start_date.strftime('%Y-%m')}~{end_date.strftime('%Y-%m')}"
        except Exception:
            return ""

    def __pick_day_color(self, date_value, minutes: int, target_minutes: int, today_date) -> str | None:
        if date_value is None:
            return None
        try:
            d = date_value.date()
        except Exception:
            return None
        if d > today_date:
            return "#9CA3AF"
        if int(minutes) < int(target_minutes):
            return "#DC2626"
        return None

    def __target_minutes_for_date(self, date_value, default_target: int) -> int:
        if date_value is None:
            return int(default_target)
        try:
            weekday = int(date_value.weekday())
        except Exception:
            return int(default_target)
        if weekday >= 5:
            return 0
        return int(default_target)

    def __build_week_days(self, grid_data: dict) -> list[dict]:
        week_dates = self.__get_week_dates()

        headers = grid_data.get("headers") or []
        cells = grid_data.get("cells") or []
        week_headers = self.__select_week_headers(headers)

        cell_by_col = {}
        for cell in cells:
            col = cell.get("col")
            if col:
                cell_by_col[str(col)] = cell

        time_cells = [cell for cell in cells if self.__looks_like_time(cell.get("text", ""))]

        days = []
        for idx, date_value in enumerate(week_dates):
            raw_text = ""
            if idx < len(week_headers):
                col = week_headers[idx].get("col")
                if col and str(col) in cell_by_col:
                    raw_text = cell_by_col[str(col)].get("text", "")
            if not raw_text and idx < len(time_cells):
                raw_text = time_cells[idx].get("text", "")
            minutes = self.__parse_time_to_minutes(raw_text)
            days.append({"date": date_value, "minutes": minutes, "raw": raw_text})
        return days

    def __select_week_headers(self, headers: list[dict]) -> list[dict]:
        candidates = []
        for header in headers:
            text = str(header.get("text") or "").strip()
            if not text:
                continue
            if self.__is_week_header(text):
                candidates.append(header)
        if len(candidates) >= 7:
            return candidates[:7]
        return candidates

    def __is_week_header(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        if "total" in lowered or "합계" in lowered:
            return False
        if self.__re_weekday_en.search(lowered):
            return True
        for label in self.__time_log_weekday_labels:
            if label in lowered:
                return True
        if self.__re_date_num.search(lowered):
            return True
        return False

    def __looks_like_time(self, text: str) -> bool:
        if not text:
            return False
        lowered = str(text).strip().lower()
        if not lowered:
            return False
        if ":" in lowered or "h" in lowered or "m" in lowered:
            return True
        if "시간" in lowered or "분" in lowered:
            return True
        return bool(self.__re_time_number.match(lowered))

    def __fetch_weekly_timelog_via_api(
        self, token: str, contact_id: str
    ) -> tuple[list[dict] | None, str | None]:
        if not token:
            return None, "api_token_missing"
        contact_id = str(contact_id or "").strip()
        if not contact_id:
            return None, "contact_not_found"
        self.__wrike_api_last_error_code = 0

        week_dates = self.__get_week_dates()
        if not week_dates:
            return None, "week_dates_empty"

        timelogs = self.__query_timelogs_week(token, contact_id, week_dates)
        if timelogs is None:
            if self.__wrike_api_last_error_code in {401, 403}:
                return None, "auth_failed"
            return None, "api_request_failed"

        days = self.__aggregate_timelogs(timelogs, week_dates)
        return days, None

    def __reset_wrike_contact_cache(self) -> None:
        self.__wrike_api_contact_id = ""
        self.__wrike_api_contact_name = ""
        self.__wrike_api_contact_token = ""
        return

    def __timelog_token_fingerprint(self, token: str) -> str:
        value = str(token or "").strip()
        if not value:
            return ""
        payload = f"windows-supporter:wrike-timelog-cache:v1:{value}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __set_wrike_api_token_session(self, token: str) -> None:
        token = str(token or "").strip()
        current = str(self.__wrike_api_token_session or "").strip()
        changed = token != current
        self.__wrike_api_token_session = token
        if changed:
            self.__reset_wrike_contact_cache()
            if hasattr(self, "_Wrike__timelog_snapshot_lock"):
                with self.__timelog_snapshot_lock:
                    self.__timelog_refresh_generation += 1
                    generation = int(self.__timelog_refresh_generation)
                    self.__timelog_refresh_running = False
                    self.__timelog_refresh_running_generation = None
                    self.__timelog_last_refresh_requested_at = None
                    self.__timelog_last_good = None
                    self.__timelog_snapshot = (
                        make_loading_snapshot(generation=generation)
                        if token
                        else make_unconfigured_snapshot(generation=generation)
                    )
        return

    def __get_wrike_api_token(self, root, prompt_if_missing: bool = False) -> str:
        token = str(self.__wrike_api_token_session or "").strip()
        token_from_legacy_file = False
        if not token:
            token = self.__lib.os.getenv(self.__wrike_api_token_env) or ""
        if not token:
            token = self.__lib.os.getenv("WRIKE_API_TOKEN") or ""
        if not token:
            token = self.__read_legacy_api_token_file()
            token_from_legacy_file = bool(str(token or "").strip())
        token = str(token).strip()
        if token:
            self.__set_wrike_api_token_session(token)
            if token_from_legacy_file and self.__save_settings():
                self.__remove_legacy_api_token_file()
            self.__log(f"api token cached length={len(token)}")
            return token

        if prompt_if_missing and root is not None:
            self.__log("api token prompt shown")
            token = self.__prompt_api_token(root)
            token = str(token or "").strip()
            if token:
                self.__set_wrike_api_token_session(token)
                self.__log(f"api token provided length={len(token)}")
                self.__save_settings()
                return token
            clipboard_token = self.__safe_clipboard_paste()
            if self.__looks_like_api_token(clipboard_token):
                token = str(clipboard_token).strip()
                self.__set_wrike_api_token_session(token)
                self.__log(f"api token from clipboard length={len(token)}")
                self.__save_settings()
                return token
            self.__log("api token empty after prompt")
        return str(token).strip()

    def __read_legacy_api_token_file(self) -> str:
        try:
            if self.__lib.os.path.isfile(self.__time_log_token_path):
                with open(self.__time_log_token_path, "r", encoding="utf-8") as fp:
                    return (fp.readline() or "").strip()
        except Exception as exc:
            self.__log_exception("legacy token read failed", exc)
        return ""

    def __remove_legacy_api_token_file(self) -> bool:
        try:
            if not self.__lib.os.path.exists(self.__time_log_token_path):
                return True
            self.__lib.os.remove(self.__time_log_token_path)
        except Exception as exc:
            self.__log_exception("legacy token remove failed", exc)
            try:
                with open(self.__time_log_token_path, "w", encoding="utf-8") as fp:
                    fp.write("")
                self.__lib.os.remove(self.__time_log_token_path)
            except Exception as cleanup_exc:
                self.__log_exception("legacy token cleanup failed", cleanup_exc)
        try:
            return not self.__lib.os.path.exists(self.__time_log_token_path)
        except Exception:
            return False

    def __prompt_api_token(self, root) -> str | None:
        try:
            from tkinter import simpledialog
        except Exception:
            return None
        try:
            message = (
                "Wrike API 키를 입력하세요.\n"
                "로컬 파일에 저장되어 다음 실행에 재사용됩니다."
            )
            return simpledialog.askstring(
                "Wrike API",
                message,
                parent=root,
                show="*",
            )
        except Exception:
            return None

    def __looks_like_api_token(self, text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        if len(raw) < 20 or len(raw) > 200:
            return False
        if any(ch.isspace() for ch in raw):
            return False
        return True

    def get_settings_snapshot(self) -> dict:
        token = str(self.__wrike_api_token_session or "").strip()
        vacation_status = self.get_vacation_ical_status_snapshot()
        snapshot = {
            "api_token_configured": bool(token),
            "api_token_masked": self.__mask_api_token(token),
            "daily_target_minutes": int(self.__daily_target_minutes),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "monitor_enabled": bool(self.__monitor_enabled),
            "monitor_interval_sec": float(self.__monitor_interval_sec),
            "monitor_folder_path": list(self.__monitor_folder_path),
            "settings_path": str(self.__settings_path or ""),
            "lunch_break_enabled": bool(self.__lunch_break_enabled),
            "lunch_start_min": int(self.__lunch_start_min),
            "lunch_end_min": int(self.__lunch_end_min),
            "ical_url_configured": bool(
                str(self.__decode_ical_url() or "").strip()
                or str(self.__ical_url_protected or "").strip()
            ),
            "break_keywords": list(self.__ical_keywords),
            "ical_poll_interval_sec": float(self.__ical_poll_interval_sec),
            "workday_plan": self.get_workday_plan(),
            "manual_break_state": self.get_manual_break_state(),
            "worktime_state_path": str(self.__worktime_state_path or ""),
            "vacation_calendar_provider": str(vacation_status["provider"]),
            "oauth_configured": bool(vacation_status["oauth_configured"]),
            "vacation_ical_secret_present": bool(
                vacation_status["secret_present"]
            ),
            "vacation_ical_url_configured": bool(
                vacation_status["configured"]
            ),
            "vacation_ical_configured": bool(vacation_status["configured"]),
            "vacation_expected_calendar_name": "",
            "vacation_observed_calendar_name": "",
            "vacation_calendar_name": "",
            "vacation_ical_state": str(vacation_status["state"]),
            "vacation_ical_poll_interval_sec": float(
                self.__vacation_ical_poll_interval_sec
            ),
            "vacation_ical_last_success_ts": vacation_status["last_success_ts"],
            "vacation_ical_last_error": str(vacation_status["error_code"]),
            "vacation_ical_status": dict(vacation_status),
        }
        return snapshot

    def __mask_api_token(self, token: str) -> str:
        raw = str(token or "").strip()
        if not raw:
            return ""
        if len(raw) <= 6:
            return "*" * len(raw)
        return raw[:3] + ("*" * max(3, len(raw) - 6)) + raw[-3:]

    def __capture_settings_runtime_state(self) -> dict:
        names = (
            "wrike_api_token_session",
            "wrike_api_contact_id",
            "wrike_api_contact_name",
            "wrike_api_contact_token",
            "daily_target_minutes",
            "tooltip_duration_ms",
            "monitor_enabled",
            "monitor_interval_sec",
            "monitor_last_total_minutes",
            "lunch_break_enabled",
            "lunch_start_min",
            "lunch_end_min",
            "ical_poll_interval_sec",
            "ical_keywords",
            "ical_url_protected",
            "ical_url_session",
            "ical_parsed_events",
            "ical_events_for_date",
            "ical_matched",
            "timelog_refresh_generation",
            "timelog_refresh_running",
            "timelog_refresh_running_generation",
            "timelog_last_refresh_requested_at",
            "timelog_snapshot",
            "timelog_last_good",
            "vacation_calendar_provider",
            "vacation_ical_url_protected",
            "vacation_ical_url_session",
            "vacation_google_oauth_protected",
            "vacation_google_oauth_session",
            "vacation_google_oauth_week_start",
            "vacation_google_oauth_delete_pending",
            "vacation_ical_poll_interval_sec",
            "vacation_ical_calendar",
            "vacation_ical_events_for_date",
            "vacation_ical_day_result",
            "vacation_ical_last_success_ts",
            "vacation_ical_last_error",
            "vacation_ical_state",
            "vacation_ical_observed_calendar_name",
            "vacation_ical_week_cache_calendar",
            "vacation_ical_week_cache",
        )
        with self.__timelog_snapshot_lock, self.__vacation_ical_lock:
            return {
                name: copy.deepcopy(getattr(self, f"_Wrike__{name}"))
                for name in names
            }

    def __restore_settings_runtime_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        with self.__timelog_snapshot_lock, self.__vacation_ical_lock:
            for name, value in state.items():
                setattr(self, f"_Wrike__{name}", copy.deepcopy(value))
        return

    def update_settings(self, data: dict) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"

        provider_supplied = "vacation_calendar_provider" in data
        requested_provider = str(
            data.get(
                "vacation_calendar_provider",
                self.__vacation_calendar_provider,
            )
            or ""
        ).strip()
        if provider_supplied and requested_provider not in VACATION_CALENDAR_PROVIDERS:
            return False, "vacation_calendar_provider"
        next_provider = self.__normalized_vacation_provider(requested_provider)
        previous_runtime_state = self.__capture_settings_runtime_state()
        provider_changed = (
            next_provider != self.__normalized_vacation_provider()
        )

        token_supplied = "api_token" in data
        clear_token = bool(data.get("clear_api_token", False))
        token = str(data.get("api_token", "") or "").strip()
        daily_minutes = data.get("daily_target_minutes", self.__daily_target_minutes)
        tooltip_ms = data.get("tooltip_duration_ms", self.__tooltip_duration_ms)
        monitor_enabled = bool(data.get("monitor_enabled", self.__monitor_enabled))
        monitor_interval = data.get("monitor_interval_sec", self.__monitor_interval_sec)

        clear_ical_url = bool(data.get("clear_ical_url", False))
        ical_url_supplied = "ical_url" in data
        ical_url_value = str(data.get("ical_url", "") or "").strip()
        clear_vacation_ical_url = bool(
            data.get("clear_vacation_ical_url", False)
        )
        vacation_ical_url_supplied = "vacation_ical_url" in data
        vacation_ical_url_value = str(
            data.get("vacation_ical_url", "") or ""
        ).strip()
        vacation_poll_raw = data.get(
            "vacation_ical_poll_interval_sec",
            self.__vacation_ical_poll_interval_sec,
        )
        lunch_enabled = bool(data.get("lunch_break_enabled", self.__lunch_break_enabled))
        lunch_start_raw = data.get("lunch_start_min", self.__lunch_start_min)
        lunch_end_raw = data.get("lunch_end_min", self.__lunch_end_min)
        poll_raw = data.get("ical_poll_interval_sec", self.__ical_poll_interval_sec)
        keywords_raw = data.get("break_keywords", None)

        try:
            lunch_start_val = int(lunch_start_raw)
            lunch_end_val = int(lunch_end_raw)
        except Exception:
            return False, "lunch window"
        lunch_start_val = max(0, min(1439, lunch_start_val))
        lunch_end_val = max(1, min(1440, lunch_end_val))
        if lunch_end_val <= lunch_start_val:
            return False, "lunch window"

        try:
            poll_val = int(round(float(poll_raw)))
        except Exception:
            return False, "calendar interval"
        poll_val = max(300, min(21600, poll_val))

        try:
            vacation_poll_val = int(round(float(vacation_poll_raw)))
        except Exception:
            return False, "vacation calendar interval"
        vacation_poll_val = max(300, min(21600, vacation_poll_val))

        next_keywords = None
        if keywords_raw is not None:
            parsed_terms = []
            if isinstance(keywords_raw, str):
                raw_terms = [piece for piece in keywords_raw.split(",")]
            elif isinstance(keywords_raw, list):
                raw_terms = [str(piece) for piece in keywords_raw]
            else:
                raw_terms = []
            for piece in raw_terms:
                term = piece.strip()
                if not term:
                    continue
                if len(term) > 40:
                    term = term[:40]
                parsed_terms.append(term)
                if len(parsed_terms) >= 12:
                    break
            next_keywords = parsed_terms

        ical_protected = None
        if clear_ical_url:
            ical_protected = ""
        elif ical_url_supplied and ical_url_value:
            lowered = ical_url_value.lower()
            if not (lowered.startswith("http://") or lowered.startswith("https://")):
                return False, "calendar url"
            ical_protected = self.__secret_store.protect(ical_url_value)
            if not ical_protected:
                return False, "api token protection"

        vacation_ical_protected = None
        if clear_vacation_ical_url:
            vacation_ical_protected = ""
        elif vacation_ical_url_supplied and vacation_ical_url_value:
            if not self.__is_allowed_vacation_ical_url(vacation_ical_url_value):
                return False, "vacation_ical_invalid_endpoint"
            vacation_ical_protected = self.__vacation_secret_store.protect(
                vacation_ical_url_value
            )
            if not vacation_ical_protected:
                return False, "vacation_ical_secret_protection_failed"

        try:
            daily_minutes = int(round(float(daily_minutes)))
        except Exception:
            return False, "daily target"
        if daily_minutes <= 0:
            return False, "daily target"

        try:
            tooltip_ms = int(round(float(tooltip_ms)))
        except Exception:
            return False, "tooltip"
        if tooltip_ms < 1200:
            tooltip_ms = 1200

        try:
            monitor_interval = float(monitor_interval)
        except Exception:
            return False, "monitor interval"
        if monitor_interval < 5:
            monitor_interval = 5.0

        if clear_token:
            self.__set_wrike_api_token_session("")
        elif token_supplied and token:
            self.__set_wrike_api_token_session(token)
        self.__daily_target_minutes = int(daily_minutes)
        self.__tooltip_duration_ms = int(tooltip_ms)
        self.__monitor_enabled = bool(monitor_enabled)
        self.__monitor_interval_sec = float(monitor_interval)
        self.__lunch_break_enabled = bool(lunch_enabled)
        self.__lunch_start_min = int(lunch_start_val)
        self.__lunch_end_min = int(lunch_end_val)
        self.__ical_poll_interval_sec = float(poll_val)
        self.__vacation_ical_poll_interval_sec = float(vacation_poll_val)
        if provider_changed:
            self.__cancel_vacation_ical_after()
            self.__cancel_vacation_google_oauth()
            self.__vacation_ical_generation += 1
            with self.__vacation_ical_lock:
                self.__vacation_ical_fetch_owner = None
        self.__vacation_calendar_provider = next_provider
        if next_keywords is not None:
            self.__ical_keywords = list(next_keywords)
        if ical_protected is not None:
            self.__ical_url_protected = ical_protected
            if not ical_protected:
                self.__ical_url_session = ""
                self.__ical_parsed_events = []
                self.__ical_events_for_date = ""
                self.__ical_matched = []
        elif ical_url_supplied and ical_protected is None and not ical_url_value:
            pass
        if vacation_ical_protected is not None:
            with self.__vacation_ical_lock:
                self.__vacation_ical_url_protected = vacation_ical_protected
                self.__vacation_ical_url_session = (
                    vacation_ical_url_value if vacation_ical_protected else ""
                )
        elif (
            vacation_ical_url_supplied
            and not vacation_ical_url_value
            and not clear_vacation_ical_url
        ):
            pass

        if provider_changed or (
            vacation_ical_protected is not None
            and next_provider == "private_ical"
        ):
            with self.__vacation_ical_lock:
                self.__vacation_ical_calendar = {}
                self.__vacation_ical_events_for_date = ""
                self.__vacation_ical_day_result = {}
                self.__vacation_ical_observed_calendar_name = ""
                self.__vacation_ical_last_success_ts = None
                self.__vacation_ical_last_error = ""
                self.__vacation_ical_week_cache_calendar = None
                self.__vacation_ical_week_cache = {}
                self.__vacation_google_oauth_week_start = ""
            _provider, secret_present, configuration, _fingerprint = (
                self.__active_vacation_configuration()
            )
            with self.__vacation_ical_lock:
                self.__vacation_ical_state = (
                    "loading"
                    if secret_present and configuration is not None
                    else "unconfigured"
                )
        self.__monitor_last_total_minutes = None
        if not self.__save_settings():
            self.__restore_settings_runtime_state(previous_runtime_state)
            if provider_changed:
                self.__start_vacation_ical_polling()
            return False, "settings save failed"
        self.__sync_worktime_panel_idle_timeout()
        self.__restart_monitor()
        self.__start_ical_polling()
        self.__start_vacation_ical_polling()
        return True, None

    def get_monitor_folder_path(self) -> list[dict]:
        return list(self.__monitor_folder_path)

    def set_monitor_folder_path(self, path: list[dict]) -> None:
        if isinstance(path, list):
            self.__monitor_folder_path = [
                f for f in path
                if isinstance(f, dict) and f.get("id")
            ]
        else:
            self.__monitor_folder_path = []
        self.__monitor_last_total_minutes = None
        self.__save_settings()
        self.__restart_monitor()
        return

    def clear_monitor_folder_path(self) -> None:
        self.__monitor_folder_path = []
        self.__monitor_last_total_minutes = None
        self.__save_settings()
        self.__restart_monitor()
        return

    def fetch_spaces(self) -> tuple[list[dict], str | None]:
        token = str(self.__wrike_api_token_session or "").strip()
        if not token:
            return [], "API 토큰이 필요합니다"
        cache_key = "__spaces__"
        cached = self.__folder_cache.get(cache_key)
        if cached is not None:
            return cached, None
        url = f"{self.__wrike_api_base}/spaces"
        data = self.__api_get_json(url, token)
        if data is None:
            if self.__wrike_api_last_error_code in {401, 403}:
                return [], "API 인증 실패"
            return [], "스페이스 조회 실패"
        items = data.get("data")
        if not isinstance(items, list):
            return [], "스페이스 데이터 형식 오류"
        result: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if sid and title:
                result.append({"id": sid, "title": title, "type": "space"})
        self.__folder_cache[cache_key] = result
        return result, None

    def fetch_child_folders(self, parent_id: str) -> tuple[list[dict], str | None]:
        parent_id = str(parent_id or "").strip()
        if not parent_id:
            return [], "상위 폴더 ID가 필요합니다"
        cached = self.__folder_cache.get(parent_id)
        if cached is not None:
            return cached, None
        token = str(self.__wrike_api_token_session or "").strip()
        if not token:
            return [], "API 토큰이 필요합니다"
        url = f"{self.__wrike_api_base}/folders/{parent_id}/folders"
        data = self.__api_get_json(url, token)
        if data is None:
            if self.__wrike_api_last_error_code in {401, 403}:
                return [], "API 인증 실패"
            return [], "하위 폴더 조회 실패"
        items = data.get("data")
        if not isinstance(items, list):
            return [], "폴더 데이터 형식 오류"
        self.__build_folder_tree(parent_id, items)
        return self.__folder_cache.get(parent_id, []), None

    def __build_folder_tree(self, parent_id: str, items: list) -> None:
        lookup: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            raw_child_ids = item.get("childIds")
            child_ids = (
                [str(c) for c in raw_child_ids if c]
                if isinstance(raw_child_ids, list) else []
            )
            if fid and title:
                lookup[fid] = {
                    "id": fid,
                    "title": title,
                    "child_ids": child_ids,
                    "has_children": bool(child_ids),
                }

        all_child_ids: set[str] = set()
        for fdata in lookup.values():
            for cid in fdata["child_ids"]:
                all_child_ids.add(cid)

        for fid, fdata in lookup.items():
            children: list[dict] = []
            for cid in fdata["child_ids"]:
                child = lookup.get(cid)
                if child:
                    children.append({
                        "id": child["id"],
                        "title": child["title"],
                        "type": "folder",
                        "has_children": child["has_children"],
                    })
            self.__folder_cache[fid] = children

        if parent_id not in lookup:
            root: list[dict] = []
            for fid, fdata in lookup.items():
                if fid not in all_child_ids:
                    root.append({
                        "id": fdata["id"],
                        "title": fdata["title"],
                        "type": "folder",
                        "has_children": fdata["has_children"],
                    })
            self.__folder_cache[parent_id] = root
        return

    def suggest_folder_index(self, folders: list[dict]) -> int | None:
        if not folders:
            return None
        now = self.__lib.datetime.now()
        year_str = str(now.year)
        month_idx = max(0, min(11, int(now.month) - 1))
        month_en = self.__time_log_month_names[month_idx]
        month_kr = f"{now.month}월"
        quarter = f"Q{(now.month - 1) // 3 + 1}"
        for i, folder in enumerate(folders):
            title = str(folder.get("title") or "")
            if not title:
                continue
            if month_en.lower() in title.lower() or month_kr in title:
                return i
        for i, folder in enumerate(folders):
            title = str(folder.get("title") or "")
            if not title:
                continue
            if year_str in title:
                return i
        for i, folder in enumerate(folders):
            title = str(folder.get("title") or "")
            if not title:
                continue
            if quarter in title.upper():
                return i
        return None

    def invalidate_folder_cache(self) -> None:
        self.__folder_cache.clear()
        return

    def __get_monitor_folder_id(self) -> str:
        if not self.__monitor_folder_path:
            return ""
        last = self.__monitor_folder_path[-1]
        if not isinstance(last, dict):
            return ""
        return str(last.get("id") or "").strip()

    def validate_api_token(self, token: str) -> tuple[bool, str | None, str | None]:
        contact_id, name, contact_error = self.__resolve_contact_identity(token)
        if contact_error == "auth_failed":
            return False, None, "Wrike API 토큰이 유효하지 않습니다"
        if contact_error == "api_request_failed":
            return False, None, "Wrike 사용자 정보 조회 실패"
        if not contact_id:
            return False, None, "Wrike 사용자 정보를 찾지 못했습니다"
        return True, name or "내 계정", None

    def log_info(self, message: str) -> None:
        try:
            self.__log(str(message))
        except Exception:
            return

    def reload_settings_from_disk(self) -> tuple[bool, str | None]:
        try:
            data, reason = self.__read_settings_file()
            ok, msg = self.__apply_settings_data(data, reason, allow_save=True)
            self.__restart_monitor()
            self.__start_ical_polling()
            self.__start_vacation_ical_polling()
            return ok, msg
        except Exception as exc:
            self.__log_exception("settings reload failed", exc)
            return False, "설정 로드 실패"

    def __read_settings_file(self) -> tuple[dict | None, str | None]:
        path = self.__settings_path
        if not path or not self.__lib.os.path.isfile(path):
            return None, "not_found"
        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = fp.read()
        except Exception:
            return None, "read_failed"
        if not raw.strip():
            return None, "empty"
        try:
            data = json.loads(raw)
        except Exception:
            return None, "invalid"
        if not isinstance(data, dict):
            return None, "invalid"
        return data, None

    def __apply_settings_data(
        self,
        data: dict | None,
        reason: str | None,
        allow_save: bool = True,
    ) -> tuple[bool, str | None]:
        had_data = bool(data) if isinstance(data, dict) else False
        defaults = {
            "settings_version": int(self.__settings_version),
            "daily_target_minutes": int(self.__daily_target_minutes),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "monitor_enabled": True,
            "monitor_interval_sec": 5.0,
            "monitor_folder_path": [],
            "lunch_break_enabled": True,
            "lunch_start_min": int(DEFAULT_LUNCH_START_MIN),
            "lunch_end_min": int(DEFAULT_LUNCH_END_MIN),
            "break_keywords": ["헬스", "운동", "gym", "fitness", "pt"],
            "ical_poll_interval_sec": 900.0,
            "vacation_ical_poll_interval_sec": 900.0,
            "vacation_calendar_provider": "private_ical",
            "vacation_ical_url_protected": "",
            "vacation_google_oauth_protected": "",
            "vacation_google_oauth_delete_pending": False,
        }
        needs_save = False
        if data is None:
            data = {}
            needs_save = True
        if reason in {"not_found", "empty", "invalid", "read_failed"}:
            needs_save = True
        if not had_data and reason is None:
            reason = "empty"

        for key, value in defaults.items():
            if key not in data:
                data[key] = value
                needs_save = True
        if "vacation_expected_calendar_name" in data:
            data.pop("vacation_expected_calendar_name", None)
            needs_save = True
        if "timelog_cache_token_fingerprint" in data:
            data.pop("timelog_cache_token_fingerprint", None)
            needs_save = True

        try:
            version = int(data.get("settings_version", 0))
        except Exception:
            version = 0
        if version < 5:
            try:
                prev_enabled = bool(data.get("monitor_enabled", False))
                prev_interval = float(data.get("monitor_interval_sec", 300.0))
            except Exception:
                prev_enabled = False
                prev_interval = 300.0
            if (not prev_enabled) and prev_interval >= 300:
                data["monitor_enabled"] = True
                data["monitor_interval_sec"] = 5.0
        if version < int(self.__settings_version):
            data["settings_version"] = int(self.__settings_version)
            needs_save = True

        token = ""
        protected_token = str(data.get("api_token_protected", "") or "").strip()
        if protected_token:
            try:
                token = self.__secret_store.unprotect(protected_token)
            except Exception:
                token = ""
        if not token and "api_token" in data:
            try:
                token = str(data.get("api_token", "") or "").strip()
            except Exception:
                token = ""
            needs_save = True
        if token:
            self.__set_wrike_api_token_session(token)
        elif not protected_token:
            self.__set_wrike_api_token_session("")
        try:
            self.__daily_target_minutes = int(data.get("daily_target_minutes", self.__daily_target_minutes))
        except Exception:
            self.__daily_target_minutes = int(self.__time_log_default_daily_minutes)
        try:
            loaded_tooltip_ms = int(
                data.get("tooltip_duration_ms", self.__tooltip_duration_ms)
            )
        except Exception:
            loaded_tooltip_ms = 1200
            needs_save = True
        self.__tooltip_duration_ms = max(1200, loaded_tooltip_ms)
        if self.__tooltip_duration_ms != loaded_tooltip_ms:
            needs_save = True
        try:
            self.__monitor_enabled = bool(data.get("monitor_enabled", self.__monitor_enabled))
        except Exception:
            self.__monitor_enabled = False
        try:
            self.__monitor_interval_sec = float(data.get("monitor_interval_sec", self.__monitor_interval_sec))
        except Exception:
            self.__monitor_interval_sec = float(self.__monitor_interval_sec)
        if self.__monitor_interval_sec < 5:
            self.__monitor_interval_sec = 5.0
            needs_save = True
        try:
            fp_raw = data.get("monitor_folder_path")
            if fp_raw is None:
                fp_raw = data.get("monitor_folders")
            if isinstance(fp_raw, list):
                self.__monitor_folder_path = [
                    f for f in fp_raw
                    if isinstance(f, dict) and f.get("id")
                ]
            else:
                self.__monitor_folder_path = []
        except Exception:
            self.__monitor_folder_path = []

        try:
            self.__lunch_break_enabled = bool(
                data.get("lunch_break_enabled", self.__lunch_break_enabled)
            )
        except Exception:
            pass
        try:
            self.__lunch_start_min = max(
                0, min(1439, int(data.get("lunch_start_min", self.__lunch_start_min)))
            )
        except Exception:
            self.__lunch_start_min = int(DEFAULT_LUNCH_START_MIN)
        try:
            self.__lunch_end_min = max(
                1, min(1440, int(data.get("lunch_end_min", self.__lunch_end_min)))
            )
        except Exception:
            self.__lunch_end_min = int(DEFAULT_LUNCH_END_MIN)
        if self.__lunch_end_min <= self.__lunch_start_min:
            self.__lunch_end_min = min(1440, self.__lunch_start_min + 60)
        try:
            raw_keywords = data.get("break_keywords")
            if isinstance(raw_keywords, list):
                cleaned_keywords = []
                for item in raw_keywords:
                    term = str(item or "").strip()
                    if term and len(term) <= 40:
                        cleaned_keywords.append(term)
                    if len(cleaned_keywords) >= 12:
                        break
                if cleaned_keywords:
                    self.__ical_keywords = cleaned_keywords
        except Exception:
            pass
        try:
            self.__ical_poll_interval_sec = float(
                data.get("ical_poll_interval_sec", self.__ical_poll_interval_sec)
            )
        except Exception:
            self.__ical_poll_interval_sec = 900.0
        self.__ical_poll_interval_sec = max(300.0, min(21600.0, self.__ical_poll_interval_sec))

        try:
            protected_ical_raw = str(data.get("ical_url_protected", "") or "").strip()
            if protected_ical_raw != str(self.__ical_url_protected or "").strip():
                self.__ical_url_protected = protected_ical_raw
                self.__ical_url_session = ""
                self.__ical_parsed_events = []
                self.__ical_events_for_date = ""
                self.__ical_matched = []
        except Exception:
            pass

        try:
            self.__vacation_ical_poll_interval_sec = float(
                data.get(
                    "vacation_ical_poll_interval_sec",
                    self.__vacation_ical_poll_interval_sec,
                )
            )
        except Exception:
            self.__vacation_ical_poll_interval_sec = 900.0
        self.__vacation_ical_poll_interval_sec = max(
            300.0,
            min(21600.0, self.__vacation_ical_poll_interval_sec),
        )
        loaded_provider = str(
            data.get("vacation_calendar_provider", "private_ical") or ""
        ).strip()
        if loaded_provider not in VACATION_CALENDAR_PROVIDERS:
            loaded_provider = "private_ical"
            data["vacation_calendar_provider"] = loaded_provider
            needs_save = True
        provider_changed = (
            loaded_provider != self.__normalized_vacation_provider()
        )
        try:
            protected_vacation_raw = str(
                data.get("vacation_ical_url_protected", "") or ""
            ).strip()
        except Exception:
            protected_vacation_raw = ""
        try:
            protected_oauth_raw = str(
                data.get("vacation_google_oauth_protected", "") or ""
            ).strip()
        except Exception:
            protected_oauth_raw = ""
        delete_pending_raw = bool(
            data.get("vacation_google_oauth_delete_pending", False)
        )
        private_changed = protected_vacation_raw != str(
            self.__vacation_ical_url_protected or ""
        ).strip()
        oauth_changed = (
            protected_oauth_raw
            != str(self.__vacation_google_oauth_protected or "").strip()
            or delete_pending_raw
            != bool(self.__vacation_google_oauth_delete_pending)
        )
        with self.__vacation_ical_lock:
            self.__vacation_calendar_provider = loaded_provider
            if private_changed:
                self.__vacation_ical_url_protected = protected_vacation_raw
                self.__vacation_ical_url_session = ""
            if oauth_changed:
                self.__vacation_google_oauth_protected = protected_oauth_raw
                self.__vacation_google_oauth_session = ""
                self.__vacation_google_oauth_delete_pending = (
                    delete_pending_raw
                )
            clear_active_cache = (
                provider_changed
                or (private_changed and loaded_provider == "private_ical")
                or (oauth_changed and loaded_provider == "google_oauth")
            )
            if clear_active_cache:
                self.__vacation_ical_calendar = {}
                self.__vacation_ical_events_for_date = ""
                self.__vacation_ical_day_result = {}
                self.__vacation_ical_observed_calendar_name = ""
                self.__vacation_ical_last_success_ts = None
                self.__vacation_ical_last_error = ""
                self.__vacation_google_oauth_week_start = ""
                self.__vacation_ical_week_cache_calendar = None
                self.__vacation_ical_week_cache = {}
        if clear_active_cache:
            _provider, secret_present, configuration, _fingerprint = (
                self.__active_vacation_configuration()
            )
            with self.__vacation_ical_lock:
                if secret_present and configuration is None:
                    self.__vacation_ical_state = "error"
                    self.__vacation_ical_last_error = (
                        VACATION_ERROR_SECRET_UNAVAILABLE
                    )
                elif configuration is not None:
                    self.__vacation_ical_state = "loading"
                else:
                    self.__vacation_ical_state = "unconfigured"

        if allow_save and needs_save:
            try:
                self.__save_settings()
            except Exception:
                pass

        msg = None
        if reason == "empty":
            msg = "설정 파일이 비어있어 기본값으로 복구"
        elif reason == "invalid":
            msg = "설정 파일 형식이 깨져 기본값으로 복구"
        elif reason == "not_found":
            msg = "설정 파일 없음, 기본값 생성"
        elif reason == "read_failed":
            msg = "설정 파일 읽기 실패, 기본값으로 복구"
        elif needs_save:
            msg = "설정 파일 누락 항목을 기본값으로 보정"
        return True, msg

    def __load_settings(self) -> None:
        data, reason = self.__read_settings_file()
        self.__apply_settings_data(data, reason, allow_save=True)
        return

    def __save_settings(self) -> bool:
        token = str(self.__wrike_api_token_session or "").strip()
        payload = {
            "settings_version": int(self.__settings_version),
            "daily_target_minutes": int(self.__daily_target_minutes),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "monitor_enabled": bool(self.__monitor_enabled),
            "monitor_interval_sec": float(self.__monitor_interval_sec),
            "monitor_folder_path": list(self.__monitor_folder_path),
            "lunch_break_enabled": bool(self.__lunch_break_enabled),
            "lunch_start_min": int(self.__lunch_start_min),
            "lunch_end_min": int(self.__lunch_end_min),
            "break_keywords": list(self.__ical_keywords),
            "ical_poll_interval_sec": float(self.__ical_poll_interval_sec),
            "vacation_ical_poll_interval_sec": float(
                self.__vacation_ical_poll_interval_sec
            ),
            "vacation_calendar_provider": self.__normalized_vacation_provider(),
            "vacation_google_oauth_delete_pending": bool(
                self.__vacation_google_oauth_delete_pending
            ),
        }
        ical_url_now = str(self.__decode_ical_url() or "").strip()
        if ical_url_now:
            protected_ical = self.__secret_store.protect(ical_url_now)
            if not protected_ical:
                self.__log("settings save skipped: ical url protection failed")
                return False
            payload["ical_url_protected"] = protected_ical
        elif str(self.__ical_url_protected or "").strip():
            payload["ical_url_protected"] = ""
        vacation_url_now = str(self.__vacation_ical_url_session or "").strip()
        protected_vacation = str(
            self.__vacation_ical_url_protected or ""
        ).strip()
        if vacation_url_now:
            if not self.__is_allowed_vacation_ical_url(vacation_url_now):
                self.__log("settings save skipped: vacation calendar url invalid")
                return False
            if not protected_vacation:
                protected_vacation = self.__vacation_secret_store.protect(
                    vacation_url_now
                )
                if not protected_vacation:
                    self.__log(
                        "settings save skipped: vacation calendar protection failed"
                    )
                    return False
        payload["vacation_ical_url_protected"] = protected_vacation
        oauth_session = str(self.__vacation_google_oauth_session or "").strip()
        protected_oauth = str(
            self.__vacation_google_oauth_protected or ""
        ).strip()
        if oauth_session:
            envelope_result = deserialize_envelope(oauth_session)
            if not isinstance(envelope_result, GoogleCalendarSuccess):
                self.__log("settings save skipped: oauth envelope invalid")
                return False
            if not protected_oauth:
                try:
                    protected_oauth = str(
                        self.__vacation_google_oauth_secret_store.protect(
                            oauth_session
                        )
                        or ""
                    ).strip()
                except Exception:
                    protected_oauth = ""
                if not protected_oauth:
                    self.__log("settings save skipped: oauth protection failed")
                    return False
        payload["vacation_google_oauth_protected"] = protected_oauth
        if token:
            protected_token = self.__secret_store.protect(token)
            if not protected_token:
                self.__log("settings save skipped: api token protection failed")
                return False
            payload["api_token_protected"] = protected_token
        temp_path = None
        fd = -1
        try:
            settings_parent = self.__lib.os.path.dirname(
                self.__lib.os.path.abspath(self.__settings_path)
            )
            self.__lib.os.makedirs(settings_parent, exist_ok=True)
            prefix = f".{self.__lib.os.path.basename(self.__settings_path)}."
            fd, temp_path = tempfile.mkstemp(
                prefix=prefix,
                suffix=".tmp",
                dir=settings_parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
                fd = -1
                json.dump(
                    payload,
                    fp,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp_path, self.__settings_path)
            temp_path = None
            self.__vacation_ical_url_protected = protected_vacation
            self.__vacation_google_oauth_protected = protected_oauth
            return True
        except Exception as exc:
            self.__log_exception("settings save failed", exc)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        return False

    def __normalize_authoritative_timelog_entry(
        self,
        item,
        week_start,
        week_end,
    ) -> dict | None:
        if type(item) is not dict:
            return None
        raw_id = item.get("id")
        if type(raw_id) is not str or not raw_id.strip():
            return None
        raw_tracked_date = item.get("trackedDate")
        if type(raw_tracked_date) is not str:
            return None
        try:
            tracked_day = datetime.strptime(
                raw_tracked_date,
                "%Y-%m-%d",
            ).date()
        except Exception:
            return None
        if tracked_day.isoformat() != raw_tracked_date:
            return None
        if tracked_day < week_start or tracked_day > week_end:
            return None

        duration_field = None
        for candidate in ("hours", "trackedHours", "minutes"):
            if candidate in item:
                duration_field = candidate
                break
        if duration_field is None:
            return None
        raw_duration = item.get(duration_field)
        if type(raw_duration) not in {int, float}:
            return None
        try:
            duration = float(raw_duration)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(duration) or duration < 0:
            return None
        scaled = duration if duration_field == "minutes" else duration * 60.0
        if not math.isfinite(scaled) or scaled < 0:
            return None
        try:
            minutes = int(round(scaled))
        except (OverflowError, ValueError):
            return None
        if minutes < 0:
            return None

        normalized = dict(item)
        normalized["id"] = raw_id.strip()
        normalized["trackedDate"] = raw_tracked_date
        normalized["_authoritative_minutes"] = minutes
        return normalized

    def __authoritative_timelog_entry_is_valid(
        self,
        item,
        week_start,
        week_end,
    ) -> bool:
        return self.__normalize_authoritative_timelog_entry(
            item,
            week_start,
            week_end,
        ) is not None

    def __query_authoritative_timelogs_week(
        self,
        token: str,
        contact_id: str,
        week_dates: list,
    ) -> tuple[list[dict] | None, str | None]:
        token = str(token or "").strip()
        contact_id = str(contact_id or "").strip()
        if not token:
            return None, "api_token_missing"
        if not contact_id:
            return None, "contact_not_found"
        if not week_dates or len(week_dates) != 7:
            return None, "week_dates_empty"
        try:
            week_start = week_dates[0].date()
            week_end = week_dates[-1].date()
        except Exception:
            return None, "week_dates_empty"

        encoded_contact_id = urllib.parse.quote(contact_id, safe="")
        contact_url = (
            f"{self.__wrike_api_base}/contacts/{encoded_contact_id}/timelogs"
        )
        base_params = {
            "trackedDate": json.dumps(
                {
                    "start": week_start.isoformat(),
                    "end": week_end.isoformat(),
                },
                separators=(",", ":"),
            ),
            "pageSize": str(int(self.__wrike_api_page_size)),
        }
        try:
            max_pages = max(1, int(self.__wrike_api_max_pages))
        except Exception:
            max_pages = 10
        self.__wrike_api_last_error_code = 0
        page = 0
        next_token = ""
        seen_page_tokens: set[str] = set()
        seen_entry_ids: set[str] = set()
        items: list[dict] = []

        while True:
            page += 1
            params = dict(base_params)
            if next_token:
                params["nextPageToken"] = next_token
            full_url = f"{contact_url}?{urllib.parse.urlencode(params)}"
            payload = self.__api_get_json(full_url, token)
            if payload is None:
                if self.__wrike_api_last_error_code in {401, 403}:
                    return None, "auth_failed"
                return None, "request_failed"
            if not isinstance(payload, dict):
                return None, "invalid_response"
            data_items = payload.get("data")
            if not isinstance(data_items, list):
                return None, "invalid_response"
            for item in data_items:
                normalized = self.__normalize_authoritative_timelog_entry(
                    item,
                    week_start,
                    week_end,
                )
                if normalized is None:
                    return None, "invalid_response"
                entry_id = normalized["id"]
                if entry_id in seen_entry_ids:
                    continue
                seen_entry_ids.add(entry_id)
                items.append(normalized)

            if "nextPageToken" not in payload:
                break
            following_token = payload["nextPageToken"]
            if (
                not isinstance(following_token, str)
                or not following_token
                or following_token.strip() != following_token
            ):
                return None, "invalid_response"
            if following_token in seen_page_tokens:
                return None, "pagination_cycle"
            if page >= max_pages:
                return None, "pagination_limit"
            seen_page_tokens.add(following_token)
            next_token = following_token

        self.__log(
            f"authoritative contact timelogs: {len(items)} entries, {page} pages"
        )
        return items, None

    def __query_timelogs_week(self, token: str, contact_id: str, week_dates: list) -> list[dict] | None:
        if not token or not contact_id or not week_dates:
            return None

        folder_id = self.__get_monitor_folder_id()
        if folder_id:
            return self.__query_timelogs_by_folders(
                token, contact_id, [folder_id], week_dates
            )

        week_start = week_dates[0].date()
        week_end = week_dates[-1].date()

        contact_url = f"{self.__wrike_api_base}/contacts/{contact_id}/timelogs"
        tracked_date_filter = json.dumps({
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
        })
        range_params = {
            "trackedDate": tracked_date_filter,
        }
        timelogs = self.__api_get_list(contact_url, token, dict(range_params))
        if timelogs is not None:
            self.__log(f"api timelogs via trackedDate range: {len(timelogs)}")
            return timelogs
        if self.__wrike_api_last_error_code in {401, 403}:
            return None
        self.__log("api trackedDate range failed, fallback to per-day query")
        return self.__query_timelogs_by_day(token, contact_url, week_dates)

    def __query_timelogs_by_folders(
        self, token: str, contact_id: str, folder_ids: list[str], week_dates: list
    ) -> list[dict] | None:
        week_start = week_dates[0].date()
        week_end = week_dates[-1].date()
        tracked_date_filter = json.dumps({
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
        })
        all_timelogs: list[dict] = []
        seen_ids: set[str] = set()
        for folder_id in folder_ids:
            url = f"{self.__wrike_api_base}/folders/{folder_id}/timelogs"
            params = {
                "trackedDate": tracked_date_filter,
                "me": "true",
                "descendants": "true",
            }
            timelogs = self.__api_get_list(url, token, dict(params))
            if timelogs is None:
                if self.__wrike_api_last_error_code in {401, 403}:
                    return None
                self.__log(f"folder {folder_id} timelog query failed, skipping")
                continue
            for item in timelogs:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                all_timelogs.append(item)
        self.__log(
            f"folder timelogs: {len(all_timelogs)} entries from {len(folder_ids)} folders"
        )
        return all_timelogs

    def __query_timelogs_by_tracked_date(
        self, token: str, contact_url: str, tracked_date: str
    ) -> list[dict] | None:
        params = {
            "trackedDate": tracked_date,
        }
        return self.__api_get_list(contact_url, token, dict(params))

    def __query_timelogs_by_day(self, token: str, contact_url: str, week_dates: list) -> list[dict] | None:
        items: list[dict] = []
        for day in week_dates:
            try:
                day_str = day.date().isoformat()
            except Exception:
                continue
            data = self.__query_timelogs_by_tracked_date(token, contact_url, day_str)
            if data is None:
                return None
            items.extend(data)
        self.__log(f"api timelogs via day: {len(items)}")
        return items

    def __api_get_list(self, url: str, token: str, params: dict | None) -> list[dict] | None:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        data = self.__api_get_json(full_url, token)
        if data is None:
            return None
        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list):
            return items
        return []

    def __tracked_date_is_after(self, timelogs: list[dict], start_date, end_date) -> bool:
        if not timelogs:
            return False
        for item in timelogs:
            if not isinstance(item, dict):
                continue
            tracked = item.get("trackedDate") or item.get("date") or ""
            date_key = self.__normalize_date_key(tracked)
            if date_key is None:
                continue
            if date_key > start_date and date_key <= end_date:
                return True
        return False

    def __api_get_paginated(
        self,
        url: str,
        token: str,
        params: dict | None,
        max_pages: int | None = None,
    ) -> list[dict] | None:
        items: list[dict] = []
        page = 0
        next_token = None
        params = params or {}
        while True:
            page += 1
            if max_pages and page > max_pages:
                break
            if next_token:
                params["nextPageToken"] = next_token
            full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
            data = self.__api_get_json(full_url, token)
            if data is None:
                return None
            data_items = data.get("data") if isinstance(data, dict) else None
            if isinstance(data_items, list):
                items.extend(data_items)
            next_token = ""
            if isinstance(data, dict):
                next_token = str(data.get("nextPageToken") or "").strip()
            if not next_token:
                break
        return items

    def __api_get_json(self, url: str, token: str) -> dict | None:
        headers = {"Authorization": f"Bearer {token}"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.__wrike_api_timeout_sec) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                self.__wrike_api_last_error_code = int(exc.code)
            except Exception:
                self.__wrike_api_last_error_code = 0
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            self.__log(f"api http error: {exc.code} {exc.reason} {body[:200]}")
            return None
        except Exception as exc:
            self.__log_exception("api request failed", exc)
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self.__log_exception("api json parse failed", exc)
            return None

    def __aggregate_timelogs(self, timelogs: list[dict], week_dates: list) -> list[dict]:
        by_date = {}
        for day in week_dates:
            try:
                by_date[day.date()] = 0
            except Exception:
                continue

        for item in timelogs:
            if type(item) is not dict:
                raise ValueError("timelog entry must be an object")
            tracked = item.get("trackedDate") or item.get("date") or ""
            date_key = self.__normalize_date_key(tracked)
            if date_key is None or date_key not in by_date:
                continue
            if "_authoritative_minutes" in item:
                minutes = item.get("_authoritative_minutes")
                if type(minutes) is not int or minutes < 0:
                    raise ValueError("authoritative minutes must be nonnegative int")
            else:
                duration_field = None
                for candidate in ("hours", "trackedHours", "minutes"):
                    if candidate in item:
                        duration_field = candidate
                        break
                if duration_field is None:
                    raise ValueError("timelog duration is missing")
                raw_duration = item.get(duration_field)
                if type(raw_duration) not in {int, float}:
                    raise ValueError("timelog duration must be a JSON number")
                try:
                    duration = float(raw_duration)
                except (OverflowError, ValueError) as exc:
                    raise ValueError("timelog duration cannot be converted") from exc
                if not math.isfinite(duration) or duration < 0:
                    raise ValueError("timelog duration must be finite and nonnegative")
                scaled = duration if duration_field == "minutes" else duration * 60.0
                if not math.isfinite(scaled) or scaled < 0:
                    raise ValueError("scaled timelog duration is invalid")
                try:
                    minutes = int(round(scaled))
                except (OverflowError, ValueError) as exc:
                    raise ValueError("timelog duration cannot be converted") from exc
            by_date[date_key] = int(by_date.get(date_key, 0)) + int(minutes)

        first_dt_by_date = {}
        for item in timelogs:
            if not isinstance(item, dict):
                continue
            tracked = item.get("trackedDate") or item.get("date") or ""
            entry_date_key = self.__normalize_date_key(tracked)
            if entry_date_key is None:
                continue
            candidate = clock_in_candidate(item)
            if candidate is None:
                continue
            previous = first_dt_by_date.get(entry_date_key)
            if previous is None or candidate < previous:
                first_dt_by_date[entry_date_key] = candidate

        days = []
        for day in week_dates:
            date_key = day.date()
            minutes = int(by_date.get(date_key, 0))
            days.append({
                "date": day,
                "minutes": minutes,
                "raw": "",
                "first_dt": first_dt_by_date.get(date_key),
            })
        return days

    def __normalize_date_key(self, value: str):
        raw = str(value or "").strip()
        if not raw:
            return None
        if "T" in raw:
            raw = raw.split("T", maxsplit=1)[0]
        if raw.endswith("Z"):
            raw = raw[:-1]
        try:
            return self.__lib.datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            return None

    def __ensure_playwright_ready(self) -> bool:
        self.__configure_playwright_env()
        if self.__playwright_checked:
            return bool(self.__playwright_ready)
        self.__playwright_checked = True
        try:
            import playwright  # noqa: F401
            if self.__ensure_playwright_browsers_installed():
                self.__playwright_ready = True
                return True
            self.__playwright_ready = False
            return False
        except Exception:
            pass
        if not self.__try_install_playwright():
            self.__playwright_ready = False
            return False
        try:
            import playwright  # noqa: F401
            if self.__ensure_playwright_browsers_installed():
                self.__playwright_ready = True
                return True
            self.__playwright_ready = False
            return False
        except Exception as exc:
            self.__log_exception("playwright import after install failed", exc)
            self.__playwright_ready = False
            return False

    def __launch_playwright_context(self, playwright_obj, user_data_dir: str):
        try:
            return playwright_obj.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
            )
        except Exception as exc:
            if self.__is_playwright_missing_browser_error(exc):
                if self.__is_frozen_runtime():
                    self.__log("playwright bundled browser missing in frozen runtime")
                    return None
                self.__log("playwright browser missing, installing chromium")
                if self.__install_playwright_browsers():
                    try:
                        return playwright_obj.chromium.launch_persistent_context(
                            user_data_dir,
                            headless=False,
                        )
                    except Exception as retry_exc:
                        self.__log_exception("playwright retry failed", retry_exc)
                        return None
            self.__log_exception("playwright launch failed", exc)
            return None

    def __is_playwright_missing_browser_error(self, exc: Exception) -> bool:
        msg = str(exc or "")
        if "Executable doesn't exist" in msg:
            return True
        if "playwright install" in msg:
            return True
        return False

    def __ensure_playwright_browsers_installed(self) -> bool:
        self.__configure_playwright_env()
        if self.__has_playwright_chromium():
            return True
        if self.__is_frozen_runtime():
            self.__log("playwright browser install skipped in frozen runtime; using bundled assets")
            return True
        return self.__install_playwright_browsers()

    def __install_playwright_browsers(self) -> bool:
        cmd = self.__build_python_module_command("playwright", ["install", "chromium"])
        if cmd is None:
            return False
        return self.__run_install_cmd(cmd)

    def __has_playwright_chromium(self) -> bool:
        base = self.__lib.os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        if base == "0":
            base = self.__playwright_package_local_browsers_path()
        elif not base or base == "1":
            base = self.__lib.os.getenv("LOCALAPPDATA") or self.__lib.os.getenv("APPDATA")
            if base:
                base = self.__lib.os.path.join(base, "ms-playwright")
        if not base or not self.__lib.os.path.isdir(base):
            return False
        try:
            entries = self.__lib.os.listdir(base)
        except Exception:
            return False
        for entry in entries:
            if not str(entry).startswith("chromium-"):
                continue
            root = self.__lib.os.path.join(base, entry)
            cand1 = self.__lib.os.path.join(root, "chrome-win64", "chrome.exe")
            cand2 = self.__lib.os.path.join(root, "chrome-win", "chrome.exe")
            if self.__lib.os.path.isfile(cand1) or self.__lib.os.path.isfile(cand2):
                return True
        return False

    def __playwright_package_local_browsers_path(self) -> str | None:
        try:
            import playwright

            package_dir = self.__lib.os.path.dirname(
                self.__lib.os.path.abspath(str(playwright.__file__))
            )
        except Exception:
            package_dir = ""

        if not package_dir:
            meipass = str(getattr(self.__lib.sys, "_MEIPASS", "") or "").strip()
            if not meipass:
                return None
            package_dir = self.__lib.os.path.join(meipass, "playwright")

        return self.__lib.os.path.join(
            package_dir,
            "driver",
            "package",
            ".local-browsers",
        )

    def __try_install_playwright(self) -> bool:
        if self.__is_frozen_runtime():
            self.__log("playwright package install skipped in frozen runtime")
            return False

        uv_path = shutil.which("uv")
        if not uv_path:
            self.__log("uv not found for playwright install")
            return False

        if not self.__run_install_cmd([uv_path, "pip", "install", "playwright"]):
            return False
        cmd = self.__build_python_module_command("playwright", ["install", "chromium"])
        if cmd is None:
            return False
        if not self.__run_install_cmd(cmd):
            return False
        return True

    def __is_frozen_runtime(self) -> bool:
        return is_frozen_runtime(self.__lib.sys)

    def __build_python_module_command(self, module: str, args: list[str]) -> list[str] | None:
        return build_python_module_command(
            module,
            args,
            sys_module=self.__lib.sys,
            log=self.__log,
        )

    def __configure_playwright_env(self) -> None:
        try:
            if self.__is_frozen_runtime():
                self.__lib.os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
            else:
                self.__lib.os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        except Exception:
            return

    def __run_install_cmd(self, argv: list[str]) -> bool:
        try:
            creationflags = 0
            if hasattr(self.__lib.subprocess, "CREATE_NO_WINDOW"):
                creationflags |= self.__lib.subprocess.CREATE_NO_WINDOW
            if hasattr(self.__lib.subprocess, "DETACHED_PROCESS"):
                creationflags |= self.__lib.subprocess.DETACHED_PROCESS
            result = self.__lib.subprocess.run(
                argv,
                capture_output=True,
                text=True,
                creationflags=creationflags,
            )
        except Exception as exc:
            self.__log_exception("install cmd failed", exc)
            return False
        self.__log(f"cmd: {argv!r} rc={result.returncode}")
        if result.stdout:
            self.__log(result.stdout.strip()[:500])
        if result.stderr:
            self.__log(result.stderr.strip()[:500])
        return int(result.returncode) == 0

    def __error_with_log(self, message: str) -> str:
        try:
            self.__log(f"error: {message}")
        except Exception:
            pass
        path = self.__time_log_log_path
        if path:
            return f"{message}\n로그: {path}"
        return message

    def __log(self, message: str) -> None:
        try:
            self.__lib.os.makedirs(self.__time_log_config_dir, exist_ok=True)
        except Exception:
            return
        try:
            ts = self.__lib.datetime.now().isoformat(timespec="seconds")
        except Exception:
            ts = "time"
        line = f"[{ts}] {message}\n"
        try:
            with open(self.__time_log_log_path, "a", encoding="utf-8") as fp:
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

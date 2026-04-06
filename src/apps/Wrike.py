from datetime import timedelta
import json
import shutil
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request

from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip


class Wrike:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__form_url = 'https://www.wrike.com/workspace.htm?acc=469516#/forms?formid=2239448'
        self.__form_tab_presses = 5
        self.__tab_interval_sec = 0.1
        self.__clipboard_timeout_sec = 0.7
        self.__clipboard_copy_retry = 6
        self.__page_ready_timeout_sec = 8.0
        self.__page_ready_poll_sec = 0.05
        self.__page_ready_stable_sec = 0.4
        self.__is_running = False
        self.__time_log_running = False
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
        self.__active_tooltip = None
        self.__settings_version = 3
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

        self.__re_brackets = self.__lib.re.compile(r'\[([^\]]*)\]')
        self.__re_internal = self.__lib.re.compile(r'^없음\s*\((.+?)\)\s*$')
        self.__re_wrike_request_chrome_title = self.__lib.re.compile(
            r'Request\s*-\s*Wrike\s*-\s*(Google\s*)?Chrome',
            self.__lib.re.IGNORECASE,
        )
        self.__re_wrike_chrome_title = self.__lib.re.compile(
            r'Wrike.*(Google\s*)?Chrome',
            self.__lib.re.IGNORECASE,
        )
        self.__re_time_h = self.__lib.re.compile(r'(\d+(?:\.\d+)?)\s*h')
        self.__re_time_m = self.__lib.re.compile(r'(\d+(?:\.\d+)?)\s*m')
        self.__re_time_hhmm = self.__lib.re.compile(r'^\s*(\d+)\s*:\s*(\d{1,2})\s*$')
        self.__re_time_number = self.__lib.re.compile(r'^\s*\d+(?:\.\d+)?\s*$')
        self.__re_weekday_en = self.__lib.re.compile(r'\b(mon|tue|wed|thu|fri|sat|sun)\b', self.__lib.re.I)
        self.__re_date_num = self.__lib.re.compile(r'\b(\d{1,2})[./-](\d{1,2})\b')
        self.__load_settings()
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

    def __safe_get_active_window_title(self) -> str:
        try:
            win = self.__lib.gw.getActiveWindow()
        except Exception:
            return ''
        if win is None:
            return ''
        try:
            title = win.title
        except Exception:
            return ''
        if not title:
            return ''
        return str(title)

    def __wait_for_wrike_form_ready(self) -> bool:
        deadline = self.__lib.time.monotonic() + self.__page_ready_timeout_sec
        last_title = self.__safe_get_active_window_title()
        last_change = self.__lib.time.monotonic()
        saw_title_change = False
        while self.__lib.time.monotonic() < deadline:
            title = self.__safe_get_active_window_title()
            now = self.__lib.time.monotonic()
            if title:
                if self.__re_wrike_request_chrome_title.search(title):
                    return True
                if title != last_title:
                    saw_title_change = True
                    last_title = title
                    last_change = now
                elif saw_title_change and (now - last_change) >= self.__page_ready_stable_sec and self.__re_wrike_chrome_title.search(title):
                    return True
            self.__lib.time.sleep(self.__page_ready_poll_sec)
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
                self.__show_tooltip(root, "클립보드 복사 실패: 텍스트를 선택한 뒤 다시 시도하세요")
                return

            self.__lib.pyautogui.hotkey('ctrl', 't')
            self.__lib.time.sleep(0.05)
            self.__lib.pyautogui.hotkey('ctrl', 'l')
            if not self.__safe_clipboard_copy(self.__form_url):
                self.__show_tooltip(root, "URL 클립보드 복사 실패: 잠시 후 다시 시도하세요")
                return
            self.__lib.time.sleep(0.02)
            self.__lib.pyautogui.hotkey('ctrl', 'v')
            self.__lib.time.sleep(0.02)
            self.__lib.pyautogui.press('enter')
            self.__lib.time.sleep(0.02)
            self.__safe_clipboard_copy(copied_text)
            if not self.__wait_for_wrike_form_ready():
                self.__show_tooltip(root, "Wrike Form 로딩 대기 시간 초과: 잠시 후 다시 시도하세요")
                return
            self.__lib.pyautogui.press('tab', presses=self.__form_tab_presses, interval=self.__tab_interval_sec)

            if self.__safe_clipboard_paste() != copied_text:
                self.__safe_clipboard_copy(copied_text)
                self.__lib.time.sleep(0.02)

            transformed_text = self.transform_text(copied_text)
            if not transformed_text:
                self.__show_tooltip(root, "치환 실패: 텍스트 형식을 확인하세요")
                return

            self.__safe_clipboard_copy(transformed_text)
            self.__lib.time.sleep(0.02)
            self.__lib.pyautogui.hotkey('ctrl', 'v')
            self.__lib.time.sleep(0.02)
            self.__lib.pyautogui.press('left', presses=4, interval=0.02)
            self.__show_tooltip(root, "Wrike Form 입력 완료")
            return
        finally:
            self.__is_running = False

    def open_in_separate_tab(self, root) -> None:
        self.__lib.pyautogui.rightClick()
        self.__lib.pyautogui.moveRel(-20, 0, duration=0.1)
        self.__lib.pyautogui.hotkey('o')
        self.__lib.pyautogui.hotkey('enter')

        tooltip = ToolTip(root, f"새로운 탭에서 열림", bind_events=False)
        tooltip.show_tooltip()
        root.after(1500, tooltip.hide_tooltip)
        return

    def show_weekly_timelog_summary(self, root) -> None:
        if self.__time_log_running:
            return
        daily_target_minutes = int(self.__daily_target_minutes)
        if daily_target_minutes <= 0:
            self.__show_tooltip(root, "Wrike 설정에서 일 목표 시간을 먼저 입력하세요")
            self.__open_settings_tab()
            return
        token = self.__get_wrike_api_token(root, prompt_if_missing=False)
        if not token:
            self.__show_tooltip(root, "Wrike 설정에서 API 키를 먼저 입력하세요")
            self.__open_settings_tab()
            return

        self.__show_tooltip(root, "Wrike 시간 조회중...")
        self.__time_log_running = True

        def task() -> None:
            try:
                contact_id, display_name, contact_error = self.__resolve_contact_identity(token)
                if contact_error or not contact_id:
                    message = "Wrike 사용자 정보를 찾지 못했습니다"
                    if contact_error == "auth_failed":
                        message = "Wrike API 키 인증 실패"
                        self.__ui_safe(root, self.__open_settings_tab)
                    elif contact_error == "api_request_failed":
                        message = "Wrike 사용자 정보 조회 실패"
                    self.__ui_safe(root, lambda: self.__show_tooltip(root, message))
                    return
                days, error = self.__fetch_weekly_timelog(contact_id, token)
                if error:
                    self.__ui_safe(root, lambda: self.__show_tooltip(root, error))
                    return
                if not days:
                    self.__ui_safe(root, lambda: self.__show_tooltip(root, "Wrike 타임로그 데이터가 없습니다"))
                    return
                lines = self.__build_timelog_summary_lines(display_name, daily_target_minutes, days)
                self.__ui_safe(root, lambda: self.__show_tooltip(root, "", lines=lines))
            finally:
                self.__time_log_running = False

        threading.Thread(target=task, daemon=True).start()
        return

    def __ui_safe(self, root, fn) -> None:
        if root is None:
            return
        try:
            root.after(0, fn)
        except Exception:
            return
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
        self.__root = root
        self.__restart_monitor()
        return

    def __restart_monitor(self) -> None:
        root = self.__root
        if root is None:
            return
        try:
            if self.__monitor_after_id is not None:
                root.after_cancel(self.__monitor_after_id)
        except Exception:
            pass
        self.__monitor_after_id = None
        if not self.__monitor_enabled:
            return
        self.__schedule_monitor_tick(root, initial_delay_sec=2.0)
        return

    def __schedule_monitor_tick(self, root, initial_delay_sec: float | None = None) -> None:
        if root is None or not self.__monitor_enabled:
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
        if root is None or not self.__monitor_enabled:
            return
        if self.__monitor_running:
            self.__schedule_monitor_tick(root)
            return

        token = str(self.__wrike_api_token_session or "").strip()
        if not token:
            self.__log("monitor skipped: token missing")
            self.__schedule_monitor_tick(root)
            return

        self.__monitor_running = True

        def worker() -> None:
            try:
                contact_id, display_name, contact_error = self.__resolve_contact_identity(token)
                if contact_error or not contact_id:
                    if contact_error:
                        self.__log(f"monitor contact resolve failed: {contact_error}")
                    return
                days, error = self.__fetch_weekly_timelog_via_api(token, contact_id)
                if error or not days:
                    if error:
                        self.__log(f"monitor api error: {error}")
                    return
                total_minutes = sum(int(d.get("minutes", 0)) for d in days)
                last_total = self.__monitor_last_total_minutes
                if last_total is None:
                    self.__monitor_last_total_minutes = total_minutes
                    return
                if total_minutes <= int(last_total):
                    return

                self.__monitor_last_total_minutes = total_minutes
                daily_target_minutes = int(self.__daily_target_minutes)
                lines = self.__build_timelog_summary_lines(
                    display_name, daily_target_minutes, days
                )
                self.__ui_safe(root, lambda: self.__show_tooltip(root, "", lines=lines))
            finally:
                self.__monitor_running = False
                self.__schedule_monitor_tick(root)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.__monitor_running = False
            self.__schedule_monitor_tick(root)
        return

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

    def __build_timelog_summary_lines(
        self, display_name: str, daily_target_minutes: int, days: list[dict]
    ) -> list[tuple[str, str | None]]:
        week_start, week_end = self.__extract_week_range(days)
        month_label = self.__format_month_label(week_start, week_end)
        lines: list[tuple[str, str | None]] = [
            (f"Wrike 타임로그 (이번 주) - {display_name}", None)
        ]
        if month_label:
            lines.append((f"조회 기준 월: {month_label}", None))
        if week_start and week_end:
            lines.append(
                (f"조회 주간: {week_start.strftime('%Y-%m-%d')} ~ {week_end.strftime('%Y-%m-%d')}", None)
            )
        if self.__monitor_folder_path:
            path_names = [
                str(f.get("title") or f.get("id", "?"))
                for f in self.__monitor_folder_path
            ]
            lines.append((f"폴더: {' / '.join(path_names)}", None))
        lines.append((f"일 목표: {self.__format_minutes(daily_target_minutes)}", None))

        today = self.__lib.datetime.now().date()
        for idx, day in enumerate(days):
            date_value = day.get("date")
            raw_minutes = int(day.get("minutes", 0))
            label = self.__format_day_label(date_value, idx)
            display = self.__format_minutes(raw_minutes)
            per_target = self.__target_minutes_for_date(date_value, int(daily_target_minutes))
            diff = int(per_target) - raw_minutes
            if diff > 0:
                status = f"필요 {self.__format_minutes(diff)}"
            elif diff < 0:
                status = f"초과 {self.__format_minutes(-diff)}"
            else:
                status = "목표 달성"
            color = self.__pick_day_color(date_value, raw_minutes, per_target, today)
            lines.append((f"{label}: 기록 {display}, {status}", color))
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

    def __ensure_wrike_logged_in(self, page, root) -> str | None:
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
            page.goto(self.__time_log_root_url, wait_until="domcontentloaded")
        return None

    def __is_login_url(self, url: str) -> bool:
        lowered = str(url or "").lower()
        return "login" in lowered or "signin" in lowered or "sso" in lowered or "auth" in lowered

    def __requires_login(self, page) -> bool:
        url = str(page.url or "")
        if self.__is_login_url(url):
            return True
        try:
            if page.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
        for label in ("Log in", "Sign in", "로그인", "SSO"):
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

    def __set_wrike_api_token_session(self, token: str) -> None:
        token = str(token or "").strip()
        current = str(self.__wrike_api_token_session or "").strip()
        if token != current:
            self.__wrike_api_token_session = token
            self.__reset_wrike_contact_cache()
        else:
            self.__wrike_api_token_session = token
        return

    def __get_wrike_api_token(self, root, prompt_if_missing: bool = False) -> str:
        token = str(self.__wrike_api_token_session or "").strip()
        if not token:
            token = self.__lib.os.getenv(self.__wrike_api_token_env) or ""
        if not token:
            token = self.__lib.os.getenv("WRIKE_API_TOKEN") or ""
        if not token:
            try:
                if self.__lib.os.path.isfile(self.__time_log_token_path):
                    with open(self.__time_log_token_path, "r", encoding="utf-8") as fp:
                        token = (fp.readline() or "").strip()
            except Exception:
                token = ""
        token = str(token).strip()
        if token:
            self.__set_wrike_api_token_session(token)
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
        return {
            "api_token": str(self.__wrike_api_token_session or ""),
            "daily_target_minutes": int(self.__daily_target_minutes),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "monitor_enabled": bool(self.__monitor_enabled),
            "monitor_interval_sec": float(self.__monitor_interval_sec),
            "monitor_folder_path": list(self.__monitor_folder_path),
            "settings_path": str(self.__settings_path or ""),
        }

    def update_settings(self, data: dict) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"

        token = str(data.get("api_token", "") or "").strip()
        daily_minutes = data.get("daily_target_minutes", self.__daily_target_minutes)
        tooltip_ms = data.get("tooltip_duration_ms", self.__tooltip_duration_ms)
        monitor_enabled = bool(data.get("monitor_enabled", self.__monitor_enabled))
        monitor_interval = data.get("monitor_interval_sec", self.__monitor_interval_sec)

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

        self.__set_wrike_api_token_session(token)
        self.__daily_target_minutes = int(daily_minutes)
        self.__tooltip_duration_ms = int(tooltip_ms)
        self.__monitor_enabled = bool(monitor_enabled)
        self.__monitor_interval_sec = float(monitor_interval)
        self.__monitor_last_total_minutes = None
        self.__save_settings()
        self.__restart_monitor()
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
            "api_token": "",
            "daily_target_minutes": int(self.__daily_target_minutes),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "monitor_enabled": True,
            "monitor_interval_sec": 5.0,
            "monitor_folder_path": [],
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

        try:
            version = int(data.get("settings_version", 0))
        except Exception:
            version = 0
        if version < int(self.__settings_version):
            try:
                prev_enabled = bool(data.get("monitor_enabled", False))
                prev_interval = float(data.get("monitor_interval_sec", 300.0))
            except Exception:
                prev_enabled = False
                prev_interval = 300.0
            if (not prev_enabled) and prev_interval >= 300:
                data["monitor_enabled"] = True
                data["monitor_interval_sec"] = 5.0
            data["settings_version"] = int(self.__settings_version)
            needs_save = True

        try:
            self.__set_wrike_api_token_session(str(data.get("api_token", "") or "").strip())
        except Exception:
            self.__set_wrike_api_token_session("")
        try:
            self.__daily_target_minutes = int(data.get("daily_target_minutes", self.__daily_target_minutes))
        except Exception:
            self.__daily_target_minutes = int(self.__time_log_default_daily_minutes)
        try:
            self.__tooltip_duration_ms = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            self.__tooltip_duration_ms = int(self.__tooltip_duration_ms)
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

    def __save_settings(self) -> None:
        payload = {
            "settings_version": int(self.__settings_version),
            "api_token": str(self.__wrike_api_token_session or ""),
            "daily_target_minutes": int(self.__daily_target_minutes),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "monitor_enabled": bool(self.__monitor_enabled),
            "monitor_interval_sec": float(self.__monitor_interval_sec),
            "monitor_folder_path": list(self.__monitor_folder_path),
        }
        try:
            self.__lib.os.makedirs(self.__time_log_config_dir, exist_ok=True)
            with open(self.__settings_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.__log_exception("settings save failed", exc)
        return

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
            if not isinstance(item, dict):
                continue
            tracked = item.get("trackedDate") or item.get("date") or ""
            date_key = self.__normalize_date_key(tracked)
            if date_key is None or date_key not in by_date:
                continue
            minutes = 0
            hours = item.get("hours")
            if hours is None:
                hours = item.get("trackedHours")
            if hours is not None:
                try:
                    minutes = int(round(float(hours) * 60))
                except Exception:
                    minutes = 0
            else:
                raw_minutes = item.get("minutes")
                if raw_minutes is not None:
                    try:
                        minutes = int(round(float(raw_minutes)))
                    except Exception:
                        minutes = 0
            by_date[date_key] = int(by_date.get(date_key, 0)) + int(minutes)

        days = []
        for day in week_dates:
            date_key = day.date()
            minutes = int(by_date.get(date_key, 0))
            days.append({"date": day, "minutes": minutes, "raw": ""})
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
        if self.__has_playwright_chromium():
            return True
        return self.__install_playwright_browsers()

    def __install_playwright_browsers(self) -> bool:
        return self.__run_install_cmd(
            [self.__lib.sys.executable, "-m", "playwright", "install", "chromium"]
        )

    def __has_playwright_chromium(self) -> bool:
        base = self.__lib.os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        if not base or base in {"0", "1"}:
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

    def __try_install_playwright(self) -> bool:
        uv_path = shutil.which("uv")
        if not uv_path:
            self.__log("uv not found for playwright install")
            return False

        if not self.__run_install_cmd([uv_path, "pip", "install", "playwright"]):
            return False
        if not self.__run_install_cmd([self.__lib.sys.executable, "-m", "playwright", "install", "chromium"]):
            return False
        return True

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

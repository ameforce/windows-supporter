from __future__ import annotations

from dataclasses import FrozenInstanceError
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.apps.wrike_worktime_panel import (
    WorktimeActivityPrompt,
    WorktimePanelDayRow,
    WorktimePanelLine,
    WorktimePanelModel,
    WorktimeQuickPanel,
)


_WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls: list[tuple[str, int, object]] = []
        self.after_history: list[tuple[str, int, object]] = []
        self.after_cancel_calls: list[str] = []
        self._next_after_id = 0
        self.pointer_x = 100
        self.pointer_y = 100
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def after(self, delay_ms, callback):
        self._next_after_id += 1
        after_id = f"after-{self._next_after_id}"
        call = (after_id, int(delay_ms), callback)
        self.after_calls.append(call)
        self.after_history.append(call)
        return after_id

    def after_cancel(self, after_id) -> None:
        self.after_cancel_calls.append(after_id)
        self.after_calls = [
            call for call in self.after_calls if call[0] != after_id
        ]

    def active_id(self, delay_ms: int) -> str:
        matches = [
            after_id
            for after_id, delay, _callback in self.after_calls
            if delay == delay_ms
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected one active {delay_ms}ms timer, got {matches!r}"
            )
        return matches[0]

    def active_delays(self) -> list[int]:
        return sorted(delay for _after_id, delay, _callback in self.after_calls)

    def run_delay(self, delay_ms: int) -> str:
        for index, (after_id, delay, callback) in enumerate(self.after_calls):
            if delay == delay_ms:
                self.after_calls.pop(index)
                callback()
                return after_id
        raise AssertionError(f"active timer not found for {delay_ms}ms")

    def winfo_rootx(self) -> int:
        return 790

    def winfo_rooty(self) -> int:
        return 590


class _FakeWidget:
    def __init__(self, owner, parent=None, **kwargs) -> None:
        self.owner = owner
        self.parent = parent
        self.kwargs = dict(kwargs)
        self.children: list[_FakeWidget] = []
        self.pack_kwargs: dict = {}
        self.pack_calls: list[dict] = []
        self.pack_forget_calls = 0
        self.packed = False
        self.pack_configure_calls: list[dict] = []
        self.configure_calls: list[dict] = []
        self.bindings: dict[str, object] = {}
        self.bind_adds: dict[str, object] = {}
        self.protocols: dict[str, object] = {}
        self.geometry_calls: list[str] = []
        self.requested_width = 700
        self.requested_height = 500
        self.x = 0
        self.y = 0
        self.width = 1
        self.height = 1
        self.geometry_failures = 0
        self.mapped = False
        self.title_calls: list[str] = []
        self.override_redirect = False
        self.attribute_calls: list[tuple] = []
        self.resizable_calls: list[tuple] = []
        self.entry_text = ""
        self.focus_set_calls = 0
        self.selection_calls: list[tuple] = []
        self.withdraw_calls = 0
        self.deiconify_calls = 0
        self.lift_calls = 0
        self.focus_force_calls = 0
        self.grab_set_calls = 0
        self.wait_window_calls = 0
        self.update_idletasks_calls = 0
        self.size_query_calls = 0
        self.destroy_calls = 0
        self.destroyed = False
        if isinstance(parent, _FakeWidget):
            parent.children.append(self)

    def pack(self, **kwargs):
        options = dict(kwargs)
        self.pack_kwargs = options
        self.pack_calls.append(options)
        self.packed = True
        return None

    def pack_forget(self):
        self.pack_forget_calls += 1
        self.packed = False
        return None

    def pack_configure(self, **kwargs):
        options = dict(kwargs)
        self.pack_kwargs.update(options)
        self.pack_configure_calls.append(options)
        return None

    def configure(self, **kwargs):
        self.configure_calls.append(dict(kwargs))
        self.kwargs.update(kwargs)
        return None

    def resizable(self, *args):
        self.resizable_calls.append(tuple(args))
        return None

    def overrideredirect(self, value=None):
        if value is not None:
            self.override_redirect = bool(value)
        return self.override_redirect

    def attributes(self, *args):
        self.attribute_calls.append(tuple(args))
        return None

    def delete(self, _start, _end=None):
        self.entry_text = ""
        return None

    def insert(self, index, value):
        text = str(value)
        if int(index) <= 0:
            self.entry_text = text + self.entry_text
        else:
            self.entry_text += text
        return None

    def get(self):
        return self.entry_text

    def focus_set(self):
        self.focus_set_calls += 1
        return None

    def selection_range(self, start, end):
        self.selection_calls.append((start, end))
        return None

    def title(self, text):
        self.title_calls.append(str(text))
        return None

    def withdraw(self):
        self.withdraw_calls += 1
        self.mapped = False
        return None

    def deiconify(self):
        self.deiconify_calls += 1
        self.mapped = True
        return None

    def lift(self):
        self.lift_calls += 1
        return None

    def focus_force(self):
        self.focus_force_calls += 1
        return None

    def grab_set(self):
        self.grab_set_calls += 1
        return None

    def wait_window(self):
        self.wait_window_calls += 1
        return None

    def bind(self, sequence, callback, add=None):
        self.bindings[str(sequence)] = callback
        self.bind_adds[str(sequence)] = add
        return None

    def protocol(self, name, callback):
        self.protocols[str(name)] = callback
        return None

    def geometry(self, value):
        text = str(value)
        self.geometry_calls.append(text)
        if self.geometry_failures > 0:
            self.geometry_failures -= 1
            return None
        matched = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", text)
        if matched is None:
            raise ValueError(f"invalid geometry: {text}")
        self.width = int(matched.group(1))
        self.height = int(matched.group(2))
        self.x = int(matched.group(3))
        self.y = int(matched.group(4))
        return None

    def native_show(self):
        self.mapped = True
        return True

    def update_idletasks(self):
        self.update_idletasks_calls += 1
        return None

    def winfo_children(self):
        return [child for child in self.children if not child.destroyed]

    def winfo_exists(self):
        return not self.destroyed

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_vrootx(self):
        return 0

    def winfo_vrooty(self):
        return 0

    def winfo_vrootwidth(self):
        return 1920

    def winfo_vrootheight(self):
        return 1080

    def winfo_pointerx(self):
        return int(self.owner.pointer_x)

    def winfo_pointery(self):
        return int(self.owner.pointer_y)

    def winfo_reqwidth(self):
        self.size_query_calls += 1
        return self.requested_width

    def winfo_reqheight(self):
        self.size_query_calls += 1
        return self.requested_height

    def winfo_width(self):
        self.size_query_calls += 1
        return self.width

    def winfo_height(self):
        self.size_query_calls += 1
        return self.height

    def winfo_x(self):
        self.size_query_calls += 1
        return self.x

    def winfo_y(self):
        self.size_query_calls += 1
        return self.y

    def winfo_ismapped(self):
        return self.mapped and not self.destroyed

    def destroy(self):
        if self.destroyed:
            return None
        self.destroy_calls += 1
        self.destroyed = True
        self.mapped = False
        for child in list(self.children):
            child.destroy()
        if isinstance(self.parent, _FakeWidget) and self in self.parent.children:
            self.parent.children.remove(self)
        return None


class _FakeButton(_FakeWidget):
    def invoke(self):
        command = self.kwargs.get("command")
        if callable(command):
            return command()
        return None


class _FakeTk:
    def __init__(self) -> None:
        self.toplevels: list[_FakeWidget] = []
        self.frames: list[_FakeWidget] = []
        self.labels: list[_FakeWidget] = []
        self.buttons: list[_FakeButton] = []
        self.entries: list[_FakeWidget] = []
        self.pointer_x = 100
        self.pointer_y = 100

    def Toplevel(self, parent):
        widget = _FakeWidget(self, parent)
        self.toplevels.append(widget)
        return widget

    def Frame(self, parent, **kwargs):
        widget = _FakeWidget(self, parent, **kwargs)
        self.frames.append(widget)
        return widget

    def Label(self, parent, **kwargs):
        widget = _FakeWidget(self, parent, **kwargs)
        self.labels.append(widget)
        return widget

    def Button(self, parent, **kwargs):
        widget = _FakeButton(self, parent, **kwargs)
        self.buttons.append(widget)
        return widget

    def Entry(self, parent, **kwargs):
        widget = _FakeWidget(self, parent, **kwargs)
        self.entries.append(widget)
        return widget

    def all_widgets(self) -> list[_FakeWidget]:
        return [
            *self.toplevels,
            *self.frames,
            *self.labels,
            *self.buttons,
            *self.entries,
        ]

    def live_label_texts(self) -> list[str]:
        return [
            str(label.kwargs.get("text", ""))
            for label in self.labels
            if not label.destroyed
        ]

    def live_buttons(self) -> list[_FakeButton]:
        return [button for button in self.buttons if not button.destroyed]

    def button(self, text: str) -> _FakeButton:
        for button in self.live_buttons():
            if button.kwargs.get("text") == text:
                return button
        raise AssertionError(f"button not found: {text!r}")

    def pack_call_count(self) -> int:
        return sum(len(widget.pack_calls) for widget in self.all_widgets())

    def pack_configure_call_count(self) -> int:
        return sum(
            len(widget.pack_configure_calls) for widget in self.all_widgets()
        )

    def destroy_call_count(self) -> int:
        return sum(widget.destroy_calls for widget in self.all_widgets())


def _model(
    *,
    actual_text: str = "Wrike 기록 1:30 · 현재 기대 2:00",
    has_clock_in: bool = False,
    clock_in_time: str | None = None,
    break_active: bool = False,
    prompt: WorktimeActivityPrompt | None = None,
    today_lines: tuple[WorktimePanelLine, ...] | None = None,
    week_range: str = "2026-04-06 - 2026-04-12",
    sync_text: str = "방금 동기화",
    target_minutes: int = 480,
    today_index: int = 0,
    row_suffix: str = "",
) -> WorktimePanelModel:
    rows = tuple(
        WorktimePanelDayRow(
            weekday=weekday,
            date=f"04/{6 + index:02d}",
            summary=f"{index + 1}:00 / 8:00{row_suffix}",
            today=index == today_index,
            color="#1D4ED8" if index == today_index else "#6B7280",
        )
        for index, weekday in enumerate(_WEEKDAYS)
    )
    if today_lines is None:
        today_lines = (
            WorktimePanelLine(actual_text, "#2563EB"),
            WorktimePanelLine("출근 09:00 · 예상 퇴근 18:00", "#059669"),
        )
    if clock_in_time is None and has_clock_in:
        clock_in_time = "09:00"
    return WorktimePanelModel(
        week_range=week_range,
        sync_text=sync_text,
        sync_state="fresh",
        today_lines=today_lines,
        target_minutes=target_minutes,
        clock_in_time=clock_in_time,
        break_active=break_active,
        rows=rows,
        prompt=prompt,
    )


def _make_panel(root, fake_tk, holder, *, idle_timeout_ms: int = 6_000):
    callbacks = {
        "refresh": Mock(),
        "clock_in_now": Mock(),
        "edit_clock_in": Mock(return_value=True),
        "edit_plan": Mock(return_value=True),
        "toggle_break": Mock(),
        "open_settings": Mock(),
        "prompt_accept": Mock(),
        "prompt_edit": Mock(return_value=True),
        "prompt_snooze": Mock(),
        "prompt_skip": Mock(),
    }
    provider = Mock(side_effect=lambda: holder["model"])
    panel = WorktimeQuickPanel(
        root,
        provider,
        refresh=callbacks["refresh"],
        clock_in_now=callbacks["clock_in_now"],
        edit_clock_in=callbacks["edit_clock_in"],
        edit_plan=callbacks["edit_plan"],
        toggle_break=callbacks["toggle_break"],
        open_settings=callbacks["open_settings"],
        prompt_accept=callbacks["prompt_accept"],
        prompt_edit=callbacks["prompt_edit"],
        prompt_snooze=callbacks["prompt_snooze"],
        prompt_skip=callbacks["prompt_skip"],
        tk_module=fake_tk,
        idle_timeout_ms=idle_timeout_ms,
        monotonic=root.monotonic,
    )
    return panel, provider, callbacks


class WorktimePanelModelTests(unittest.TestCase):
    def test_models_are_immutable_and_require_exactly_seven_rows(self) -> None:
        model = _model()

        with self.assertRaises(FrozenInstanceError):
            model.sync_text = "변경"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            WorktimePanelModel(
                week_range=model.week_range,
                sync_text=model.sync_text,
                sync_state=model.sync_state,
                today_lines=list(model.today_lines),  # type: ignore[arg-type]
                target_minutes=model.target_minutes,
                clock_in_time=None,
                break_active=False,
                rows=model.rows,
            )
        with self.assertRaises(ValueError):
            WorktimePanelModel(
                week_range=model.week_range,
                sync_text=model.sync_text,
                sync_state=model.sync_state,
                today_lines=model.today_lines,
                target_minutes=model.target_minutes,
                clock_in_time=None,
                break_active=False,
                rows=model.rows[:6],
            )
        with self.assertRaises(ValueError):
            WorktimeActivityPrompt("24:00")
        with self.assertRaises(ValueError):
            _model(clock_in_time="24:00")
        self.assertFalse(_model().has_clock_in)
        self.assertTrue(_model(clock_in_time="08:05").has_clock_in)
        self.assertEqual(
            WorktimeActivityPrompt("08:05").detected_hhmm,
            "08:05",
        )


class WorktimeQuickPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        noactivate_patcher = patch(
            "src.apps.wrike_worktime_panel._show_window_without_activation",
            side_effect=lambda window: window.native_show(),
        )
        active_patcher = patch(
            "src.apps.wrike_worktime_panel._show_window_activated",
            side_effect=lambda window: window.native_show(),
        )
        foreground_patcher = patch(
            "src.apps.wrike_worktime_panel._window_is_foreground",
            return_value=False,
        )
        self.show_without_activation = noactivate_patcher.start()
        self.show_activated = active_patcher.start()
        self.window_is_foreground = foreground_patcher.start()
        self.addCleanup(noactivate_patcher.stop)
        self.addCleanup(active_patcher.stop)
        self.addCleanup(foreground_patcher.stop)

    def test_show_reuses_window_has_distinct_timers_and_rearms_dismissal(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, provider, _callbacks = _make_panel(root, fake_tk, holder)

        self.assertTrue(panel.show(activate=False))
        window = fake_tk.toplevels[0]
        first_buttons = fake_tk.live_buttons()
        refresh_id = root.active_id(1_000)
        first_dismiss_id = root.active_id(6_000)
        geometry_count = len(window.geometry_calls)

        self.assertTrue(panel.show(activate=False))

        self.assertEqual(len(fake_tk.toplevels), 1)
        self.assertIs(fake_tk.toplevels[0], window)
        self.assertEqual(window.geometry_calls, ["700x500+116+116"])
        self.assertEqual(len(window.geometry_calls), geometry_count)
        self.assertEqual(fake_tk.live_buttons(), first_buttons)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])
        self.assertEqual(root.active_id(1_000), refresh_id)
        self.assertNotEqual(root.active_id(6_000), first_dismiss_id)
        self.assertIn(first_dismiss_id, root.after_cancel_calls)
        self.assertNotEqual(refresh_id, root.active_id(6_000))
        texts = fake_tk.live_label_texts()
        self.assertTrue(set(_WEEKDAYS) <= set(texts))
        self.assertEqual(sum(text in _WEEKDAYS for text in texts), 7)
        self.assertIn("오늘", texts)

    def test_equal_model_returns_before_assignment_or_any_render_path(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, _provider, _callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        rendered_model = panel._model
        holder["model"] = _model()
        self.assertIsNot(holder["model"], rendered_model)

        with (
            patch.object(panel, "_window_exists") as window_exists,
            patch.object(panel, "_render_structure") as rebuild,
            patch.object(panel, "_update_rendered_model") as update,
            patch.object(panel, "_reconcile_geometry") as reconcile,
        ):
            self.assertTrue(panel.refresh_now())

        self.assertIs(panel._model, rendered_model)
        window_exists.assert_not_called()
        rebuild.assert_not_called()
        update.assert_not_called()
        reconcile.assert_not_called()

    def test_text_only_refresh_preserves_widgets_without_layout_or_geometry(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(actual_text="실제 1:00 vs 기대 2:00")}
        panel, _provider, _callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        window = fake_tk.toplevels[0]
        buttons = tuple(fake_tk.live_buttons())
        widget_counts = (
            len(fake_tk.frames),
            len(fake_tk.labels),
            len(fake_tk.buttons),
        )
        pack_count = fake_tk.pack_call_count()
        pack_configure_count = fake_tk.pack_configure_call_count()
        destroy_count = fake_tk.destroy_call_count()
        update_count = window.update_idletasks_calls
        size_query_count = window.size_query_calls
        geometry_count = len(window.geometry_calls)
        holder["model"] = _model(
            actual_text="실제 1:01 vs 기대 2:01",
            week_range="2026-04-13 - 2026-04-19",
            sync_text="1초 전 동기화",
            today_index=1,
            row_suffix=" 변경",
        )

        with patch(
            "src.apps.wrike_worktime_panel._work_area_for_window"
        ) as work_area:
            self.assertTrue(panel.refresh_now())

        self.assertEqual(tuple(fake_tk.live_buttons()), buttons)
        self.assertEqual(
            (len(fake_tk.frames), len(fake_tk.labels), len(fake_tk.buttons)),
            widget_counts,
        )
        self.assertEqual(fake_tk.pack_call_count(), pack_count)
        self.assertEqual(
            fake_tk.pack_configure_call_count(),
            pack_configure_count,
        )
        self.assertEqual(fake_tk.destroy_call_count(), destroy_count)
        self.assertEqual(window.update_idletasks_calls, update_count)
        self.assertEqual(window.size_query_calls, size_query_count)
        self.assertEqual(len(window.geometry_calls), geometry_count)
        work_area.assert_not_called()
        texts = fake_tk.live_label_texts()
        self.assertIn("실제 1:01 vs 기대 2:01", texts)
        self.assertNotIn("실제 1:00 vs 기대 2:00", texts)
        self.assertIn("2026-04-13 - 2026-04-19", texts)
        self.assertIn("동기화 · 1초 전 동기화", texts)

    def test_time_edits_share_one_panel_local_inline_editor(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {
            "model": _model(
                has_clock_in=True,
                clock_in_time="08:00",
                prompt=WorktimeActivityPrompt("08:35"),
            )
        }
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        panel_window = fake_tk.toplevels[0]
        shared_entry = panel._widgets["inline_entry"]

        fake_tk.button("출근 수정").invoke()
        self.assertTrue(panel._inline_editor_active)
        self.assertEqual(shared_entry.get(), "08:00")
        shared_entry.delete(0, "end")
        shared_entry.insert(0, "24:00")
        fake_tk.button("저장").invoke()
        callbacks["edit_clock_in"].assert_not_called()
        self.assertIn("23:59", panel._widgets["inline_error"].kwargs["text"])
        self.assertTrue(panel._inline_editor_active)
        shared_entry.delete(0, "end")
        shared_entry.insert(0, "08:15")
        fake_tk.button("저장").invoke()
        callbacks["edit_clock_in"].assert_called_once_with("08:15")
        self.assertFalse(panel._inline_editor_active)

        fake_tk.button("계획 수정").invoke()
        self.assertIs(panel._widgets["inline_entry"], shared_entry)
        self.assertEqual(shared_entry.get(), "08:00")
        shared_entry.delete(0, "end")
        shared_entry.insert(0, "07:30")
        fake_tk.button("저장").invoke()
        callbacks["edit_plan"].assert_called_once_with(450)
        self.assertFalse(panel._inline_editor_active)

        fake_tk.button("시간 수정").invoke()
        self.assertIs(panel._widgets["inline_entry"], shared_entry)
        self.assertEqual(shared_entry.get(), "08:35")
        shared_entry.delete(0, "end")
        shared_entry.insert(0, "08:40")
        fake_tk.button("저장").invoke()
        callbacks["prompt_edit"].assert_called_once_with("08:35", "08:40")
        self.assertFalse(panel._inline_editor_active)

        self.assertEqual(fake_tk.toplevels, [panel_window])
        self.assertEqual(panel_window.grab_set_calls, 0)
        self.assertEqual(panel_window.wait_window_calls, 0)
        callbacks["clock_in_now"].assert_not_called()

        fake_tk.button("휴게 시작").invoke()
        callbacks["toggle_break"].assert_called_once_with()
        fake_tk.button("설정").invoke()
        callbacks["open_settings"].assert_called_once_with()

    def test_prompt_inline_editor_closes_when_detected_context_changes(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(prompt=WorktimeActivityPrompt("08:35"))}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)

        fake_tk.button("시간 수정").invoke()
        self.assertTrue(panel._inline_editor_active)
        self.assertEqual(panel._widgets["inline_entry"].get(), "08:35")
        holder["model"] = _model(prompt=WorktimeActivityPrompt("08:36"))
        panel.refresh_now()

        self.assertFalse(panel._inline_editor_active)
        callbacks["prompt_edit"].assert_not_called()
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

    def test_prompt_time_updates_in_place_but_presence_change_rebuilds(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(prompt=WorktimeActivityPrompt("08:35"))}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        all_buttons = tuple(fake_tk.live_buttons())
        accept_button = fake_tk.button("08:35으로 출근")
        edit_button = fake_tk.button("시간 수정")
        accept_command = accept_button.kwargs["command"]
        edit_command = edit_button.kwargs["command"]

        with patch.object(
            panel,
            "_reconcile_geometry",
            wraps=panel._reconcile_geometry,
        ) as reconcile:
            holder["model"] = _model(prompt=WorktimeActivityPrompt("09:10"))
            panel.refresh_now()
            reconcile.assert_not_called()
            self.assertEqual(tuple(fake_tk.live_buttons()), all_buttons)
            self.assertIs(fake_tk.button("09:10으로 출근"), accept_button)
            self.assertIs(accept_button.kwargs["command"], accept_command)
            self.assertIs(edit_button.kwargs["command"], edit_command)
            self.assertTrue(
                all(
                    "command" not in call
                    for button in all_buttons
                    for call in button.configure_calls
                )
            )
            fake_tk.button("09:10으로 출근").invoke()
            callbacks["prompt_accept"].assert_called_once_with("09:10")

            holder["model"] = _model(prompt=None)
            panel.refresh_now()
            self.assertEqual(reconcile.call_count, 1)

        self.assertTrue(all(button.destroyed for button in all_buttons))
        self.assertNotIn(
            "09:10으로 출근",
            {button.kwargs.get("text") for button in fake_tk.live_buttons()},
        )
        self.assertIsNot(fake_tk.button("새로고침"), all_buttons[0])

    def test_today_line_cardinality_change_is_structural(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, _provider, _callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        original_button = fake_tk.button("새로고침")
        one_line = (WorktimePanelLine("한 줄만", "#2563EB"),)

        with patch.object(
            panel,
            "_reconcile_geometry",
            wraps=panel._reconcile_geometry,
        ) as reconcile:
            holder["model"] = _model(today_lines=one_line)
            panel.refresh_now()

        reconcile.assert_called_once_with()
        self.assertTrue(original_button.destroyed)
        self.assertIsNot(fake_tk.button("새로고침"), original_button)
        self.assertIn("한 줄만", fake_tk.live_label_texts())

    def test_provider_failure_and_invalid_model_preserve_last_good_render(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(actual_text="마지막 정상")}
        panel, provider, _callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        last_good = panel._model
        buttons = tuple(fake_tk.live_buttons())
        labels = tuple(fake_tk.live_label_texts())

        holder["model"] = object()
        self.assertFalse(panel.refresh_now())
        provider.side_effect = RuntimeError("offline")
        self.assertFalse(panel.refresh_now())

        self.assertIs(panel._model, last_good)
        self.assertEqual(tuple(fake_tk.live_buttons()), buttons)
        self.assertEqual(tuple(fake_tk.live_label_texts()), labels)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

        provider.side_effect = lambda: holder["model"]
        holder["model"] = _model(actual_text="복구됨")
        self.assertTrue(panel.refresh_now())
        self.assertIn("복구됨", fake_tk.live_label_texts())

    def test_nonactivating_show_uses_no_tk_activation_or_modal_calls(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )

        panel.show(activate=False)
        window = fake_tk.toplevels[0]

        self.assertTrue(panel.is_visible())
        self.assertEqual(window.deiconify_calls, 0)
        self.assertEqual(window.lift_calls, 0)
        self.assertEqual(window.focus_force_calls, 0)
        self.assertEqual(window.grab_set_calls, 0)
        self.assertEqual(window.wait_window_calls, 0)
        self.assertEqual(window.geometry_calls[-1], "700x500+116+116")

    def test_failed_noactivate_or_unmapped_show_withdraws_and_can_retry(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        self.show_without_activation.side_effect = None
        self.show_without_activation.return_value = False

        self.assertFalse(panel.show(activate=False))
        window = fake_tk.toplevels[0]
        self.assertFalse(panel.is_visible())
        self.assertEqual(root.after_calls, [])

        self.show_without_activation.return_value = True
        self.assertFalse(panel.show(activate=False))
        self.assertFalse(panel.is_visible())
        self.assertEqual(root.after_calls, [])

        self.show_without_activation.side_effect = lambda target: target.native_show()
        self.assertTrue(panel.show(activate=False))
        self.assertTrue(panel.is_visible())
        self.assertEqual(len(fake_tk.toplevels), 1)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])
        self.assertEqual(window.withdraw_calls, 3)

    def test_failed_active_foreground_acquisition_withdraws_and_can_retry(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        self.show_activated.side_effect = None
        self.show_activated.return_value = False

        self.assertFalse(panel.show(activate=True))
        window = fake_tk.toplevels[0]
        self.assertFalse(panel.is_visible())
        self.assertEqual(window.withdraw_calls, 2)
        self.assertEqual(root.after_calls, [])

        self.show_activated.side_effect = lambda target: target.native_show()
        self.assertTrue(panel.show(activate=True))
        self.assertTrue(panel.is_visible())
        self.assertEqual(len(fake_tk.toplevels), 1)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

    def test_failed_initial_geometry_retries_only_on_dedicated_tick_path(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        window = panel._ensure_window()
        window.geometry_failures = 1

        self.assertTrue(panel.show(activate=False))
        self.assertEqual(window.geometry_calls, ["700x500+116+116"])
        self.assertEqual((window.width, window.height), (1, 1))
        rendered_model = panel._model

        self.assertTrue(panel.refresh_now())
        self.assertEqual(window.geometry_calls, ["700x500+116+116"])
        self.assertIs(panel._model, rendered_model)

        with (
            patch.object(panel, "_render_structure") as rebuild,
            patch.object(panel, "_update_rendered_model") as update,
        ):
            root.run_delay(1_000)

        rebuild.assert_not_called()
        update.assert_not_called()
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(
            window.geometry_calls,
            ["700x500+116+116", "700x500+116+116"],
        )
        self.assertEqual(
            (window.x, window.y, window.width, window.height),
            (116, 116, 700, 500),
        )
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

    def test_user_geometry_is_untouched_in_place_and_reconciled_on_structure(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, _provider, _callbacks = _make_panel(root, fake_tk, holder)
        self.assertTrue(panel.show(activate=False))
        window = fake_tk.toplevels[0]
        window.geometry("740x540-1000+50")

        with patch(
            "src.apps.wrike_worktime_panel._work_area_for_window",
            return_value=(-1920, 0, 0, 1080),
        ) as work_area:
            geometry_count = len(window.geometry_calls)
            holder["model"] = _model(actual_text="텍스트만 변경")
            panel.refresh_now()
            self.assertEqual(len(window.geometry_calls), geometry_count)
            work_area.assert_not_called()

            window.requested_height = 620
            holder["model"] = _model(
                actual_text="텍스트만 변경",
                prompt=WorktimeActivityPrompt("08:35"),
            )
            panel.refresh_now()
            self.assertEqual(window.geometry_calls[-1], "740x620-1000+50")
            self.assertEqual(work_area.call_count, 1)

            work_area.return_value = (-800, -100, 0, 500)
            holder["model"] = _model(actual_text="텍스트만 변경", prompt=None)
            panel.refresh_now()
            self.assertEqual(window.geometry_calls[-1], "740x600-800-100")
            self.assertEqual(work_area.call_count, 2)

    def test_timeout_clamps_and_runtime_update_rearms_with_new_delay(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
            idle_timeout_ms=100,
        )
        panel.show(activate=False)
        clamped_id = root.active_id(1_200)
        self.assertEqual(root.active_delays(), [200, 1_000, 1_200])

        panel.set_idle_timeout_ms(2_500)

        self.assertIn(clamped_id, root.after_cancel_calls)
        self.assertEqual(root.active_delays(), [200, 1_000, 2_500])
        panel.set_idle_timeout_ms(-1)
        self.assertEqual(root.active_delays(), [200, 1_000, 1_200])
        with self.assertRaises(TypeError):
            panel.set_idle_timeout_ms(1.5)  # type: ignore[arg-type]

    def test_idle_timeout_withdraws_without_destroy_and_stops_both_timers(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        panel.show(activate=False)
        window = fake_tk.toplevels[0]
        refresh_id = root.active_id(1_000)
        dismiss_id = root.active_id(6_000)
        window.mapped = False

        self.assertEqual(root.run_delay(6_000), dismiss_id)

        self.assertFalse(panel.is_visible())
        self.assertFalse(panel._visible)
        self.assertEqual(window.withdraw_calls, 2)
        self.assertEqual(window.destroy_calls, 0)
        self.assertEqual(root.after_calls, [])
        self.assertIn(refresh_id, root.after_cancel_calls)
        self.assertNotIn(dismiss_id, root.after_cancel_calls)

        self.assertTrue(panel.show(activate=False))
        self.assertIs(fake_tk.toplevels[0], window)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

    def test_hover_keeps_absolute_deadline_then_leave_closes_immediately(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        panel.show(activate=False)
        window = fake_tk.toplevels[0]
        first_dismiss_id = root.active_id(6_000)
        first_deadline = panel._dismiss_deadline
        expected_bindings = {
            "<Escape>",
            "<Enter>",
            "<Leave>",
            "<Motion>",
            "<Button>",
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<KeyPress>",
        }
        self.assertTrue(expected_bindings <= set(window.bindings))

        window.bindings["<Enter>"](object())
        self.assertTrue(panel._pointer_inside)
        self.assertEqual(panel._dismiss_deadline, first_deadline)
        self.assertEqual(root.active_id(6_000), first_dismiss_id)
        self.assertNotIn(first_dismiss_id, root.after_cancel_calls)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

        root.advance(1.2)
        root.run_delay(200)
        self.assertEqual(panel._widgets["countdown"].kwargs["text"], "5초 후 닫힘")
        window.bindings["<Motion>"](object())
        self.assertEqual(panel._dismiss_deadline, first_deadline)
        self.assertEqual(root.active_id(6_000), first_dismiss_id)

        root.advance(4.8)
        root.run_delay(6_000)
        self.assertTrue(panel.is_visible())
        self.assertTrue(panel._dismiss_expired_while_hovered)
        self.assertEqual(
            panel._widgets["countdown"].kwargs["text"],
            "마우스 호버 중 · 이동 시 닫힘",
        )
        dismiss_schedule_count = sum(
            delay == 6_000
            for _after_id, delay, _callback in root.after_history
        )

        window.bindings["<Leave>"](object())
        self.assertFalse(panel.is_visible())
        self.assertFalse(panel._dismiss_expired_while_hovered)
        self.assertNotIn(6_000, root.active_delays())
        self.assertEqual(
            sum(delay == 6_000 for _id, delay, _callback in root.after_history),
            dismiss_schedule_count,
        )

    def test_command_interaction_blocks_dismissal_until_outermost_return(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        window = fake_tk.toplevels[0]
        original_id = root.active_id(6_000)
        stale_callback = next(
            callback
            for after_id, delay, callback in root.after_history
            if after_id == original_id and delay == 6_000
        )
        observed_depths: list[int] = []

        def assert_interaction_is_protected(expected_depth: int) -> None:
            observed_depths.append(panel._interaction_depth)
            self.assertEqual(panel._interaction_depth, expected_depth)
            stale_callback()
            window.bindings["<Leave>"](object())
            window.bindings["<KeyPress>"](SimpleNamespace(keysym="Return"))
            panel.set_idle_timeout_ms(6_000)
            self.assertTrue(panel.is_visible())
            self.assertEqual(root.active_delays(), [1_000])

        def nested_probe() -> None:
            assert_interaction_is_protected(2)

        def command_probe() -> None:
            assert_interaction_is_protected(1)
            panel._run_command(nested_probe)
            self.assertEqual(panel._interaction_depth, 1)
            self.assertTrue(panel.is_visible())
            self.assertEqual(root.active_delays(), [1_000])

        callbacks["refresh"].side_effect = command_probe

        fake_tk.button("새로고침").invoke()

        self.assertEqual(observed_depths, [1, 2])
        self.assertEqual(panel._interaction_depth, 0)
        self.assertTrue(panel.is_visible())
        self.assertIn(original_id, root.after_cancel_calls)
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])
        final_id = root.active_id(6_000)
        self.assertNotEqual(final_id, original_id)
        self.assertEqual(
            [delay for _after_id, delay, _callback in root.after_history].count(6_000),
            2,
        )
        callbacks["refresh"].assert_called_once_with()

    def test_escape_close_toggle_and_destroy_hide_immediately_and_cancel_both(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        panel.show(activate=False)
        window = fake_tk.toplevels[0]
        escape_ids = {root.active_id(1_000), root.active_id(6_000)}

        result = window.bindings["<Escape>"](object())

        self.assertEqual(result, "break")
        self.assertFalse(panel.is_visible())
        self.assertEqual(window.destroy_calls, 0)
        self.assertTrue(escape_ids <= set(root.after_cancel_calls))

        panel.show(activate=False)
        close_ids = {root.active_id(1_000), root.active_id(6_000)}
        window.protocols["WM_DELETE_WINDOW"]()
        self.assertFalse(panel.is_visible())
        self.assertTrue(close_ids <= set(root.after_cancel_calls))

        panel.show(activate=False)
        toggle_ids = {root.active_id(1_000), root.active_id(6_000)}
        panel.toggle(activate=False)
        self.assertFalse(panel.is_visible())
        self.assertTrue(toggle_ids <= set(root.after_cancel_calls))

        panel.show(activate=False)
        destroy_ids = {root.active_id(1_000), root.active_id(6_000)}
        panel.destroy()
        panel.destroy()
        panel.show(activate=False)
        self.assertFalse(panel.is_visible())
        self.assertTrue(destroy_ids <= set(root.after_cancel_calls))
        self.assertEqual(root.after_calls, [])
        self.assertEqual(window.destroy_calls, 1)
        self.assertEqual(len(fake_tk.toplevels), 1)


    def test_chromeless_topmost_hover_border_and_cursor_reanchor(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )

        self.assertTrue(panel.show(activate=True))
        window = fake_tk.toplevels[0]
        self.assertTrue(window.overrideredirect())
        self.assertIn(("-topmost", True), window.attribute_calls)
        self.assertIn((False, False), window.resizable_calls)
        self.show_activated.assert_called_once_with(window)
        self.assertEqual(panel._shell.kwargs["highlightbackground"], "#E5E7EB")

        dismiss_id = root.active_id(6_000)
        deadline = panel._dismiss_deadline
        window.bindings["<Enter>"](object())
        self.assertEqual(panel._shell.kwargs["highlightbackground"], "#2563EB")
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])
        self.assertEqual(root.active_id(6_000), dismiss_id)
        self.assertEqual(panel._dismiss_deadline, deadline)
        window.bindings["<Leave>"](object())
        self.assertEqual(panel._shell.kwargs["highlightbackground"], "#E5E7EB")
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])
        self.assertEqual(root.active_id(6_000), dismiss_id)
        self.assertEqual(panel._dismiss_deadline, deadline)

        panel.hide()
        fake_tk.pointer_x = 1500
        fake_tk.pointer_y = 800
        with patch(
            "src.apps.wrike_worktime_panel._work_area_for_point",
            return_value=(0, 0, 1920, 1080),
        ):
            self.assertTrue(panel.show(activate=False))
        self.assertEqual(window.geometry_calls[-1], "700x500+784+284")

    def test_countdown_and_common_inline_target_validation_are_view_local(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(target_minutes=480)}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)

        countdown = panel._widgets["countdown"]
        self.assertEqual(countdown.kwargs["text"], "6초 후 닫힘")
        root.advance(1.2)
        root.run_delay(200)
        self.assertEqual(countdown.kwargs["text"], "5초 후 닫힘")

        fake_tk.button("계획 수정").invoke()
        self.assertEqual(countdown.kwargs["text"], "편집 중 · 자동 닫힘 일시정지")
        self.assertEqual(root.active_delays(), [1_000])
        entry = panel._widgets["inline_entry"]
        entry.delete(0, "end")
        entry.insert(0, "07:00")
        fake_tk.button("취소").invoke()
        callbacks["edit_plan"].assert_not_called()
        self.assertFalse(panel._inline_editor_active)
        fake_tk.button("계획 수정").invoke()
        self.assertEqual(entry.get(), "08:00")
        entry.delete(0, "end")
        entry.insert(0, "24:30")
        fake_tk.button("저장").invoke()
        callbacks["edit_plan"].assert_not_called()
        self.assertIn("24:00", panel._widgets["inline_error"].kwargs["text"])
        self.assertTrue(panel._inline_editor_active)

        entry.delete(0, "end")
        entry.insert(0, "8:00")
        fake_tk.button("저장").invoke()
        callbacks["edit_plan"].assert_not_called()
        self.assertIn("HH:MM", panel._widgets["inline_error"].kwargs["text"])
        self.assertTrue(panel._inline_editor_active)

        entry.delete(0, "end")
        entry.insert(0, "00:00")
        holder["model"] = _model(target_minutes=0)
        fake_tk.button("저장").invoke()
        callbacks["edit_plan"].assert_called_once_with(0)
        self.assertFalse(panel._inline_editor_active)
        self.assertEqual(countdown.kwargs["text"], "6초 후 닫힘")
        self.assertEqual(root.active_delays(), [200, 1_000, 6_000])

    def test_sync_header_has_one_owner_and_uses_current_error_text(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {
            "model": _model(
                sync_text="2026-08-27 14:00:00 · error · request_failed"
            )
        }
        panel, _provider, _callbacks = _make_panel(root, fake_tk, holder)

        panel.show(activate=False)

        labels = fake_tk.live_label_texts()
        sync_labels = [
            text for text in labels if str(text).startswith("동기화 · ")
        ]
        self.assertEqual(
            sync_labels,
            ["동기화 · 2026-08-27 14:00:00 · error · request_failed"],
        )
        self.assertTrue(
            all("동기화" not in line.text for line in holder["model"].today_lines)
        )

    def test_hotkey_toggle_surfaces_obscured_panel_before_hiding_foreground(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        panel.show(activate=True)
        window = fake_tk.toplevels[0]

        self.window_is_foreground.return_value = False
        panel.toggle(activate=True)
        self.assertTrue(panel.is_visible())
        self.assertEqual(self.show_activated.call_count, 2)
        self.assertEqual(window.withdraw_calls, 1)

        self.window_is_foreground.return_value = True
        panel.toggle(activate=True)
        self.assertFalse(panel.is_visible())
        self.assertEqual(window.withdraw_calls, 2)


if __name__ == "__main__":
    unittest.main()

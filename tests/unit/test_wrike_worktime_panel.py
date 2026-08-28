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

    def resizable(self, *_args):
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
        return 800

    def winfo_screenheight(self):
        return 600

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

    def all_widgets(self) -> list[_FakeWidget]:
        return [*self.toplevels, *self.frames, *self.labels, *self.buttons]

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
    break_active: bool = False,
    prompt: WorktimeActivityPrompt | None = None,
    today_lines: tuple[WorktimePanelLine, ...] | None = None,
    week_range: str = "2026-04-06 - 2026-04-12",
    sync_text: str = "방금 동기화",
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
    return WorktimePanelModel(
        week_range=week_range,
        sync_text=sync_text,
        sync_state="fresh",
        today_lines=today_lines,
        has_clock_in=has_clock_in,
        break_active=break_active,
        rows=rows,
        prompt=prompt,
    )


def _make_panel(root, fake_tk, holder, *, idle_timeout_ms: int = 6_000):
    callbacks = {
        "refresh": Mock(),
        "clock_in_now": Mock(),
        "edit_clock_in": Mock(),
        "edit_plan": Mock(),
        "toggle_break": Mock(),
        "open_settings": Mock(),
        "prompt_accept": Mock(),
        "prompt_edit": Mock(),
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
                has_clock_in=False,
                break_active=False,
                rows=model.rows,
            )
        with self.assertRaises(ValueError):
            WorktimePanelModel(
                week_range=model.week_range,
                sync_text=model.sync_text,
                sync_state=model.sync_state,
                today_lines=model.today_lines,
                has_clock_in=False,
                break_active=False,
                rows=model.rows[:6],
            )
        with self.assertRaises(ValueError):
            WorktimeActivityPrompt("24:00")
        self.assertEqual(
            WorktimeActivityPrompt("08:05").detected_hhmm,
            "08:05",
        )


class WorktimeQuickPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch(
            "src.apps.wrike_worktime_panel._show_window_without_activation",
            side_effect=lambda window: window.native_show(),
        )
        self.show_without_activation = patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertEqual(window.geometry_calls, ["700x500+100+100"])
        self.assertEqual(len(window.geometry_calls), geometry_count)
        self.assertEqual(fake_tk.live_buttons(), first_buttons)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(root.active_delays(), [1_000, 6_000])
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

    def test_action_transitions_reuse_buttons_and_stable_commands(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)
        original_buttons = tuple(fake_tk.live_buttons())
        clock_button = fake_tk.button("지금 출근")
        break_button = fake_tk.button("휴게 시작")
        clock_command = clock_button.kwargs["command"]
        break_command = break_button.kwargs["command"]

        holder["model"] = _model(has_clock_in=True, break_active=True)
        panel.refresh_now()

        self.assertEqual(tuple(fake_tk.live_buttons()), original_buttons)
        self.assertIs(fake_tk.button("출근 수정"), clock_button)
        self.assertIs(fake_tk.button("휴게 종료"), break_button)
        self.assertIs(clock_button.kwargs["command"], clock_command)
        self.assertIs(break_button.kwargs["command"], break_command)
        self.assertTrue(
            all(
                "command" not in call
                for button in original_buttons
                for call in button.configure_calls
            )
        )
        fake_tk.button("출근 수정").invoke()
        callbacks["edit_clock_in"].assert_called_once_with()
        callbacks["clock_in_now"].assert_not_called()
        fake_tk.button("휴게 종료").invoke()
        callbacks["toggle_break"].assert_called_once_with()
        fake_tk.button("계획 수정").invoke()
        callbacks["edit_plan"].assert_called_once_with()
        fake_tk.button("설정").invoke()
        callbacks["open_settings"].assert_called_once_with()

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
        self.assertEqual(root.active_delays(), [1_000, 6_000])

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
        self.assertEqual(window.geometry_calls[-1], "700x500+100+100")

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
        self.assertEqual(root.active_delays(), [1_000, 6_000])
        self.assertEqual(window.withdraw_calls, 3)

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
        self.assertEqual(window.geometry_calls, ["700x500+100+100"])
        self.assertEqual((window.width, window.height), (1, 1))
        rendered_model = panel._model

        self.assertTrue(panel.refresh_now())
        self.assertEqual(window.geometry_calls, ["700x500+100+100"])
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
            ["700x500+100+100", "700x500+100+100"],
        )
        self.assertEqual(
            (window.x, window.y, window.width, window.height),
            (100, 100, 700, 500),
        )
        self.assertEqual(root.active_delays(), [1_000, 6_000])

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
        self.assertEqual(root.active_delays(), [1_000, 1_200])

        panel.set_idle_timeout_ms(2_500)

        self.assertIn(clamped_id, root.after_cancel_calls)
        self.assertEqual(root.active_delays(), [1_000, 2_500])
        panel.set_idle_timeout_ms(-1)
        self.assertEqual(root.active_delays(), [1_000, 1_200])
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
        self.assertEqual(root.active_delays(), [1_000, 6_000])

    def test_pointer_and_key_activity_defer_until_full_delay_after_leave(self) -> None:
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
        self.assertTrue(
            all(window.bind_adds[sequence] == "+" for sequence in expected_bindings)
        )

        window.bindings["<Enter>"](object())
        self.assertIn(first_dismiss_id, root.after_cancel_calls)
        self.assertEqual(root.active_delays(), [1_000])
        for sequence in ("<Motion>", "<Button>", "<MouseWheel>"):
            window.bindings[sequence](object())
            self.assertEqual(root.active_delays(), [1_000])
        root.run_delay(1_000)
        self.assertTrue(panel.is_visible())
        self.assertEqual(root.active_delays(), [1_000])

        window.bindings["<KeyPress>"](SimpleNamespace(keysym="a"))
        self.assertEqual(root.active_delays(), [1_000])
        window.bindings["<Leave>"](object())
        leave_id = root.active_id(6_000)
        window.bindings["<KeyPress>"](SimpleNamespace(keysym="Return"))
        key_id = root.active_id(6_000)
        self.assertNotEqual(key_id, leave_id)
        self.assertIn(leave_id, root.after_cancel_calls)

        window.bindings["<KeyPress>"](SimpleNamespace(keysym="Escape"))
        self.assertEqual(root.active_id(6_000), key_id)

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
        self.assertEqual(root.active_delays(), [1_000, 6_000])
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


if __name__ == "__main__":
    unittest.main()

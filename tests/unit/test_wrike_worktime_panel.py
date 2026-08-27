from __future__ import annotations

from dataclasses import FrozenInstanceError
import re
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
        self.after_cancel_calls: list[str] = []
        self._next_after_id = 0

    def after(self, delay_ms, callback):
        self._next_after_id += 1
        after_id = f"after-{self._next_after_id}"
        self.after_calls.append((after_id, int(delay_ms), callback))
        return after_id

    def after_cancel(self, after_id) -> None:
        self.after_cancel_calls.append(after_id)
        self.after_calls = [
            call for call in self.after_calls if call[0] != after_id
        ]

    def run_next(self) -> None:
        _after_id, _delay, callback = self.after_calls.pop(0)
        callback()

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
        self.configure_calls: list[dict] = []
        self.bindings: dict[str, object] = {}
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
        self.destroy_calls = 0
        self.destroyed = False
        if isinstance(parent, _FakeWidget):
            parent.children.append(self)

    def pack(self, **kwargs):
        self.pack_kwargs = dict(kwargs)
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

    def bind(self, sequence, callback):
        self.bindings[str(sequence)] = callback
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
        return self.requested_width

    def winfo_reqheight(self):
        return self.requested_height

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def winfo_x(self):
        return self.x

    def winfo_y(self):
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


def _model(
    *,
    actual_text: str = "Wrike 기록 1:30 · 현재 기대 2:00",
    has_clock_in: bool = False,
    break_active: bool = False,
    prompt: WorktimeActivityPrompt | None = None,
) -> WorktimePanelModel:
    rows = tuple(
        WorktimePanelDayRow(
            weekday=weekday,
            date=f"04/{6 + index:02d}",
            summary=f"{index + 1}:00 / 8:00",
            today=index == 0,
            color="#1D4ED8" if index == 0 else "#6B7280",
        )
        for index, weekday in enumerate(_WEEKDAYS)
    )
    return WorktimePanelModel(
        week_range="2026-04-06 - 2026-04-12",
        sync_text="방금 동기화",
        sync_state="fresh",
        today_lines=(
            WorktimePanelLine(actual_text, "#2563EB"),
            WorktimePanelLine("출근 09:00 · 예상 퇴근 18:00", "#059669"),
        ),
        has_clock_in=has_clock_in,
        break_active=break_active,
        rows=rows,
        prompt=prompt,
    )


def _make_panel(root, fake_tk, holder):
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

    def test_show_reuses_one_toplevel_and_renders_all_seven_rows(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, provider, _callbacks = _make_panel(root, fake_tk, holder)

        self.assertTrue(panel.show(activate=False))
        first_window = fake_tk.toplevels[0]
        first_buttons = fake_tk.live_buttons()
        self.assertTrue(panel.show(activate=False))

        self.assertEqual(len(fake_tk.toplevels), 1)
        self.assertIs(fake_tk.toplevels[0], first_window)
        self.assertEqual(first_window.geometry_calls, ["700x500+100+100"])
        self.assertEqual(fake_tk.live_buttons(), first_buttons)
        self.assertTrue(all(not button.destroyed for button in first_buttons))
        self.assertEqual(provider.call_count, 2)
        texts = fake_tk.live_label_texts()
        self.assertTrue(set(_WEEKDAYS) <= set(texts))
        self.assertEqual(sum(text in _WEEKDAYS for text in texts), 7)
        self.assertIn("오늘", texts)
        self.assertEqual(len(root.after_calls), 1)
        self.assertEqual(root.after_calls[0][1], 1_000)

    def test_one_second_tick_reloads_actual_vs_expected_text(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(actual_text="실제 1:00 vs 기대 2:00")}
        panel, provider, _callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)

        holder["model"] = _model(actual_text="실제 1:01 vs 기대 2:01")
        root.run_next()

        self.assertEqual(provider.call_count, 2)
        texts = fake_tk.live_label_texts()
        self.assertIn("실제 1:01 vs 기대 2:01", texts)
        self.assertNotIn("실제 1:00 vs 기대 2:00", texts)
        self.assertEqual(len(root.after_calls), 1)
        self.assertEqual(root.after_calls[0][1], 1_000)

    def test_main_button_text_and_callbacks_follow_model_state(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model()}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)

        initial_texts = {
            button.kwargs.get("text") for button in fake_tk.live_buttons()
        }
        self.assertTrue(
            {"새로고침", "지금 출근", "휴게 시작", "계획 수정", "설정"}
            <= initial_texts
        )
        fake_tk.button("새로고침").invoke()
        callbacks["refresh"].assert_called_once_with()
        fake_tk.button("지금 출근").invoke()
        callbacks["clock_in_now"].assert_called_once_with()

        holder["model"] = _model(has_clock_in=True, break_active=True)
        panel.refresh_now()
        updated_texts = {
            button.kwargs.get("text") for button in fake_tk.live_buttons()
        }
        self.assertIn("출근 수정", updated_texts)
        self.assertIn("휴게 종료", updated_texts)
        self.assertNotIn("지금 출근", updated_texts)
        self.assertNotIn("휴게 시작", updated_texts)

        fake_tk.button("출근 수정").invoke()
        callbacks["edit_clock_in"].assert_called_once_with()
        callbacks["clock_in_now"].assert_called_once_with()
        fake_tk.button("휴게 종료").invoke()
        callbacks["toggle_break"].assert_called_once_with()
        fake_tk.button("계획 수정").invoke()
        callbacks["edit_plan"].assert_called_once_with()
        fake_tk.button("설정").invoke()
        callbacks["open_settings"].assert_called_once_with()

    def test_prompt_buttons_appear_invoke_callbacks_and_then_hide(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        holder = {"model": _model(prompt=WorktimeActivityPrompt("08:35"))}
        panel, _provider, callbacks = _make_panel(root, fake_tk, holder)
        panel.show(activate=False)

        expected = {"08:35으로 출근", "시간 수정", "30분 후", "오늘 건너뛰기"}
        self.assertTrue(
            expected
            <= {button.kwargs.get("text") for button in fake_tk.live_buttons()}
        )
        fake_tk.button("08:35으로 출근").invoke()
        callbacks["prompt_accept"].assert_called_once_with("08:35")
        fake_tk.button("시간 수정").invoke()
        callbacks["prompt_edit"].assert_called_once_with("08:35")
        fake_tk.button("30분 후").invoke()
        callbacks["prompt_snooze"].assert_called_once_with()
        fake_tk.button("오늘 건너뛰기").invoke()
        callbacks["prompt_skip"].assert_called_once_with()

        holder["model"] = _model(prompt=None)
        panel.refresh_now()
        live_texts = {
            button.kwargs.get("text") for button in fake_tk.live_buttons()
        }
        self.assertTrue(expected.isdisjoint(live_texts))

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
        self.assertEqual(len(root.after_calls), 1)
        self.assertEqual(window.withdraw_calls, 3)

    def test_failed_initial_geometry_is_retried_on_refresh(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        window = panel._ensure_window()
        window.geometry_failures = 1

        self.assertTrue(panel.show(activate=False))
        self.assertEqual(window.geometry_calls, ["700x500+100+100"])
        self.assertEqual((window.width, window.height), (1, 1))

        root.run_next()

        self.assertEqual(
            window.geometry_calls,
            ["700x500+100+100", "700x500+100+100"],
        )
        self.assertEqual((window.x, window.y, window.width, window.height), (100, 100, 700, 500))

    def test_geometry_preserves_user_position_then_grows_and_reclamps(self) -> None:
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
            panel.refresh_now()
            self.assertEqual(len(window.geometry_calls), geometry_count)

            window.requested_height = 620
            holder["model"] = _model(prompt=WorktimeActivityPrompt("08:35"))
            panel.refresh_now()
            self.assertEqual(window.geometry_calls[-1], "740x620-1000+50")

            work_area.return_value = (-800, -100, 0, 500)
            panel.refresh_now()
            self.assertEqual(window.geometry_calls[-1], "740x600-800-100")

    def test_escape_and_window_close_hide_without_destroying(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        panel.show(activate=False)
        window = fake_tk.toplevels[0]
        first_after_id = root.after_calls[0][0]

        result = window.bindings["<Escape>"](object())

        self.assertEqual(result, "break")
        self.assertFalse(panel.is_visible())
        self.assertEqual(window.withdraw_calls, 2)
        self.assertEqual(window.destroy_calls, 0)
        self.assertEqual(root.after_cancel_calls, [first_after_id])

        panel.show(activate=False)
        window.protocols["WM_DELETE_WINDOW"]()
        self.assertFalse(panel.is_visible())
        self.assertEqual(window.destroy_calls, 0)
        self.assertEqual(len(fake_tk.toplevels), 1)

    def test_toggle_reuses_window_and_destroy_is_idempotent_with_cancel(self) -> None:
        root = _FakeRoot()
        fake_tk = _FakeTk()
        panel, _provider, _callbacks = _make_panel(
            root,
            fake_tk,
            {"model": _model()},
        )
        panel.toggle(activate=False)
        window = fake_tk.toplevels[0]
        first_after_id = root.after_calls[0][0]

        panel.destroy()
        panel.destroy()
        panel.show(activate=False)

        self.assertFalse(panel.is_visible())
        self.assertEqual(root.after_cancel_calls, [first_after_id])
        self.assertEqual(root.after_calls, [])
        self.assertEqual(window.destroy_calls, 1)
        self.assertEqual(len(fake_tk.toplevels), 1)


if __name__ == "__main__":
    unittest.main()

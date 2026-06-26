import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.apps.KakaoManager import (
    KakaoManager,
    KakaoRuntimeSnapshot,
    KakaoTargetResolution,
    KakaoWorkResult,
    MonitorSnapshot,
    WindowMove,
    WindowMovePlan,
)


def _monitor_snapshot(
    handle: int,
    device: str,
    display_num: int | None,
    *,
    primary: bool = False,
    work: tuple[int, int, int, int] | None = None,
    monitor: tuple[int, int, int, int] | None = None,
) -> MonitorSnapshot:
    rect = work if work is not None else (0, 0, 1920, 1080)
    monitor_rect = monitor if monitor is not None else rect
    return MonitorSnapshot(
        handle=handle,
        device=device,
        display_num=display_num,
        is_primary=primary,
        work=rect,
        monitor=monitor_rect,
    )


class _FakeThread:
    created = []

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        _FakeThread.created.append(self)

    def start(self):
        return None


class _FakeAfterRoot:
    def __init__(self, *, after_result="after#1", after_side_effect=None):
        self.after_calls = []
        self.after_result = after_result
        self.after_side_effect = after_side_effect

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        if self.after_side_effect is not None:
            raise self.after_side_effect
        return self.after_result


class _RaisingWindow:
    def lift(self):
        raise RuntimeError("lift failed")

    def tkraise(self):
        raise RuntimeError("raise failed")


class KakaoManagerTickUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = KakaoManager()
        self.manager._KakaoManager__config_loaded = True
        self.manager._KakaoManager__config_missing = False
        self.manager._KakaoManager__target_display_num = 1
        self.manager._KakaoManager__next_poll_time = 0.0

    def test_tick_dispatches_background_work_without_inline_heavy_calls(self) -> None:
        request_background = Mock()
        self.manager._KakaoManager__request_background_tick = request_background

        with patch.object(self.manager, "_KakaoManager__refresh_monitors", side_effect=AssertionError("inline monitor refresh")):
            with patch.object(self.manager, "_KakaoManager__refresh_kakao_pids", side_effect=AssertionError("inline pid scan")):
                with patch.object(self.manager, "_KakaoManager__get_kakao_top_windows", side_effect=AssertionError("inline window enumeration")):
                    with patch.object(self.manager, "_KakaoManager__move_window", side_effect=AssertionError("inline window move")):
                        self.manager.tick(root=object())

        request_background.assert_called_once()

    def test_tick_dispatches_again_after_normal_monitor_scheduler_interval(self) -> None:
        request_background = Mock()
        self.manager._KakaoManager__request_background_tick = request_background

        with patch.object(
            self.manager._KakaoManager__lib.time,
            "monotonic",
            side_effect=(10.0, 10.14, 10.20),
        ):
            self.manager.tick(root=object())
            self.assertEqual(request_background.call_count, 1)

            self.manager.tick(root=object())
            self.assertEqual(request_background.call_count, 1)

            self.manager.tick(root=object())
            self.assertEqual(request_background.call_count, 2)

    def test_update_settings_disables_background_tick(self) -> None:
        request_background = Mock()
        self.manager._KakaoManager__request_background_tick = request_background

        with patch.object(self.manager, "_KakaoManager__save_config") as save_config:
            ok, err = self.manager.update_settings({"enabled": False})
            self.manager.tick(root=object())

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertFalse(self.manager.get_settings_snapshot()["enabled"])
        request_background.assert_not_called()
        save_config.assert_called_once()

    def test_open_monitor_selector_requests_refresh_when_no_snapshot_exists(self) -> None:
        request_refresh = Mock()
        self.manager.request_refresh = request_refresh
        self.manager._KakaoManager__monitors = []

        rendered = self.manager.open_monitor_selector(root=object())

        self.assertFalse(rendered)
        request_refresh.assert_called_once()

    def test_open_monitor_selector_returns_false_for_empty_embedded_snapshot(self) -> None:
        request_refresh = Mock()
        self.manager.request_refresh = request_refresh
        self.manager._KakaoManager__monitors = []

        rendered = self.manager.open_monitor_selector(root=object(), embedded_parent=object())

        self.assertFalse(rendered)
        request_refresh.assert_called_once()

    def test_open_monitor_selector_existing_window_returns_true_despite_lift_failures(self) -> None:
        self.manager._KakaoManager__is_selecting = True
        self.manager._KakaoManager__select_window = _RaisingWindow()
        self.manager._KakaoManager__overlay_windows = [object()]

        rendered = self.manager.open_monitor_selector(root=object())

        self.assertTrue(rendered)

    def test_open_monitor_selector_resets_stale_selecting_state_before_empty_retry(self) -> None:
        request_refresh = Mock()
        self.manager.request_refresh = request_refresh
        self.manager._KakaoManager__is_selecting = True
        self.manager._KakaoManager__select_window = None
        self.manager._KakaoManager__monitors = []

        rendered = self.manager.open_monitor_selector(root=object())

        self.assertFalse(rendered)
        self.assertFalse(self.manager._KakaoManager__is_selecting)
        request_refresh.assert_called_once()

    def test_request_refresh_is_single_flight_and_marks_pending_rerun(self) -> None:
        _FakeThread.created = []
        with patch("src.apps.KakaoManager.threading.Thread", _FakeThread):
            self.manager.request_refresh(root=None)
            self.manager.request_refresh(root=None)

        self.assertEqual(len(_FakeThread.created), 1)
        self.assertTrue(self.manager._KakaoManager__worker_active)
        self.assertTrue(self.manager._KakaoManager__pending_rerun)
        self.assertEqual(self.manager._KakaoManager__latest_request_generation, 1)
        diagnostics = self.manager.get_refresh_diagnostics_snapshot()
        self.assertEqual(diagnostics["coalesced_requests"], 1)

    def test_pending_refresh_does_not_discard_completed_active_result(self) -> None:
        _FakeThread.created = []
        with patch("src.apps.KakaoManager.threading.Thread", _FakeThread):
            self.manager.request_refresh(root=None)
            self.manager.request_refresh(root=None)

        self.assertEqual(self.manager._KakaoManager__latest_request_generation, 1)
        self.assertTrue(self.manager._KakaoManager__pending_rerun)
        result = KakaoWorkResult(
            request_generation=1,
            state_epoch=self.manager._KakaoManager__state_epoch,
            requested_at=10.0,
            compute_duration_ms=200.0,
            runtime_snapshot=KakaoRuntimeSnapshot(
                kakao_pids=(101,),
                chat_order=(301,),
                last_main_hwnd=300,
                monitors=(
                    MonitorSnapshot(
                        handle=11,
                        device="DISPLAY1",
                        display_num=1,
                        is_primary=True,
                        work=(0, 0, 1920, 1080),
                        monitor=(0, 0, 1920, 1080),
                    ),
                ),
                next_pid_scan_time=12.5,
                next_monitor_scan_time=34.5,
            ),
            target_resolution=KakaoTargetResolution(
                requested_display_num=1,
                resolved_display_num=1,
                resolved_monitor_handle=11,
                config_missing=False,
                fallback_reason="",
            ),
            move_plan=WindowMovePlan(
                moves=(
                    WindowMove(hwnd=301, x=10, y=20, width=400, height=500, resize=True),
                ),
            ),
        )

        with patch("src.apps.KakaoManager.apply_precomputed_window_position") as apply_move:
            with patch.object(self.manager, "_KakaoManager__request_background_tick") as request_tick:
                self.manager._KakaoManager__handle_work_result(root="root", result=result)

        apply_move.assert_called_once()
        request_tick.assert_called_once()
        self.assertEqual(self.manager._KakaoManager__chat_order, [301])
        self.assertFalse(self.manager._KakaoManager__pending_rerun)
        diagnostics = self.manager.get_refresh_diagnostics_snapshot()
        self.assertTrue(diagnostics["last_accepted"])
        self.assertTrue(diagnostics["last_rerun_requested"])
        self.assertEqual(diagnostics["last_move_count"], 1)

    def test_request_refresh_captures_target_monitor_descriptor(self) -> None:
        _FakeThread.created = []
        self.manager._KakaoManager__target_monitor = {
            "display_num": 2,
            "device": r"\\.\DISPLAY2",
            "work": (1920, 0, 3840, 1040),
            "monitor": (1920, 0, 3840, 1080),
            "selected_at_topology_signature": "sig",
        }

        with patch("src.apps.KakaoManager.threading.Thread", _FakeThread):
            self.manager.request_refresh(root=None)

        captured_requests = []
        self.manager._KakaoManager__compute_work_result = (
            lambda request: captured_requests.append(request) or (_ for _ in ()).throw(RuntimeError())
        )
        _FakeThread.created[0].target()
        self.assertEqual(captured_requests[0].requested_display_num, 2)
        self.assertEqual(captured_requests[0].requested_target_monitor["device"], r"\\.\DISPLAY2")

    def test_post_ui_reports_contract_success_and_failure(self) -> None:
        calls = []
        self.manager.set_ui_post(lambda fn: calls.append(fn))

        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None), True)
        self.assertEqual(len(calls), 1)

        self.manager.set_ui_post(None)
        root = _FakeAfterRoot(after_result="after#1")

        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None, root=root), False)
        self.assertEqual(root.after_calls, [])
        self.assertIs(self.manager._KakaoManager__post_ui(None, root=root), False)

    def test_post_ui_does_not_fall_back_to_tk_after_ui_post_exception(self) -> None:
        self.manager.set_ui_post(Mock(side_effect=RuntimeError("post failed")))

        root = _FakeAfterRoot(after_result="after#1")
        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None, root=root), False)
        self.assertEqual(root.after_calls, [])

    def test_worker_compute_exception_cleans_active_latch(self) -> None:
        _FakeThread.created = []
        with patch("src.apps.KakaoManager.threading.Thread", _FakeThread):
            self.manager.request_refresh(root=None)

        with patch.object(
            self.manager,
            "_KakaoManager__compute_work_result",
            side_effect=RuntimeError("compute failed"),
        ):
            try:
                _FakeThread.created[0].target()
            except RuntimeError:
                pass

        self.assertFalse(self.manager._KakaoManager__worker_active)
        self.assertFalse(self.manager._KakaoManager__pending_rerun)

    def test_worker_post_failure_cleans_latch_and_allows_next_refresh(self) -> None:
        _FakeThread.created = []
        with patch("src.apps.KakaoManager.threading.Thread", _FakeThread):
            with patch.object(self.manager, "_KakaoManager__compute_work_result", return_value=object()):
                self.manager.request_refresh(root=None)
                _FakeThread.created[0].target()
                self.manager.request_refresh(root=None)

        self.assertFalse(self.manager._KakaoManager__pending_rerun)
        self.assertEqual(len(_FakeThread.created), 2)

    def test_failed_worker_cleanup_consumes_pending_rerun_once(self) -> None:
        self.manager._KakaoManager__worker_active = True
        self.manager._KakaoManager__pending_rerun = True

        with patch.object(self.manager, "_KakaoManager__request_background_tick") as request_tick:
            self.manager._KakaoManager__finish_failed_worker(root="root")
            self.manager._KakaoManager__finish_failed_worker(root="root")

        request_tick.assert_called_once()
        self.assertEqual(request_tick.call_args.args[0], "root")
        self.assertFalse(self.manager._KakaoManager__worker_active)
        self.assertFalse(self.manager._KakaoManager__pending_rerun)

    def test_request_refresh_bootstraps_persisted_target_before_dispatch(self) -> None:
        manager = KakaoManager()
        manager._KakaoManager__config_loaded = False
        manager._KakaoManager__target_display_num = None
        manager._KakaoManager__monitors = [
            {
                "handle": 11,
                "device": "DISPLAY1",
                "display_num": 1,
                "is_primary": True,
                "work": (0, 0, 1920, 1080),
                "monitor": (0, 0, 1920, 1080),
            }
        ]

        def fake_load() -> None:
            manager._KakaoManager__target_display_num = 7
            manager._KakaoManager__config_missing = False
            return None

        dispatched_targets: list[int | None] = []

        with patch.object(manager, "_KakaoManager__load_config", side_effect=fake_load):
            with patch.object(manager, "_KakaoManager__request_background_tick") as request_tick:
                request_tick.side_effect = (
                    lambda _root, _now: dispatched_targets.append(
                        manager._KakaoManager__target_display_num
                    )
                )
                manager.request_refresh(root=None)

        self.assertEqual(dispatched_targets, [7])
        self.assertTrue(manager._KakaoManager__config_loaded)

    def test_accept_work_result_commits_snapshot_and_uses_pure_apply_helper(self) -> None:
        self.manager._KakaoManager__latest_request_generation = 2
        self.manager._KakaoManager__state_epoch = 3
        self.manager._KakaoManager__chat_order = [999]
        self.manager._KakaoManager__last_main_hwnd = 999
        self.manager._KakaoManager__kakao_pids = {999}
        self.manager._KakaoManager__target_display_num = 7
        self.manager._KakaoManager__config_missing = False

        runtime_snapshot = KakaoRuntimeSnapshot(
            kakao_pids=(101, 202),
            chat_order=(301, 302),
            last_main_hwnd=300,
            monitors=(
                MonitorSnapshot(
                    handle=11,
                    device="DISPLAY1",
                    display_num=1,
                    is_primary=True,
                    work=(0, 0, 1920, 1080),
                    monitor=(0, 0, 1920, 1080),
                ),
            ),
            next_pid_scan_time=12.5,
            next_monitor_scan_time=34.5,
        )
        result = KakaoWorkResult(
            request_generation=2,
            state_epoch=3,
            runtime_snapshot=runtime_snapshot,
            target_resolution=KakaoTargetResolution(
                requested_display_num=7,
                resolved_display_num=1,
                resolved_monitor_handle=11,
                config_missing=False,
                fallback_reason="target_unavailable",
            ),
            move_plan=WindowMovePlan(
                moves=(
                    WindowMove(hwnd=300, x=10, y=20, width=400, height=500, resize=False),
                    WindowMove(hwnd=301, x=30, y=40, width=410, height=510, resize=True),
                )
            ),
        )

        with patch("src.apps.KakaoManager.apply_precomputed_window_position") as apply_move:
            with patch.object(self.manager, "_KakaoManager__move_window", side_effect=AssertionError("legacy move helper should not run")):
                accepted = self.manager._KakaoManager__accept_work_result(result)

        self.assertTrue(accepted)
        self.assertEqual(self.manager._KakaoManager__chat_order, [301, 302])
        self.assertEqual(self.manager._KakaoManager__last_main_hwnd, 300)
        self.assertEqual(self.manager._KakaoManager__kakao_pids, {101, 202})
        self.assertEqual(self.manager._KakaoManager__resolved_target_display_num, 1)
        self.assertEqual(self.manager._KakaoManager__resolved_target_monitor_handle, 11)
        self.assertFalse(self.manager._KakaoManager__config_missing)
        self.assertEqual(apply_move.call_count, 2)

    def test_resolve_target_monitor_prefers_saved_device_over_display_number(self) -> None:
        monitors = [
            _monitor_snapshot(
                11,
                r"\\.\DISPLAY2",
                2,
                primary=True,
                work=(0, 0, 1920, 1040),
                monitor=(0, 0, 1920, 1080),
            ),
            _monitor_snapshot(
                22,
                r"\\.\DISPLAY1",
                1,
                work=(1920, 0, 3840, 1040),
                monitor=(1920, 0, 3840, 1080),
            ),
        ]

        resolution, monitor = self.manager._KakaoManager__resolve_target_monitor(
            monitors,
            requested_display_num=2,
            requested_target_monitor={"display_num": 2, "device": r"\\.\DISPLAY1"},
            config_missing=False,
        )

        self.assertIsNotNone(monitor)
        self.assertEqual(monitor.handle, 22)
        self.assertEqual(resolution.resolved_monitor_handle, 22)
        self.assertEqual(resolution.fallback_reason, "device")

    def test_resolve_target_monitor_does_not_mark_missing_when_target_unavailable(self) -> None:
        monitors = [
            _monitor_snapshot(
                11,
                r"\\.\DISPLAY1",
                1,
                primary=True,
                work=(0, 0, 1920, 1040),
                monitor=(0, 0, 1920, 1080),
            ),
        ]

        resolution, monitor = self.manager._KakaoManager__resolve_target_monitor(
            monitors,
            requested_display_num=2,
            requested_target_monitor={"display_num": 2, "device": r"\\.\DISPLAY2"},
            config_missing=False,
        )

        self.assertIsNotNone(monitor)
        self.assertEqual(monitor.handle, 11)
        self.assertFalse(resolution.config_missing)
        self.assertEqual(resolution.fallback_reason, "target_unavailable")

    def test_resolve_target_monitor_prefers_descriptor_rect_before_stale_display_number(self) -> None:
        target_work = (1920, 0, 3840, 1040)
        target_monitor = (1920, 0, 3840, 1080)
        monitors = [
            _monitor_snapshot(
                11,
                r"\\.\DISPLAY9",
                2,
                primary=True,
                work=(0, 0, 1920, 1040),
                monitor=(0, 0, 1920, 1080),
            ),
            _monitor_snapshot(
                22,
                r"\\.\DISPLAY3",
                3,
                work=target_work,
                monitor=target_monitor,
            ),
        ]

        resolution, monitor = self.manager._KakaoManager__resolve_target_monitor(
            monitors,
            requested_display_num=2,
            requested_target_monitor={
                "display_num": 2,
                "device": r"\\.\DISPLAY2",
                "work": target_work,
                "monitor": target_monitor,
            },
            config_missing=False,
        )

        self.assertIsNotNone(monitor)
        self.assertEqual(monitor.handle, 22)
        self.assertEqual(resolution.resolved_monitor_handle, 22)
        self.assertEqual(resolution.fallback_reason, "descriptor_rect")

    def test_selector_selected_display_uses_target_monitor_descriptor(self) -> None:
        self.assertEqual(
            self.manager._KakaoManager__display_num_from_target_monitor(
                {"display_num": 3, "device": r"\\.\DISPLAY3"},
                fallback_display_num=1,
            ),
            3,
        )
        self.assertEqual(
            self.manager._KakaoManager__display_num_from_target_monitor(
                {"device": r"\\.\DISPLAY3"},
                fallback_display_num=2,
            ),
            2,
        )

    def test_selector_initial_overlay_uses_default_index_descriptor(self) -> None:
        self.assertEqual(
            self.manager._KakaoManager__display_num_from_selector_index(
                [
                    {"display_num": 1, "device": r"\\.\DISPLAY1"},
                    {"display_num": 3, "device": r"\\.\DISPLAY2"},
                ],
                selected_index=1,
                fallback_display_num=1,
            ),
            3,
        )
        self.assertEqual(
            self.manager._KakaoManager__display_num_from_selector_index(
                [{"display_num": 1, "device": r"\\.\DISPLAY1"}],
                selected_index=99,
                fallback_display_num=2,
            ),
            2,
        )

    def test_selector_default_index_prefers_descriptor_device_over_stale_display_number(self) -> None:
        items = [
            {
                "display_num": 1,
                "target_monitor": {"display_num": 1, "device": r"\\.\DISPLAY1"},
            },
            {
                "display_num": 3,
                "target_monitor": {"display_num": 3, "device": r"\\.\DISPLAY2"},
            },
            {
                "display_num": 2,
                "target_monitor": {"display_num": 2, "device": r"\\.\DISPLAY9"},
            },
        ]

        index = self.manager._KakaoManager__select_monitor_item_index(
            items,
            requested_target_monitor={"display_num": 2, "device": r"\\.\DISPLAY2"},
            requested_display_num=2,
        )

        self.assertEqual(index, 1)

    def test_selector_default_index_prefers_descriptor_rect_over_stale_display_number(self) -> None:
        target_work = (1920, 0, 3840, 1040)
        target_monitor = (1920, 0, 3840, 1080)
        items = [
            {
                "display_num": 2,
                "target_monitor": {
                    "display_num": 2,
                    "device": r"\\.\DISPLAY9",
                    "work": (0, 0, 1920, 1040),
                    "monitor": (0, 0, 1920, 1080),
                },
            },
            {
                "display_num": 3,
                "target_monitor": {
                    "display_num": 3,
                    "device": r"\\.\DISPLAY3",
                    "work": target_work,
                    "monitor": target_monitor,
                },
            },
        ]

        index = self.manager._KakaoManager__select_monitor_item_index(
            items,
            requested_target_monitor={
                "display_num": 2,
                "device": r"\\.\DISPLAY2",
                "work": target_work,
                "monitor": target_monitor,
            },
            requested_display_num=2,
        )

        self.assertEqual(index, 1)

    def test_selector_overlay_display_uses_selector_state_before_display_number(self) -> None:
        self.manager._KakaoManager__select_index_to_target = [
            {"display_num": 1, "device": r"\\.\DISPLAY1"},
            {"display_num": 3, "device": r"\\.\DISPLAY2"},
        ]
        self.manager._KakaoManager__select_current_index = 1
        self.manager._KakaoManager__target_display_num = 1
        self.manager._KakaoManager__target_monitor = {"display_num": 1}

        self.assertEqual(
            self.manager._KakaoManager__selector_overlay_display_num(),
            3,
        )

    def test_invalidate_display_topology_clears_runtime_target_state(self) -> None:
        request_refresh = Mock()
        root = object()
        self.manager.request_refresh = request_refresh
        self.manager._KakaoManager__monitors = [{"handle": 11, "display_num": 1}]
        self.manager._KakaoManager__resolved_target_display_num = 1
        self.manager._KakaoManager__resolved_target_monitor_handle = 11
        self.manager._KakaoManager__next_monitor_scan_time = 123.0
        self.manager._KakaoManager__is_selecting = True
        self.manager._KakaoManager__select_window = _RaisingWindow()
        self.manager._KakaoManager__overlay_windows = [_RaisingWindow()]
        before_epoch = self.manager._KakaoManager__state_epoch

        self.manager.invalidate_display_topology(root=root, reason="display_change")

        self.assertEqual(self.manager._KakaoManager__monitors, [])
        self.assertIsNone(self.manager._KakaoManager__resolved_target_display_num)
        self.assertIsNone(self.manager._KakaoManager__resolved_target_monitor_handle)
        self.assertEqual(self.manager._KakaoManager__next_monitor_scan_time, 0.0)
        self.assertFalse(self.manager._KakaoManager__is_selecting)
        self.assertIsNone(self.manager._KakaoManager__select_window)
        self.assertGreater(self.manager._KakaoManager__state_epoch, before_epoch)
        request_refresh.assert_called_once_with(root)

    def test_config_loads_legacy_target_display_and_saves_target_monitor_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = f"{tmp}\\kakao_manager.json"
            with open(config_path, "w", encoding="utf-8") as fp:
                json.dump({"target_display": 2}, fp)

            manager = KakaoManager()
            manager._KakaoManager__config_dir = tmp
            manager._KakaoManager__config_path = config_path
            manager._KakaoManager__load_config()

            self.assertFalse(manager._KakaoManager__config_missing)
            self.assertEqual(manager._KakaoManager__target_display_num, 2)
            self.assertEqual(
                manager._KakaoManager__target_monitor,
                {"display_num": 2},
            )

            manager._KakaoManager__set_requested_target_monitor(
                {
                    "display_num": 3,
                    "device": r"\\.\DISPLAY3",
                    "work": (0, 0, 1280, 720),
                    "monitor": (0, 0, 1280, 768),
                    "selected_at_topology_signature": "abc",
                }
            )
            manager._KakaoManager__save_config()

            with open(config_path, "r", encoding="utf-8") as fp:
                saved = json.load(fp)

        self.assertNotIn("target_display", saved)
        self.assertEqual(saved["target_monitor"]["display_num"], 3)
        self.assertEqual(saved["target_monitor"]["device"], r"\\.\DISPLAY3")

    def test_apply_move_plan_skips_when_target_monitor_signature_is_stale(self) -> None:
        plan = WindowMovePlan(
            target_monitor_signature=(
                11,
                r"\\.\DISPLAY1",
                (0, 0, 1920, 1040),
                (0, 0, 1920, 1080),
            ),
            moves=(WindowMove(hwnd=300, x=10, y=20, width=400, height=500, resize=False),),
        )

        with patch(
            "src.apps.KakaoManager.win32api.GetMonitorInfo",
            return_value={
                "Device": r"\\.\DISPLAY1",
                "Work": (0, 0, 1600, 900),
                "Monitor": (0, 0, 1600, 900),
            },
        ):
            with patch("src.apps.KakaoManager.apply_precomputed_window_position") as apply_move:
                self.manager._KakaoManager__apply_move_plan(plan)

        apply_move.assert_not_called()

    def test_accept_work_result_drops_stale_generation_without_mutating_state(self) -> None:
        self.manager._KakaoManager__latest_request_generation = 5
        self.manager._KakaoManager__state_epoch = 1
        self.manager._KakaoManager__chat_order = [111]
        self.manager._KakaoManager__last_main_hwnd = 222
        self.manager._KakaoManager__kakao_pids = {333}
        self.manager._KakaoManager__config_missing = False

        result = KakaoWorkResult(
            request_generation=4,
            state_epoch=1,
            runtime_snapshot=KakaoRuntimeSnapshot(
                kakao_pids=(1,),
                chat_order=(2,),
                last_main_hwnd=3,
                monitors=(),
                next_pid_scan_time=1.0,
                next_monitor_scan_time=2.0,
            ),
            target_resolution=KakaoTargetResolution(
                requested_display_num=1,
                resolved_display_num=1,
                resolved_monitor_handle=1,
                config_missing=False,
                fallback_reason="",
            ),
            move_plan=WindowMovePlan(moves=(WindowMove(hwnd=1, x=1, y=1, width=1, height=1, resize=True),)),
        )

        with patch("src.apps.KakaoManager.apply_precomputed_window_position") as apply_move:
            accepted = self.manager._KakaoManager__accept_work_result(result)

        self.assertFalse(accepted)
        self.assertEqual(self.manager._KakaoManager__chat_order, [111])
        self.assertEqual(self.manager._KakaoManager__last_main_hwnd, 222)
        self.assertEqual(self.manager._KakaoManager__kakao_pids, {333})
        apply_move.assert_not_called()

    def test_accept_work_result_drops_stale_epoch_without_mutating_state(self) -> None:
        self.manager._KakaoManager__latest_request_generation = 5
        self.manager._KakaoManager__state_epoch = 9
        self.manager._KakaoManager__chat_order = [111]
        self.manager._KakaoManager__last_main_hwnd = 222
        self.manager._KakaoManager__kakao_pids = {333}

        result = KakaoWorkResult(
            request_generation=5,
            state_epoch=8,
            runtime_snapshot=KakaoRuntimeSnapshot(
                kakao_pids=(1,),
                chat_order=(2,),
                last_main_hwnd=3,
                monitors=(),
                next_pid_scan_time=1.0,
                next_monitor_scan_time=2.0,
            ),
            target_resolution=KakaoTargetResolution(
                requested_display_num=1,
                resolved_display_num=1,
                resolved_monitor_handle=1,
                config_missing=False,
                fallback_reason="",
            ),
            move_plan=WindowMovePlan(moves=(WindowMove(hwnd=1, x=1, y=1, width=1, height=1, resize=True),)),
        )

        with patch("src.apps.KakaoManager.apply_precomputed_window_position") as apply_move:
            accepted = self.manager._KakaoManager__accept_work_result(result)

        self.assertFalse(accepted)
        self.assertEqual(self.manager._KakaoManager__chat_order, [111])
        self.assertEqual(self.manager._KakaoManager__last_main_hwnd, 222)
        self.assertEqual(self.manager._KakaoManager__kakao_pids, {333})
        apply_move.assert_not_called()


if __name__ == "__main__":
    unittest.main()

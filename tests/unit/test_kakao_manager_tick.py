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
        self.assertEqual(self.manager._KakaoManager__latest_request_generation, 2)

    def test_post_ui_reports_contract_success_and_failure(self) -> None:
        calls = []
        self.manager.set_ui_post(lambda fn: calls.append(fn))

        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None), True)
        self.assertEqual(len(calls), 1)

        self.manager.set_ui_post(None)
        root = _FakeAfterRoot(after_result="after#1")

        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None, root=root), True)
        self.assertIs(self.manager._KakaoManager__post_ui(None, root=root), False)

        falsey_root = _FakeAfterRoot(after_result=None)
        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None, root=falsey_root), False)

    def test_post_ui_falls_back_after_ui_post_exception_and_reports_failure(self) -> None:
        self.manager.set_ui_post(Mock(side_effect=RuntimeError("post failed")))

        root = _FakeAfterRoot(after_result="after#1")
        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None, root=root), True)

        failing_root = _FakeAfterRoot(after_side_effect=RuntimeError("after failed"))
        self.assertIs(self.manager._KakaoManager__post_ui(lambda: None, root=failing_root), False)

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
                config_missing=True,
                fallback_reason="requested_display_unavailable",
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
        self.assertTrue(self.manager._KakaoManager__config_missing)
        self.assertEqual(apply_move.call_count, 2)

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

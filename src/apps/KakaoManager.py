from dataclasses import dataclass
from src.utils.LibConnector import LibConnector
from src.utils.windows_window import apply_precomputed_window_position

import json
import threading
import win32api
import win32con


@dataclass(frozen=True)
class MonitorSnapshot:
    handle: int
    device: str
    display_num: int | None
    is_primary: bool
    work: tuple[int, int, int, int]
    monitor: tuple[int, int, int, int]


@dataclass(frozen=True)
class KakaoRuntimeSnapshot:
    kakao_pids: tuple[int, ...]
    chat_order: tuple[int, ...]
    last_main_hwnd: int | None
    monitors: tuple[MonitorSnapshot, ...]
    next_pid_scan_time: float
    next_monitor_scan_time: float


@dataclass(frozen=True)
class KakaoTargetResolution:
    requested_display_num: int | None
    resolved_display_num: int | None
    resolved_monitor_handle: int | None
    config_missing: bool
    fallback_reason: str


@dataclass(frozen=True)
class WindowMove:
    hwnd: int
    x: int
    y: int
    width: int
    height: int
    resize: bool


@dataclass(frozen=True)
class WindowMovePlan:
    moves: tuple[WindowMove, ...] = ()


@dataclass(frozen=True)
class KakaoWorkRequest:
    request_generation: int
    state_epoch: int
    now: float
    requested_display_num: int | None
    runtime_snapshot: KakaoRuntimeSnapshot


@dataclass(frozen=True)
class KakaoWorkResult:
    request_generation: int
    state_epoch: int
    runtime_snapshot: KakaoRuntimeSnapshot
    target_resolution: KakaoTargetResolution
    move_plan: WindowMovePlan


class KakaoManager:
    def __init__(self) -> None:
        self.__lib = LibConnector()

        self.__process_name = "KakaoTalk.exe"
        self.__main_title = "카카오톡"

        self.__poll_interval_sec = 0.35
        self.__pid_scan_interval_sec = 2.0
        self.__monitor_scan_interval_sec = 5.0

        self.__next_poll_time = 0.0
        self.__next_pid_scan_time = 0.0
        self.__next_monitor_scan_time = 0.0

        self.__kakao_pids = set()
        self.__chat_order = []
        self.__last_main_hwnd = None
        self.__resolved_target_display_num = None
        self.__resolved_target_monitor_handle = None

        appdata = self.__lib.os.environ.get("APPDATA")
        base_dir = appdata if appdata else self.__lib.os.path.expanduser("~")
        self.__config_dir = self.__lib.os.path.join(base_dir, "windows-supporter")
        self.__config_path = self.__lib.os.path.join(self.__config_dir, "kakao_manager.json")
        self.__config_loaded = False
        self.__config_missing = True
        self.__target_display_num = None

        self.__monitors = []
        self.__is_selecting = False
        self.__select_window = None
        self.__overlay_windows = []
        self.__ui_post = None
        self.__worker_lock = threading.Lock()
        self.__worker_active = False
        self.__pending_rerun = False
        self.__latest_request_generation = 0
        self.__state_epoch = 0
        return

    def set_ui_post(self, ui_post) -> None:
        self.__ui_post = ui_post if callable(ui_post) else None
        return

    def request_refresh(self, root=None) -> None:
        self.__ensure_config_state_bootstrapped()
        self.__request_background_tick(root, self.__lib.time.monotonic())
        return

    def tick(self, root=None) -> None:
        now = self.__lib.time.monotonic()
        self.__ensure_config_state_bootstrapped()

        if root is not None and self.__config_missing and (not self.__is_selecting):
            try:
                ui = getattr(root, "_ws_main_ui", None)
            except Exception:
                ui = None
            if ui is not None:
                try:
                    ui.show_kakao_monitor()
                except Exception:
                    pass
            else:
                self.open_monitor_selector(root)

        if now < self.__next_poll_time:
            return
        self.__next_poll_time = now + self.__poll_interval_sec
        self.__request_background_tick(root, now)
        return

    def __ensure_config_state_bootstrapped(self) -> None:
        if not self.__config_loaded:
            prev_target = self.__normalize_display_num(self.__target_display_num)
            self.__load_config()
            self.__config_loaded = True
            if prev_target != self.__normalize_display_num(self.__target_display_num):
                self.__invalidate_effective_target()
        if self.__target_display_num is None:
            self.__target_display_num = self.__get_default_display_num()
        return

    def __normalize_display_num(self, value):
        try:
            if value is None:
                return None
            normalized = int(value)
            return normalized if normalized > 0 else None
        except Exception:
            return None

    def __invalidate_effective_target(self) -> None:
        try:
            self.__state_epoch = int(self.__state_epoch) + 1
        except Exception:
            self.__state_epoch = 1
        return

    def __set_requested_target_display(self, value) -> bool:
        previous = self.__normalize_display_num(self.__target_display_num)
        updated = self.__normalize_display_num(value)
        if updated is None:
            updated = self.__get_default_display_num()
        self.__target_display_num = updated
        changed = previous != updated
        if changed:
            self.__invalidate_effective_target()
        return changed

    def __build_runtime_snapshot(self) -> KakaoRuntimeSnapshot:
        monitors = tuple(self.__snapshot_monitor(m) for m in self.__monitors)
        return KakaoRuntimeSnapshot(
            kakao_pids=tuple(sorted(int(pid) for pid in self.__kakao_pids if int(pid) > 0)),
            chat_order=tuple(int(hwnd) for hwnd in self.__chat_order if int(hwnd) > 0),
            last_main_hwnd=self.__last_main_hwnd,
            monitors=monitors,
            next_pid_scan_time=float(self.__next_pid_scan_time),
            next_monitor_scan_time=float(self.__next_monitor_scan_time),
        )

    def __snapshot_monitor(self, monitor) -> MonitorSnapshot:
        work = tuple(int(v) for v in tuple(monitor.get("work", (0, 0, 0, 0)))[:4])
        monitor_rect = tuple(int(v) for v in tuple(monitor.get("monitor", (0, 0, 0, 0)))[:4])
        return MonitorSnapshot(
            handle=int(monitor.get("handle") or 0),
            device=str(monitor.get("device") or ""),
            display_num=self.__normalize_display_num(monitor.get("display_num")),
            is_primary=bool(monitor.get("is_primary")),
            work=work if len(work) == 4 else (0, 0, 0, 0),
            monitor=monitor_rect if len(monitor_rect) == 4 else (0, 0, 0, 0),
        )

    def __request_background_tick(self, root, now: float) -> None:
        request = None
        with self.__worker_lock:
            self.__latest_request_generation = int(self.__latest_request_generation) + 1
            generation = int(self.__latest_request_generation)
            if self.__worker_active:
                self.__pending_rerun = True
                return
            self.__worker_active = True
            self.__pending_rerun = False
            request = KakaoWorkRequest(
                request_generation=generation,
                state_epoch=int(self.__state_epoch),
                now=float(now),
                requested_display_num=self.__normalize_display_num(self.__target_display_num),
                runtime_snapshot=self.__build_runtime_snapshot(),
            )

        def worker() -> None:
            try:
                result = self.__compute_work_result(request)
            except Exception:
                self.__finish_failed_worker(root)
                return
            if not self.__post_ui(lambda: self.__handle_work_result(root, result), root=root):
                self.__finish_failed_worker(root)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.__finish_failed_worker(root)
        return

    def __post_ui(self, fn, root=None) -> bool:
        if not callable(fn):
            return False
        ui_post = self.__ui_post
        if callable(ui_post):
            try:
                ui_post(fn)
                return True
            except Exception:
                pass
        if root is None:
            return False
        try:
            return bool(root.after(0, fn))
        except Exception:
            return False
        return False

    def __finish_failed_worker(self, root=None) -> None:
        pending = False
        with self.__worker_lock:
            self.__worker_active = False
            pending = bool(self.__pending_rerun)
            self.__pending_rerun = False
        if pending:
            self.__request_background_tick(root, self.__lib.time.monotonic())
        return

    def __handle_work_result(self, root, result: KakaoWorkResult) -> None:
        rerun = False
        with self.__worker_lock:
            self.__worker_active = False
            rerun = bool(self.__pending_rerun) or (
                int(result.request_generation) != int(self.__latest_request_generation)
            )
            self.__pending_rerun = False
        self.__accept_work_result(result)
        if rerun:
            self.__request_background_tick(root, self.__lib.time.monotonic())
        return

    def __accept_work_result(self, result: KakaoWorkResult) -> bool:
        if int(result.request_generation) != int(self.__latest_request_generation):
            return False
        if int(result.state_epoch) != int(self.__state_epoch):
            return False

        runtime_snapshot = result.runtime_snapshot
        self.__kakao_pids = {int(pid) for pid in runtime_snapshot.kakao_pids}
        self.__chat_order = [int(hwnd) for hwnd in runtime_snapshot.chat_order]
        self.__last_main_hwnd = runtime_snapshot.last_main_hwnd
        self.__monitors = [
            {
                "handle": int(m.handle),
                "device": str(m.device),
                "display_num": self.__normalize_display_num(m.display_num),
                "is_primary": bool(m.is_primary),
                "work": tuple(m.work),
                "monitor": tuple(m.monitor),
            }
            for m in runtime_snapshot.monitors
        ]
        self.__next_pid_scan_time = float(runtime_snapshot.next_pid_scan_time)
        self.__next_monitor_scan_time = float(runtime_snapshot.next_monitor_scan_time)
        self.__config_missing = bool(result.target_resolution.config_missing)
        self.__resolved_target_display_num = self.__normalize_display_num(
            result.target_resolution.resolved_display_num
        )
        try:
            resolved_handle = int(result.target_resolution.resolved_monitor_handle or 0)
            self.__resolved_target_monitor_handle = resolved_handle if resolved_handle > 0 else None
        except Exception:
            self.__resolved_target_monitor_handle = None

        self.__apply_move_plan(result.move_plan)
        return True

    def __apply_move_plan(self, move_plan: WindowMovePlan) -> None:
        for move in tuple(move_plan.moves or ()):
            try:
                apply_precomputed_window_position(
                    int(move.hwnd),
                    int(move.x),
                    int(move.y),
                    int(move.width),
                    int(move.height),
                    resize=bool(move.resize),
                )
            except Exception:
                continue
        return

    def __compute_work_result(self, request: KakaoWorkRequest) -> KakaoWorkResult:
        runtime_snapshot = request.runtime_snapshot
        now = float(request.now)

        monitors = list(runtime_snapshot.monitors)
        next_monitor_scan_time = float(runtime_snapshot.next_monitor_scan_time)
        if (not monitors) or now >= next_monitor_scan_time:
            monitors = self.__collect_monitor_snapshots()
            next_monitor_scan_time = now + self.__monitor_scan_interval_sec

        resolution, target_monitor = self.__resolve_target_monitor(
            monitors,
            request.requested_display_num,
        )

        kakao_pids = list(runtime_snapshot.kakao_pids)
        next_pid_scan_time = float(runtime_snapshot.next_pid_scan_time)
        if (not kakao_pids) or now >= next_pid_scan_time:
            kakao_pids = sorted(self.__collect_kakao_pids())
            next_pid_scan_time = now + self.__pid_scan_interval_sec

        if not kakao_pids:
            next_snapshot = KakaoRuntimeSnapshot(
                kakao_pids=(),
                chat_order=(),
                last_main_hwnd=None,
                monitors=tuple(monitors),
                next_pid_scan_time=next_pid_scan_time,
                next_monitor_scan_time=next_monitor_scan_time,
            )
            return KakaoWorkResult(
                request_generation=int(request.request_generation),
                state_epoch=int(request.state_epoch),
                runtime_snapshot=next_snapshot,
                target_resolution=resolution,
                move_plan=WindowMovePlan(),
            )

        window_details = self.__collect_window_details(kakao_pids)
        if not window_details:
            next_snapshot = KakaoRuntimeSnapshot(
                kakao_pids=tuple(kakao_pids),
                chat_order=tuple(runtime_snapshot.chat_order),
                last_main_hwnd=runtime_snapshot.last_main_hwnd,
                monitors=tuple(monitors),
                next_pid_scan_time=next_pid_scan_time,
                next_monitor_scan_time=next_monitor_scan_time,
            )
            return KakaoWorkResult(
                request_generation=int(request.request_generation),
                state_epoch=int(request.state_epoch),
                runtime_snapshot=next_snapshot,
                target_resolution=resolution,
                move_plan=WindowMovePlan(),
            )

        main_hwnd = self.__pick_main_hwnd_from_details(window_details)
        chat_order = tuple(runtime_snapshot.chat_order)
        if main_hwnd and int(runtime_snapshot.last_main_hwnd or 0) != int(main_hwnd):
            chat_order = ()
        chat_hwnds = [
            int(item["hwnd"])
            for item in window_details
            if int(item["hwnd"]) != int(main_hwnd or 0)
            and str(item.get("title") or "") != self.__main_title
        ]
        updated_chat_order = tuple(self.__merge_chat_order(chat_order, chat_hwnds))

        move_plan = self.__build_move_plan(
            window_details=window_details,
            main_hwnd=main_hwnd,
            chat_order=updated_chat_order,
            target_monitor=target_monitor,
        )
        next_snapshot = KakaoRuntimeSnapshot(
            kakao_pids=tuple(kakao_pids),
            chat_order=updated_chat_order,
            last_main_hwnd=main_hwnd,
            monitors=tuple(monitors),
            next_pid_scan_time=next_pid_scan_time,
            next_monitor_scan_time=next_monitor_scan_time,
        )
        return KakaoWorkResult(
            request_generation=int(request.request_generation),
            state_epoch=int(request.state_epoch),
            runtime_snapshot=next_snapshot,
            target_resolution=resolution,
            move_plan=move_plan,
        )

    def __collect_monitor_snapshots(self) -> list[MonitorSnapshot]:
        monitors = []
        try:
            for hmon, _, _ in win32api.EnumDisplayMonitors(None, None):
                info = win32api.GetMonitorInfo(hmon)
                device = info.get("Device", "")
                display_num = self.__parse_display_num(device)
                is_primary = bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY)
                monitors.append(
                    MonitorSnapshot(
                        handle=int(hmon or 0),
                        device=str(device or ""),
                        display_num=self.__normalize_display_num(display_num),
                        is_primary=is_primary,
                        work=tuple(int(v) for v in tuple(info.get("Work", (0, 0, 0, 0)))[:4]),
                        monitor=tuple(
                            int(v) for v in tuple(info.get("Monitor", (0, 0, 0, 0)))[:4]
                        ),
                    )
                )
        except Exception:
            monitors = []
        monitors.sort(
            key=lambda m: (m.display_num if m.display_num is not None else 9999, m.work[0])
        )
        return monitors

    def __collect_kakao_pids(self) -> set[int]:
        pids = set()
        try:
            for proc in self.__lib.psutil.process_iter(["name"]):
                try:
                    if proc.info.get("name") == self.__process_name:
                        pids.add(int(proc.pid))
                except Exception:
                    continue
        except Exception:
            return set()
        return pids

    def __resolve_target_monitor(
        self,
        monitors: list[MonitorSnapshot],
        requested_display_num: int | None,
    ) -> tuple[KakaoTargetResolution, MonitorSnapshot | None]:
        requested = self.__normalize_display_num(requested_display_num)
        if requested is not None:
            for monitor in monitors:
                if self.__normalize_display_num(monitor.display_num) == requested:
                    return (
                        KakaoTargetResolution(
                            requested_display_num=requested,
                            resolved_display_num=requested,
                            resolved_monitor_handle=int(monitor.handle),
                            config_missing=False,
                            fallback_reason="",
                        ),
                        monitor,
                    )
        fallback_reason = ""
        fallback = None
        if requested is not None:
            fallback_reason = "requested_display_unavailable"
        for monitor in monitors:
            if bool(monitor.is_primary):
                fallback = monitor
                if not fallback_reason:
                    fallback_reason = "primary"
                break
        if fallback is None and monitors:
            fallback = monitors[0]
            if not fallback_reason:
                fallback_reason = "first_monitor"

        return (
            KakaoTargetResolution(
                requested_display_num=requested,
                resolved_display_num=self.__normalize_display_num(
                    fallback.display_num if fallback is not None else None
                ),
                resolved_monitor_handle=int(fallback.handle) if fallback is not None else None,
                config_missing=bool(requested is not None and fallback is not None and fallback_reason == "requested_display_unavailable"),
                fallback_reason=str(fallback_reason or ""),
            ),
            fallback,
        )

    def __collect_window_details(self, kakao_pids) -> list[dict]:
        result = []
        win32gui = self.__lib.win32gui
        win32process = self.__lib.win32process
        pid_set = {int(pid) for pid in kakao_pids if int(pid) > 0}

        def cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if int(pid or 0) not in pid_set:
                    return
                title = str(win32gui.GetWindowText(hwnd) or "").strip()
                if not title:
                    return
                exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if exstyle & win32con.WS_EX_TOOLWINDOW:
                    return
                rect = tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
                result.append(
                    {
                        "hwnd": int(hwnd),
                        "title": title,
                        "rect": rect,
                        "is_iconic": bool(win32gui.IsIconic(hwnd)),
                    }
                )
            except Exception:
                return

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return []
        return result

    def __pick_main_hwnd_from_details(self, windows) -> int | None:
        best_hwnd = None
        best_area = -1
        for item in list(windows or ()):
            if str(item.get("title") or "") != self.__main_title:
                continue
            rect = tuple(item.get("rect") or ())
            if len(rect) != 4:
                continue
            area = max(0, int(rect[2]) - int(rect[0])) * max(0, int(rect[3]) - int(rect[1]))
            if area > best_area:
                best_area = area
                best_hwnd = int(item.get("hwnd") or 0)
        return best_hwnd if best_hwnd and best_hwnd > 0 else None

    def __merge_chat_order(self, existing_order, chat_hwnds) -> list[int]:
        chat_set = {int(hwnd) for hwnd in chat_hwnds if int(hwnd) > 0}
        if not chat_set:
            return []
        merged = [int(hwnd) for hwnd in existing_order if int(hwnd) in chat_set]
        existing = set(merged)
        new_hwnds = [int(hwnd) for hwnd in chat_hwnds if int(hwnd) not in existing]
        new_hwnds.sort()
        merged.extend(new_hwnds)
        return merged

    def __build_move_plan(
        self,
        window_details,
        main_hwnd: int | None,
        chat_order,
        target_monitor: MonitorSnapshot | None,
    ) -> WindowMovePlan:
        if target_monitor is None:
            return WindowMovePlan()

        detail_map = {
            int(item.get("hwnd") or 0): item for item in list(window_details or ()) if int(item.get("hwnd") or 0) > 0
        }
        main_detail = detail_map.get(int(main_hwnd or 0))
        place_main = bool(main_detail and (not bool(main_detail.get("is_iconic"))))
        ref_detail = main_detail if place_main else None
        if ref_detail is None:
            for hwnd in tuple(chat_order or ()):
                detail = detail_map.get(int(hwnd or 0))
                if detail is None or bool(detail.get("is_iconic")):
                    continue
                ref_detail = detail
                break
        if ref_detail is None:
            return WindowMovePlan()

        ref_rect = tuple(ref_detail.get("rect") or ())
        if len(ref_rect) != 4:
            return WindowMovePlan()
        ref_w = int(ref_rect[2]) - int(ref_rect[0])
        ref_h = int(ref_rect[3]) - int(ref_rect[1])
        if ref_w <= 0 or ref_h <= 0:
            return WindowMovePlan()

        work_left, work_top, _, work_bottom = tuple(target_monitor.work)
        target_y = int(ref_rect[1])
        if target_y < work_top:
            target_y = work_top
        max_y = int(work_bottom) - ref_h
        if max_y < work_top:
            max_y = work_top
        if target_y > max_y:
            target_y = max_y

        moves = []
        if place_main and main_detail is not None:
            main_rect = tuple(main_detail.get("rect") or ())
            if len(main_rect) == 4 and (
                int(main_rect[0]) != int(work_left) or int(main_rect[1]) != int(target_y)
            ):
                moves.append(
                    WindowMove(
                        hwnd=int(main_hwnd),
                        x=int(work_left),
                        y=int(target_y),
                        width=int(ref_w),
                        height=int(ref_h),
                        resize=False,
                    )
                )

        slot_iter = self.__iter_slots(
            [(int(target_monitor.handle), tuple(target_monitor.work))],
            ref_w,
            ref_h,
            primary_handle=int(target_monitor.handle) if place_main else None,
            y_base=target_y,
        )
        for hwnd in tuple(chat_order or ()):
            detail = detail_map.get(int(hwnd or 0))
            if detail is None or bool(detail.get("is_iconic")):
                continue
            try:
                x, y = next(slot_iter)
            except StopIteration:
                break
            rect = tuple(detail.get("rect") or ())
            if len(rect) != 4:
                continue
            cur_w = int(rect[2]) - int(rect[0])
            cur_h = int(rect[3]) - int(rect[1])
            if int(rect[0]) == int(x) and int(rect[1]) == int(y) and cur_w == ref_w and cur_h == ref_h:
                continue
            moves.append(
                WindowMove(
                    hwnd=int(hwnd),
                    x=int(x),
                    y=int(y),
                    width=int(ref_w),
                    height=int(ref_h),
                    resize=True,
                )
            )

        return WindowMovePlan(moves=tuple(moves))

    def open_monitor_selector(self, root, embedded_parent=None) -> bool:
        if self.__is_selecting:
            if self.__select_window is not None:
                try:
                    try:
                        self.__select_window.lift()
                    except Exception:
                        try:
                            self.__select_window.tkraise()
                        except Exception:
                            pass
                except Exception:
                    pass

                try:
                    if not self.__overlay_windows:
                        selected = self.__target_display_num
                        if selected is None:
                            selected = self.__get_default_display_num()
                        self.show_monitor_overlays(root, duration_ms=0, selected_display_num=selected)
                except Exception:
                    pass
                return True

            self.__is_selecting = False

        items = self.__build_monitor_items()
        if not items:
            self.request_refresh(root)
            return False

        tk = self.__lib.tk
        self.__is_selecting = True
        is_embedded = bool(embedded_parent is not None)
        if is_embedded:
            top = embedded_parent
            self.__select_window = top
            try:
                for w in list(top.winfo_children()):
                    try:
                        w.destroy()
                    except Exception:
                        continue
            except Exception:
                pass
        else:
            top = tk.Toplevel(root)
            self.__select_window = top
            top.title("KakaoTalk 모니터 선택")
            try:
                top.attributes("-topmost", True)
            except Exception:
                pass
        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        text_muted = "#6B7280"

        try:
            top.configure(bg=bg)
        except Exception:
            pass

        container = tk.Frame(top, bg=bg)
        container.pack(fill="both", expand=True)

        default_display = self.__get_default_display_num()
        header = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        header.pack(fill="x", padx=12, pady=(12, 8))

        header_inner = tk.Frame(header, bg=card_bg)
        header_inner.pack(fill="x", padx=14, pady=12)

        tk.Label(
            header_inner,
            text="KakaoTalk 모니터 설정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")

        tk.Label(
            header_inner,
            text=(
                "카카오톡 정렬에 사용할 모니터를 선택하세요.\n"
                f"기본값: {default_display}"
            ),
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        def get_current_selected_display_num():
            try:
                sel = listbox.curselection()
                if sel:
                    return int(index_to_display[int(sel[0])])
            except Exception:
                pass
            return int(target)

        body_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        body_card.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        body = tk.Frame(body_card, bg=card_bg)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        list_frame = tk.Frame(body, bg=card_bg)
        list_frame.pack(fill="both", expand=True)

        listbox = tk.Listbox(
            list_frame,
            width=70,
            height=max(4, min(7, len(items))),
            font=("Segoe UI", 9),
            activestyle="none",
        )
        vsb = tk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        hsb = tk.Scrollbar(list_frame, orient="horizontal", command=listbox.xview)
        listbox.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        listbox.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        try:
            list_frame.grid_rowconfigure(0, weight=1)
            list_frame.grid_columnconfigure(0, weight=1)
        except Exception:
            pass

        index_to_display = []
        default_index = 0
        target = int(self.__target_display_num) if self.__target_display_num else int(default_display)
        for idx, it in enumerate(items):
            index_to_display.append(it["display_num"])
            listbox.insert("end", it["label"])
            if it["display_num"] == target:
                default_index = idx

        try:
            listbox.selection_set(default_index)
            listbox.activate(default_index)
            listbox.see(default_index)
        except Exception:
            pass

        try:
            self.show_monitor_overlays(root, duration_ms=0, selected_display_num=target)
        except Exception:
            pass

        def on_listbox_select(event=None) -> None:
            try:
                self.__set_overlay_selected(get_current_selected_display_num())
            except Exception:
                pass
            return

        try:
            listbox.bind("<<ListboxSelect>>", on_listbox_select)
        except Exception:
            pass

        btn_frame = tk.Frame(body, bg=card_bg)
        btn_frame.pack(pady=(10, 0), fill="x")

        def show_numbers() -> None:
            try:
                self.show_monitor_overlays(root, duration_ms=0, selected_display_num=get_current_selected_display_num())
            except Exception:
                pass
            return

        def close_window() -> None:
            self.__is_selecting = False
            try:
                self.__destroy_overlays()
            except Exception:
                pass
            if is_embedded:
                try:
                    for w in list(top.winfo_children()):
                        try:
                            w.destroy()
                        except Exception:
                            continue
                except Exception:
                    pass
                try:
                    root.withdraw()
                except Exception:
                    pass
            else:
                try:
                    if self.__select_window is not None:
                        self.__select_window.destroy()
                except Exception:
                    pass
            self.__select_window = None
            return

        def commit_selection() -> None:
            try:
                sel = listbox.curselection()
                if sel:
                    chosen = index_to_display[int(sel[0])]
                else:
                    chosen = 1
            except Exception:
                chosen = 1

            changed = self.__set_requested_target_display(int(chosen) if int(chosen) > 0 else 1)
            self.__config_missing = False
            self.__save_config()
            close_window()
            if changed:
                self.request_refresh(root)
            return

        def cancel() -> None:
            changed = False
            if self.__target_display_num is None:
                changed = self.__set_requested_target_display(int(default_display))
            self.__config_missing = False
            self.__save_config()
            close_window()
            if changed:
                self.request_refresh(root)
            return

        show_btn = tk.Button(btn_frame, text="모니터 번호 표시", command=show_numbers)
        show_btn.pack(side="left")

        ok_btn = tk.Button(btn_frame, text="확인", command=commit_selection)
        ok_btn.pack(side="right")

        cancel_btn = tk.Button(btn_frame, text=f"취소(기본값 {default_display})", command=cancel)
        cancel_btn.pack(side="right", padx=(0, 8))

        if not is_embedded:
            top.protocol("WM_DELETE_WINDOW", cancel)
        try:
            if not is_embedded:
                top.focus_force()
                ok_btn.focus_set()
        except Exception:
            pass
        return True

    def hide_monitor_overlays(self) -> None:
        try:
            self.__destroy_overlays()
        except Exception:
            pass
        return

    def show_monitor_overlays(self, root, duration_ms: int = 1500, selected_display_num=None) -> None:
        self.__destroy_overlays()
        if not self.__monitors:
            self.request_refresh(root)
            return

        tk = self.__lib.tk
        win32gui = self.__lib.win32gui
        selected_num = None
        try:
            if selected_display_num is not None:
                selected_num = int(selected_display_num)
        except Exception:
            selected_num = None
        for m in self.__monitors:
            n = m.get("display_num")
            if n is None:
                continue
            try:
                n_int = int(n)
            except Exception:
                continue

            left, top, right, bottom = m["work"]

            win = tk.Toplevel(root)
            overlay = {"win": win, "display_num": n_int, "label": None}
            self.__overlay_windows.append(overlay)
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass

            is_selected = bool(selected_num is not None and n_int == selected_num)
            bg = "#16a34a" if is_selected else "#111827"
            label = tk.Label(
                win,
                text=str(n_int),
                font=("Segoe UI", 28, "bold"),
                fg="white",
                bg=bg,
                padx=12,
                pady=6,
                relief="solid",
                borderwidth=1,
            )
            label.pack()
            overlay["label"] = label

            try:
                win.update_idletasks()
                ow = win.winfo_reqwidth()
                oh = win.winfo_reqheight()
                if ow <= 0:
                    ow = 1
                if oh <= 0:
                    oh = 1

                margin = 12
                x = left + margin
                y = bottom - oh - margin
                if x < left:
                    x = left
                if y < top:
                    y = top
                if x + ow > right:
                    x = right - ow
                if y + oh > bottom:
                    y = bottom - oh

                win.geometry(f"{ow}x{oh}+0+0")

                def place_overlay(attempt=0, _win=win, _x=x, _y=y, _w=ow, _h=oh) -> None:
                    try:
                        if not _win.winfo_exists():
                            return
                        try:
                            _win.update_idletasks()
                        except Exception:
                            pass
                        hwnd = _win.winfo_id()
                        if not hwnd:
                            if attempt < 15:
                                root.after(30, lambda: place_overlay(attempt + 1, _win, _x, _y, _w, _h))
                            return

                        try:
                            target_hwnd = hwnd
                            for _ in range(0, 8):
                                style = win32gui.GetWindowLong(target_hwnd, win32con.GWL_STYLE)
                                if style & win32con.WS_CHILD:
                                    parent = win32gui.GetParent(target_hwnd)
                                    if not parent:
                                        break
                                    target_hwnd = parent
                                    continue
                                break
                        except Exception:
                            target_hwnd = hwnd

                        try:
                            if not win32gui.IsWindow(target_hwnd):
                                if attempt < 15:
                                    root.after(30, lambda: place_overlay(attempt + 1, _win, _x, _y, _w, _h))
                                return
                        except Exception:
                            target_hwnd = hwnd
                        flags = (
                            win32con.SWP_NOACTIVATE
                            | win32con.SWP_NOOWNERZORDER
                            | win32con.SWP_SHOWWINDOW
                            | win32con.SWP_NOSENDCHANGING
                        )
                        win32gui.SetWindowPos(target_hwnd, win32con.HWND_TOPMOST, _x, _y, _w, _h, flags)
                    except Exception:
                        if attempt < 15:
                            try:
                                root.after(50, lambda: place_overlay(attempt + 1, _win, _x, _y, _w, _h))
                            except Exception:
                                return
                    return

                root.after(0, place_overlay)
            except Exception:
                win.geometry(f"{ow}x{oh}+12+12")

        if int(duration_ms) > 0:
            try:
                root.after(int(duration_ms), self.__destroy_overlays)
            except Exception:
                self.__destroy_overlays()
        return

    def __set_overlay_selected(self, selected_display_num) -> None:
        try:
            selected_num = int(selected_display_num)
        except Exception:
            return

        for it in self.__overlay_windows:
            try:
                if not isinstance(it, dict):
                    continue
                label = it.get("label")
                n = it.get("display_num")
                if label is None or n is None:
                    continue

                is_selected = bool(int(n) == selected_num)
                bg = "#16a34a" if is_selected else "#111827"
                label.configure(bg=bg)
            except Exception:
                continue
        return

    def __destroy_overlays(self) -> None:
        wins = self.__overlay_windows
        self.__overlay_windows = []
        for w in wins:
            try:
                if isinstance(w, dict):
                    win = w.get("win")
                    if win is not None:
                        win.destroy()
                else:
                    w.destroy()
            except Exception:
                continue
        return

    def __refresh_kakao_pids(self, now: float) -> None:
        if now < self.__next_pid_scan_time:
            return
        self.__next_pid_scan_time = now + self.__pid_scan_interval_sec

        pids = set()
        try:
            for p in self.__lib.psutil.process_iter(["name"]):
                try:
                    if p.info.get("name") == self.__process_name:
                        pids.add(p.pid)
                except Exception:
                    continue
        except Exception:
            pids = set()
        self.__kakao_pids = pids
        return

    def __load_config(self) -> None:
        self.__config_missing = True
        try:
            if not self.__lib.os.path.exists(self.__config_path):
                return
            with open(self.__config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            value = int(data.get("target_display", 0))
            if value > 0:
                self.__target_display_num = value
                self.__config_missing = False
        except Exception:
            self.__config_missing = True
        return

    def __save_config(self) -> None:
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
            data = {"target_display": int(self.__target_display_num) if self.__target_display_num else 1}
            with open(self.__config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            return
        return

    def __refresh_monitors(self, now: float) -> None:
        if now < self.__next_monitor_scan_time:
            return
        self.__next_monitor_scan_time = now + self.__monitor_scan_interval_sec

        monitors = []
        try:
            for hmon, _, _ in win32api.EnumDisplayMonitors(None, None):
                info = win32api.GetMonitorInfo(hmon)
                device = info.get("Device", "")
                display_num = self.__parse_display_num(device)
                is_primary = bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY)
                monitors.append(
                    {
                        "handle": hmon,
                        "device": device,
                        "display_num": display_num,
                        "is_primary": is_primary,
                        "work": info["Work"],
                        "monitor": info["Monitor"],
                    }
                )
        except Exception:
            monitors = []

        monitors.sort(key=lambda m: (m["display_num"] if m["display_num"] is not None else 9999, m["work"][0]))
        self.__monitors = monitors
        return

    def __parse_display_num(self, device: str):
        try:
            m = self.__lib.re.search(r"DISPLAY(\d+)$", device)
            if not m:
                return None
            return int(m.group(1))
        except Exception:
            return None

    def __build_monitor_items(self):
        items = []
        for m in self.__monitors:
            n = m.get("display_num")
            if n is None:
                continue
            left, top, right, bottom = m["work"]
            w = right - left
            h = bottom - top
            primary = " (PRIMARY)" if m.get("is_primary") else ""
            items.append(
                {
                    "display_num": int(n),
                    "label": f"{n}: {m.get('device')} {w}x{h} work=({left},{top},{right},{bottom}){primary}",
                }
            )
        items.sort(key=lambda x: x["display_num"])
        return items

    def __get_default_display_num(self) -> int:
        for m in self.__monitors:
            if m.get("display_num") == 1:
                return 1

        for m in self.__monitors:
            if m.get("is_primary") and (m.get("display_num") is not None):
                return int(m.get("display_num"))

        nums = [m.get("display_num") for m in self.__monitors if m.get("display_num") is not None]
        if nums:
            try:
                return int(min(nums))
            except Exception:
                return int(nums[0])
        return 1

    def __get_target_monitor_work(self):
        target_num = self.__target_display_num
        if target_num is not None:
            for m in self.__monitors:
                if m.get("display_num") == int(target_num):
                    self.__config_missing = False
                    return m
            self.__config_missing = True

        for m in self.__monitors:
            if m.get("is_primary"):
                return m

        return self.__monitors[0] if self.__monitors else None

    def __get_kakao_top_windows(self):
        result = []
        win32gui = self.__lib.win32gui
        win32process = self.__lib.win32process

        def cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return

                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in self.__kakao_pids:
                    return

                title = win32gui.GetWindowText(hwnd).strip()
                if not title:
                    return

                exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if exstyle & win32con.WS_EX_TOOLWINDOW:
                    return

                result.append((hwnd, title))
            except Exception:
                return

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return []
        return result

    def __pick_main_hwnd(self, windows) -> int or None:
        win32gui = self.__lib.win32gui
        best_hwnd = None
        best_area = -1
        for hwnd, title in windows:
            if title != self.__main_title:
                continue
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = (right - left) * (bottom - top)
                if area > best_area:
                    best_area = area
                    best_hwnd = hwnd
            except Exception:
                continue
        return best_hwnd

    def __iter_slots(self, monitors, w: int, h: int, primary_handle, y_base: int):
        for row in range(0, 256):
            for hmon, work in monitors:
                left, top, right, bottom = work
                base_y = y_base
                if base_y < top:
                    base_y = top
                max_y0 = bottom - h
                if max_y0 < top:
                    max_y0 = top
                if base_y > max_y0:
                    base_y = max_y0

                y = base_y + row * h
                if y + h > bottom:
                    continue

                cols = int((right - left) // w)
                if cols <= 0:
                    continue

                start_col = 1 if hmon == primary_handle else 0
                if start_col >= cols:
                    continue

                for col in range(start_col, cols):
                    x = left + (col * w)
                    if x + w > right:
                        break
                    yield (x, y)
        return

    def __update_chat_order(self, chat_hwnds) -> None:
        chat_set = set(chat_hwnds)
        if not chat_set:
            self.__chat_order.clear()
            return

        self.__chat_order = [h for h in self.__chat_order if h in chat_set]
        existing = set(self.__chat_order)
        new_hwnds = [h for h in chat_hwnds if h not in existing]
        new_hwnds.sort()
        self.__chat_order.extend(new_hwnds)
        return

    def __move_window(self, hwnd, x: int, y: int, w: int, h: int, resize: bool) -> None:
        win32gui = self.__lib.win32gui
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            cur_w = right - left
            cur_h = bottom - top

            if resize:
                if left == x and top == y and cur_w == w and cur_h == h:
                    return
                flags = (
                    win32con.SWP_NOZORDER
                    | win32con.SWP_NOACTIVATE
                    | win32con.SWP_NOOWNERZORDER
                    | win32con.SWP_ASYNCWINDOWPOS
                )
                win32gui.SetWindowPos(hwnd, 0, x, y, w, h, flags)
                return

            if left == x and top == y:
                return
            flags = (
                win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE
                | win32con.SWP_NOOWNERZORDER
                | win32con.SWP_NOSIZE
                | win32con.SWP_ASYNCWINDOWPOS
            )
            win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0, flags)
        except Exception:
            return
        return

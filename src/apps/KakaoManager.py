from src.utils.LibConnector import LibConnector

import json
import win32api
import win32con


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
        return

    def tick(self, root=None) -> None:
        now = self.__lib.time.monotonic()

        if not self.__config_loaded:
            self.__load_config()
            self.__config_loaded = True

        self.__refresh_monitors(now)
        if self.__target_display_num is None:
            self.__target_display_num = self.__get_default_display_num()

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

        self.__refresh_kakao_pids(now)
        if not self.__kakao_pids:
            self.__chat_order.clear()
            self.__last_main_hwnd = None
            return

        windows = self.__get_kakao_top_windows()
        if not windows:
            return

        win32gui = self.__lib.win32gui
        main_hwnd = self.__pick_main_hwnd(windows)
        if main_hwnd:
            if self.__last_main_hwnd != main_hwnd:
                self.__chat_order.clear()
                self.__last_main_hwnd = main_hwnd

        target_monitor = self.__get_target_monitor_work()
        if not target_monitor:
            return

        chat_hwnds = []
        for hwnd, title in windows:
            if hwnd == main_hwnd:
                continue
            if title == self.__main_title:
                continue
            chat_hwnds.append(hwnd)

        self.__update_chat_order(chat_hwnds)

        place_main = bool(main_hwnd and (not win32gui.IsIconic(main_hwnd)))
        ref_hwnd = main_hwnd if place_main else None
        if ref_hwnd is None:
            for h in self.__chat_order:
                try:
                    if not win32gui.IsIconic(h):
                        ref_hwnd = h
                        break
                except Exception:
                    continue

        if ref_hwnd is None:
            return

        try:
            ref_left, ref_top, ref_right, ref_bottom = win32gui.GetWindowRect(ref_hwnd)
        except Exception:
            return
        ref_w = ref_right - ref_left
        ref_h = ref_bottom - ref_top
        if ref_w <= 0 or ref_h <= 0:
            return

        work_left, work_top, _, work_bottom = target_monitor["work"]
        target_y = ref_top
        if target_y < work_top:
            target_y = work_top
        max_y = work_bottom - ref_h
        if max_y < work_top:
            max_y = work_top
        if target_y > max_y:
            target_y = max_y

        if place_main:
            self.__move_window(main_hwnd, work_left, target_y, ref_w, ref_h, resize=False)

        if not self.__chat_order:
            return

        monitors = [(target_monitor["handle"], target_monitor["work"])]
        primary_handle = target_monitor["handle"] if place_main else None
        slot_iter = self.__iter_slots(
            monitors,
            ref_w,
            ref_h,
            primary_handle=primary_handle,
            y_base=target_y,
        )
        for hwnd in self.__chat_order:
            try:
                x, y = next(slot_iter)
            except StopIteration:
                break
            try:
                if win32gui.IsIconic(hwnd):
                    continue
            except Exception:
                continue
            self.__move_window(hwnd, x, y, ref_w, ref_h, resize=True)
        return

    def open_monitor_selector(self, root, embedded_parent=None) -> None:
        if self.__is_selecting:
            try:
                if self.__select_window is not None:
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
            return

        self.__refresh_monitors(self.__lib.time.monotonic())
        items = self.__build_monitor_items()
        if not items:
            return

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

            self.__target_display_num = int(chosen) if int(chosen) > 0 else 1
            self.__config_missing = False
            self.__save_config()
            close_window()
            return

        def cancel() -> None:
            if self.__target_display_num is None:
                self.__target_display_num = int(default_display)
            self.__config_missing = False
            self.__save_config()
            close_window()
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
        return

    def hide_monitor_overlays(self) -> None:
        try:
            self.__destroy_overlays()
        except Exception:
            pass
        return

    def show_monitor_overlays(self, root, duration_ms: int = 1500, selected_display_num=None) -> None:
        self.__destroy_overlays()
        self.__refresh_monitors(self.__lib.time.monotonic())

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

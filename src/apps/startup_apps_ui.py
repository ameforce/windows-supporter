from __future__ import annotations

import os
import queue
import sys
import threading
import time
from typing import Any


class StartupAppsWindow:
    def __init__(self, root: Any, manager: Any) -> None:
        self._root = root
        self._manager = manager

        self._tk = None
        self._ttk = None
        self._messagebox = None
        self._filedialog = None

        self._win = None
        self._embedded = False
        self._tree = None
        self._instances: list[dict[str, Any]] = []
        self._runtime_after_id = None
        self._tree_resize_after_id = None

        self._icon_path: str | None = None
        self._icon_path_resolved = False

        self._filter_var = None
        self._filter_after_id = None
        self._stats_var = None
        self._toast_var = None
        self._toast_label = None
        self._toast_after_id = None
        self._enabled_pill = None
        self._ui_queue = None
        self._ui_pump_after_id = None

        self._runtime_cache: dict[str, tuple[bool, int | None]] = {}
        self._runtime_cache_ts = 0.0
        self._runtime_worker_running = False

        self._context_menu = None
        self._style_ready = False

        self._busy = False
        self._controls: list[Any] = []
        self._btn_toggle = None
        return

    def is_open(self) -> bool:
        try:
            return bool(self._win is not None and self._win.winfo_exists())
        except Exception:
            return False

    def show(self) -> None:
        if self.is_open():
            try:
                self._apply_window_icon(self._win)
                self._win.deiconify()
                self._win.lift()
                self._win.focus_force()
            except Exception:
                pass
            return

        self._embedded = False
        self._lazy_import_tk()
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        self._ensure_style()
        win = tk.Toplevel(self._root)
        self._win = win
        self._controls = []
        self._btn_toggle = None
        self._ui_queue = queue.SimpleQueue()
        self._ui_pump_after_id = None
        self._start_ui_pump()
        self._build_contents(win, as_toplevel=True)
        return

    def mount(self, parent: Any) -> None:
        if parent is None:
            return
        if self.is_open():
            try:
                self._on_close()
            except Exception:
                pass

        self._embedded = True
        self._lazy_import_tk()
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        try:
            for w in list(parent.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    continue
        except Exception:
            pass

        self._ensure_style()
        win = tk.Frame(parent)
        try:
            win.pack(fill="both", expand=True)
        except Exception:
            pass

        self._win = win
        self._controls = []
        self._btn_toggle = None
        self._ui_queue = queue.SimpleQueue()
        self._ui_pump_after_id = None
        self._start_ui_pump()
        self._build_contents(win, as_toplevel=False)
        return

    def _hide_main_window(self) -> None:
        try:
            self._root.withdraw()
        except Exception:
            pass
        return

    def _build_contents(self, win: Any, as_toplevel: bool) -> None:
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        if as_toplevel:
            try:
                win.title("Windows Supporter - Startup Apps")
            except Exception:
                pass
            self._apply_window_icon(win)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass

            try:
                win.geometry("980x520")
            except Exception:
                pass
            try:
                win.minsize(980, 520)
            except Exception:
                pass

        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        text_muted = "#6B7280"

        try:
            win.configure(bg=bg)
        except Exception:
            pass

        container = tk.Frame(win, bg=bg)
        container.pack(fill="both", expand=True)

        header_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        header_card.pack(fill="x", padx=12, pady=(12, 8))

        header_inner = tk.Frame(header_card, bg=card_bg)
        header_inner.pack(fill="x", padx=14, pady=12)

        title_row = tk.Frame(header_inner, bg=card_bg)
        title_row.pack(fill="x")

        tk.Label(
            title_row,
            text="Startup Apps",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        self._enabled_pill = tk.Label(
            title_row,
            text="",
            bg="#9CA3AF",
            fg="#FFFFFF",
            padx=10,
            pady=2,
            font=("Segoe UI", 9, "bold"),
        )
        self._enabled_pill.pack(side="right")

        btn_global = ttk.Button(
            title_row,
            text="전체 켜기/끄기",
            command=self._on_toggle_global_enabled,
        )
        btn_global.pack(side="right", padx=(0, 8))
        self._controls.append(btn_global)

        subtitle = tk.Label(
            header_inner,
            text=(
                "알림 수신용 앱을 실행/유지합니다.  더블클릭=편집, 우클릭=메뉴, 검색으로 빠르게 찾기"
            ),
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        )
        subtitle.pack(anchor="w", pady=(6, 10))

        toolbar = tk.Frame(header_inner, bg=card_bg)
        toolbar.pack(fill="x")

        tk.Label(
            toolbar,
            text="검색",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))

        self._filter_var = tk.StringVar(value="")
        search = ttk.Entry(toolbar, textvariable=self._filter_var, width=34)
        search.pack(side="left")
        try:
            search.bind("<KeyRelease>", self._on_filter_key)
        except Exception:
            pass

        self._stats_var = tk.StringVar(value="")
        tk.Label(
            toolbar,
            textvariable=self._stats_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 0))

        self._toast_var = tk.StringVar(value="")
        self._toast_label = tk.Label(
            toolbar,
            textvariable=self._toast_var,
            bg=card_bg,
            fg="#6B7280",
            font=("Segoe UI", 9, "bold"),
        )
        self._toast_label.pack(side="right", padx=(0, 12))

        btn_rescan = ttk.Button(toolbar, text="재스캔 + 적용", command=self._on_rescan)
        btn_rescan.pack(side="right")
        self._controls.append(btn_rescan)

        btn_save = ttk.Button(toolbar, text="저장 + 적용", command=self._on_save)
        btn_save.pack(side="right", padx=(0, 8))
        self._controls.append(btn_save)

        content_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        content_card.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        body = tk.Frame(content_card, bg=card_bg)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        tree_frame = tk.Frame(body, bg=card_bg)
        try:
            body.grid_rowconfigure(0, weight=1)
            body.grid_columnconfigure(0, weight=1)
        except Exception:
            pass
        tree_frame.grid(row=0, column=0, sticky="nsew")

        cols = (
            "enabled",
            "app",
            "running",
            "pid",
            "type",
            "hide",
            "profile",
            "app_id",
            "lnk",
            "exe",
        )
        tree = ttk.Treeview(
            tree_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
            style="WS.Treeview",
        )
        self._tree = tree

        tree.heading("enabled", text="사용")
        tree.heading("app", text="앱")
        tree.heading("running", text="실행 상태")
        tree.heading("pid", text="PID")
        tree.heading("type", text="종류")
        tree.heading("hide", text="동작")
        tree.heading("profile", text="프로필")
        tree.heading("app_id", text="App ID")
        tree.heading("lnk", text="Shortcut")
        tree.heading("exe", text="Exe")

        tree.column("enabled", width=60, minwidth=55, anchor="center", stretch=False)
        tree.column("app", width=200, minwidth=160, anchor="w", stretch=True)
        tree.column("running", width=95, minwidth=80, anchor="center", stretch=False)
        tree.column("pid", width=90, minwidth=70, anchor="center", stretch=False)
        tree.column("type", width=90, minwidth=70, anchor="center", stretch=False)
        tree.column("hide", width=90, minwidth=70, anchor="center", stretch=False)
        tree.column("profile", width=160, minwidth=120, anchor="w", stretch=False)
        tree.column("app_id", width=200, minwidth=160, anchor="w", stretch=False)
        tree.column("lnk", width=200, minwidth=160, anchor="w", stretch=False)
        tree.column("exe", width=220, minwidth=180, anchor="w", stretch=False)

        try:
            tree.tag_configure("odd", background="#F9FAFB")
            tree.tag_configure("disabled", foreground="#9CA3AF")
            tree.tag_configure("running", background="#ECFDF5")
        except Exception:
            pass

        try:
            tree.bind("<Double-1>", self._on_tree_double_click)
            tree.bind("<Button-3>", self._on_tree_right_click)
            tree.bind("<Return>", lambda _e: self._on_edit())
            tree.bind("<Delete>", lambda _e: self._on_delete())
            tree.bind("<space>", lambda _e: self._on_toggle_enabled())
            tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        except Exception:
            pass

        try:
            tree_frame.grid_rowconfigure(0, weight=1)
            tree_frame.grid_columnconfigure(0, weight=1)
        except Exception:
            pass

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        btns = tk.Frame(body, bg=card_bg)
        btns.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        btn_add = ttk.Button(btns, text="추가", command=self._on_add)
        btn_add.pack(side="left")
        self._controls.append(btn_add)

        btn_edit = ttk.Button(btns, text="편집", command=self._on_edit)
        btn_edit.pack(side="left", padx=(8, 0))
        self._controls.append(btn_edit)

        btn_del = ttk.Button(btns, text="삭제", command=self._on_delete)
        btn_del.pack(side="left", padx=(8, 0))
        self._controls.append(btn_del)

        btn_toggle = ttk.Button(btns, text="선택 앱 사용 변경", command=self._on_toggle_enabled)
        btn_toggle.pack(side="left", padx=(8, 0))
        self._controls.append(btn_toggle)
        self._btn_toggle = btn_toggle

        close_cmd = self._on_close if as_toplevel else self._hide_main_window
        ttk.Button(btns, text="닫기", command=close_cmd).pack(side="right")

        if as_toplevel:
            win.protocol("WM_DELETE_WINDOW", self._on_close)
            try:
                win.bind("<Escape>", lambda _e: self._on_close())
            except Exception:
                pass

        self._reload_from_disk()
        self._update_header()
        self._schedule_runtime_refresh()
        return

    def _ensure_style(self) -> None:
        if self._style_ready or self._ttk is None:
            return
        ttk = self._ttk
        try:
            style = ttk.Style()
            names = set(style.theme_names())
            for name in ("vista", "xpnative", "clam"):
                if name in names:
                    style.theme_use(name)
                    break
            style.configure("WS.Treeview", rowheight=28, font=("Segoe UI", 9))
            style.configure("WS.Treeview.Heading", font=("Segoe UI", 9, "bold"))
            style.map(
                "WS.Treeview",
                background=[("selected", "#DBEAFE")],
                foreground=[("selected", "#111827")],
            )
        except Exception:
            pass
        self._style_ready = True
        return

    def _get_global_enabled(self) -> bool:
        try:
            return bool(self._manager.get_enabled_state())
        except Exception:
            try:
                cfg = self._manager.load_config()
                return bool(cfg.get("enabled", True)) if isinstance(cfg, dict) else True
            except Exception:
                return True

    def _update_header(self) -> None:
        pill = self._enabled_pill
        if pill is None:
            return
        enabled = self._get_global_enabled()
        try:
            pill.configure(
                text=("ON" if enabled else "OFF"),
                bg=("#10B981" if enabled else "#9CA3AF"),
            )
        except Exception:
            pass
        return

    def _toast(self, message: str, ok: bool = True, ttl_ms: int = 2400) -> None:
        win = self._win
        v = self._toast_var
        if win is None or v is None:
            return

        label = self._toast_label
        try:
            v.set(str(message or "").strip())
        except Exception:
            return

        if label is not None:
            try:
                label.configure(fg=("#10B981" if ok else "#EF4444"))
            except Exception:
                pass

        try:
            if self._toast_after_id is not None:
                win.after_cancel(self._toast_after_id)
        except Exception:
            pass
        self._toast_after_id = None

        def clear() -> None:
            self._toast_after_id = None
            if self._toast_var is not None:
                try:
                    self._toast_var.set("")
                except Exception:
                    pass
            if self._toast_label is not None:
                try:
                    self._toast_label.configure(fg="#6B7280")
                except Exception:
                    pass
            return

        try:
            self._toast_after_id = win.after(int(max(600, ttl_ms)), clear)
        except Exception:
            self._toast_after_id = None
        return

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        win = self._win
        if win is not None:
            try:
                win.configure(cursor=("watch" if self._busy else ""))
            except Exception:
                pass
        for c in list(self._controls):
            try:
                c.configure(state=("disabled" if self._busy else "normal"))
            except Exception:
                continue
        return

    def _ui_post(self, fn: Any) -> None:
        q = self._ui_queue
        if q is None:
            return
        try:
            q.put(fn)
        except Exception:
            return
        return

    def _start_ui_pump(self) -> None:
        if self._win is None or self._ui_queue is None:
            return
        if self._ui_pump_after_id is not None:
            return
        self._pump_ui_queue()
        return

    def _pump_ui_queue(self) -> None:
        win = self._win
        q = self._ui_queue
        if win is None or q is None:
            return

        while True:
            try:
                fn = q.get_nowait()
            except Exception:
                break
            try:
                fn()
            except Exception:
                pass

        try:
            self._ui_pump_after_id = win.after(50, self._pump_ui_queue)
        except Exception:
            self._ui_pump_after_id = None
        return

    def _run_bg(self, fn: Any, on_done: Any | None = None) -> None:
        if self._busy:
            return
        if self._win is None or self._ui_queue is None:
            return
        self._set_busy(True)

        def worker() -> None:
            ok = True
            err: str | None = None
            try:
                fn()
            except Exception as exc:
                ok = False
                err = str(exc)

            def finish() -> None:
                self._set_busy(False)
                if on_done is not None:
                    try:
                        on_done(ok, err)
                    except Exception:
                        pass
                return

            try:
                self._ui_post(finish)
            except Exception:
                pass
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._set_busy(False)
        return

    def _on_toggle_global_enabled(self) -> None:
        def task() -> None:
            try:
                self._manager.toggle_enabled()
            finally:
                try:
                    self._manager.start(self._root)
                except Exception:
                    pass
            return

        def done(_ok: bool, _err: str | None) -> None:
            self._runtime_cache_ts = 0.0
            self._reload_from_disk()
            self._update_header()
            return

        self._run_bg(task, done)
        return

    def _on_filter_key(self, _event: Any) -> None:
        if self._win is None:
            return
        try:
            if self._filter_after_id is not None:
                self._win.after_cancel(self._filter_after_id)
        except Exception:
            pass
        try:
            self._filter_after_id = self._win.after(180, self._refresh_tree)
        except Exception:
            self._filter_after_id = None
        return

    def _filter_text(self) -> str:
        v = self._filter_var
        if v is None:
            return ""
        try:
            return str(v.get() or "").strip().casefold()
        except Exception:
            return ""

    def _get_runtime_cached(self, force: bool = False) -> dict[str, tuple[bool, int | None]]:
        now = time.monotonic()
        if force or (now - float(self._runtime_cache_ts) >= 1.0):
            self._start_runtime_worker()
        return self._runtime_cache

    def _start_runtime_worker(self) -> None:
        if self._runtime_worker_running or self._ui_queue is None:
            return
        self._runtime_worker_running = True
        instances_snapshot = list(self._instances)

        def worker() -> None:
            runtime: dict[str, tuple[bool, int | None]] = {}
            try:
                runtime = self._manager.get_instances_runtime(instances_snapshot)
            except Exception:
                runtime = {}
            if not isinstance(runtime, dict):
                runtime = {}

            def apply() -> None:
                self._runtime_worker_running = False
                self._runtime_cache = runtime
                self._runtime_cache_ts = float(time.monotonic())
                self._apply_runtime_to_tree(runtime)
                return

            try:
                self._ui_post(apply)
            except Exception:
                self._runtime_worker_running = False
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._runtime_worker_running = False
        return

    def _apply_runtime_to_tree(
        self, runtime: dict[str, tuple[bool, int | None]]
    ) -> None:
        tree = self._tree
        if tree is None:
            return

        try:
            iids = list(tree.get_children())
        except Exception:
            iids = []

        for iid in iids:
            try:
                idx = int(iid)
            except Exception:
                continue
            if idx < 0 or idx >= len(self._instances):
                continue
            inst = self._instances[idx]
            inst_id = str(inst.get("id", "")).strip()
            if not inst_id:
                inst_id = str(inst.get("lnk_path", "")).strip()
            running, pid = runtime.get(inst_id, (False, None))
            running_text = "RUNNING" if running else "STOPPED"
            pid_text = str(pid) if pid else ""

            try:
                values = list(tree.item(iid, "values"))
            except Exception:
                continue
            if len(values) < 4:
                continue
            values[2] = running_text
            values[3] = pid_text
            try:
                tree.item(iid, values=values)
            except Exception:
                pass

            tags: list[str] = []
            if idx % 2 == 1:
                tags.append("odd")
            if not bool(inst.get("enabled", True)):
                tags.append("disabled")
            if running:
                tags.append("running")
            try:
                tree.item(iid, tags=tuple(tags))
            except Exception:
                pass
        return

    def _on_tree_select(self, _event: Any) -> None:
        self._update_toggle_button_text()
        return

    def _update_toggle_button_text(self) -> None:
        btn = self._btn_toggle
        if btn is None:
            return
        idx = self._selected_index()
        if idx is None:
            try:
                btn.configure(text="선택 앱 사용 변경")
            except Exception:
                pass
            return

        try:
            cur = bool(self._instances[idx].get("enabled", True))
        except Exception:
            cur = True

        text = "선택 앱 OFF 변경" if cur else "선택 앱 ON 변경"
        try:
            btn.configure(text=text)
        except Exception:
            pass
        return

    def _on_tree_double_click(self, event: Any) -> None:
        tree = self._tree
        if tree is None:
            return
        try:
            if tree.identify("region", event.x, event.y) != "cell":
                return
            iid = tree.identify_row(event.y)
            if iid:
                tree.selection_set(iid)
        except Exception:
            pass
        self._on_edit()
        return

    def _ensure_context_menu(self) -> Any:
        if self._context_menu is not None or self._tk is None or self._win is None:
            return self._context_menu
        tk = self._tk
        menu = tk.Menu(self._win, tearoff=0)
        menu.add_command(label="편집...", command=self._on_edit)
        menu.add_command(label="선택 앱 사용 변경", command=self._on_toggle_enabled)
        menu.add_separator()
        menu.add_command(label="삭제", command=self._on_delete)
        menu.add_separator()
        menu.add_command(label="바로가기(.lnk) 열기", command=self._open_selected_lnk)
        menu.add_command(label="Exe 폴더 열기", command=self._open_selected_exe_dir)
        menu.add_separator()
        menu.add_command(label="창 표시(Show)", command=lambda: self._on_window_action("show"))
        menu.add_command(label="창 숨기기(Hide)", command=lambda: self._on_window_action("hide"))
        menu.add_command(
            label="창 최소화(Minimize)", command=lambda: self._on_window_action("minimize")
        )
        menu.add_command(label="창 닫기(Close)", command=lambda: self._on_window_action("close"))
        self._context_menu = menu
        return menu

    def _on_tree_right_click(self, event: Any) -> None:
        tree = self._tree
        if tree is None:
            return
        try:
            iid = tree.identify_row(event.y)
            if iid:
                tree.selection_set(iid)
        except Exception:
            pass
        menu = self._ensure_context_menu()
        if menu is None:
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        try:
            menu.grab_release()
        except Exception:
            pass
        return

    def _on_window_action(self, action: str) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        try:
            inst_snapshot = dict(self._instances[idx])
        except Exception:
            return

        def task() -> None:
            try:
                self._manager.apply_instance_window_action(inst_snapshot, action)
            except Exception:
                pass
            return

        def done(_ok: bool, _err: str | None) -> None:
            self._runtime_cache_ts = 0.0
            self._get_runtime_cached(force=True)
            return

        self._run_bg(task, done)
        return

    def _open_selected_lnk(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        try:
            path = str(self._instances[idx].get("lnk_path", "") or "").strip()
        except Exception:
            path = ""
        if not path:
            return
        try:
            os.startfile(path)
        except Exception:
            return
        return

    def _open_selected_exe_dir(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        try:
            path = str(self._instances[idx].get("exe", "") or "").strip()
        except Exception:
            path = ""
        if not path:
            return
        try:
            folder = os.path.dirname(path)
        except Exception:
            folder = ""
        if not folder:
            return
        try:
            os.startfile(folder)
        except Exception:
            return
        return

    def _resolve_icon_path(self) -> str | None:
        if self._icon_path_resolved:
            return self._icon_path
        self._icon_path_resolved = True

        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            candidate = os.path.abspath(
                os.path.join(base_dir, "..", "utils", "windows_supporter.ico")
            )
            if os.path.isfile(candidate):
                self._icon_path = candidate
                return self._icon_path
        except Exception:
            pass

        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            try:
                candidate = os.path.join(meipass, "src", "utils", "windows_supporter.ico")
                if os.path.isfile(candidate):
                    self._icon_path = candidate
                    return self._icon_path
            except Exception:
                pass

        self._icon_path = None
        return None

    def _apply_window_icon(self, win: Any) -> None:
        path = self._resolve_icon_path()
        if not path:
            return
        try:
            win.iconbitmap(path)
        except Exception:
            return
        return

    def _lazy_import_tk(self) -> None:
        if self._tk is not None:
            return
        try:
            import tkinter as tk
            from tkinter import filedialog, messagebox, ttk
        except Exception:
            self._tk = None
            self._ttk = None
            self._messagebox = None
            self._filedialog = None
            return
        self._tk = tk
        self._ttk = ttk
        self._messagebox = messagebox
        self._filedialog = filedialog
        return

    def _reload_from_disk(self) -> None:
        try:
            cfg = self._manager.load_config()
        except Exception:
            cfg = {}

        instances = cfg.get("instances", [])
        if not isinstance(instances, list):
            instances = []
        self._instances = [i for i in instances if isinstance(i, dict)]
        self._refresh_tree()
        self._schedule_runtime_refresh()
        return

    def _refresh_tree(self) -> None:
        tree = self._tree
        if tree is None:
            return

        try:
            for iid in tree.get_children():
                tree.delete(iid)
        except Exception:
            pass

        runtime = self._get_runtime_cached(force=False)
        q = self._filter_text()

        total = 0
        shown = 0
        running_cnt = 0

        for idx, inst in enumerate(self._instances):
            total += 1
            enabled = "ON" if bool(inst.get("enabled", True)) else "OFF"
            inst_id = str(inst.get("id", "")).strip()
            if not inst_id:
                inst_id = str(inst.get("lnk_path", "")).strip()
            running, pid = runtime.get(inst_id, (False, None))
            if running:
                running_cnt += 1
            running_text = "RUNNING" if running else "STOPPED"
            pid_text = str(pid) if pid else ""
            app = str(inst.get("app", "") or "")
            typ = str(inst.get("type", "") or "")
            profile = str(inst.get("profile_directory", "") or "")
            app_id = str(inst.get("app_id", "") or "")
            hide = str(inst.get("hide_action", "") or "")
            lnk = os.path.basename(str(inst.get("lnk_path", "") or ""))
            exe = os.path.basename(str(inst.get("exe", "") or ""))

            if q:
                hay = (
                    f"{app}\n{typ}\n{profile}\n{app_id}\n{hide}\n{lnk}\n{exe}\n"
                    f"{str(inst.get('name', '') or '')}"
                ).casefold()
                if q not in hay:
                    continue

            tags: list[str] = []
            if idx % 2 == 1:
                tags.append("odd")
            if not bool(inst.get("enabled", True)):
                tags.append("disabled")
            if running:
                tags.append("running")

            tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    enabled,
                    app,
                    running_text,
                    pid_text,
                    typ,
                    hide,
                    profile,
                    app_id,
                    lnk,
                    exe,
                ),
                tags=tuple(tags),
            )
            shown += 1

        if self._stats_var is not None:
            try:
                self._stats_var.set(f"{shown}개 표시 / 총 {total}개 · 실행중 {running_cnt}개")
            except Exception:
                pass
        self._update_toggle_button_text()
        return

    def _selected_index(self) -> int | None:
        tree = self._tree
        if tree is None:
            return None
        try:
            sel = tree.selection()
            if not sel:
                return None
            return int(sel[0])
        except Exception:
            return None

    def _on_close(self) -> None:
        self._busy = False
        self._runtime_worker_running = False
        try:
            if self._runtime_after_id is not None and self._win is not None:
                self._win.after_cancel(self._runtime_after_id)
        except Exception:
            pass
        self._runtime_after_id = None
        try:
            if self._toast_after_id is not None and self._win is not None:
                self._win.after_cancel(self._toast_after_id)
        except Exception:
            pass
        self._toast_after_id = None
        try:
            if self._filter_after_id is not None and self._win is not None:
                self._win.after_cancel(self._filter_after_id)
        except Exception:
            pass
        self._filter_after_id = None
        try:
            if self._ui_pump_after_id is not None and self._win is not None:
                self._win.after_cancel(self._ui_pump_after_id)
        except Exception:
            pass
        self._ui_pump_after_id = None
        self._ui_queue = None
        try:
            if self._win is not None:
                self._win.destroy()
        except Exception:
            pass
        self._win = None
        self._embedded = False
        self._context_menu = None
        self._controls = []
        self._btn_toggle = None
        return

    def _schedule_runtime_refresh(self) -> None:
        if self._win is None:
            return
        try:
            if self._runtime_after_id is not None:
                self._win.after_cancel(self._runtime_after_id)
        except Exception:
            pass
        try:
            self._runtime_after_id = self._win.after(1500, self._refresh_runtime_only)
        except Exception:
            self._runtime_after_id = None
        return

    def _refresh_runtime_only(self) -> None:
        if not self.is_open():
            return
        try:
            if self._win is not None and (not bool(self._win.winfo_viewable())):
                self._runtime_after_id = self._win.after(2000, self._refresh_runtime_only)
                return
        except Exception:
            pass
        self._get_runtime_cached(force=True)

        try:
            self._runtime_after_id = self._win.after(5000, self._refresh_runtime_only)
        except Exception:
            self._runtime_after_id = None
        return

    def _on_save(self) -> None:
        instances_snapshot = [dict(i) for i in self._instances]

        def task() -> None:
            try:
                cfg = self._manager.load_config()
            except Exception:
                cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            cfg["instances"] = instances_snapshot
            self._manager.save_config(cfg)
            try:
                self._manager.start(self._root)
            except Exception:
                pass
            return

        def done(ok: bool, _err: str | None) -> None:
            self._runtime_cache_ts = 0.0
            self._reload_from_disk()
            self._update_header()
            self._toast("저장 완료" if ok else "저장 실패", ok=bool(ok))
            return

        self._run_bg(task, done)
        return

    def _on_rescan(self) -> None:
        def task() -> None:
            self._manager.rescan_defaults_merge()
            try:
                self._manager.start(self._root)
            except Exception:
                pass
            return

        def done(_ok: bool, _err: str | None) -> None:
            self._runtime_cache_ts = 0.0
            self._reload_from_disk()
            self._update_header()
            return

        self._run_bg(task, done)
        return

    def _on_toggle_enabled(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        try:
            cur = bool(self._instances[idx].get("enabled", True))
            self._instances[idx]["enabled"] = (not cur)
        except Exception:
            return
        self._refresh_tree()
        return

    def _on_delete(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        try:
            del self._instances[idx]
        except Exception:
            return
        self._refresh_tree()
        return

    def _on_add(self) -> None:
        self._lazy_import_tk()
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        if self._win is None:
            return

        dialog = tk.Toplevel(self._win)
        dialog.title("인스턴스 추가")
        self._apply_window_icon(dialog)
        dialog.transient(self._win)
        try:
            dialog.attributes("-topmost", True)
        except Exception:
            pass
        dialog.grab_set()

        data: dict[str, Any] = {
            "id": f"custom:{len(self._instances) + 1}",
            "type": "chrome_pwa",
            "app": "Custom",
            "name": "Custom",
            "enabled": True,
            "hide_action": "hide",
            "lnk_path": "",
            "exe": "",
            "raw_args": "",
            "profile_directory": "",
            "app_id": "",
            "extra_args": [],
            "window_title_regex": "",
        }

        self._edit_dialog(dialog, data)
        self._center_on_parent(dialog, self._win)
        try:
            dialog.lift()
            dialog.focus_force()
        except Exception:
            pass
        try:
            dialog.wait_window()
        except Exception:
            pass

        if data.get("_ok"):
            data.pop("_ok", None)
            self._instances.append(data)
            self._refresh_tree()
        return

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return

        self._lazy_import_tk()
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        if self._win is None:
            return

        original = self._instances[idx]
        data = dict(original)

        dialog = tk.Toplevel(self._win)
        dialog.title("인스턴스 편집")
        self._apply_window_icon(dialog)
        dialog.transient(self._win)
        try:
            dialog.attributes("-topmost", True)
        except Exception:
            pass
        dialog.grab_set()

        self._edit_dialog(dialog, data)
        self._center_on_parent(dialog, self._win)
        try:
            dialog.lift()
            dialog.focus_force()
        except Exception:
            pass
        try:
            dialog.wait_window()
        except Exception:
            pass

        if data.get("_ok"):
            data.pop("_ok", None)
            self._instances[idx] = data
            self._refresh_tree()
        return

    def _edit_dialog(self, dialog: Any, data: dict[str, Any]) -> None:
        ttk = self._ttk
        tk = self._tk
        filedialog = self._filedialog
        if ttk is None or tk is None:
            return

        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        try:
            dialog.configure(bg=bg)
        except Exception:
            pass

        outer = tk.Frame(dialog, bg=bg)
        outer.pack(fill="both", expand=True)

        card = tk.Frame(
            outer,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        card.pack(fill="both", expand=True, padx=12, pady=12)

        content = ttk.Frame(card, padding=12)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)

        row = 0

        def add_label(text: str) -> None:
            nonlocal row
            ttk.Label(content, text=text).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=4
            )

        def add_entry(var) -> None:
            nonlocal row
            e = ttk.Entry(content, textvariable=var, width=80)
            e.grid(row=row, column=1, sticky="we", pady=4)

        enabled_var = tk.BooleanVar(value=bool(data.get("enabled", True)))
        type_var = tk.StringVar(value=str(data.get("type", "chrome_pwa")))
        app_var = tk.StringVar(value=str(data.get("app", "")))
        name_var = tk.StringVar(value=str(data.get("name", "")))
        hide_var = tk.StringVar(value=str(data.get("hide_action", "hide")))
        lnk_var = tk.StringVar(value=str(data.get("lnk_path", "")))
        exe_var = tk.StringVar(value=str(data.get("exe", "")))
        profile_var = tk.StringVar(value=str(data.get("profile_directory", "")))
        appid_var = tk.StringVar(value=str(data.get("app_id", "")))
        args_var = tk.StringVar(value=str(data.get("raw_args", "")))
        title_var = tk.StringVar(value=str(data.get("window_title_regex", "")))

        ttk.Checkbutton(content, text="사용(Enabled)", variable=enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1

        add_label("Type")
        ttk.Combobox(
            content,
            textvariable=type_var,
            values=("chrome_pwa", "exe"),
            state="readonly",
        ).grid(row=row, column=1, sticky="we", pady=4)
        row += 1

        add_label("App")
        add_entry(app_var)
        row += 1

        add_label("Name")
        add_entry(name_var)
        row += 1

        add_label("Hide Action")
        ttk.Combobox(
            content,
            textvariable=hide_var,
            values=("hide", "minimize", "show"),
            state="readonly",
        ).grid(row=row, column=1, sticky="we", pady=4)
        row += 1

        add_label("Shortcut (.lnk)")
        add_entry(lnk_var)
        ttk.Button(
            content,
            text="찾기...",
            command=lambda: self._pick_file(
                filedialog,
                lnk_var,
                [("Shortcut", "*.lnk")],
            ),
        ).grid(row=row, column=2, sticky="w", pady=4)
        row += 1

        add_label("Exe")
        add_entry(exe_var)
        ttk.Button(
            content,
            text="찾기...",
            command=lambda: self._pick_file(
                filedialog,
                exe_var,
                [("Executable", "*.exe"), ("All", "*.*")],
            ),
        ).grid(row=row, column=2, sticky="w", pady=4)
        row += 1

        add_label("Raw Args")
        add_entry(args_var)
        row += 1

        add_label("Profile Directory")
        add_entry(profile_var)
        row += 1

        add_label("App ID")
        add_entry(appid_var)
        row += 1

        add_label("Window Title Regex (옵션)")
        add_entry(title_var)
        row += 1

        def extract_from_lnk() -> None:
            try:
                info = self._manager.read_shortcut_public(lnk_var.get())
            except Exception:
                info = None
            if not info:
                return
            exe_var.set(str(info.get("target", "") or ""))
            args_var.set(str(info.get("args", "") or ""))
            try:
                prof, aid, extra = self._manager.parse_chrome_pwa_args_public(
                    args_var.get()
                )
            except Exception:
                prof, aid, extra = (None, None, [])
            if prof is not None:
                profile_var.set(str(prof))
            if aid is not None:
                appid_var.set(str(aid))
            if extra:
                try:
                    data["extra_args"] = list(extra)
                except Exception:
                    pass
            return

        ttk.Button(content, text="바로가기에서 추출", command=extract_from_lnk).grid(
            row=row, column=0, sticky="w", pady=(6, 12)
        )

        def on_ok() -> None:
            data["_ok"] = True
            data["enabled"] = bool(enabled_var.get())
            data["type"] = str(type_var.get()).strip() or "exe"
            data["app"] = str(app_var.get()).strip()
            data["name"] = str(name_var.get()).strip()
            data["hide_action"] = str(hide_var.get()).strip() or "hide"
            data["lnk_path"] = str(lnk_var.get()).strip()
            data["exe"] = str(exe_var.get()).strip()
            data["raw_args"] = str(args_var.get()).strip()
            data["profile_directory"] = str(profile_var.get()).strip()
            data["app_id"] = str(appid_var.get()).strip()
            data["window_title_regex"] = str(title_var.get()).strip()
            try:
                dialog.destroy()
            except Exception:
                pass
            return

        def on_cancel() -> None:
            data["_ok"] = False
            try:
                dialog.destroy()
            except Exception:
                pass
            return

        try:
            dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        except Exception:
            pass
        try:
            dialog.bind("<Escape>", lambda _e: on_cancel())
        except Exception:
            pass
        try:
            dialog.bind("<Return>", lambda _e: on_ok())
        except Exception:
            pass

        btn_row = ttk.Frame(content)
        btn_row.grid(row=row, column=0, columnspan=3, sticky="e")
        ttk.Button(btn_row, text="취소", command=on_cancel).pack(side="right")
        ttk.Button(btn_row, text="확인", command=on_ok).pack(
            side="right", padx=(0, 8)
        )
        return

    def _center_on_parent(self, child: Any, parent: Any) -> None:
        try:
            parent.update_idletasks()
        except Exception:
            pass
        try:
            child.update_idletasks()
        except Exception:
            pass

        try:
            pw = int(parent.winfo_width())
            ph = int(parent.winfo_height())
            px = int(parent.winfo_rootx())
            py = int(parent.winfo_rooty())
        except Exception:
            pw = ph = 0
            px = py = 0

        try:
            cw = int(child.winfo_reqwidth())
            ch = int(child.winfo_reqheight())
        except Exception:
            cw = ch = 0

        try:
            sw = int(child.winfo_screenwidth())
            sh = int(child.winfo_screenheight())
        except Exception:
            sw = sh = 0

        if pw <= 1 or ph <= 1 or sw <= 0 or sh <= 0 or cw <= 0 or ch <= 0:
            return

        x = px + (pw - cw) // 2
        y = py + (ph - ch) // 2

        if x < 0:
            x = 0
        if y < 0:
            y = 0
        if x + cw > sw:
            x = max(0, sw - cw)
        if y + ch > sh:
            y = max(0, sh - ch)

        try:
            child.geometry(f"{cw}x{ch}+{x}+{y}")
        except Exception:
            return
        return

    def _pick_file(self, filedialog: Any, var: Any, types: list[tuple[str, str]]) -> None:
        if filedialog is None:
            return
        try:
            path = filedialog.askopenfilename(filetypes=types)
        except Exception:
            path = ""
        if path:
            try:
                var.set(path)
            except Exception:
                pass
        return

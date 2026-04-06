from __future__ import annotations

from typing import Any
import threading


class WrikeSettingsView:
    def __init__(self, root: Any, wrike: Any) -> None:
        self._root = root
        self._wrike = wrike

        self._tk = None
        self._ttk = None
        self._win = None

        self._token_var = None
        self._daily_var = None
        self._tooltip_var = None
        self._monitor_enabled_var = None
        self._monitor_interval_var = None
        self._status_var = None
        self._status_label = None
        self._show_token_var = None
        self._token_entry = None
        self._folder_path_frame = None
        self._folder_levels: list[dict] = []
        self._folder_path_label = None
        self._folder_restoring = False
        self._status_colors = {
            "info": "#6B7280",
            "ok": "#10B981",
            "error": "#DC2626",
        }
        return

    def mount(self, parent: Any) -> None:
        if parent is None:
            return
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

        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        text_muted = "#6B7280"

        container = tk.Frame(parent, bg=bg)
        try:
            container.pack(fill="both", expand=True)
        except Exception:
            return
        self._win = container

        self._status_var = tk.StringVar(value="")

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
            text="Wrike Timelog Monitoring 설정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        btn_row = tk.Frame(title_row, bg=card_bg)
        btn_row.pack(side="right")
        ttk.Button(btn_row, text="저장", command=self._on_save).pack(side="right")
        ttk.Button(btn_row, text="토큰 지우기", command=self._on_clear_token).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(btn_row, text="로드하기", command=self._on_reload).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(btn_row, text="토큰 검증", command=self._on_validate_token).pack(
            side="right", padx=(0, 8)
        )

        tk.Label(
            header_inner,
            text="API 키/일 목표/모니터링을 설정합니다.",
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(6, 0))

        self._status_label = tk.Label(
            header_inner,
            textvariable=self._status_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        )
        self._status_label.pack(anchor="w", pady=(6, 0))

        content_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        content_card.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        body = tk.Frame(content_card, bg=card_bg)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        content = tk.Frame(body, bg=card_bg)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)

        self._token_var = tk.StringVar(value="")
        self._daily_var = tk.StringVar(value="")
        self._tooltip_var = tk.StringVar(value="")
        self._monitor_enabled_var = tk.BooleanVar(value=False)
        self._monitor_interval_var = tk.StringVar(value="")
        self._show_token_var = tk.BooleanVar(value=False)

        row = 0

        def add_label(text: str) -> None:
            nonlocal row
            tk.Label(
                content,
                text=text,
                bg=card_bg,
                fg="#111827",
                font=("Segoe UI", 9),
            ).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=6
            )

        def add_entry(var, show: str | None = None):
            nonlocal row
            entry = ttk.Entry(content, textvariable=var, width=50)
            if show:
                try:
                    entry.configure(show=show)
                except Exception:
                    pass
            entry.grid(row=row, column=1, sticky="we", pady=6)
            return entry

        add_label("API 토큰")
        self._token_entry = add_entry(self._token_var, show="*")
        tk.Checkbutton(
            content,
            text="표시",
            variable=self._show_token_var,
            command=self._toggle_token_visibility,
            bg=card_bg,
            fg="#111827",
            activebackground=card_bg,
            activeforeground="#111827",
            selectcolor=card_bg,
            font=("Segoe UI", 9),
        ).grid(row=row, column=2, sticky="w", padx=(8, 0))
        row += 1

        add_label("일 목표 시간(시간)")
        add_entry(self._daily_var)
        row += 1

        add_label("툴팁 표시 시간(초)")
        add_entry(self._tooltip_var)
        row += 1

        add_label("모니터링 활성화")
        tk.Checkbutton(
            content,
            variable=self._monitor_enabled_var,
            bg=card_bg,
            activebackground=card_bg,
            selectcolor=card_bg,
            fg="#111827",
            activeforeground="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=1, sticky="w", pady=6)
        row += 1

        add_label("모니터링 주기(초)")
        add_entry(self._monitor_interval_var)
        row += 1

        tk.Label(
            content,
            text="모니터링 폴더",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=6)

        folder_outer = tk.Frame(content, bg=card_bg)
        folder_outer.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=6)
        content.rowconfigure(row, weight=1)
        row += 1

        self._folder_path_frame = tk.Frame(folder_outer, bg=card_bg)
        self._folder_path_frame.pack(fill="both", expand=True)

        self._folder_path_label = tk.Label(
            folder_outer,
            text="",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 8),
            anchor="w",
            wraplength=600,
            justify="left",
        )
        self._folder_path_label.pack(fill="x", pady=(4, 0))

        folder_btn_row = tk.Frame(folder_outer, bg=card_bg)
        folder_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(
            folder_btn_row, text="경로 저장", command=self._on_save_folder_path
        ).pack(side="left")
        ttk.Button(
            folder_btn_row, text="경로 초기화", command=self._on_clear_folder_path
        ).pack(side="left", padx=(4, 0))
        ttk.Button(
            folder_btn_row, text="캐시 새로고침", command=self._on_refresh_cache
        ).pack(side="left", padx=(4, 0))
        tk.Label(
            folder_btn_row,
            text="비워두면 전체 타임로그를 조회합니다.",
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(8, 0))

        path = ""
        try:
            path = str(self._wrike.get_settings_snapshot().get("settings_path", ""))
        except Exception:
            path = ""
        path_label = tk.Label(
            content,
            text=f"저장 위치: {path}" if path else "저장 위치: (알 수 없음)",
            bg=card_bg,
            fg="#2563EB" if path else text_muted,
            font=("Segoe UI", 9),
            cursor="hand2" if path else "",
        )
        path_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 4))
        if path:
            try:
                path_label.bind("<Button-1>", lambda _e: self._open_settings_file(path))
            except Exception:
                pass
        row += 1

        self._load_settings()
        try:
            if self._win is not None:
                self._win.after(120, self._auto_validate_token)
        except Exception:
            pass
        return

    def _lazy_import_tk(self) -> None:
        if self._tk is not None and self._ttk is not None:
            return
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            self._tk = None
            self._ttk = None
            return
        self._tk = tk
        self._ttk = ttk
        return

    def _toggle_token_visibility(self) -> None:
        entry = self._token_entry
        if entry is None or self._show_token_var is None:
            return
        show = "" if bool(self._show_token_var.get()) else "*"
        try:
            entry.configure(show=show)
        except Exception:
            pass
        return

    def _format_hours(self, minutes: int) -> str:
        try:
            minutes = int(minutes)
        except Exception:
            return "0"
        if minutes <= 0:
            return "0"
        hours = minutes / 60.0
        if abs(hours - int(hours)) < 1e-6:
            return str(int(hours))
        return f"{hours:.2f}".rstrip("0").rstrip(".")

    def _format_seconds(self, seconds: float) -> str:
        try:
            seconds = float(seconds)
        except Exception:
            return "0"
        if seconds <= 0:
            return "0"
        if abs(seconds - int(seconds)) < 1e-6:
            return str(int(seconds))
        return f"{seconds:.1f}".rstrip("0").rstrip(".")

    def _load_settings(self) -> None:
        try:
            settings = self._wrike.get_settings_snapshot()
        except Exception:
            settings = {}
        try:
            self._token_var.set(str(settings.get("api_token", "") or ""))
        except Exception:
            pass
        try:
            minutes = int(settings.get("daily_target_minutes", 480))
            self._daily_var.set(self._format_hours(minutes))
        except Exception:
            pass
        try:
            tooltip_ms = int(settings.get("tooltip_duration_ms", 6000))
            self._tooltip_var.set(self._format_seconds(tooltip_ms / 1000.0))
        except Exception:
            pass
        try:
            self._monitor_enabled_var.set(bool(settings.get("monitor_enabled", False)))
        except Exception:
            pass
        try:
            interval = float(settings.get("monitor_interval_sec", 5))
            self._monitor_interval_var.set(str(int(interval)))
        except Exception:
            pass
        try:
            self._status_var.set("")
        except Exception:
            pass
        self._toggle_token_visibility()
        self._restore_folder_path()
        return

    def _on_clear_token(self) -> None:
        try:
            self._token_var.set("")
        except Exception:
            pass
        self._on_save()
        return

    def _on_reload(self) -> None:
        try:
            self._set_status("로드 중...", level="info")
        except Exception:
            pass
        ok = False
        msg = None
        try:
            ok, msg = self._wrike.reload_settings_from_disk()
        except Exception:
            ok = False
            msg = None
        self._load_settings()
        if msg:
            if "복구" in msg or "실패" in msg:
                self._set_status(str(msg), level="error")
            else:
                self._set_status(str(msg), level="info")
        else:
            self._set_status("로드 완료" if ok else "로드 실패", level=("ok" if ok else "error"))
        return

    def _open_settings_file(self, path: str) -> None:
        try:
            import os
            if path and os.path.isfile(path):
                os.startfile(path)
        except Exception:
            return

    def _on_validate_token(self) -> None:
        token = str(self._token_var.get() or "").strip()
        if not token:
            self._set_status("API 토큰을 입력하세요", level="error")
            return
        self._set_status("토큰 검증 중...", level="info")

        def worker() -> tuple[bool, str | None, str | None]:
            try:
                return self._wrike.validate_api_token(token)
            except Exception:
                return False, None, "토큰 검증 실패"

        def apply_result(result: tuple[bool, str | None, str | None]) -> None:
            ok_val, name_val, msg_val = result
            if ok_val:
                label = name_val or "내 계정"
                self._set_status(f"{label}님 어서오세요.", level="ok")
            else:
                self._set_status(str(msg_val or "토큰 검증 실패"), level="error")

        self._run_bg(worker, apply_result)
        return

    def _auto_validate_token(self) -> None:
        token = str(self._token_var.get() or "").strip()
        if not token:
            return
        self._on_validate_token()
        return

    def _run_bg(self, fn, on_done) -> None:
        win = self._win
        if win is None:
            return

        def worker() -> None:
            result = fn()
            try:
                win.after(0, lambda: on_done(result))
            except Exception:
                return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            return

    def _on_save(self) -> None:
        token = str(self._token_var.get() or "").strip()
        daily_text = str(self._daily_var.get() or "").strip()
        tooltip_text = str(self._tooltip_var.get() or "").strip()
        monitor_enabled = bool(self._monitor_enabled_var.get())
        interval_text = str(self._monitor_interval_var.get() or "").strip()

        daily_minutes = self._parse_hours_to_minutes(daily_text)
        tooltip_ms = self._parse_seconds_to_ms(tooltip_text)
        interval_sec = self._parse_seconds(interval_text)

        ok, err = self._wrike.update_settings(
            {
                "api_token": token,
                "daily_target_minutes": daily_minutes,
                "tooltip_duration_ms": tooltip_ms,
                "monitor_enabled": monitor_enabled,
                "monitor_interval_sec": interval_sec,
            }
        )
        try:
            if ok:
                self._set_status("저장 완료", level="ok")
                self._hide_main_ui()
            else:
                self._set_status(f"저장 실패: {err}", level="error")
        except Exception:
            pass
        return

    def _set_status(self, text: str, level: str = "info") -> None:
        label = self._status_label
        if label is None or self._status_var is None:
            return
        try:
            self._status_var.set(str(text or ""))
        except Exception:
            return
        color = self._status_colors.get(level, self._status_colors["info"])
        try:
            label.configure(fg=color)
        except Exception:
            pass
        return

    def _hide_main_ui(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            ui = getattr(root, "_ws_main_ui", None)
        except Exception:
            ui = None
        if ui is not None:
            try:
                ui.hide()
                return
            except Exception:
                pass
        try:
            root.withdraw()
        except Exception:
            pass
        return

    def _parse_hours_to_minutes(self, text: str) -> int:
        if not text:
            return 480
        try:
            hours = float(text)
        except Exception:
            return 480
        if hours <= 0:
            return 480
        return int(round(hours * 60))

    def _parse_seconds_to_ms(self, text: str) -> int:
        if not text:
            return 6000
        try:
            seconds = float(text)
        except Exception:
            return 6000
        if seconds <= 0:
            return 6000
        return int(round(seconds * 1000))

    def _parse_seconds(self, text: str) -> float:
        if not text:
            return 5.0
        try:
            seconds = float(text)
        except Exception:
            return 5.0
        if seconds <= 0:
            return 5.0
        return seconds

    def _restore_folder_path(self) -> None:
        try:
            saved_path = self._wrike.get_monitor_folder_path()
        except Exception:
            saved_path = []
        self._clear_folder_levels(0)
        self._update_folder_path_label()
        if not saved_path:
            self._load_folder_level(0, None, saved_path)
            return
        self._folder_restoring = True
        self._load_folder_level(0, None, saved_path)
        return

    def _load_folder_level(
        self, level: int, parent_id: str | None, saved_path: list[dict] | None = None,
    ) -> None:
        is_space_level = (level == 0)

        def worker():
            if is_space_level:
                return self._wrike.fetch_spaces()
            return self._wrike.fetch_child_folders(str(parent_id or ""))

        def on_done(result):
            items, error = result
            if error:
                self._set_status(str(error), level="error")
                self._folder_restoring = False
                return
            if not items:
                self._folder_restoring = False
                return
            self._add_folder_combo(level, items, saved_path)

        self._run_bg(worker, on_done)
        return

    def _add_folder_combo(
        self, level: int, items: list[dict], saved_path: list[dict] | None = None,
    ) -> None:
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return
        frame = self._folder_path_frame
        if frame is None:
            return

        combo_frame = tk.Frame(frame, bg="#FFFFFF")
        combo_frame.pack(fill="x", pady=1)

        label_text = "스페이스:" if level == 0 else f"레벨 {level}:"
        tk.Label(
            combo_frame,
            text=label_text,
            bg="#FFFFFF",
            fg="#6B7280",
            font=("Segoe UI", 8),
            width=8,
            anchor="e",
        ).pack(side="left", padx=(0, 4))

        titles = [str(item.get("title") or "") for item in items]
        combo = ttk.Combobox(
            combo_frame,
            values=titles,
            state="readonly",
            width=50,
            font=("Segoe UI", 9),
        )
        combo.pack(side="left", fill="x", expand=True)

        suggest_idx = self._wrike.suggest_folder_index(items)

        level_info = {
            "frame": combo_frame,
            "combo": combo,
            "items": items,
            "level": level,
        }
        self._folder_levels.append(level_info)

        def on_select(_event=None):
            sel = combo.current()
            if sel < 0:
                return
            self._clear_folder_levels(level + 1)
            self._update_folder_path_label()
            selected = items[sel]
            has_children = bool(selected.get("has_children", True))
            if selected.get("type") == "space" or has_children:
                self._load_folder_level(level + 1, selected["id"])

        combo.bind("<<ComboboxSelected>>", on_select)

        pre_select_idx = None
        if saved_path and level < len(saved_path):
            saved_id = str(saved_path[level].get("id") or "")
            for i, item in enumerate(items):
                if str(item.get("id") or "") == saved_id:
                    pre_select_idx = i
                    break
            if pre_select_idx is None and suggest_idx is not None:
                pre_select_idx = suggest_idx
        elif suggest_idx is not None:
            pre_select_idx = suggest_idx

        if suggest_idx is not None and suggest_idx < len(titles):
            tag = " ← 추천"
            current_title = titles[suggest_idx]
            if tag not in current_title:
                titles[suggest_idx] = current_title + tag
                combo["values"] = titles

        if pre_select_idx is not None and 0 <= pre_select_idx < len(items):
            combo.current(pre_select_idx)
            selected = items[pre_select_idx]
            has_children = bool(selected.get("has_children", True))
            if selected.get("type") == "space" or has_children:
                next_saved = saved_path if (saved_path and level + 1 < len(saved_path)) else None
                self._load_folder_level(level + 1, selected["id"], next_saved)
            else:
                self._folder_restoring = False
                self._update_folder_path_label()
        else:
            self._folder_restoring = False
            self._update_folder_path_label()
        return

    def _clear_folder_levels(self, from_level: int) -> None:
        while len(self._folder_levels) > from_level:
            info = self._folder_levels.pop()
            try:
                info["frame"].destroy()
            except Exception:
                pass
        return

    def _get_current_path(self) -> list[dict]:
        path: list[dict] = []
        for info in self._folder_levels:
            combo = info.get("combo")
            items = info.get("items", [])
            if combo is None:
                break
            try:
                idx = combo.current()
            except Exception:
                break
            if idx < 0 or idx >= len(items):
                break
            item = items[idx]
            path.append({
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "type": str(item.get("type") or "folder"),
            })
        return path

    def _update_folder_path_label(self) -> None:
        label = self._folder_path_label
        if label is None:
            return
        path = self._get_current_path()
        if not path:
            try:
                label.configure(text="경로 미선택 (전체 타임로그 조회)")
            except Exception:
                pass
            return
        names = [str(p.get("title") or "?") for p in path]
        text = " / ".join(names)
        try:
            label.configure(text=text)
        except Exception:
            pass
        return

    def _on_save_folder_path(self) -> None:
        path = self._get_current_path()
        try:
            self._wrike.set_monitor_folder_path(path)
        except Exception:
            self._set_status("경로 저장 실패", level="error")
            return
        if path:
            self._set_status("폴더 경로 저장 완료", level="ok")
        else:
            self._set_status("폴더 경로 초기화됨 (전체 조회)", level="ok")
        return

    def _on_clear_folder_path(self) -> None:
        try:
            self._wrike.clear_monitor_folder_path()
        except Exception:
            pass
        self._clear_folder_levels(0)
        self._update_folder_path_label()
        self._load_folder_level(0, None, None)
        self._set_status("폴더 경로 초기화 완료", level="ok")
        return

    def _on_refresh_cache(self) -> None:
        try:
            self._wrike.invalidate_folder_cache()
        except Exception:
            pass
        saved_path = None
        try:
            saved_path = self._wrike.get_monitor_folder_path()
        except Exception:
            pass
        self._clear_folder_levels(0)
        self._load_folder_level(0, None, saved_path or [])
        self._set_status("캐시 새로고침 완료", level="ok")
        return

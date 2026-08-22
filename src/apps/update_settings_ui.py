from __future__ import annotations

from typing import Any

from src.utils.update_monitor import format_update_status_parts
from src.utils.update_settings import MAX_CHECK_INTERVAL_MINUTES, MIN_CHECK_INTERVAL_MINUTES


class UpdateSettingsView:
    def __init__(self, root: Any, updater: Any) -> None:
        self._root = root
        self._updater = updater
        self._parent = None
        self._tk = None
        self._ttk = None
        self._enabled_var = None
        self._interval_var = None
        self._status_label = None
        self._loading = False
        return

    def mount(self, parent: Any) -> None:
        self._parent = parent
        if not self._lazy_import_tk():
            return
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return
        try:
            for child in list(parent.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    continue
        except Exception:
            pass

        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        text = "#111827"
        muted = "#6B7280"
        try:
            parent.configure(bg=bg)
        except Exception:
            pass

        container = tk.Frame(parent, bg=bg)
        container.pack(fill="both", expand=True)
        card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        card.pack(fill="both", expand=True, padx=12, pady=12)
        body = tk.Frame(card, bg=card_bg)
        body.pack(fill="x", padx=14, pady=12, anchor="n")
        body.columnconfigure(1, weight=1)

        tk.Label(
            body,
            text="Update",
            bg=card_bg,
            fg=text,
            font=("Segoe UI", 14, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(
            body,
            text="Git checkout 기반 자동 업데이트 확인과 수동 업데이트 실행을 관리합니다.",
            bg=card_bg,
            fg=muted,
            font=("Segoe UI", 9),
            wraplength=680,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="we", pady=(3, 12))

        self._enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            body,
            text="자동 업데이트 확인",
            variable=self._enabled_var,
            command=self._save_settings,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

        tk.Label(
            body,
            text="확인 주기(분)",
            bg=card_bg,
            fg=text,
            font=("Segoe UI", 9),
        ).grid(row=3, column=0, sticky="w", pady=(0, 8))
        self._interval_var = tk.StringVar(value="10")
        interval = ttk.Spinbox(
            body,
            from_=MIN_CHECK_INTERVAL_MINUTES,
            to=MAX_CHECK_INTERVAL_MINUTES,
            increment=1,
            textvariable=self._interval_var,
            width=8,
            command=self._save_settings,
        )
        interval.grid(row=3, column=1, sticky="w", pady=(0, 8))
        try:
            interval.bind("<FocusOut>", lambda _event: self._save_settings())
            interval.bind("<Return>", lambda _event: self._save_settings())
        except Exception:
            pass

        ttk.Button(
            body,
            text="지금 업데이트 확인",
            command=self._check_now,
            width=18,
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Button(
            body,
            text="새로고침",
            command=self._load_settings,
            width=12,
        ).grid(row=4, column=1, sticky="w", pady=(4, 0), padx=(8, 0))

        self._status_label = tk.Label(
            body,
            text="",
            bg=card_bg,
            fg=muted,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=720,
            anchor="w",
        )
        self._status_label.grid(row=5, column=0, columnspan=3, sticky="we", pady=(12, 0))
        self._load_settings()
        return

    def refresh(self) -> None:
        self._refresh_status()
        return

    def _lazy_import_tk(self) -> bool:
        if self._tk is not None and self._ttk is not None:
            return True
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            self._tk = None
            self._ttk = None
            return False
        self._tk = tk
        self._ttk = ttk
        return True

    def _safe_settings(self) -> dict[str, Any]:
        try:
            data = self._updater.get_settings_snapshot()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _safe_status(self) -> dict[str, Any]:
        try:
            data = self._updater.get_status_snapshot()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_settings(self) -> None:
        self._loading = True
        settings = self._safe_settings()
        try:
            if self._enabled_var is not None:
                self._enabled_var.set(bool(settings.get("auto_check_enabled", True)))
            if self._interval_var is not None:
                self._interval_var.set(str(int(settings.get("check_interval_minutes", 10))))
        except Exception:
            pass
        self._loading = False
        self._refresh_status()
        return

    def _save_settings(self) -> bool:
        if self._loading or self._updater is None:
            return False
        try:
            enabled = bool(self._enabled_var.get()) if self._enabled_var is not None else True
            minutes = int(str(self._interval_var.get()).strip()) if self._interval_var is not None else 10
        except Exception:
            self._set_status(
                f"확인 주기는 {MIN_CHECK_INTERVAL_MINUTES}분 이상 "
                f"{MAX_CHECK_INTERVAL_MINUTES}분 이하 숫자로 입력해 주세요."
            )
            return False
        if minutes < MIN_CHECK_INTERVAL_MINUTES or minutes > MAX_CHECK_INTERVAL_MINUTES:
            self._set_status(
                f"확인 주기는 {MIN_CHECK_INTERVAL_MINUTES}분 이상 "
                f"{MAX_CHECK_INTERVAL_MINUTES}분 이하 숫자로 입력해 주세요."
            )
            return False
        try:
            ok, err = self._updater.update_settings(
                {
                    "auto_check_enabled": enabled,
                    "check_interval_minutes": minutes,
                }
            )
        except Exception as exc:
            ok, err = False, str(exc)
        if not ok:
            self._set_status(str(err or "설정을 저장할 수 없습니다."))
            return False
        self._load_settings()
        return True

    def _check_now(self) -> None:
        try:
            self._updater.check_now(manual=True)
        except Exception:
            pass
        self._refresh_status()
        return

    def _refresh_status(self) -> None:
        settings = self._safe_settings()
        status = self._safe_status()
        state = str(status.get("state") or "unknown")
        reason = str(settings.get("unavailable_reason") or "")
        available = bool(settings.get("auto_update_available", True))
        interval = int(settings.get("check_interval_minutes", 10) or 10)
        mode = "사용" if bool(settings.get("auto_check_enabled", True)) else "중지"
        _enabled, parts = format_update_status_parts(status)
        progress_text = " | ".join(str(part) for part, _kind in parts if str(part).strip())
        text = f"자동 확인: {mode} | 주기: {interval}분 | 상태: {state}"
        if progress_text:
            text = f"{text}\n진행: {progress_text}"
        if not available and reason:
            text = f"{text}\n지원 여부: {reason}"
        self._set_status(text)
        return

    def _set_status(self, value: str) -> None:
        if self._status_label is None:
            return
        try:
            self._status_label.configure(text=str(value or ""))
        except Exception:
            pass
        return

from __future__ import annotations

from typing import Any


class LidPowerSettingsView:
    def __init__(self, root: Any, service: Any) -> None:
        self._root = root
        self._service = service
        self._enabled_var = None
        self._status_label = None
        self._loading = False

    def mount(self, parent: Any) -> None:
        import tkinter as tk
        from tkinter import ttk

        for child in list(parent.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue

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

        tk.Label(
            body,
            text="클램쉘 전원 정책",
            bg=card_bg,
            fg=text,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "AC에서 덮개를 닫아 시작한 클램쉘 세션은 전원 분리 뒤에도 유지합니다. "
                "DC 상태에서 새로 덮개를 닫거나 배터리가 15% 이하가 되면 절전으로 "
                "전환합니다. 원래 Windows 덮개 동작은 비활성화·종료·오류 시 복원됩니다."
            ),
            bg=card_bg,
            fg=muted,
            font=("Segoe UI", 9),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))

        self._enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body,
            text="상태형 클램쉘 전원 정책 사용",
            variable=self._enabled_var,
            command=self._save,
        ).pack(anchor="w")

        self._status_label = tk.Label(
            body,
            text="",
            bg=card_bg,
            fg=muted,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=760,
            anchor="w",
        )
        self._status_label.pack(fill="x", anchor="w", pady=(10, 0))
        self.refresh()

    def refresh(self) -> None:
        data = self._snapshot()
        self._loading = True
        try:
            if self._enabled_var is not None:
                self._enabled_var.set(bool(data.get("enabled", False)))
        finally:
            self._loading = False
        state = "활성" if data.get("runtime_enabled") else "비활성"
        source = data.get("power_source") or "초기 상태 대기"
        lid = data.get("lid_open")
        lid_text = "열림" if lid is True else "닫힘" if lid is False else "초기 상태 대기"
        session = "유지 중" if data.get("ac_clamshell_session") else "없음"
        text = (
            f"상태: {state} | 전원: {source} | 덮개: {lid_text} | "
            f"AC 클램쉘 세션: {session}"
        )
        if data.get("last_error"):
            text = f"{text}\n안전 비활성화: {data['last_error']}"
        self._set_status(text)

    def _snapshot(self) -> dict:
        try:
            data = self._service.get_settings_snapshot()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        if self._loading:
            return
        enabled = (
            bool(self._enabled_var.get()) if self._enabled_var is not None else False
        )
        try:
            ok, error = self._service.update_enabled(enabled)
        except Exception as exc:
            ok, error = False, str(exc)
        if not ok:
            self._set_status(str(error or "설정을 적용할 수 없습니다."))
        self.refresh()

    def _set_status(self, value: str) -> None:
        if self._status_label is None:
            return
        try:
            self._status_label.configure(text=str(value or ""))
        except Exception:
            pass

"""Per-profile state for a reusable native inspector toolbar."""
from __future__ import annotations
from typing import Any


class ProfileActionBinding:
    def __init__(self) -> None:
        self.enabled = True
        self.widget: Any = None

    def attach(self, widget: Any) -> None:
        self.widget = widget
        self._apply()

    def detach(self) -> None:
        self.widget = None

    def _apply(self) -> None:
        if self.widget is not None:
            self.widget.configure(state="normal" if self.enabled else "disabled")

    def configure(self, **options: Any) -> None:
        if "state" in options:
            self.enabled = options["state"] != "disabled"
        self._apply()

    config = configure

    def state(self, flags: Any = None) -> tuple[str, ...]:
        if flags is not None:
            if "disabled" in flags:
                self.enabled = False
            elif "!disabled" in flags:
                self.enabled = True
            self._apply()
        return () if self.enabled else ("disabled",)

    def instate(self, flags: Any) -> bool:
        return all((not self.enabled if flag == "disabled" else self.enabled)
                   for flag in flags)


class ProfileInspector:
    """Native controls are shared; profile values and action states are not."""
    def __init__(self, view: Any, parent: Any, bg: str) -> None:
        self.view = view
        self.selected_id = ""
        self.attached: list[ProfileActionBinding] = []
        tk, ttk = view._tk, view._ttk
        self.frame = tk.Frame(parent, bg=bg)
        self.frame.columnconfigure(0, weight=1)
        self.title = tk.Label(self.frame, text="카드 제목을 선택하여 프로필을 설정합니다.",
                              bg=bg, anchor="w", width=1, font=("Segoe UI", 10, "bold"))
        self.title.grid(row=0, column=0, sticky="we", pady=(0, 4))
        row = tk.Frame(self.frame, bg=bg)
        row.grid(row=1, column=0, sticky="we")
        self.provider = ttk.Combobox(row, values=("codex", "cursor", "claude"),
                                     state="readonly", width=8)
        self.enabled = tk.Checkbutton(row, text="수집", bg=bg, activebackground=bg)
        self.selected = tk.Checkbutton(row, text="작업표시줄 표시", bg=bg, activebackground=bg)
        view._bind_responsive_widget_row(row, [self.provider, self.enabled, self.selected], columns=3)
        actions = tk.Frame(self.frame, bg=bg)
        actions.grid(row=2, column=0, sticky="we")
        self.buttons = {}
        for key, text, width in (("up", "▲", 3), ("down", "▼", 3),
                                 ("query", "새로고침", 8), ("login", "연결", 6),
                                 ("logout", "연결 해제", 8), ("delete", "삭제", 6)):
            self.buttons[key] = ttk.Button(actions, text=text, width=width,
                                           command=lambda action=key: self.invoke(action))
        view._bind_responsive_widget_row(actions, list(self.buttons.values()), columns=6)
        self._set_empty()

    def _set_empty(self) -> None:
        for control in (self.provider, self.enabled, self.selected, *self.buttons.values()):
            control.configure(state="disabled")

    def select(self, profile_id: str) -> None:
        view = self.view
        if profile_id not in view._account_order:
            return
        for state in self.attached:
            state.detach()
        self.attached.clear()
        self.selected_id = profile_id
        view._active_account_id = profile_id
        board = getattr(view, '_profile_board', None)
        if board is not None:
            board.select(profile_id)
        self.title.configure(textvariable=view._account_label_vars[profile_id])
        self.provider.configure(textvariable=view._account_provider_vars[profile_id], state="readonly")
        self.enabled.configure(variable=view._account_enabled_vars[profile_id], state="normal")
        self.selected.configure(variable=view._account_taskbar_selected_vars[profile_id], state="normal",
                                command=lambda: view._on_taskbar_selection_changed(self.selected_id))
        bindings = {"query": view._account_query_buttons[profile_id],
                    "login": view._account_login_buttons[profile_id],
                    "logout": view._account_logout_buttons[profile_id]}
        move = view._account_move_buttons.get(profile_id)
        if move is not None:
            bindings.update(up=move[0], down=move[1])
        for key, state in bindings.items():
            state.attach(self.buttons[key])
            self.attached.append(state)
        for key in ("up", "down"):
            if key not in bindings:
                self.buttons[key].configure(state="disabled")
        self.buttons["delete"].configure(state="normal")

    def invoke(self, action: str) -> None:
        view, profile_id = self.view, self.selected_id
        if profile_id not in view._account_order:
            return
        callbacks = {
            "up": lambda: view._on_move_account(profile_id, -1),
            "down": lambda: view._on_move_account(profile_id, 1),
            "query": lambda: view._on_account_query(profile_id),
            "login": lambda: view._on_account_login(profile_id),
            "logout": lambda: view._on_account_release_profile(profile_id),
            "delete": lambda: view._on_delete_profile(profile_id, view._account_labels.get(profile_id, "")),
        }
        callbacks[action]()

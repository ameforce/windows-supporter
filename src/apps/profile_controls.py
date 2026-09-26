"""Per-profile action state and the single popup menu shared by all cards."""
from __future__ import annotations
from typing import Any


PROVIDER_CHOICES = (("codex", "Codex"), ("cursor", "Cursor"), ("claude", "Claude"))
# Three bullets render in Segoe UI itself and sit centred on the title line;
# U+22EF falls back to a baseline-aligned glyph that is hard to spot.
PROFILE_MENU_GLYPH = "\u2022\u2022\u2022"


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


class ProfileCardMenu:
    """One native popup menu shared by every profile card.

    Profile cards stay windowless regions of the board canvas. The menu is
    created on first use, and every popup re-points its entries at the chosen
    profile's own variables and action states, so the number of native
    widgets never grows with the number of profiles.
    """

    _ENTRIES = (
        ("query", "command", "새로고침"),
        ("login", "command", "연결"),
        ("logout", "command", "연결 해제"),
        (None, "separator", ""),
        ("provider", "cascade", "제공자"),
        ("enabled", "checkbutton", "수집"),
        ("selected", "checkbutton", "작업표시줄 표시"),
        (None, "separator", ""),
        ("up", "command", "위로 이동"),
        ("down", "command", "아래로 이동"),
        (None, "separator", ""),
        ("delete", "command", "삭제…"),
    )

    def __init__(self, view: Any, parent: Any) -> None:
        self.view = view
        self.parent = parent
        self.profile_id = ""
        self.menu: Any = None
        self.provider_menu: Any = None
        self.indexes: dict[str, int] = {}

    def _ensure_menu(self) -> Any:
        if self.menu is not None:
            return self.menu
        tk = self.view._tk
        menu = tk.Menu(self.parent, tearoff=0)
        provider_menu = tk.Menu(menu, tearoff=0)
        for value, label in PROVIDER_CHOICES:
            provider_menu.add_radiobutton(label=label, value=value)
        for key, kind, label in self._ENTRIES:
            if kind == "separator":
                menu.add_separator()
                continue
            if kind == "cascade":
                menu.add_cascade(label=label, menu=provider_menu)
            elif kind == "checkbutton":
                menu.add_checkbutton(label=label)
            else:
                menu.add_command(label=label, command=lambda action=key: self.invoke(action))
            self.indexes[str(key)] = int(menu.index("end"))
        self.menu, self.provider_menu = menu, provider_menu
        return menu

    def bind_to(self, profile_id: str) -> bool:
        """Point every entry at ``profile_id``; action states are read now."""
        view = self.view
        if profile_id not in view._account_order:
            return False
        menu = self._ensure_menu()
        self.profile_id = profile_id
        provider_var = view._account_provider_vars[profile_id]
        for index in range(len(PROVIDER_CHOICES)):
            self.provider_menu.entryconfigure(index, variable=provider_var)
        menu.entryconfigure(
            self.indexes["enabled"],
            variable=view._account_enabled_vars[profile_id],
        )
        menu.entryconfigure(
            self.indexes["selected"],
            variable=view._account_taskbar_selected_vars[profile_id],
            command=lambda target=profile_id: view._on_taskbar_selection_changed(target),
        )
        move = view._account_move_buttons.get(profile_id)
        states = {
            "query": view._account_query_buttons.get(profile_id),
            "login": view._account_login_buttons.get(profile_id),
            "logout": view._account_logout_buttons.get(profile_id),
            "up": move[0] if move is not None else None,
            "down": move[1] if move is not None else None,
        }
        for key, binding in states.items():
            enabled = binding is not None and not binding.instate(["disabled"])
            menu.entryconfigure(self.indexes[key], state="normal" if enabled else "disabled")
        menu.entryconfigure(self.indexes["delete"], state="normal")
        return True

    def open(self, profile_id: str, x_root: int, y_root: int) -> bool:
        if not self.bind_to(profile_id):
            return False
        menu = self.menu
        try:
            menu.tk_popup(int(x_root), int(y_root))
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return True

    def invoke(self, action: str) -> None:
        view, profile_id = self.view, self.profile_id
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
        callback = callbacks.get(str(action))
        if callback is not None:
            callback()

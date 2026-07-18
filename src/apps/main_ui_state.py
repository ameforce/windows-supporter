from __future__ import annotations

import json
import os
from typing import Iterable


DEFAULT_TAB = "dashboard"
STATE_FILENAME = "main_ui_state.json"
_TAB_KEY_EQUIVALENTS = {
    "ai_usage": ("ai_usage", "codex_usage"),
    "codex_usage": ("ai_usage", "codex_usage"),
}


def _resolve_tab_key(tab_key: str, valid_tabs: Iterable[str]) -> str:
    tab = str(tab_key or "").strip()
    valid = {str(value) for value in valid_tabs}
    if tab in valid:
        return tab
    for candidate in _TAB_KEY_EQUIVALENTS.get(tab, ()):
        if candidate in valid:
            return candidate
    return ""


def get_state_path(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    base_dir = env.get("APPDATA") or env.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(str(base_dir), "windows-supporter", STATE_FILENAME)


def load_last_tab(
    *,
    valid_tabs: Iterable[str],
    default: str = DEFAULT_TAB,
    path: str | None = None,
) -> str:
    valid = {str(x) for x in valid_tabs}
    default_key = str(default)
    if default_key not in valid:
        default_key = DEFAULT_TAB if DEFAULT_TAB in valid else next(iter(valid), "")
    if not default_key:
        return str(default)

    state_path = str(path or get_state_path())
    try:
        with open(state_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return default_key
    if not isinstance(data, dict):
        return default_key
    tab = _resolve_tab_key(data.get("last_tab", ""), valid)
    if tab:
        return tab
    return default_key


def save_last_tab(
    tab_key: str,
    *,
    valid_tabs: Iterable[str],
    path: str | None = None,
) -> bool:
    valid = {str(x) for x in valid_tabs}
    tab = _resolve_tab_key(tab_key, valid)
    if not tab:
        return False
    state_path = str(path or get_state_path())
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fp:
            json.dump({"last_tab": tab}, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        return True
    except Exception:
        return False

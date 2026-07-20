from __future__ import annotations

from typing import Any

from src.apps.codex_usage_ui import CodexUsageSettingsView


class AIUsageSettingsView(CodexUsageSettingsView):
    """Provider-neutral facade over the existing Codex settings surface."""

    _COMMON_TEXT_REPLACEMENTS = {
        "Codex Usage Monitoring 설정": "AI 사용량 설정",
        "Codex 사용량 자동 모니터링 동작을 설정합니다.": (
            "AI 사용량 프로필과 자동 모니터링 동작을 설정합니다."
        ),
    }

    def __init__(
        self,
        root: Any,
        usage_monitor: Any = None,
        ui_post=None,
        *,
        codex_monitor: Any = None,
        on_external_settings_reconciled=None,
    ) -> None:
        monitor = usage_monitor if usage_monitor is not None else codex_monitor
        super().__init__(
            root,
            monitor,
            ui_post=ui_post,
            on_external_settings_reconciled=on_external_settings_reconciled,
        )

    def mount(self, parent: Any) -> None:
        super().mount(parent)
        self._rewrite_common_text(parent)
        return

    def _rewrite_common_text(self, widget: Any) -> None:
        pending = [widget]
        while pending:
            current = pending.pop()
            children = getattr(current, "winfo_children", None)
            if callable(children):
                try:
                    pending.extend(list(children()))
                except Exception:
                    pass
            getter = getattr(current, "cget", None)
            setter = getattr(current, "configure", None)
            if not callable(getter) or not callable(setter):
                continue
            try:
                text = str(getter("text") or "")
            except Exception:
                continue
            replacement = self._COMMON_TEXT_REPLACEMENTS.get(text)
            if replacement is None:
                continue
            try:
                setter(text=replacement)
            except Exception:
                continue
        return


AiUsageSettingsView = AIUsageSettingsView

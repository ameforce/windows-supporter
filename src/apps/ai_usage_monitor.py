from __future__ import annotations

from src.apps.codex_usage_multi_monitor import CodexUsageMultiMonitor


class AiUsageProfileManager(CodexUsageMultiMonitor):
    """Provider-neutral facade; the legacy class remains import-compatible."""


AIUsageProfileManager = AiUsageProfileManager

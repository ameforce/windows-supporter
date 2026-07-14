from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX_BROWSER_SOURCES = (
    REPO_ROOT / "src" / "apps" / "codex_usage_monitor.py",
    REPO_ROOT / "src" / "apps" / "codex_usage_browser_types.py",
    REPO_ROOT / "src" / "apps" / "codex_usage_playwright_driver.py",
    REPO_ROOT / "src" / "apps" / "codex_usage_playwright_session.py",
)
FORBIDDEN_DIRECT_BROWSER_PATTERNS = {
    "raw CDP lifecycle": re.compile(r"(?i)(?:\bcdp\b|_cdp|cdp_)"),
    "remote debugging flags": re.compile(r"remote-debugging", re.IGNORECASE),
    "raw DevTools commands": re.compile(r"(?:Target|Page|Runtime)\.[A-Za-z]+"),
    "raw websocket transport": re.compile(r"websocket", re.IGNORECASE),
    "bundled browser runtime selection": re.compile(r"PLAYWRIGHT_BROWSERS_PATH"),
    "direct browser process launch": re.compile(r"\bPopen\s*\("),
    "browser process scan": re.compile(r"(?:process_iter|EnumWindows)\s*\("),
    "browser Win32 control": re.compile(r"\bwin32(?:api|con|gui|process)\b"),
}


class CodexUsageBrowserArchitectureTest(unittest.TestCase):
    def test_codex_usage_browser_transport_has_no_direct_chromium_or_cdp_control(self) -> None:
        violations: list[str] = []

        for source_path in CODEX_BROWSER_SOURCES:
            source = source_path.read_text(encoding="utf-8")
            for label, pattern in FORBIDDEN_DIRECT_BROWSER_PATTERNS.items():
                if match := pattern.search(source):
                    line = source.count("\n", 0, match.start()) + 1
                    violations.append(f"{source_path.name}:{line}: {label}: {match.group(0)!r}")

        self.assertEqual(
            violations,
            [],
            "Codex usage must use Playwright APIs only:\n" + "\n".join(violations),
        )

    def test_codex_usage_driver_requires_installed_chrome_channel(self) -> None:
        driver_source = CODEX_BROWSER_SOURCES[2].read_text(encoding="utf-8")

        self.assertIn("launch_persistent_context", driver_source)
        self.assertRegex(driver_source, r'channel\s*=\s*["\']chrome["\']')
        self.assertNotRegex(driver_source, r"chromium\.launch\s*\(")


if __name__ == "__main__":
    unittest.main()

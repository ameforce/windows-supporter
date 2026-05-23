from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from src.utils.app_version import get_app_version, get_app_version_label


class AppVersionUnitTest(unittest.TestCase):
    def test_returns_dev_version_when_build_info_module_is_missing(self) -> None:
        with patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop("windows_supporter_build_info", None)

            version = get_app_version()

        self.assertEqual(version.display_version, "dev")
        self.assertEqual(version.numeric_version, "0.0.0.0")
        self.assertEqual(get_app_version_label(), "Version dev")

    def test_reads_generated_build_info_module(self) -> None:
        module = types.ModuleType("windows_supporter_build_info")
        module.DISPLAY_VERSION = "v0.3.5 (64d97c3)"
        module.NUMERIC_VERSION = "0.3.5.0"
        module.SOURCE_TAG = "v0.3.1"
        module.COMMIT = "64d97c3"
        module.DIRTY = False

        with patch.dict(sys.modules, {"windows_supporter_build_info": module}):
            version = get_app_version()
            label = get_app_version_label()

        self.assertEqual(version.display_version, "v0.3.5 (64d97c3)")
        self.assertEqual(version.numeric_version, "0.3.5.0")
        self.assertEqual(version.source_tag, "v0.3.1")
        self.assertEqual(version.commit, "64d97c3")
        self.assertFalse(version.dirty)
        self.assertEqual(label, "Version v0.3.5 (64d97c3)")


if __name__ == "__main__":
    unittest.main()

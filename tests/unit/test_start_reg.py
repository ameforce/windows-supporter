from __future__ import annotations

import unittest

from src.utils.StartReg import StartReg


class _FakePath:
    def realpath(self, value):
        return value


class _FakeOs:
    path = _FakePath()


class _FakeSys:
    def __init__(self, argv):
        self.argv = list(argv)


class _FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_ALL_ACCESS = 0xF003F
    REG_SZ = 1

    def __init__(self) -> None:
        self.open_calls = []
        self.values = []
        self.closed = []

    def OpenKey(self, key_type, key_path, reserved, access):
        handle = object()
        self.open_calls.append((key_type, key_path, reserved, access, handle))
        return handle

    def SetValueEx(self, handle, name, reserved, value_type, value):
        self.values.append((handle, name, reserved, value_type, value))

    def CloseKey(self, handle):
        self.closed.append(handle)


class _FakeLib:
    def __init__(self, argv):
        self.reg = _FakeReg()
        self.os = _FakeOs()
        self.sys = _FakeSys(argv)


class StartRegUnitTest(unittest.TestCase):
    def test_add_to_startup_writes_resolved_persistent_executable(self) -> None:
        temporary = r"C:\Users\epapyrus\.codex\worktrees\9f9a\windows-supporter\windows-supporter.exe"
        persistent = r"C:\workspace\daeng\git\tools\windows-supporter\windows-supporter.exe"
        lib = _FakeLib([temporary])
        messages = []
        start_reg = StartReg(
            lib=lib,
            startup_path_resolver=lambda current: persistent if current == temporary else None,
            logger=messages.append,
        )

        start_reg.add_to_startup()

        self.assertEqual(len(lib.reg.values), 1)
        self.assertEqual(lib.reg.values[0][1], "Windows Supporter")
        self.assertEqual(lib.reg.values[0][4], persistent)
        self.assertEqual(lib.reg.closed, [lib.reg.open_calls[0][4]])
        self.assertEqual(messages, [])

    def test_add_to_startup_fails_closed_when_no_persistent_executable_is_available(self) -> None:
        temporary = r"C:\Users\epapyrus\.codex\worktrees\9f9a\windows-supporter\windows-supporter.exe"
        lib = _FakeLib([temporary])
        messages = []
        start_reg = StartReg(
            lib=lib,
            startup_path_resolver=lambda _current: None,
            logger=messages.append,
        )

        start_reg.add_to_startup()

        self.assertEqual(lib.reg.open_calls, [])
        self.assertEqual(lib.reg.values, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("persistent executable not resolved", messages[0])

    def test_add_to_startup_logs_resolver_failures(self) -> None:
        temporary = r"C:\Users\epapyrus\.codex\worktrees\9f9a\windows-supporter\windows-supporter.exe"
        lib = _FakeLib([temporary])
        messages = []

        def failing_resolver(_current):
            raise RuntimeError("git failed")

        start_reg = StartReg(
            lib=lib,
            startup_path_resolver=failing_resolver,
            logger=messages.append,
        )

        start_reg.add_to_startup()

        self.assertEqual(lib.reg.open_calls, [])
        self.assertEqual(lib.reg.values, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("resolver failed", messages[0])


if __name__ == "__main__":
    unittest.main()

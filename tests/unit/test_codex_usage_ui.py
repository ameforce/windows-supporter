import unittest

from src.apps.codex_usage_ui import CodexUsageSettingsView


class _FakeLabel:
    def __init__(self, owner, *args, **kwargs):
        _ = args
        self._owner = owner
        self.kwargs = dict(kwargs)
        self.grid_kwargs = {}
        owner.labels.append(self)

    def grid(self, **kwargs):
        self.grid_kwargs = dict(kwargs)
        return None


class _FakeTk:
    def __init__(self):
        self.labels = []

    def Label(self, *args, **kwargs):
        return _FakeLabel(self, *args, **kwargs)


class CodexUsageUiUnitTest(unittest.TestCase):
    def test_add_value_row_uses_wrapping_and_fill_for_runtime_values(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        fake_tk = _FakeTk()
        view._tk = fake_tk

        view._add_value_row(
            parent=object(),
            row=0,
            label="조회 상태",
            value_var=object(),
            bg="#FFFFFF",
        )

        self.assertEqual(len(fake_tk.labels), 2)
        value_label = fake_tk.labels[1]
        self.assertEqual(value_label.grid_kwargs.get("sticky"), "we")
        self.assertGreater(int(value_label.kwargs.get("wraplength", 0)), 0)


if __name__ == "__main__":
    unittest.main()

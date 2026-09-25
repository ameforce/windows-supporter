import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from src.utils.tk_viewport import SettledViewport
from src.apps.main_ui import WindowsSupporterMainUI


class Scheduler:
    def __init__(self):
        self.callbacks = {}
        self.serial = 0

    def bind(self, *_args, **_kwargs):
        pass

    def after(self, delay, callback):
        self.serial += 1
        self.callbacks[self.serial] = callback
        return self.serial

    def after_cancel(self, identifier):
        self.callbacks.pop(identifier, None)

    def fire(self):
        pending, self.callbacks = self.callbacks, {}
        for callback in pending.values():
            callback()


class SettledViewportTest(unittest.TestCase):
    def setUp(self):
        self.root, self.content = Scheduler(), Mock()
        self.viewport = SettledViewport(self.root, self.content, 800, 600)

    def resize(self, width, height=600):
        self.viewport._configure(SimpleNamespace(widget=self.root, width=width, height=height))

    def test_resize_burst_commits_only_latest_dimensions(self):
        for width in range(801, 901):
            self.resize(width)
        self.assertEqual(self.content.place.call_count, 1)
        self.assertEqual(len(self.root.callbacks), 1)
        self.root.fire()
        self.assertEqual(self.content.place.call_count, 2)
        self.content.place.assert_called_with(x=0, y=0, width=900, height=600)

    def test_return_to_original_size_cancels_unneeded_reflow(self):
        self.resize(1000)
        self.resize(800)
        self.assertEqual(self.root.callbacks, {})
        self.root.fire()
        self.assertEqual(self.content.place.call_count, 1)

    def test_explicit_tab_fit_cancels_pending_resize(self):
        self.resize(1000)
        self.viewport.commit(760, 520)
        self.assertEqual(self.root.callbacks, {})
        self.content.place.assert_called_with(x=0, y=0, width=760, height=520)

    def test_child_events_and_same_size_do_not_restart_timer(self):
        self.resize(1000)
        identifier = self.viewport.pending
        self.resize(1000)
        self.viewport._configure(SimpleNamespace(widget=object(), width=900, height=500))
        self.assertEqual(self.viewport.pending, identifier)

    def test_destroy_cancels_pending_layout(self):
        self.resize(1000)
        self.viewport._destroy(SimpleNamespace(widget=self.root))
        self.root.fire()
        self.assertTrue(self.viewport.closed)
        self.assertEqual(self.content.place.call_count, 1)


class CaptionUpdatesTest(unittest.TestCase):
    def build(self):
        ui = object.__new__(WindowsSupporterMainUI)
        keys = ('dashboard', 'startup_apps', 'kakao_monitor', 'wrike', 'ai_usage', 'update')
        widgets = {key: object() for key in keys}
        ui._tab_widget = widgets.get
        labels, writes = {}, []
        def tab(widget, option=None, **options):
            if option == 'text':
                return labels.get(widget, '')
            labels[widget] = options['text']
            writes.append(options['text'])
        ui._notebook = SimpleNamespace(tab=tab)
        return ui, writes

    def test_unchanged_width_mode_never_rewrites_tab_captions(self):
        ui, writes = self.build()
        ui._apply_notebook_labels_for_width(1000)
        count = len(writes)
        self.assertEqual(count, 6)
        for width in range(1001, 1100):
            ui._apply_notebook_labels_for_width(width)
        self.assertEqual(len(writes), count)

    def test_crossing_compact_boundary_changes_only_shortened_titles(self):
        ui, writes = self.build()
        ui._apply_notebook_labels_for_width(1000)
        writes.clear()
        ui._apply_notebook_labels_for_width(750)
        self.assertCountEqual(writes, ['Startup', 'Kakao', 'AI'])

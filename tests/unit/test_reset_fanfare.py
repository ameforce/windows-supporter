from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from src.utils import reset_fanfare


class ResetFanfarePlaybackTest(unittest.TestCase):
    def setUp(self):
        cache = tempfile.TemporaryDirectory(prefix="test-reset-fanfare-")
        self.addCleanup(cache.cleanup)
        for name, value in (("_sound_cache", cache), ("_sound_path", None), ("_last_buffer", None)):
            replacement = patch.object(reset_fanfare, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.backend = SimpleNamespace(SND_FILENAME=0x20000, SND_ASYNC=1,
                                       SND_MEMORY=4, SND_NODEFAULT=2)
        self.backend.PlaySound = Mock(side_effect=self.validate_playback)
        replacement = patch.dict("sys.modules", {"winsound": self.backend})
        replacement.start()
        self.addCleanup(replacement.stop)

    def validate_playback(self, sound, flags):
        self.assertIsInstance(sound, str)
        self.assertEqual(flags, self.backend.SND_FILENAME | self.backend.SND_ASYNC | self.backend.SND_NODEFAULT)
        self.assertFalse(flags & self.backend.SND_MEMORY)
        data = Path(sound).read_bytes()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"WAVE")

    def test_native_async_file_contract_and_reusable_cache(self):
        with patch.object(reset_fanfare, "build_fanfare_wav_bytes", wraps=reset_fanfare.build_fanfare_wav_bytes) as build:
            self.assertTrue(reset_fanfare.play_reset_fanfare())
            self.assertTrue(reset_fanfare.play_reset_fanfare())
            build.assert_called_once()
        self.assertEqual(self.backend.PlaySound.call_count, 2)
        self.assertEqual(self.backend.PlaySound.call_args_list[0], self.backend.PlaySound.call_args_list[1])

    def test_native_error_is_reported_as_failure(self):
        self.backend.PlaySound.side_effect = RuntimeError("audio unavailable")
        self.assertFalse(reset_fanfare.play_reset_fanfare())

    def test_cache_write_failure_does_not_claim_playback(self):
        with patch.object(reset_fanfare, "_cached_fanfare_path", side_effect=OSError("disk failure")):
            self.assertFalse(reset_fanfare.play_reset_fanfare())
        self.backend.PlaySound.assert_not_called()

    def test_concurrent_alerts_share_one_complete_wav(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: reset_fanfare.play_reset_fanfare(), range(8)))
        self.assertEqual(results, [True] * 8)
        paths = {call.args[0] for call in self.backend.PlaySound.call_args_list}
        self.assertEqual(len(paths), 1)

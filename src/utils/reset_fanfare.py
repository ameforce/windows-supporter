from __future__ import annotations

import io
import math
import struct
import tempfile
import threading
from pathlib import Path
import wave

_SAMPLE_RATE = 22050
_ARPEGGIO = ((523.25, 0.09), (659.25, 0.09), (783.99, 0.09))
_CHORD = ((1046.5, 0.45), (1318.5, 0.45), (1567.98, 0.45))
_TOTAL_SECONDS = 0.80
_POP_SECONDS = 0.03
_OVERLAP_SECONDS = 0.02
_last_buffer: bytes | None = None
_sound_cache: tempfile.TemporaryDirectory | None = None
_sound_path: Path | None = None
_sound_lock = threading.Lock()


def _note_samples(freq: float, duration_s: float, volume: float) -> list[float]:
    count = max(1, int(_SAMPLE_RATE * duration_s))
    attack = max(1, int(_SAMPLE_RATE * 0.008))
    release = max(1, int(_SAMPLE_RATE * 0.06))
    samples: list[float] = []
    for i in range(count):
        t = i / _SAMPLE_RATE
        env = 1.0
        if i < attack:
            env = i / attack
        elif i >= count - release:
            env = max(0.0, (count - i) / release)
        value = (
            math.sin(2.0 * math.pi * freq * t)
            + 0.35 * math.sin(2.0 * math.pi * freq * 2.0 * t)
            + 0.15 * math.sin(2.0 * math.pi * freq * 3.0 * t)
        )
        samples.append(value * env * volume)
    return samples


def build_fanfare_wav_bytes() -> bytes:
    total = int(_SAMPLE_RATE * _TOTAL_SECONDS)
    mix = [0.0] * total
    seed = 0x2F6E2B1
    pop_len = int(_SAMPLE_RATE * _POP_SECONDS)
    for i in range(pop_len):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        noise = (seed / 0x3FFFFFFF) - 1.0
        mix[i] += noise * math.exp(-i / (_SAMPLE_RATE * 0.008)) * 0.35
    offset = int(_SAMPLE_RATE * _POP_SECONDS)
    for freq, dur in _ARPEGGIO:
        seg = _note_samples(freq, dur, 0.42)
        for j, value in enumerate(seg):
            idx = offset + j
            if idx < total:
                mix[idx] += value
        offset += int(_SAMPLE_RATE * (dur - _OVERLAP_SECONDS))
    for freq, dur in _CHORD:
        seg = _note_samples(freq, dur, 0.22)
        for j, value in enumerate(seg):
            idx = offset + j
            if idx < total:
                mix[idx] += value
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        frames = bytearray()
        for value in mix:
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _cached_fanfare_path() -> Path:
    """Keep an owned, complete WAV alive for native asynchronous playback."""
    global _last_buffer, _sound_cache, _sound_path
    with _sound_lock:
        if _sound_path is not None and _sound_path.is_file():
            return _sound_path
        data = _last_buffer or build_fanfare_wav_bytes()
        if _sound_cache is None:
            _sound_cache = tempfile.TemporaryDirectory(prefix="windows-supporter-fanfare-")
        path = Path(_sound_cache.name) / "reset.wav"
        path.write_bytes(data)
        _last_buffer = data
        _sound_path = path
        return path


def play_reset_fanfare() -> bool:
    try:
        import winsound
    except ImportError:
        return False
    try:
        # Python explicitly rejects SND_MEMORY | SND_ASYNC. A cached WAV file
        # permits native asynchronous playback without blocking Tk for 0.8s,
        # adding a worker per alert, or depending on a packaged audio asset.
        path = _cached_fanfare_path()
        flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        winsound.PlaySound(str(path), flags)
        return True
    except Exception:
        return False

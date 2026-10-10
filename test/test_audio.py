# SPDX-License-Identifier: Apache-2.0
"""Test real media splitting, silence cuts and encoded upload bounds."""

import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

import support  # Set up source-tree imports.
from src.audio import (
    audio_duration,
    audio_for_upload,
    part_for_upload,
    plan_parts,
    validate_parts,
    UPLOAD_LIMIT,
)


@unittest.skipUnless(
    shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required'
)
class AudioTests(unittest.TestCase):
    def test_non_api_container_is_converted_without_changing_source(self):
        import subprocess
        from src.storage import audio_hash

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'voice note.aiff'
            subprocess.run(
                [
                    'ffmpeg',
                    '-v',
                    'error',
                    '-f',
                    'lavfi',
                    '-i',
                    'sine=duration=2',
                    str(path),
                ],
                check=True,
                capture_output=True,
            )
            before = audio_hash(path)
            with audio_for_upload(path) as upload:
                self.assertEqual(upload.suffix, '.mp3')
                self.assertLess(upload.stat().st_size, UPLOAD_LIMIT)
                self.assertAlmostEqual(audio_duration(upload), 2, delta=0.15)
            self.assertFalse(upload.exists())
            self.assertEqual(before, audio_hash(path))

    def test_oversized_wav_is_split_and_encoded_below_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setnchannels(2)
                stream.setsampwidth(2)
                stream.setframerate(48000)
                for _ in range(27):
                    stream.writeframes(b'\0' * 1_000_000)
            self.assertGreater(path.stat().st_size, UPLOAD_LIMIT)
            duration = audio_duration(path)
            parts = plan_parts(path, duration, 1200)
            self.assertEqual(len(parts), 2)
            validate_parts(parts, duration, 1200)
            total = 0
            for part in parts:
                with part_for_upload(path, part) as upload:
                    self.assertLess(upload.stat().st_size, UPLOAD_LIMIT)
                    measured = audio_duration(upload)
                    self.assertAlmostEqual(
                        measured, part['end'] - part['start'], delta=0.15
                    )
                    total += measured
                self.assertFalse(upload.exists())
            self.assertAlmostEqual(total, duration, delta=0.3)

    def test_duration_cap_and_fallback_without_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tone.wav'
            import array

            rate = 8000
            samples = array.array(
                'h',
                (
                    int(8000 * math.sin(2 * math.pi * 440 * i / rate))
                    for i in range(rate * 23)
                ),
            )
            with wave.open(str(path), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(rate)
                stream.writeframes(samples.tobytes())
            parts = plan_parts(path, 23, 10)
            self.assertEqual(len(parts), 3)
            validate_parts(parts, 23, 10)
            self.assertAlmostEqual(parts[0]['end'], 23 / 3, places=5)

    def test_small_input_does_not_require_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'small.wav'
            path.write_bytes(b'audio')
            with patch('src.audio.check_ffmpeg', side_effect=AssertionError):
                self.assertEqual(
                    plan_parts(path, 100, 1200),
                    [{'index': 1, 'start': 0.0, 'end': 100}],
                )

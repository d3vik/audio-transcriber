# SPDX-License-Identifier: Apache-2.0
"""Optional MP4 playback, binary durability and cache reuse."""

import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from support import WorkflowFixture
from src.audio import (
    audio_duration, check_video_tools, decoded_audio_duration, encode_video,
)
from src.cli import main
from src.engine import Pipeline
from src.i18n import LANGUAGES
from src.storage import atomic_output, audio_hash
from src.rendering import render_srt


class VideoWorkflowTests(WorkflowFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.decoded_patch = patch(
            'src.engine.decoded_audio_duration', return_value=65.7
        )
        self.decoded_patch.start()
        self.addCleanup(self.decoded_patch.stop)

    def test_mp4_is_optional_and_final_source_only(self):
        job = self.job('-o', 'mp4,txt')
        self.assertEqual(
            job.methods_for('reconcile'), ('md', 'srt', 'mp4', 'txt')
        )
        self.assertEqual(job.methods_for('summary'), ('md', 'txt'))
        self.assertNotIn('mp4', self.job().methods_for('reconcile'))
        with self.assertRaisesRegex(ValueError, 'reconciliation'):
            self.job('-p', 'quick', '-o', 'mp4')

    def test_missing_encoders_fail_before_any_api_initialization(self):
        with (
            patch('src.engine.check_video_tools',
                  side_effect=ValueError('Missing libx264')),
            patch('src.cli.OpenAIAdapter') as factory,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(main([
                '-c', str(self.recording), '-o', 'mp4',
            ]), 1)
        self.assertIn('Missing libx264', output.getvalue())
        factory.return_value.client.assert_not_called()
        factory.return_value.execute.assert_not_called()

    def test_encode_failure_preserves_documents_cache_and_old_mp4(self):
        output = self.root / 'recording'
        output.mkdir()
        video = output / 'call.reconcile.mp4'
        video.write_bytes(b'previous video')

        def failed(audio, subtitles, temporary, duration):
            temporary.write_bytes(b'partial encoding')
            raise ValueError('Simulated encoding failure')

        with (
            patch('src.engine.check_video_tools'),
            patch('src.engine.encode_video', side_effect=failed),
            self.assertRaisesRegex(ValueError, 'MP4 export failed'),
        ):
            self.run_job('-o', 'mp4')
        self.assertEqual(video.read_bytes(), b'previous video')
        self.assertTrue((output / 'call.reconcile.md').is_file())
        self.assertTrue((output / 'call.reconcile.srt').is_file())
        self.assertTrue((output / 'call.summary.md').is_file())
        self.assertFalse((output / '.running').exists())
        self.assertFalse(list(output.glob('.writing-*')))
        self.adapter.calls.clear()

        def succeeded(audio, subtitles, temporary, duration):
            temporary.write_bytes(b'complete video')

        with (
            patch('src.engine.check_video_tools'),
            patch('src.engine.encode_video', side_effect=succeeded),
        ):
            pipeline = self.run_job('-o', 'mp4')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(video.read_bytes(), b'complete video')
        backup = next(pipeline.history.glob('*.mp4.*.bak'))
        self.assertEqual(backup.read_bytes(), b'previous video')

    def test_mp4_paths_checked_before_calls(self):
        (self.root / 'recording/call.reconcile.mp4').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'output file'):
            self.run_job('-o', 'mp4')
        self.assertEqual(self.adapter.calls, [])

    def test_atomic_binary_output_keeps_old_file_on_failure(self):
        output = self.root / 'video.mp4'
        output.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError, 'failure'):
            with atomic_output(output) as temporary:
                temporary.write_bytes(b'incomplete')
                raise ValueError('failure')
        self.assertEqual(output.read_bytes(), b'old')
        self.assertFalse(list(self.root.glob('.writing-*')))

    def test_encoder_requirements_are_explicit(self):
        with (
            patch('src.audio.shutil.which', return_value='/ffmpeg'),
            patch('src.audio.subprocess.run') as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = (
                ' V..... libx264 fake\n A..... aac fake\n'
            )
            with self.assertRaisesRegex(ValueError, 'mov_text'):
                check_video_tools()


@unittest.skipUnless(
    shutil.which('ffmpeg') and shutil.which('ffprobe'),
    'FFmpeg/ffprobe required for MP4 integration',
)
class VideoMediaTests(WorkflowFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.duration_patch.stop()
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\0' * 48000)

    def probe(self, path):
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_streams', '-show_format',
             '-of', 'json', str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(result.stdout)

    def test_real_video_full_audio_subtitles_and_cached_cli_run(self):
        before = audio_hash(self.audio)
        pipeline = self.run_job('-o', 'mp4')
        output = pipeline.root / 'call.reconcile.mp4'
        probe = self.probe(output)
        streams = probe['streams']
        self.assertEqual(
            [(stream['codec_type'], stream['codec_name'])
             for stream in streams],
            [('video', 'h264'), ('audio', 'aac'), ('subtitle', 'mov_text')],
        )
        self.assertEqual(streams[0]['width'], 1280)
        self.assertEqual(streams[0]['height'], 720)
        pixel = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', str(output),
             '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=1:1',
             '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
            capture_output=True, check=True, timeout=30,
        ).stdout
        self.assertEqual(len(pixel), 3)
        self.assertTrue(all(channel <= 3 for channel in pixel))
        self.assertEqual(streams[2]['disposition']['default'], 1)
        self.assertAlmostEqual(float(streams[1]['duration']), 3, delta=0.05)
        self.assertAlmostEqual(
            float(probe['format']['duration']), 3, delta=0.15
        )
        extracted = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', str(output),
             '-map', '0:s:0', '-f', 'srt', '-'],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        self.assertIn('Morgan: Hej, det er Morgan.', extracted)
        self.assertIn('00:00:00,200 --> 00:00:02,400', extracted)
        self.assertEqual(audio_hash(self.audio), before)
        mtimes = {path: path.stat().st_mtime_ns
                  for path in pipeline.root.rglob('*') if path.is_file()}
        self.adapter.calls.clear()
        with (
            patch('src.engine.check_video_tools',
                  side_effect=AssertionError('Cached video needs no tools')),
            patch(
                'src.engine.encode_video',
                side_effect=AssertionError('Cached video needs no encoding'),
            ),
            patch(
                'src.engine.audio_duration',
                side_effect=AssertionError('Cached duration needs no probe'),
            ),
            patch('src.cli.OpenAIAdapter') as factory,
            contextlib.redirect_stderr(io.StringIO()) as log,
        ):
            self.assertEqual(main([
                '-c', str(self.recording), '-o', 'mp4',
            ]), 0, log.getvalue())
            factory.return_value.client.assert_not_called()
            factory.return_value.execute.assert_not_called()
        self.assertEqual(
            mtimes, {path: path.stat().st_mtime_ns for path in mtimes}
        )

    def test_missing_or_changed_video_regenerates_without_model_calls(self):
        pipeline = self.run_job('-o', 'mp4')
        output = pipeline.root / 'call.reconcile.mp4'
        self.adapter.calls.clear()
        output.unlink()
        self.run_job('-o', 'mp4')
        output.write_bytes(b'corrupted output')
        pipeline = self.run_job('-o', 'mp4')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.probe(output)['streams'][1]['codec_name'], 'aac')
        backup = next(pipeline.history.glob('*.mp4.*.bak'))
        self.assertEqual(backup.read_bytes(), b'corrupted output')

    def test_video_checksum_reuse_still_detects_tampering(self):
        first = self.run_job('-o', 'mp4')
        video = first.output_path('reconcile', 'mp4')
        pipeline = Pipeline(
            self.audio, self.job('-o', 'mp4'), self.adapter, log=lambda _: None
        )
        reads = []

        def counted(path):
            if path == video:
                reads.append(path)
            return audio_hash(path)

        with patch('src.engine.audio_hash', side_effect=counted):
            pipeline.preflight()
            pipeline.run()
        self.assertEqual(len(reads), 1)
        stat = video.stat()
        content = video.read_bytes()
        video.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        os.utime(video, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with patch('src.engine.audio_hash', side_effect=counted):
            self.assertFalse(pipeline.video_cached(pipeline.video_metadata()))
        self.assertEqual(len(reads), 2)

    def test_subtitles_ending_early_do_not_truncate_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = root / 'dialogue.srt'
            subtitles.write_text(
                '1\n00:00:00,100 --> 00:00:00,500\nMorgan: Hello.\n\n',
                encoding='utf-8',
            )
            output = root / 'video.mp4'
            encode_video(self.audio, subtitles, output, 3)
            self.assertAlmostEqual(
                float(self.probe(output)['streams'][1]['duration']),
                3, delta=0.05,
            )
            subtitles.write_text('', encoding='utf-8')
            encode_video(self.audio, subtitles, output, 3)
            self.assertEqual(
                [stream['codec_type']
                 for stream in self.probe(output)['streams']],
                ['video', 'audio'],
            )

    def test_embedded_subtitles_preserve_all_overlapping_speech(self):
        data = {
            'notes': [],
            'speaker_names': [
                {'speaker': 'A', 'name': 'Morgan'},
                {'speaker': 'B', 'name': 'Riley'},
            ],
            'turns': [
                {'speaker': 'A', 'text': 'First.', 'notes': [],
                 'source_segments': [0]},
                {'speaker': 'B', 'text': 'Second.', 'notes': [],
                 'source_segments': [1]},
            ],
        }
        diary = {
            'segments': [
                {'speaker': 'A', 'text': 'First.', 'start': 0.1, 'end': 2},
                {'speaker': 'B', 'text': 'Second.', 'start': 1, 'end': 2.5},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = root / 'video.srt'
            subtitles.write_text(
                render_srt(data, diary, 3, 'en', merge_overlaps=True),
                encoding='utf-8',
            )
            output = root / 'video.mp4'
            encode_video(self.audio, subtitles, output, 3)
            extracted = subprocess.run(
                ['ffmpeg', '-v', 'error', '-i', str(output),
                 '-map', '0:s:0', '-f', 'srt', '-'],
                capture_output=True, text=True, check=True, timeout=30,
            ).stdout
        self.assertIn(
            '00:00:01,000 --> 00:00:02,000\nMorgan: First.\nRiley: Second.',
            extracted,
        )
        self.assertIn(
            '00:00:02,000 --> 00:00:02,500\nRiley: Second.', extracted
        )
        original = render_srt(data, diary, 3, 'en')
        self.assertIn(
            '00:00:00,100 --> 00:00:02,000\nMorgan: First.', original
        )

    def test_display_label_changes_regenerate_video_without_model_calls(self):
        original = self.adapter.execute

        def unnamed(stage, *args):
            result = original(stage, *args)
            if stage.type == 'reconcile':
                result['speaker_names'] = []
            return result

        with patch.object(self.adapter, 'execute', unnamed):
            pipeline = self.run_job('-o', 'mp4')
        output = pipeline.root / 'call.reconcile.mp4'
        original_bytes = output.read_bytes()
        self.adapter.calls.clear()
        locale = replace(LANGUAGES['da'], speaker='Person')
        with patch.dict(LANGUAGES, {'da': locale}):
            pipeline = self.run_job('-o', 'mp4')
        self.assertEqual(self.adapter.calls, [])
        extracted = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', str(output),
             '-map', '0:s:0', '-f', 'srt', '-'],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        self.assertIn('Person A: Hej, det er Morgan.', extracted)
        backup = next(pipeline.history.glob('*.mp4.*.bak'))
        self.assertEqual(backup.read_bytes(), original_bytes)

    def test_invalid_video_manifest_is_an_actionable_preflight_error(self):
        pipeline = self.run_job('-o', 'mp4')
        pipeline.video_manifest.write_text('[]', encoding='utf-8')
        self.adapter.calls.clear()
        for extra in ((), ('--redo', 'summary')):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(ValueError, 'Invalid MP4 manifest'),
            ):
                self.run_job('-o', 'mp4', *extra)
        self.assertEqual(self.adapter.calls, [])

    def test_estimated_duration_cache_is_corrected_without_paid_reruns(self):
        original = self.adapter.execute

        def unmatched(stage, *args):
            result = original(stage, *args)
            if stage.type == 'reconcile':
                result['turns'].append({
                    'speaker': 'A', 'text': 'Trailing speech.',
                    'source_segments': [], 'notes': [],
                })
            return result

        with (
            patch('src.engine.audio_duration', return_value=7),
            patch.object(self.adapter, 'execute', unmatched),
        ):
            first = self.run_job()
        original = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in first.cache.rglob('*') if path.is_file()
        }
        self.adapter.calls.clear()
        messages = []
        pipeline = Pipeline(
            self.audio, self.job('-o', 'mp4'), self.adapter,
            log=messages.append,
        )
        pipeline.run()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(pipeline.duration, 7)
        self.assertEqual(pipeline.output_duration, 3)
        for path, (content, mtime) in original.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertTrue(
            any('Duration correction' in line for line in messages)
        )
        video = pipeline.root / 'call.reconcile.mp4'
        self.assertAlmostEqual(
            float(self.probe(video)['streams'][1]['duration']), 3, delta=0.05
        )
        srt = pipeline.root / 'call.reconcile.srt'
        self.assertIn(
            '00:00:02,400 --> 00:00:03,000\nMorgan: Trailing speech.',
            srt.read_text(encoding='utf-8'),
        )
        before = srt.read_bytes()
        with patch('src.engine.decoded_audio_duration',
                   side_effect=AssertionError('Reuse decoded duration cache')):
            self.run_job()
        self.assertEqual(srt.read_bytes(), before)
        self.assertEqual(self.adapter.calls, [])

    def test_duration_validation_reports_expected_and_actual_lengths(self):
        subtitles = self.root / 'empty.srt'
        subtitles.write_text('', encoding='utf-8')
        with self.assertRaisesRegex(
            ValueError, r'audio duration 3\.000s.*7\.000s.*-4\.000s'
        ):
            encode_video(self.audio, subtitles, self.root / 'bad.mp4', 7)

    def test_raw_amr_metadata_uses_decoded_duration(self):
        for codec in ('amr_nb', 'amr_wb'):
            response = subprocess.CompletedProcess(
                [], 0, json.dumps({
                    'streams': [{
                        'codec_type': 'audio', 'codec_name': codec,
                        'duration': '7.0',
                    }],
                    'format': {'duration': '7.0'},
                }), '',
            )
            with (
                self.subTest(codec=codec),
                patch('src.audio.subprocess.run', return_value=response),
                patch(
                    'src.audio.decoded_audio_duration', return_value=3
                ) as decode,
            ):
                self.assertEqual(audio_duration(self.audio), 3)
                decode.assert_called_once_with(self.audio)
        self.assertEqual(decoded_audio_duration(self.audio), 3)

    def test_invalid_decoded_duration_cache_fails_before_calls(self):
        pipeline = self.run_job('-o', 'mp4')
        record = json.loads(
            pipeline.decoded_manifest.read_text(encoding='utf-8')
        )
        self.adapter.calls.clear()
        for invalid in (
            [], dict(record, duration=False), dict(record, duration=-1),
            dict(record, duration=float('inf')),
            dict(record, extra='unexpected'),
        ):
            pipeline.decoded_manifest.write_text(
                json.dumps(invalid), encoding='utf-8'
            )
            with (
                self.subTest(record=invalid),
                self.assertRaisesRegex(ValueError, 'decoded duration cache'),
            ):
                self.run_job('-o', 'mp4')
        self.assertEqual(self.adapter.calls, [])

    def test_decode_errors_are_not_treated_as_short_recordings(self):
        invalid = self.root / 'invalid.wav'
        invalid.write_bytes(b'not a media file')
        with self.assertRaisesRegex(ValueError, 'Cannot measure decoded'):
            decoded_audio_duration(invalid)

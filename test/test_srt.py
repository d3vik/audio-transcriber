# SPDX-License-Identifier: Apache-2.0
"""SRT timing, interpolation and cached document outputs."""

import contextlib
from copy import deepcopy
import io
import re
import unittest
from unittest.mock import patch

from support import WorkflowFixture
from src.cli import main
from src.config import output_methods
from src.engine import Pipeline
from src.rendering import render_srt


def material(timings, texts=None):
    segments, turns = [], []
    for index, timing in enumerate(timings):
        indices = []
        text = texts[index] if texts is not None else f'Words {index + 1}.'
        if timing is not None:
            indices = [len(segments)]
            segments.append({
                'speaker': 'A', 'start': timing[0], 'end': timing[1],
                'text': text,
            })
        turns.append({
            'speaker': 'A', 'source_segments': indices,
            'text': text, 'notes': [],
        })
    return (
        {'topic': 'Delivery', 'tags': [], 'notes': [],
         'speaker_names': [{'speaker': 'A', 'name': 'Morgan'}],
         'turns': turns},
        {'text': ' '.join(turn['text'] for turn in turns),
         'segments': segments},
    )


def parse_srt(text):
    cues = []
    if not text:
        return cues
    for number, block in enumerate(text.strip().split('\n\n'), 1):
        lines = block.splitlines()
        if len(lines) != 3 or lines[0] != str(number):
            raise AssertionError(f'Invalid cue block: {block}')
        match = re.fullmatch(
            r'(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> '
            r'(\d{2,}):(\d{2}):(\d{2}),(\d{3})', lines[1],
        )
        if match is None:
            raise AssertionError(f'Invalid SRT time line: {lines[1]}')
        values = tuple(map(int, match.groups()))
        bounds = []
        for offset in (0, 4):
            hours, minutes, seconds, milliseconds = values[offset:offset + 4]
            if minutes >= 60 or seconds >= 60:
                raise AssertionError(f'Invalid timestamp: {lines[1]}')
            bounds.append(
                ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds
            )
        if not 0 <= bounds[0] < bounds[1]:
            raise AssertionError(f'Nonpositive cue: {lines[1]}')
        cues.append((bounds[0], bounds[1], lines[2]))
    return cues


class SubtitleTests(unittest.TestCase):
    def test_exact_milliseconds_hours_and_overlapping_source_turns(self):
        data, diary = material(
            [(0.123, 2.456), (1.567, 3.891), (3661.007, 3663.009)]
        )
        original = deepcopy((data, diary))
        text = render_srt(data, diary, 3700, 'en')
        self.assertEqual(parse_srt(text), [
            (123, 2456, 'Morgan: Words 1.'),
            (1567, 3891, 'Morgan: Words 2.'),
            (3661007, 3663009, 'Morgan: Words 3.'),
        ])
        self.assertIn('01:01:01,007 --> 01:01:03,009', text)
        self.assertEqual((data, diary), original)
        self.assertNotIn('---', text)

    def test_grouped_source_segments_use_full_time_span(self):
        data, diary = material([(1, 2), (3, 4)])
        data['turns'][0]['source_segments'] = [0, 1]
        data['turns'][0]['text'] = 'Complete turn.'
        data['turns'].pop()
        self.assertEqual(parse_srt(render_srt(data, diary, 5, 'en')), [
            (1000, 4000, 'Morgan: Complete turn.'),
        ])

    def test_leading_middle_and_trailing_interpolation(self):
        data, diary = material([
            None, None, (2, 4), None, None, (8, 9), None,
        ])
        cues = parse_srt(render_srt(data, diary, 12, 'en'))
        self.assertEqual([(start, end) for start, end, _ in cues], [
            (0, 1000), (1000, 2000), (2000, 4000), (4000, 6000),
            (6000, 8000), (8000, 9000), (9000, 12000),
        ])
        self.assertEqual(len(cues), len(data['turns']))

    def test_completely_untimed_speech_fills_recording(self):
        data, diary = material([None, None, None])
        self.assertEqual(
            [(start, end) for start, end, _ in parse_srt(
                render_srt(data, diary, 0.010, 'en')
            )],
            [(0, 3), (3, 6), (6, 10)],
        )

    def test_interpolation_rejects_no_gap_or_insufficient_milliseconds(self):
        for timings in (
            [(0, 2), None, (2, 3)],
            [(0, 4), None, (3, 5)],
            [(0, 10), (1, 2), None, (3, 4)],
            [None, (0, 2)],
            [(0, 5), None],
            [(0, 1), None, None, (1.001, 2)],
        ):
            data, diary = material(timings)
            with self.subTest(timings=timings):
                with self.assertRaisesRegex(ValueError, 'gap'):
                    render_srt(data, diary, 5, 'en')

    def test_padding_clamped_zero_length_rejected_and_silence_allowed(self):
        data, diary = material([(0.2, 1.1)])
        self.assertEqual(parse_srt(render_srt(data, diary, 1, 'en')), [
            (200, 1000, 'Morgan: Words 1.'),
        ])
        for start, end in ((1, 1), (0.1, 0.1001), (1.1, 1.2)):
            data, diary = material([(start, end)])
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(ValueError, 'duration'):
                    render_srt(data, diary, 1, 'en')
        data, diary = material([])
        self.assertEqual(render_srt(data, diary, 1, 'en'), '')

    def test_whitespace_cannot_break_cues_and_danish_text_is_preserved(self):
        data, diary = material(
            [(0, 1)], ['Jeg ønsker\n\nkartofler.\r\nIkke en bæver.']
        )
        data['speaker_names'] = []
        self.assertEqual(parse_srt(render_srt(data, diary, 1, 'da')), [
            (0, 1000, 'Taler A: Jeg ønsker kartofler. Ikke en bæver.'),
        ])


class SubtitleWorkflowTests(WorkflowFixture, unittest.TestCase):
    def test_output_selection_and_default_files(self):
        for method in ('md', 'srt', 'txt', 'stdout', 'md, srt', 'txt,stdout'):
            with self.subTest(method=method):
                job = self.job('-o', method)
                self.assertIn('md', job.methods_for('reconcile'))
                self.assertIn('srt', job.methods_for('reconcile'))
                self.assertNotIn('srt', job.methods_for('summary'))
        self.recording.write_text(
            self.recording.read_text() + 'output=["md", "srt"]\n',
            encoding='utf-8',
        )
        pipeline = self.run_job()
        self.assertTrue((pipeline.root / 'call.summary.md').is_file())
        self.assertTrue((pipeline.root / 'call.reconcile.md').is_file())
        self.assertEqual(parse_srt(
            (pipeline.root / 'call.reconcile.srt').read_text()
        ), [(200, 2400, 'Morgan: Hej, det er Morgan.')])
        self.assertFalse((pipeline.root / 'call.reconcile_draft.srt').exists())
        self.assertFalse((pipeline.root / 'call.summary.srt').exists())
        self.assertFalse((pipeline.root / 'call.diarize.srt').exists())

    def test_quick_has_no_subtitles_and_explicit_srt_is_rejected(self):
        pipeline = self.run_job('-p', 'quick')
        self.assertFalse(list(pipeline.root.glob('*.srt')))
        for value in (True, [], ['md', 'md'], 'md,', 'unknown', ['srt', 1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                output_methods(value)
        with self.assertRaisesRegex(ValueError, 'reconciliation'):
            self.job('-p', 'quick', '-o', 'srt')

    def test_missing_srt_and_extra_modes_reuse_caches(self):
        pipeline = self.run_job()
        subtitles = pipeline.root / 'call.reconcile.srt'
        original = subtitles.read_bytes()
        subtitles.unlink()
        markdown = pipeline.root / 'call.reconcile.md'
        mtime = markdown.stat().st_mtime_ns
        self.adapter.calls.clear()
        self.run_job('-o', 'txt')
        self.assertEqual(subtitles.read_bytes(), original)
        self.assertEqual(markdown.stat().st_mtime_ns, mtime)
        self.assertTrue((pipeline.root / 'call.summary.txt').is_file())
        before = {p: p.stat().st_mtime_ns for p in pipeline.root.rglob('*')
                  if p.is_file()}
        messages = []
        run = Pipeline(
            self.audio, self.job('-o', 'stdout'), self.adapter,
            log=messages.append,
        )
        text = run.run()
        self.assertIn('[00:00, Morgan]:', text)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(any(line.startswith('Saved:') for line in messages))

    def test_srt_path_checked_before_calls_even_with_stdout(self):
        path = self.root / 'recording/call.reconcile.srt'
        path.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'output file'):
            self.run_job('-o', 'stdout')
        self.assertEqual(self.adapter.calls, [])

    def test_interpolation_error_keeps_documents_and_caches(self):
        original = self.adapter.execute

        def no_gap(stage, *args):
            result = original(stage, *args)
            if stage.id == 'reconcile':
                result['turns'].insert(0, {
                    'speaker': 'A', 'source_segments': [],
                    'text': 'Unmatched speech.', 'notes': [],
                })
            if stage.type == 'diarize':
                result['segments'][0]['start'] = 0
            return result

        with patch.object(self.adapter, 'execute', no_gap):
            with self.assertRaisesRegex(ValueError, 'Markdown.*saved'):
                self.run_job()
        output = self.root / 'recording'
        self.assertTrue((output / 'call.reconcile.md').is_file())
        self.assertTrue((output / 'call.summary.md').is_file())
        self.assertFalse(list(output.glob('*.srt')))
        self.assertFalse((output / '.running').exists())
        self.adapter.calls.clear()
        with self.assertRaisesRegex(ValueError, 'gap'):
            self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_multipart_srt_uses_combined_speakers_and_global_timing(self):
        parts = [
            {'index': 1, 'start': 0.0, 'end': 30.0},
            {'index': 2, 'start': 30.0, 'end': 65.7},
        ]
        original = self.adapter.execute

        def grouped(stage, *args):
            result = original(stage, *args)
            if stage.type == 'combine':
                result['speaker_groups'] = [
                    {'speakers': ['P0001:A', 'P0002:A'], 'name': 'Morgan'},
                ]
                result['edits'] = [
                    {'unit': 1, 'text': 'Corrected second part.'},
                ]
            return result

        with (
            patch('src.engine.plan_parts', return_value=parts),
            patch('src.engine.check_ffmpeg'),
            patch(
                'src.engine.part_for_upload',
                side_effect=lambda audio, part: contextlib.nullcontext(audio),
            ),
            patch.object(self.adapter, 'execute', grouped),
        ):
            pipeline = self.run_job()
            self.adapter.calls.clear()
            self.run_job('-o', 'md,srt')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(parse_srt(
            (pipeline.root / 'call.reconcile.srt').read_text()
        ), [
            (200, 2400, 'Morgan: Hej, det er Morgan.'),
            (30200, 32400, 'Morgan: Corrected second part.'),
        ])

    def test_source_is_saved_even_when_only_summary_is_selected(self):
        profile = self.root / 'profiles/reviewed.toml'
        profile.write_text(profile.read_text().replace(
            'outputs = ["reconcile","summary"]', 'outputs = ["summary"]'
        ), encoding='utf-8')
        pipeline = self.run_job()
        self.assertEqual(len(self.adapter.calls), 7)
        self.assertTrue((pipeline.root / 'call.reconcile.md').exists())
        self.assertTrue((pipeline.root / 'call.reconcile.srt').exists())

    def test_cli_stdout_also_saves_documents_and_subtitles(self):
        with (
            patch('src.cli.OpenAIAdapter', return_value=self.adapter),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main([
                '-c', str(self.recording), '-o', 'stdout',
            ]), 0, stderr.getvalue())
        self.assertIn('[00:00, Morgan]:', stdout.getvalue())
        output = self.root / 'recording'
        self.assertTrue((output / 'call.summary.md').is_file())
        self.assertTrue((output / 'call.reconcile.srt').is_file())

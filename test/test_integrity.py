# SPDX-License-Identifier: Apache-2.0
"""Reject lost speech and invalid timelines; retain work on failure."""

from copy import deepcopy
import contextlib
import unittest
from unittest.mock import patch

from support import WorkflowFixture
from src.engine import Pipeline, Result
from src.formats import validate_reconciliation, validate_transcript


class IntegrityTests(WorkflowFixture, unittest.TestCase):
    def test_blank_reconciliation_is_saved_as_failure_and_retry_reuses_audio(
        self,
    ):
        original = self.adapter.execute

        def blank(stage, *args, **kwargs):
            data = original(stage, *args, **kwargs)
            if stage.id == 'reconcile':
                data['turns'][0]['text'] = '  '
            return data

        with (
            patch.object(self.adapter, 'execute', blank),
            self.assertRaisesRegex(ValueError, 'empty text'),
        ):
            self.run_job()
        cache = self.root / 'recording/cache'
        self.assertTrue(list(cache.glob('reconcile.failed.*.json')))
        self.assertFalse(list(cache.glob('reconcile.gpt-*.json')))
        self.assertFalse((cache.parent / '.running').exists())
        self.adapter.calls.clear()
        self.run_job()
        self.assertEqual(self.adapter.calls, ['reconcile', 'summary'])

    def test_bad_timing_is_rejected_before_reconciliation(self):
        original = self.adapter.execute

        def outside(stage, *args, **kwargs):
            data = original(stage, *args, **kwargs)
            if stage.type == 'diarize':
                data['segments'][0]['end'] = 1000
            return data

        with (
            patch.object(self.adapter, 'execute', outside),
            self.assertRaisesRegex(ValueError, 'duration'),
        ):
            self.run_job()
        self.assertNotIn('reconcile', self.adapter.calls)
        self.assertTrue(
            list((self.root / 'recording/cache').glob('diarize.failed.*.json'))
        )

    def test_overlapping_turns_can_interleave_but_cannot_run_backwards(self):
        source = {
            'text': 'Hello. Yes. More.',
            'segments': [
                {'speaker': 'A', 'start': 0, 'end': 2, 'text': 'Hello.'},
                {'speaker': 'B', 'start': 1, 'end': 1.5, 'text': 'Yes.'},
                {'speaker': 'A', 'start': 1.6, 'end': 3, 'text': 'More.'},
            ],
        }
        data = {
            'turns': [
                {
                    'speaker': 'A',
                    'source_segments': [0, 2],
                    'text': 'Hello. More.',
                    'notes': [],
                },
                {
                    'speaker': 'B',
                    'source_segments': [1],
                    'text': 'Yes.',
                    'notes': [],
                },
            ],
            'speaker_names': [],
            'notes': [],
        }
        validate_transcript(source, True, duration=3)
        validate_reconciliation(data, source)
        data['turns'].reverse()
        with self.assertRaisesRegex(ValueError, 'start-time order'):
            validate_reconciliation(data, source)
        source['segments'].reverse()
        with self.assertRaisesRegex(ValueError, 'start-time order'):
            validate_transcript(source, True, duration=3)

    def test_silence_and_small_end_padding_remain_valid(self):
        validate_transcript({'text': '', 'segments': []}, True, duration=1)
        validate_reconciliation(
            {'turns': [], 'speaker_names': [], 'notes': []}, {'segments': []}
        )
        source = {
            'text': 'Hello',
            'segments': [
                {'speaker': 'A', 'start': 0.5, 'end': 1.1, 'text': 'Hello'}
            ],
        }
        validate_transcript(source, True, duration=1)
        for start, end in [(1.1, 1.2), (0.5, 2.1)]:
            bad = deepcopy(source)
            bad['segments'][0].update(start=start, end=end)
            with (
                self.subTest(start=start, end=end),
                self.assertRaisesRegex(ValueError, 'duration'),
            ):
                validate_transcript(bad, True, duration=1)

    def test_out_of_range_indices_fail_without_indexing_the_source(self):
        source = {
            'segments': [{'speaker': 'A', 'start': 0, 'text': 'Keep this'}]
        }
        for index in (-1, 3, True):
            data = {
                'turns': [
                    {
                        'speaker': 'A',
                        'source_segments': [index],
                        'text': 'Keep',
                        'notes': [],
                    }
                ],
                'speaker_names': [],
                'notes': [],
            }
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_reconciliation(data, source)

    def test_empty_summary_is_not_saved_as_success(self):
        original = self.adapter.execute

        def empty(stage, *args, **kwargs):
            data = original(stage, *args, **kwargs)
            if stage.type == 'summary':
                data['overview'] = ' '
            return data

        with (
            patch.object(self.adapter, 'execute', empty),
            self.assertRaisesRegex(ValueError, 'overview'),
        ):
            self.run_job()
        self.assertFalse(
            list((self.root / 'recording/cache').glob('summary.gpt-*.json'))
        )

    def test_conversion_dependency_is_checked_before_requests(self):
        self.audio = self.audio.rename(self.audio.with_suffix('.aiff'))
        with patch(
            'src.engine.check_ffmpeg', side_effect=ValueError('Install FFmpeg')
        ):
            with self.assertRaisesRegex(ValueError, 'Install FFmpeg'):
                Pipeline(
                    self.audio,
                    self.job(self.audio),
                    self.adapter,
                    log=lambda _: None,
                ).run()
        self.assertEqual(self.adapter.calls, [])

    def test_format_change_does_not_reuse_a_different_upload_recipe(self):
        self.run_job()
        self.adapter.calls.clear()
        # The AMR extension selects encoding despite identical bytes.
        self.audio = self.audio.rename(self.audio.with_suffix('.amr'))
        with (
            patch('src.engine.check_ffmpeg'),
            patch(
                'src.engine.audio_for_upload',
                side_effect=contextlib.nullcontext,
            ),
        ):
            pipeline = self.run_job(self.audio)
        self.assertCountEqual(self.adapter.calls, pipeline.profile.required())

    def test_aggregate_generation_date_compares_instants_not_strings(self):
        pipeline = Pipeline(self.audio, self.job('-p', 'quick'), self.adapter)
        pipeline.parts = [
            {'index': 1, 'start': 0, 'end': 30},
            {'index': 2, 'start': 30, 'end': 65.7},
        ]
        pipeline.records = [
            {
                'transcribe': Result(
                    'a', {'text': 'first'}, '2026-10-10T12:00:00+02:00'
                )
            },
            {
                'transcribe': Result(
                    'b', {'text': 'second'}, '2026-10-10T11:00:00+00:00'
                )
            },
        ]
        pipeline.aggregate()
        self.assertEqual(
            pipeline.results['transcribe'].generated_at,
            '2026-10-10T11:00:00+00:00',
        )

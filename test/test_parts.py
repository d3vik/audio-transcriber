# SPDX-License-Identifier: Apache-2.0
"""Multipart scheduling, continuity, recovery and lossless assembly."""

import contextlib
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from support import WorkflowFixture
from src.assembly import validate_combination
from src.cli import main
from src.config import load_profile


class PartsTests(WorkflowFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.parts = [
            {'index': 1, 'start': 0.0, 'end': 30.0},
            {'index': 2, 'start': 30.0, 'end': 65.7},
        ]
        self.events = []
        original = self.adapter.execute

        def execute(stage, payload, language, upload=None):
            index = payload.get('part', {}).get('index')
            self.events.append((index, stage.id, deepcopy(payload)))
            data = original(stage, payload, language, upload)
            if stage.type == 'transcribe':
                data['text'] = f'Words from part {index}.'
            if stage.type == 'reconcile':
                data['turns'][0]['text'] = f'Reconciled part {index}.'
            return data

        self.adapter.execute = execute

        @contextlib.contextmanager
        def upload(audio, part):
            yield audio

        for target, kwargs in [
            ('src.engine.plan_parts', {'return_value': self.parts}),
            ('src.engine.part_for_upload', {'side_effect': upload}),
            ('src.engine.check_ffmpeg', {}),
        ]:
            manager = patch(target, **kwargs)
            manager.start()
            self.addCleanup(manager.stop)

    def test_each_part_finishes_before_next_and_summary_uses_every_part(self):
        pipeline = self.run_job()
        order = [(i, name) for i, name, _ in self.events]
        self.assertLess(
            order.index((1, 'reconcile')), order.index((2, 'transcribe'))
        )
        self.assertLess(
            order.index((2, 'reconcile')), order.index((None, 'combine'))
        )
        self.assertEqual(order[-1], (None, 'summary'))
        self.assertEqual(len(order), 14)
        previous = next(
            payload
            for i, name, payload in self.events
            if (i, name) == (2, 'transcribe')
        )['context'][1]
        self.assertEqual(previous['source'], 'previous')
        self.assertIn('Reconciled part 1.', previous['result']['text'])
        self.assertEqual(previous['part']['start'], 0)
        summary = self.events[-1][2]
        self.assertEqual(
            [t['text'] for t in summary['inputs'][0]['result']['turns']],
            ['Reconciled part 1.', 'Reconciled part 2.'],
        )
        self.assertEqual([c['source'] for c in summary['context']], ['user'])
        text = (pipeline.root / 'call.reconcile.md').read_text()
        self.assertIn('[00:30, Morgan (Taler P0002:A)]', text)
        self.assertEqual(len(list(pipeline.root.glob('call.summary.md'))), 1)
        self.assertTrue((pipeline.cache / 'part-0001').is_dir())

    def test_all_profiles_handle_parts_without_extra_part_summaries(self):
        for name in ('quick', 'standard', 'reviewed', 'researched'):
            self.events.clear()
            pipeline = self.run_job(
                '-p', self.root / f'profiles/{name}.toml', '--name', name
            )
            names = [stage for _, stage, _ in self.events]
            self.assertEqual(names.count('combine'), 1)
            self.assertEqual(names.count('summary'), 1)
            if name == 'quick':
                self.assertEqual(
                    names, ['transcribe', 'transcribe', 'combine', 'summary']
                )
                text = (
                    pipeline.root / 'call.transcribe.md'
                ).read_text()
                self.assertIn('Words from part 1.', text)
                self.assertIn('Words from part 2.', text)
                self.assertNotIn('Taler', text)

    def test_cached_run_needs_no_media_or_api(self):
        pipeline = self.run_job()
        before = {
            p: p.stat().st_mtime_ns
            for p in pipeline.root.rglob('*')
            if p.is_file()
        }
        with (
            patch(
                'src.engine.check_ffmpeg', side_effect=AssertionError('ffmpeg')
            ),
            patch('src.engine.plan_parts', side_effect=AssertionError('plan')),
            patch('src.cli.OpenAIAdapter') as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(['-c', str(self.recording)]), 0)
            factory.return_value.client.assert_not_called()
            factory.return_value.execute.assert_not_called()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})

    def test_repeated_preflight_and_run_reuse_completed_results(self):
        pipeline = self.run_job()
        self.events.clear()
        for _ in range(2):
            pipeline.preflight()
            self.assertTrue(pipeline.prepared)
            self.assertFalse(pipeline.needs_api)
        pipeline.run()
        self.assertEqual(self.events, [])
        self.assertEqual(pipeline.completed, pipeline.task_count)

    def test_failed_preflight_clears_state_and_can_be_retried(self):
        pipeline = self.run_job()
        path = next((pipeline.cache / 'part-0002').glob('diarize.*.json'))
        original = path.read_text(encoding='utf-8')
        record = json.loads(original)
        del record['generated_at']
        path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid cache'):
            pipeline.preflight()
        self.assertFalse(pipeline.prepared)
        self.assertFalse(pipeline.needs_api)
        self.assertEqual(pipeline.results, {})
        self.assertEqual(pipeline.records, [])
        self.assertIsNone(pipeline.part)
        self.assertIsNone(pipeline.previous)
        self.assertIsNone(pipeline.material)
        self.assertIsNone(pipeline._combined_diary)
        self.assertIsNone(pipeline._upload)
        self.assertTrue(pipeline._cache_reads)
        path.write_text(original, encoding='utf-8')
        self.events.clear()
        pipeline.run()
        self.assertEqual(self.events, [])
        self.assertTrue(pipeline.prepared)
        self.assertFalse(pipeline.needs_api)

    def test_unrelated_output_changes_do_not_rerun_final_stages(self):
        for name in ('quick', 'reviewed'):
            with self.subTest(profile=name):
                profile = self.root / f'profiles/{name}.toml'
                model = self.root / f'models/extra-{name}.toml'
                model.write_text(
                    (self.root / 'models/transcribe-primary.toml')
                    .read_text(encoding='utf-8'),
                    encoding='utf-8',
                )
                profile.write_text(
                    profile.read_text(encoding='utf-8').replace(
                        ',"summary"]', ',"summary","extra"]'
                    )
                    + '\n[[stages]]\nid="extra"\n'
                    + f'model="../models/{model.name}"\n',
                    encoding='utf-8',
                )
                self.run_job('-p', profile, '--name', name)
                combine = next(
                    payload for _, stage, payload in self.events
                    if stage == 'combine'
                )
                self.events.clear()
                model.write_text(
                    model.read_text(encoding='utf-8').replace(
                        'Make a faithful first', 'Make a careful first'
                    ),
                    encoding='utf-8',
                )
                self.run_job('-p', profile, '--name', name)
                self.assertEqual(
                    [(part, stage) for part, stage, _ in self.events],
                    [(1, 'extra'), (2, 'extra')],
                )
                self.assertNotIn('recipes', combine)
                self.events.clear()

    def test_source_recipe_change_reruns_combine_with_identical_material(self):
        first = self.run_job()
        original = next(
            payload for _, stage, payload in self.events
            if stage == 'combine'
        )
        model = self.root / 'models/transcribe-primary.toml'
        model.write_text(
            model.read_text(encoding='utf-8').replace(
                'Make a faithful first', 'Make a careful first'
            ),
            encoding='utf-8',
        )
        self.events.clear()
        second = self.run_job()
        current = next(
            payload for _, stage, payload in self.events
            if stage == 'combine'
        )
        self.assertEqual(current, original)
        self.assertNotEqual(
            first.results['reconcile'].key,
            second.results['reconcile'].key,
        )
        self.assertIn('summary', [stage for _, stage, _ in self.events])

    def test_failure_in_second_part_reuses_first_part(self):
        original = self.adapter.execute

        def failed(stage, payload, *args):
            if stage.id == 'reconcile' and payload['part']['index'] == 2:
                raise ValueError('Part two failed')
            return original(stage, payload, *args)

        with (
            patch.object(self.adapter, 'execute', failed),
            self.assertRaisesRegex(ValueError, 'Part two failed'),
        ):
            self.run_job()
        self.events.clear()
        self.run_job()
        self.assertEqual(
            [(i, name) for i, name, _ in self.events],
            [(2, 'reconcile'), (None, 'combine'), (None, 'summary')],
        )

    def test_redo_all_and_selective_combine(self):
        self.run_job()
        self.events.clear()
        self.run_job('--redo', 'all')
        self.assertEqual(len(self.events), 14)
        self.events.clear()
        self.run_job('--redo', 'combine')
        self.assertEqual([name for _, name, _ in self.events], ['combine'])

    def test_earlier_reconciliation_changes_later_text_but_not_diarization(
        self,
    ):
        self.run_job()
        model = self.root / 'models/reconcile-final.toml'
        model.write_text(
            model.read_text().replace('effort = "high"', 'effort = "medium"')
        )
        self.events.clear()
        self.run_job()
        calls = [(i, name) for i, name, _ in self.events]
        self.assertEqual(calls[0], (1, 'reconcile'))
        self.assertIn((2, 'transcribe'), calls)
        self.assertIn((2, 'transcribe_independent'), calls)
        self.assertNotIn('diarize', [name for _, name in calls])

    def test_final_mapping_and_sparse_edits_keep_all_turns_and_times(self):
        original = self.adapter.execute

        def edited(stage, *args):
            result = original(stage, *args)
            if stage.type == 'combine':
                result['speaker_groups'] = [
                    {'speakers': ['P0001:A', 'P0002:A'], 'name': 'Morgan'}
                ]
                result['edits'] = [
                    {'unit': 1, 'text': 'Corrected wording for part two.'}
                ]
            return result

        with patch.object(self.adapter, 'execute', edited):
            pipeline = self.run_job()
        source = pipeline.results['reconcile'].data
        self.assertEqual(len(source['turns']), 2)
        self.assertEqual(source['turns'][0]['text'], 'Reconciled part 1.')
        self.assertEqual(
            source['turns'][1]['text'], 'Corrected wording for part two.'
        )
        self.assertEqual(
            [t['speaker'] for t in source['turns']], ['P0001:A', 'P0001:A']
        )
        self.assertEqual(
            [s['start'] for s in pipeline.results['diarize'].data['segments']],
            [0.2, 30.2],
        )
        self.assertEqual(
            [t['source_segments'] for t in source['turns']], [[0], [1]]
        )

    def test_combination_preserves_sibling_diarization_and_draft_outputs(self):
        profile = self.root / 'profiles/reviewed.toml'
        profile.write_text(
            profile.read_text().replace(
                'outputs = ["reconcile","summary"]',
                'outputs = ["reconcile","reconcile_draft",'
                '"diarize","summary"]',
            )
        )
        original = self.adapter.execute

        def grouped(stage, *args):
            data = original(stage, *args)
            if stage.type == 'combine':
                data['speaker_groups'] = [
                    {'speakers': ['P0001:A', 'P0002:A'], 'name': 'Morgan'}
                ]
            return data

        with patch.object(self.adapter, 'execute', grouped):
            pipeline = self.run_job()
            pipeline.validate(
                pipeline.profile.stages['reconcile_draft'],
                pipeline.results['reconcile_draft'].data,
            )
            self.events.clear()
            cached = self.run_job()
        self.assertEqual(self.events, [])
        for run in (pipeline, cached):
            self.assertEqual(
                [
                    segment['speaker']
                    for segment in run.results['diarize'].data['segments']
                ],
                ['P0001:A', 'P0002:A'],
            )
            self.assertEqual(
                [
                    turn['speaker']
                    for turn in run.results['reconcile'].data['turns']
                ],
                ['P0001:A', 'P0001:A'],
            )
        draft = (pipeline.root / 'call.reconcile_draft.md').read_text()
        self.assertIn('Morgan (Taler P0002:A)', draft)
        final = (pipeline.root / 'call.reconcile.md').read_text()
        self.assertIn('[00:30, Morgan]', final)
        self.assertNotIn('Morgan (Taler', final)
        self.assertIn(
            'Taler P0002:A',
            (
                pipeline.root / 'call.diarize.md'
            ).read_text(),
        )

    def test_four_parts_match_context_names_with_swapped_local_ids(self):
        parts = [
            {'index': 1, 'start': 0.0, 'end': 15.0},
            {'index': 2, 'start': 15.0, 'end': 30.0},
            {'index': 3, 'start': 30.0, 'end': 45.0},
            {'index': 4, 'start': 45.0, 'end': 65.7},
        ]
        context = (
            'Morgan is the customer requesting a refund. '
            'Riley is the supplier representative recording the complaint. '
            'Casey is a mentioned third party, not a participant.'
        )
        self.context.write_text(context, encoding='utf-8')
        profile = self.root / 'profiles/researched.toml'
        profile.write_text(profile.read_text(encoding='utf-8').replace(
            'outputs = ["reconcile","summary","research"]',
            'outputs = ["reconcile","summary","research",'
            '"reconcile_draft","diarize"]',
        ), encoding='utf-8')
        original = self.adapter.execute

        def attributed(stage, payload, language, upload=None):
            data = original(stage, payload, language, upload)
            index = payload.get('part', {}).get('index')
            if index is not None:
                supplier, customer = ('A', 'B') if index % 2 else ('B', 'A')
                segments = [
                    {'speaker': supplier, 'start': 0.2, 'end': 2.0,
                     'text': (
                         'I recorded the complaint. Casey will review it.'
                     )},
                    {'speaker': customer, 'start': 2.5, 'end': 5.0,
                     'text': 'I requested a refund, not an approval.'},
                ]
                if stage.type == 'diarize':
                    data.update(segments=segments, text=' '.join(
                        segment['text'] for segment in segments
                    ))
                if stage.type == 'reconcile':
                    data['turns'] = [
                        {'speaker': segment['speaker'],
                         'source_segments': [i], 'text': segment['text'],
                         'notes': []}
                        for i, segment in enumerate(segments)
                    ]
                    # Supply fallible names for the combiner to recheck.
                    data['speaker_names'] = [
                        {'speaker': supplier,
                         'name': 'Morgan' if index == 3 else 'Riley'},
                        {'speaker': customer,
                         'name': 'Riley' if index == 3 else 'Morgan'},
                    ]
            if stage.type == 'combine':
                data['speaker_groups'] = [
                    {'speakers': ['P0001:A', 'P0002:B', 'P0003:A', 'P0004:B'],
                     'name': 'Riley'},
                    {'speakers': ['P0001:B', 'P0002:A', 'P0003:B', 'P0004:A'],
                     'name': 'Morgan'},
                ]
            return data

        with (
            patch('src.engine.plan_parts', return_value=parts),
            patch.object(self.adapter, 'execute', attributed),
        ):
            self.events.clear()
            pipeline = self.run_job('-p', profile, '-l', 'en')
            self.assertEqual(len(self.events), 30)
            self.events.clear()
            self.run_job('-p', profile, '-l', 'en')
        self.assertEqual(self.events, [])
        for index, record in enumerate(pipeline.records, 1):
            supplier, customer = ('A', 'B') if index % 2 else ('B', 'A')
            self.assertEqual(
                [segment['speaker'] for segment in record['diarize'].data[
                    'segments'
                ]],
                [supplier, customer],
            )
            self.assertEqual(
                [turn['speaker'] for turn in record['reconcile_draft'].data[
                    'turns'
                ]],
                [supplier, customer],
            )
        combine = self.adapter.payloads['combine']
        self.assertEqual(combine['context'][0]['documents'], [context])
        supplied = {
            speaker['id']: speaker for speaker in combine['inputs']['speakers']
        }
        self.assertEqual(len(supplied), 8)
        self.assertEqual(supplied['P0002:A']['declared_name'], 'Morgan')
        self.assertEqual(supplied['P0003:A']['declared_name'], 'Morgan')
        final = pipeline.results['reconcile'].data
        self.assertEqual(len(final['turns']), 8)
        self.assertEqual(final['speaker_names'], [
            {'speaker': 'P0001:A', 'name': 'Riley'},
            {'speaker': 'P0001:B', 'name': 'Morgan'},
        ])
        self.assertEqual(
            [turn['source_segments'] for turn in final['turns']],
            [[index] for index in range(8)],
        )
        self.assertEqual(
            [turn['speaker'] for turn in final['turns']],
            ['P0001:A', 'P0001:B'] * 4,
        )
        summary = self.adapter.payloads['summary']['inputs'][0]['result']
        self.assertEqual(summary['speaker_names'], final['speaker_names'])
        text = (pipeline.root / 'call.reconcile.md').read_text()
        self.assertIn('participants: ["Riley", "Morgan"]', text)
        self.assertIn('[00:45, Riley]', text)
        self.assertIn('[00:47, Morgan]', text)
        self.assertNotIn('Speaker P', text)
        subtitles = (pipeline.root / 'call.reconcile.srt').read_text()
        self.assertEqual(subtitles.count('Riley: '), 4)
        self.assertEqual(subtitles.count('Morgan: '), 4)
        self.assertIn('00:00:45,200 --> 00:00:47,000', subtitles)

    def test_invalid_combine_cannot_omit_or_duplicate_speakers_or_delete_units(
        self,
    ):
        material = {
            'speakers': [
                {'id': 'P0001:A', 'part': 1},
                {'id': 'P0001:B', 'part': 1},
            ],
            'units': [{'id': 0, 'text': 'Keep me.'}],
        }
        good = {
            'topic': 'Call',
            'tags': [],
            'notes': [],
            'edits': [],
            'speaker_groups': [
                {'speakers': ['P0001:A'], 'name': None},
                {'speakers': ['P0001:B'], 'name': None},
            ],
        }
        cases = [
            dict(good, speaker_groups=[]),
            dict(
                good,
                speaker_groups=[
                    {'speakers': ['P0001:A', 'P0001:B'], 'name': None}
                ],
            ),
            dict(good, edits=[{'unit': 0, 'text': ''}]),
            dict(good, edits=[{'unit': 7, 'text': 'Wrong'}]),
            dict(
                good,
                edits=[{'unit': 0, 'text': 'A'}, {'unit': 0, 'text': 'B'}],
            ),
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_combination(value, material)

    def test_parts_configuration_is_required_and_validated(self):
        path = self.root / 'profiles/reviewed.toml'
        original = path.read_text()
        cases = [
            original.replace('max_seconds = 300', 'max_seconds = true'),
            original.replace('max_seconds = 300', 'max_seconds = 9999'),
            original.replace('source = "reconcile"', 'source = "missing"'),
            original.replace(
                '../models/combine-parts.toml',
                '../models/summarize-recording.toml',
            ),
            original.replace('inputs = ["reconcile"]', 'inputs = ["diarize"]'),
            original.replace(
                'context = ["user","previous"]', 'context = ["summary"]', 1
            ),
        ]
        for value in cases:
            path.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_profile(path)

    def test_corrupt_boundaries_fail_before_any_request(self):
        pipeline = self.run_job()
        metadata = json.loads(pipeline.media_path.read_text())
        metadata['parts'][1]['start'] += 1
        pipeline.media_path.write_text(json.dumps(metadata))
        self.events.clear()
        with self.assertRaisesRegex(ValueError, 'Invalid audio metadata'):
            self.run_job()
        self.assertEqual(self.events, [])

    def test_three_parts_carry_full_previous_and_compact_earlier_context(self):
        parts = [
            {'index': 1, 'start': 0.0, 'end': 20.0},
            {'index': 2, 'start': 20.0, 'end': 40.0},
            {'index': 3, 'start': 40.0, 'end': 65.7},
        ]
        with patch('src.engine.plan_parts', return_value=parts):
            pipeline = self.run_job()
        third = next(
            payload
            for i, name, payload in self.events
            if (i, name) == (3, 'transcribe')
        )
        previous = next(
            c for c in third['context'] if c.get('source') == 'previous'
        )
        self.assertIn('Reconciled part 2.', previous['result']['text'])
        self.assertEqual(
            previous['earlier_parts'],
            [
                {
                    'part': 1,
                    'topic': 'Samtale med Morgan',
                    'declared_names': ['Morgan'],
                }
            ],
        )
        self.assertNotIn('context', previous)
        text = (pipeline.root / 'call.summary.md').read_text()
        self.assertIn('parts: 3', text)
        self.assertIn('length: "01:05"', text)

    def test_changing_combine_model_only_repeats_final_processing(self):
        self.run_job()
        path = self.root / 'models/combine-parts.toml'
        path.write_text(
            path.read_text().replace('effort = "high"', 'effort = "medium"')
        )
        self.events.clear()
        self.run_job()
        self.assertEqual(
            [name for _, name, _ in self.events], ['combine', 'summary']
        )

    def test_invalid_combination_is_retained_and_retry_reuses_part_results(
        self,
    ):
        original = self.adapter.execute

        def invalid(stage, *args):
            result = original(stage, *args)
            if stage.type == 'combine':
                result['speaker_groups'] = []
            return result

        with (
            patch.object(self.adapter, 'execute', invalid),
            self.assertRaisesRegex(ValueError, 'invalid response saved'),
        ):
            self.run_job()
        cache = self.root / 'recording/cache'
        self.assertTrue(list(cache.glob('combine.failed.*.json')))
        self.assertFalse(list((self.root / 'recording').glob('*.md')))
        self.events.clear()
        self.run_job()
        self.assertEqual(
            [name for _, name, _ in self.events], ['combine', 'summary']
        )

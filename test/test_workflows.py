# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support import WorkflowFixture
from src.api import OpenAIAdapter
from src.cli import main, parser
from src.config import load_job, load_profile
from src.engine import Pipeline
from src.rendering import dialogue, load_templates, render_stage


class WorkflowTests(WorkflowFixture, unittest.TestCase):
    def test_graph_parallelism_and_context_routing(self):
        self.adapter.coordinate = True
        pipeline = self.run_job()
        self.assertEqual(
            set(self.adapter.calls),
            {
                'transcribe',
                'transcribe_independent',
                'diarize',
                'reconcile_draft',
                'transcribe_verify',
                'reconcile',
                'summary',
            },
        )
        for initial in ('transcribe', 'transcribe_independent', 'diarize'):
            self.assertLess(
                self.adapter.calls.index(initial),
                self.adapter.calls.index('reconcile_draft'),
            )
        self.assertLess(
            self.adapter.calls.index('reconcile_draft'),
            self.adapter.calls.index('transcribe_verify'),
        )
        self.assertLess(
            self.adapter.calls.index('transcribe_verify'),
            self.adapter.calls.index('reconcile'),
        )
        self.assertEqual(self.adapter.payloads['diarize']['context'], [])
        self.assertEqual(
            self.adapter.payloads['transcribe']['context'][0]['documents'],
            ['Known vocabulary: Kartoffelkøbing.'],
        )
        self.assertEqual(
            self.adapter.payloads['summary']['inputs'][0]['id'], 'reconcile'
        )
        self.assertEqual(
            self.adapter.payloads['summary']['context'][0]['source'], 'user'
        )
        reconcile = self.adapter.payloads['reconcile']
        second = next(
            item
            for item in reconcile['inputs']
            if item['id'] == 'transcribe_independent'
        )
        self.assertEqual(second['provenance']['context'], ['user', 'previous'])
        self.assertEqual(
            [item['id'] for item in reconcile['inputs']],
            [
                'transcribe',
                'transcribe_independent',
                'transcribe_verify',
                'diarize',
            ],
        )
        verified = self.adapter.payloads['transcribe_verify']['context'][1]
        self.assertEqual(verified['id'], 'reconcile_draft')
        self.assertIn('Hej, det er Morgan.', verified['result']['text'])
        outputs = list(pipeline.root.glob('*.md'))
        self.assertEqual(len(outputs), 2)
        self.assertIn(
            '[00:00, Morgan]',
            next(
                p for p in outputs if p.name == 'call.reconcile.md'
            ).read_text(encoding='utf-8'),
        )
        self.assertEqual(len(list(pipeline.cache.glob('*.json'))), 8)

    def test_cache_and_precise_invalidation(self):
        self.run_job()
        self.adapter.calls.clear()
        self.run_job()
        self.assertEqual(self.adapter.calls, [])
        # Text output format doesn't cause API work.
        self.run_job('-o', 'txt')
        self.assertEqual(self.adapter.calls, [])
        model = self.root / 'models/summarize-recording.toml'
        model.write_text(
            model.read_text(encoding='utf-8').replace(
                'Summarize the supplied', 'Carefully summarize the supplied'
            ),
            encoding='utf-8',
        )
        self.run_job()
        self.assertEqual(self.adapter.calls, ['summary'])
        self.adapter.calls.clear()
        review = self.root / 'models/transcribe-independent.toml'
        review.write_text(
            review.read_text(encoding='utf-8').replace(
                'gpt-4o-transcribe', 'alternative-transcriber'
            ),
            encoding='utf-8',
        )
        self.run_job()
        self.assertEqual(
            self.adapter.calls,
            [
                'transcribe_independent',
                'reconcile_draft',
                'transcribe_verify',
                'reconcile',
                'summary',
            ],
        )
        self.adapter.calls.clear()
        self.context.write_text('Known vocabulary: Bæverby.', encoding='utf-8')
        self.run_job()
        self.assertCountEqual(
            self.adapter.calls,
            [
                'transcribe',
                'transcribe_independent',
                'reconcile_draft',
                'transcribe_verify',
                'reconcile',
                'summary',
            ],
        )

    def test_redo_identical_result_does_not_repeat_downstream(self):
        self.run_job()
        self.adapter.calls.clear()
        self.run_job('--redo', 'transcribe')
        self.assertEqual(self.adapter.calls, ['transcribe'])

    def test_cache_only_needs_no_sdk_key_or_conversion(self):
        self.run_job()
        with patch.object(
            self.adapter, 'execute', side_effect=AssertionError('API called')
        ):
            self.run_job()

    def test_partial_failure_retains_success_and_unlocks(self):
        self.adapter.fail = 'diarize'
        with self.assertRaisesRegex(ValueError, 'Simulated API failure'):
            self.run_job()
        self.assertIn('transcribe', self.adapter.calls)
        self.assertFalse((self.root / 'recording' / '.running').exists())
        self.adapter.calls.clear()
        self.adapter.fail = None
        self.run_job()
        self.assertNotIn('transcribe', self.adapter.calls)
        self.assertIn('diarize', self.adapter.calls)

    def test_conversion_once_for_all_audio_passes(self):
        conversions = []

        @contextlib.contextmanager
        def convert(path):
            conversions.append(path)
            yield path

        with patch('src.engine.audio_for_upload', convert):
            self.run_job()
        self.assertEqual(conversions, [self.audio])

    def test_bad_response_is_not_cached(self):
        original = self.adapter.execute

        def invalid(stage, *args, **kwargs):
            data = original(stage, *args, **kwargs)
            if stage.id == 'reconcile':
                data['turns'][0]['source_segments'] = [8]
            return data

        with patch.object(self.adapter, 'execute', invalid):
            with self.assertRaisesRegex(ValueError, 'invalid response'):
                self.run_job()
        cache = self.root / 'recording' / 'cache'
        self.assertTrue(list(cache.glob('reconcile.failed.*.json')))
        self.assertFalse(list(cache.glob('reconcile.gpt-*.json')))

    def test_duplicate_names_remain_distinguishable(self):
        data = {
            'notes': [],
            'speaker_names': [
                {'speaker': 'A', 'name': 'Morgan'},
                {'speaker': 'B', 'name': 'Morgan'},
            ],
            'turns': [
                {
                    'source_segments': [],
                    'speaker': s,
                    'text': 'Hej',
                    'notes': [],
                }
                for s in 'AB'
            ],
        }
        text = str(dialogue(data, 'reconcile', {'segments': []})[0])
        self.assertIn('Morgan (Speaker A)', text)
        self.assertIn('Morgan (Speaker B)', text)

    def test_audio_modified_during_request_is_not_cached(self):
        job = self.job('-p', self.root / 'profiles/quick.toml')
        original = self.adapter.execute

        def changed(stage, *args, **kwargs):
            data = original(stage, *args, **kwargs)
            self.audio.write_bytes(b'changed audio')
            return data

        with patch.object(self.adapter, 'execute', changed):
            with self.assertRaisesRegex(ValueError, 'Audio changed'):
                Pipeline(
                    self.audio, job, self.adapter, log=lambda value: None
                ).run()
        self.assertFalse(
            list((self.root / 'recording').rglob('transcribe.*.json'))
        )

    def test_paths_overrides_and_context_deduplication(self):
        extra = self.root / 'extra.md'
        extra.write_text('Morgan', encoding='utf-8')
        job = self.job('-x', extra, '-x', extra, '-l', 'en')
        self.assertEqual(
            job.context, ('Known vocabulary: Kartoffelkøbing.', 'Morgan')
        )
        self.assertEqual(job.audio, (self.audio,))
        self.assertEqual(job.language, 'en')
        self.assertEqual(
            job.profile.stages['transcribe_independent'].model.api_model,
            'gpt-4o-transcribe',
        )

    def test_profile_names_and_custom_paths(self):
        self.recording.write_text(
            'version=1\naudio="call.wav"\nprofile="reviewed"\n',
            encoding='utf-8',
        )
        self.assertEqual(self.job().profile.id, 'reviewed')
        self.assertEqual(self.job('-p', 'quick').profile.id, 'quick')
        # A same-named local file cannot shadow a bundled name.
        (self.root / 'reviewed').write_text('invalid TOML', encoding='utf-8')
        self.assertEqual(self.job().profile.id, 'reviewed')
        self.recording.write_text(
            'version=1\naudio="call.wav"\nprofile="profiles/standard.toml"\n',
            encoding='utf-8',
        )
        self.assertEqual(self.job().profile.id, 'standard')
        absolute = (self.root / 'profiles/quick.toml').as_posix()
        self.recording.write_text(
            f'version=1\naudio="call.wav"\nprofile="{absolute}"\n',
            encoding='utf-8',
        )
        self.assertEqual(self.job().profile.id, 'quick')
        with patch('src.config.Path.cwd', return_value=self.root):
            self.assertEqual(
                self.job('-p', 'profiles/standard.toml').profile.id, 'standard'
            )
        with self.assertRaisesRegex(ValueError, 'Unknown bundled profile'):
            self.job('-p', 'unknown-profile')

    def test_output_naming(self):
        self.assertEqual(
            self.job().output_root(self.audio), self.root / 'recording'
        )
        self.assertEqual(
            self.job('--name', 'Potato delivery').output_root(self.audio),
            self.root / 'Potato delivery',
        )
        direct = load_job(parser().parse_args([str(self.audio)]))
        self.assertEqual(direct.output_root(self.audio), self.root / 'call')
        pipeline = Pipeline(
            self.audio, self.job('--name', 'Potato delivery'), self.adapter
        )
        self.assertEqual(
            pipeline.output_path('reconcile', 'md'),
            self.root / 'Potato delivery/call.reconcile.md',
        )
        self.assertEqual(
            pipeline.output_path('summary', 'txt'),
            self.root / 'Potato delivery/call.summary.txt',
        )

    def test_renamed_audio_outputs_reuse_caches_and_preserve_old_files(self):
        first = self.run_job()
        old_files = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in first.root.rglob('*') if path.is_file()
        }
        self.audio = self.audio.rename(self.root / 'recording take.02.wav')
        self.adapter.calls.clear()
        pipeline = self.run_job(self.audio)
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(
            (pipeline.root / 'recording take.02.reconcile.md').is_file()
        )
        self.assertTrue(
            (pipeline.root / 'recording take.02.summary.md').is_file()
        )
        self.assertTrue(
            (pipeline.root / 'recording take.02.reconcile.srt').is_file()
        )
        for path, (content, mtime) in old_files.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_output_filename_does_not_depend_on_model_or_parts(self):
        pipeline = Pipeline(self.audio, self.job(), self.adapter)
        expected = pipeline.output_path('reconcile', 'md')
        pipeline.parts = [
            {'index': 1, 'start': 0.0, 'end': 30.0},
            {'index': 2, 'start': 30.0, 'end': 65.7},
        ]
        self.assertEqual(pipeline.output_path('reconcile', 'md'), expected)
        self.assertEqual(expected.name, 'call.reconcile.md')

    def test_frontmatter_and_elapsed_timestamps(self):
        data = {
            'topic': 'Levering: kartofler',
            'tags': ['kartofler'],
            'notes': [],
            'speaker_names': [{'speaker': 'A', 'name': 'Riley'}],
            'turns': [
                {
                    'speaker': 'A',
                    'source_segments': [0],
                    'text': 'Hej.',
                    'notes': [],
                }
            ],
        }
        job = self.job('--start', '2026-04-12T09:00:00+02:00')
        text = render_stage(
            load_templates({'reconcile'}),
            'reconcile',
            data,
            'reconcile',
            data,
            {'segments': [{'start': 608.198, 'end': 616.998}]},
            self.audio,
            job,
            700,
            '2026-10-10T14:30:00+02:00',
        )
        self.assertTrue(text.startswith('---\n'))
        self.assertNotIn('\n---\n\n\n', text)
        self.assertIn('end: "2026-04-12 09:11:40+02:00"', text)
        self.assertIn('participants: ["Riley"]', text)
        self.assertIn('profile: "reviewed"', text)
        self.assertIn('[10:08, Riley]: Hej.', text)
        self.assertNotIn(' · ', text)
        self.assertNotIn('model:', text)

    def test_preflight_failure_prevents_all_stage_calls(self):
        with patch(
            'src.engine.audio_duration',
            side_effect=ValueError('ffprobe missing'),
        ):
            with self.assertRaisesRegex(ValueError, 'ffprobe missing'):
                self.run_job()
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse((self.root / 'recording').exists())

    def test_cached_run_skips_sdk_and_media_tools(self):
        self.run_job()
        with (
            patch('src.cli.OpenAIAdapter') as factory,
            patch(
                'src.engine.audio_duration',
                side_effect=AssertionError('probe'),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(['-c', str(self.recording)]), 0)
            factory.return_value.client.assert_not_called()
            factory.return_value.execute.assert_not_called()

    def test_missing_key_prevents_work(self):
        with (
            patch('src.cli.OpenAIAdapter') as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            factory.return_value.client.side_effect = ValueError(
                'Set OPENAI_API_KEY'
            )
            self.assertEqual(main(['-c', str(self.recording)]), 1)
            factory.return_value.execute.assert_not_called()
        self.assertFalse((self.root / 'recording').exists())

    def test_plan_is_read_only(self):
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            patch('src.cli.OpenAIAdapter', side_effect=AssertionError),
        ):
            self.assertEqual(main(['-c', str(self.recording), '--plan']), 0)
        self.assertIn('transcribe_independent', out.getvalue())
        self.assertFalse((self.root / 'recording').exists())

    def test_all_bundled_profiles(self):
        for path in (self.root / 'profiles').glob('*.toml'):
            profile = load_profile(path)
            self.assertTrue(profile.required())
        job = self.job('-p', self.root / 'profiles/quick.toml')
        Pipeline(self.audio, job, self.adapter, log=lambda value: None).run()
        self.assertEqual(self.adapter.calls, ['transcribe', 'summary'])

    def test_bad_graphs_and_configuration_are_rejected(self):
        path = self.root / 'profiles/reviewed.toml'
        original = path.read_text(encoding='utf-8')
        cases = [
            original.replace('context = ["user"]', 'context = ["summary"]', 1),
            original.replace(
                'inputs = ["transcribe","transcribe_independent","diarize"]',
                'inputs = ["missing", "diarize"]',
            ),
            original.replace(
                'id = "transcribe_independent"', 'id = "transcribe"'
            ),
            original.replace(
                'id = "transcribe"', 'id = "transcribe"\ntype = "summary"', 1
            ),
            original.replace(
                'outputs = ["reconcile","summary"]', 'outputs = ["missing"]'
            ),
            original.replace(
                'version = 1', 'version = 1\naudio = "wrong.wav"'
            ),
        ]
        for case in cases:
            with self.subTest(case=case):
                path.write_text(case, encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_profile(path)
        path.write_text(original, encoding='utf-8')
        with self.assertRaises(ValueError):
            self.job('--redo', 'unknown')
        self.recording.write_text(
            self.recording.read_text(encoding='utf-8') + 'typo = true\n',
            encoding='utf-8',
        )
        with self.assertRaises(ValueError):
            self.job()

    def test_lock_prevents_any_api_work(self):
        root = self.root / 'recording'
        root.mkdir()
        (root / '.running').write_text('123', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'locked'):
            self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_adapter_forwards_selected_models_prompts_and_context(self):
        job = self.job()
        adapter = OpenAIAdapter()
        captured = []

        def speech(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(model_dump=lambda **options: {'text': 'ok'})

        def text(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                status='completed',
                output_text='{}',
                model_dump=lambda **options: {'output': []},
            )

        client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=speech)
            ),
            responses=SimpleNamespace(create=text),
        )
        with patch.object(adapter, 'client', return_value=client):
            second = job.profile.stages['transcribe_independent']
            adapter.execute(
                second, {'draft': 'An uncertain draft'}, 'en', self.audio
            )
            self.assertEqual(captured[-1]['model'], second.model.api_model)
            self.assertIn(second.model.prompt, captured[-1]['prompt'])
            self.assertIn('An uncertain draft', captured[-1]['prompt'])
            adapter.execute(
                job.profile.stages['diarize'], {}, 'da', self.audio
            )
            self.assertNotIn('prompt', captured[-1])
            adapter.execute(
                job.profile.stages['summary'],
                {'context': 'Kartoffelkøbing'},
                'da',
            )
        self.assertFalse(captured[-1]['store'])
        self.assertEqual(captured[-1]['max_output_tokens'], 6000)
        self.assertTrue(captured[-1]['text']['format']['strict'])


if __name__ == '__main__':
    unittest.main()

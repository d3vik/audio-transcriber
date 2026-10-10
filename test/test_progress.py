# SPDX-License-Identifier: Apache-2.0
"""Test parallel and cached progress, parts and recovery."""

import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import patch

from support import FakeAdapter, ROOT, WorkflowFixture
from src.cli import main
from src.engine import Pipeline
from src.api import APIResult, InvalidResponse, TokenUsage


class ProgressTests(WorkflowFixture, unittest.TestCase):
    def counting_adapter(self, unavailable=None, failure=None):
        original = self.adapter.execute

        def counted(stage, *args):
            data = original(stage, *args)
            usage = (
                None if stage.id == unavailable else TokenUsage(1000, 250)
            )
            if stage.id == failure:
                raise InvalidResponse(
                    'Simulated refusal', 'Refused fixture.', usage
                )
            return APIResult(data, usage)

        manager = patch.object(self.adapter, 'execute', counted)
        manager.start()
        self.addCleanup(manager.stop)

    def run_logged(self, *args):
        messages = []
        pipeline = Pipeline(
            self.audio, self.job(*args), self.adapter, log=messages.append
        )
        pipeline.run()
        return messages

    def assert_progress(self, messages, total, finished, cached):
        lines = [line for line in messages if re.match(r'^\[\d+/\d+\] ', line)]
        self.assertEqual(len(lines), finished + cached)
        for index, line in enumerate(lines, 1):
            self.assertTrue(line.startswith(f'[{index}/{total}] '), line)
        self.assertEqual(
            sum('] Finished: ' in line for line in lines), finished
        )
        self.assertEqual(sum('] Cached: ' in line for line in lines), cached)
        for line in lines:
            if '] Finished: ' in line:
                self.assertRegex(line, r' \(\d+\.\ds\); tokens ')
            else:
                self.assertNotIn('(0.0s)', line)
        self.assertFalse(
            any(line.startswith(('Finished:', 'Cached:')) for line in messages)
        )

    def multipart(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        parts = [
            {'index': 1, 'start': 0.0, 'end': 30.0},
            {'index': 2, 'start': 30.0, 'end': 65.7},
        ]
        stack.enter_context(patch('src.engine.plan_parts', return_value=parts))
        stack.enter_context(patch('src.engine.check_ffmpeg'))
        stack.enter_context(
            patch(
                'src.engine.part_for_upload',
                side_effect=lambda audio, part: contextlib.nullcontext(audio),
            )
        )

    def test_single_part_compact_parallel_and_cached(self):
        self.adapter.coordinate = True
        messages = self.run_logged()
        self.assert_progress(messages, 7, finished=7, cached=0)
        self.assertEqual(
            messages[0], 'Preflight: 0 cached, 7 pending — call.wav'
        )
        self.assertIn('Running: transcribe (gpt-4o-transcribe)', messages)
        self.assertFalse(any('part' in line.lower() for line in messages))
        self.assertFalse(any('combine' in line for line in messages))
        messages = self.run_logged()
        self.assert_progress(messages, 7, finished=0, cached=7)
        self.assertEqual(
            messages[0], 'Preflight: 7 cached, 0 pending — call.wav'
        )
        self.assertFalse(
            any(
                line.startswith(('Running:', 'Preparing audio:'))
                for line in messages
            )
        )

    def test_failed_stage_is_not_counted_and_retry_counts_cached_work(self):
        self.adapter.fail = 'summary'
        messages = []
        with self.assertRaisesRegex(ValueError, 'Simulated API failure'):
            Pipeline(
                self.audio, self.job(), self.adapter, log=messages.append
            ).run()
        self.assert_progress(messages, 7, finished=6, cached=0)
        self.assertIn('Failed: summary: Simulated API failure', messages)
        self.adapter.fail = None
        messages = self.run_logged()
        self.assert_progress(messages, 7, finished=1, cached=6)
        self.assertEqual(
            messages[0], 'Preflight: 6 cached, 1 pending — call.wav'
        )
        self.assertTrue(
            any(
                line.startswith('[7/7] Finished: summary (')
                for line in messages
            )
        )

    def test_multipart_has_one_counter_through_combine_and_summary(self):
        self.multipart()
        messages = self.run_logged()
        self.assert_progress(messages, 14, finished=14, cached=0)
        self.assertEqual(
            messages[0], 'Preflight: 2 parts, 0 cached, 14 pending — call.wav'
        )
        self.assertIn('Processing part 1/2 (00:00–00:30)', messages)
        self.assertIn('Processing part 2/2 (00:30–01:05)', messages)
        self.assertEqual(
            sum(line.startswith('Completed part ') for line in messages), 2
        )
        self.assertFalse(
            any('Part ' in line or ' · ' in line for line in messages)
        )
        self.assertTrue(
            any(
                line.startswith('[13/14] Finished: combine (')
                for line in messages
            )
        )
        self.assertTrue(
            any(
                line.startswith('[14/14] Finished: summary (')
                for line in messages
            )
        )
        self.assert_progress(self.run_logged(), 14, finished=0, cached=14)
        # An identical forced combination leaves the summary reusable.
        messages = self.run_logged('--redo', 'combine')
        self.assert_progress(messages, 14, finished=1, cached=13)
        self.assertIn('[14/14] Cached: summary; tokens -', messages)

    def test_combine_failure_is_reported_and_not_counted(self):
        self.multipart()
        self.adapter.fail = 'combine'
        messages = []
        with self.assertRaisesRegex(RuntimeError, 'Simulated API failure'):
            Pipeline(
                self.audio, self.job(), self.adapter, log=messages.append
            ).run()
        self.assert_progress(messages, 14, finished=12, cached=0)
        self.assertIn('Failed: combine: Simulated API failure', messages)
        self.adapter.fail = None
        self.assert_progress(self.run_logged(), 14, finished=2, cached=12)

    def test_counter_follows_selected_profile(self):
        for profile, total in [
            ('quick', 2),
            ('standard', 4),
            ('researched', 8),
        ]:
            with self.subTest(profile=profile):
                messages = self.run_logged('-p', profile, '--name', profile)
                self.assert_progress(messages, total, finished=total, cached=0)

    def test_token_totals_concurrent_tasks_cached_runs_and_forced_reruns(self):
        self.counting_adapter()
        self.adapter.coordinate = True
        messages = self.run_logged()
        self.assertIn(
            '[7/7] Finished: summary',
            next(line for line in messages if '] Finished: summary' in line),
        )
        for line in messages:
            if '] Finished:' in line:
                self.assertIn('tokens 1,000 in / 250 out', line)
        self.assertIn('tokens 7,000 in / 1,750 out', messages[-1])
        cache = self.root / 'recording/cache'
        record = json.loads(next(cache.glob('summary.*.json')).read_text())
        self.assertEqual(
            record['usage'], {'input_tokens': 1000, 'output_tokens': 250}
        )
        self.assertNotIn('usage', record['result'])
        self.assertNotIn(
            'usage', self.adapter.payloads['summary']['inputs'][0]['result']
        )
        cached = self.run_logged()
        for line in cached:
            if '] Cached:' in line:
                self.assertTrue(line.endswith('; tokens -'), line)
        self.assertIn('tokens 0 in / 0 out', cached[-1])
        forced = self.run_logged('--redo', 'summary')
        self.assertIn('tokens 1,000 in / 250 out', forced[-1])

    def test_multipart_tokens_count_all_parts_combination_and_summary(self):
        self.counting_adapter()
        self.multipart()
        messages = self.run_logged()
        self.assertIn('tokens 14,000 in / 3,500 out', messages[-1])
        self.assertIn('tokens 0 in / 0 out', self.run_logged()[-1])

    def test_duration_based_or_missing_usage_is_not_silently_zero(self):
        self.counting_adapter(unavailable='diarize')
        messages = self.run_logged()
        self.assertTrue(any(
            '] Finished: diarize' in line and 'tokens unavailable' in line
            for line in messages
        ))
        self.assertIn(
            'tokens 6,000 in / 1,500 out; 1 call unavailable',
            messages[-1],
        )
        self.assertIn('tokens 0 in / 0 out', self.run_logged()[-1])

    def test_refused_response_usage_is_included_in_stopped_total(self):
        self.counting_adapter(failure='summary')
        messages = []
        with self.assertRaisesRegex(ValueError, 'Simulated refusal'):
            Pipeline(
                self.audio, self.job(), self.adapter, log=messages.append
            ).run()
        self.assertTrue(messages[-1].startswith('Stopped:'))
        self.assertIn('tokens 7,000 in / 1,750 out', messages[-1])
        self.assertEqual(sum('] Finished:' in line for line in messages), 6)
        self.assertFalse(
            any('] Finished: summary' in line for line in messages)
        )

    def test_result_validation_failure_still_counts_returned_tokens(self):
        self.counting_adapter()
        original = self.adapter.execute

        def invalid(stage, *args):
            result = original(stage, *args)
            if stage.type == 'summary':
                result['overview'] = ''
            return result

        messages = []
        with (
            patch.object(self.adapter, 'execute', invalid),
            self.assertRaisesRegex(ValueError, 'overview'),
        ):
            Pipeline(
                self.audio, self.job(), self.adapter, log=messages.append
            ).run()
        self.assertIn('tokens 7,000 in / 1,750 out', messages[-1])

    def test_same_pipeline_resets_current_run_token_totals(self):
        self.counting_adapter()
        messages = []
        pipeline = Pipeline(
            self.audio, self.job(), self.adapter, log=messages.append
        )
        pipeline.run()
        self.assertIn('tokens 7,000 in / 1,750 out', messages[-1])
        pipeline.run()
        self.assertIn('tokens 0 in / 0 out', messages[-1])
        self.assertEqual(pipeline.usage.input_tokens, 0)
        self.assertEqual(pipeline.usage.output_tokens, 0)

    def test_audio_usage_changes_do_not_invalidate_downstream_requests(self):
        original = self.adapter.execute
        count = [1000]

        def counted(stage, *args):
            data = original(stage, *args)
            if stage.type in ('transcribe', 'diarize'):
                data['usage'] = {
                    'type': 'tokens', 'input_tokens': count[0],
                    'output_tokens': 250,
                }
            return APIResult(data, TokenUsage(count[0], 250))

        with patch.object(self.adapter, 'execute', counted):
            first = self.run_job('-p', 'standard')
            raw = json.loads(next(
                first.cache.glob('transcribe.*.json')
            ).read_text(encoding='utf-8'))
            self.assertEqual(raw['result']['usage']['input_tokens'], 1000)
            self.assertEqual(first.usage, TokenUsage(4000, 1000))
            payload = self.adapter.payloads['reconcile']
            self.assertTrue(all(
                'usage' not in entry['result'] for entry in payload['inputs']
            ))
            for stage in ('transcribe', 'diarize'):
                with self.subTest(stage=stage):
                    self.adapter.calls.clear()
                    count[0] += 500
                    forced = self.run_job(
                        '-p', 'standard', '--redo', stage
                    )
                    self.assertEqual(self.adapter.calls, [stage])
                    self.assertEqual(
                        forced.usage, TokenUsage(count[0], 250)
                    )

    def test_previous_audio_usage_does_not_change_multipart_requests(self):
        self.multipart()
        original = self.adapter.execute

        def counted(stage, *args):
            data = original(stage, *args)
            if stage.type == 'transcribe':
                data['usage'] = {
                    'type': 'tokens', 'input_tokens': 1000,
                    'output_tokens': 250,
                }
            return data

        with patch.object(self.adapter, 'execute', counted):
            pipeline = self.run_job('-p', 'quick')
        records = pipeline.records
        pipeline.select_part(pipeline.parts[1])
        source = pipeline.profile.stages['transcribe']
        payload = pipeline.payload(source)
        self.assertNotIn('usage', payload['context'][1]['result'])
        self.assertIn('usage', records[0]['transcribe'].data)
        self.adapter.calls.clear()
        pipeline = self.run_job('-p', 'quick')
        self.assertEqual(self.adapter.calls, [])


class ReferenceExampleTests(unittest.TestCase):
    def test_launcher_rejects_unsupported_python_before_importing_cli(self):
        import runpy
        import sys

        with patch.object(sys, 'version_info', (3, 10, 0)):
            with self.assertRaisesRegex(SystemExit, 'Python 3.11 or newer'):
                runpy.run_path(
                    str(ROOT / 'transcribe.py'), run_name='__main__'
                )

    def test_launcher_finds_bundled_resources_from_another_directory(self):
        import os
        import subprocess
        import sys
        import tempfile

        environment = os.environ.copy()
        environment.pop('PYTHONPATH', None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'transcribe.py'),
                    '-c',
                    str(ROOT / 'example/tuber-and-timber-call.toml'),
                    '--plan',
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Profile: standard; language: en', result.stdout)
            self.assertIn('reconcile: reconcile / gpt-6.1-sol', result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_bundled_example_plan_and_mocked_run(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            example = Path(directory)
            for name in (
                'tuber-and-timber-call.toml',
                'tuber-and-timber-call.mp3',
                'context.md',
            ):
                shutil.copyfile(ROOT / 'example' / name, example / name)
            config = str(example / 'tuber-and-timber-call.toml')
            args = ['-c', config]
            with (
                patch(
                    'src.cli.OpenAIAdapter',
                    side_effect=AssertionError('API access'),
                ),
                contextlib.redirect_stdout(io.StringIO()) as plan,
            ):
                self.assertEqual(main([*args, '--plan']), 0)
            self.assertIn('Profile: standard; language: en', plan.getvalue())
            self.assertFalse((example / 'tuber-and-timber-call').exists())
            if not all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
                self.skipTest(
                    'FFmpeg tools not installed; example plan validated'
                )
            adapter = FakeAdapter()
            with (
                patch('src.cli.OpenAIAdapter', return_value=adapter),
                contextlib.redirect_stdout(io.StringIO()) as out,
                contextlib.redirect_stderr(io.StringIO()) as log,
            ):
                self.assertEqual(main(args), 0, log.getvalue())
            self.assertEqual(out.getvalue(), '')
            self.assertEqual(len(adapter.calls), 8)
            self.assertEqual(adapter.calls.count('transcribe'), 2)
            self.assertEqual(adapter.calls.count('diarize'), 2)
            self.assertEqual(adapter.calls.count('reconcile'), 2)
            self.assertNotIn('transcribe_verify', adapter.calls)
            self.assertIn(
                'Preflight: 2 parts, 0 cached, 8 pending — '
                'tuber-and-timber-call.mp3',
                log.getvalue(),
            )
            self.assertRegex(
                log.getvalue(), r'\[8/8\] Finished: summary \(\d+\.\ds\)'
            )
            self.assertNotIn('part 1/1', log.getvalue())
            result = (
                example / 'tuber-and-timber-call'
                / 'tuber-and-timber-call.reconcile.md'
            )
            self.assertTrue(result.is_file())
            self.assertTrue(
                (
                    result.parent / 'tuber-and-timber-call.reconcile.srt'
                ).is_file()
            )
            self.assertTrue(
                (result.parent / 'tuber-and-timber-call.summary.md').is_file()
            )
            self.assertIn(
                'start: "2026-10-10 16:17:32+02:00"', result.read_text()
            )

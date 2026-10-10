# SPDX-License-Identifier: Apache-2.0
"""Validation, cache isolation, and rendering regression tests."""

import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from support import WorkflowFixture
from src.api import InvalidResponse
from src.cli import main
from src.config import load_profile
from src.formats import validate_transcript
from src.rendering import dialogue, load_templates, tags


class ValidationTests(WorkflowFixture, unittest.TestCase):
    def test_cached_runs_do_not_rewrite_outputs(self):
        pipeline = self.run_job()
        before = {
            p: p.stat().st_mtime_ns
            for p in pipeline.root.rglob('*')
            if p.is_file()
        }
        self.run_job()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})

    def test_output_directories_have_independent_caches(self):
        pipeline = self.run_job()
        pipeline.root.rename(self.root / 'other-output')
        self.adapter.calls.clear()
        self.run_job()
        self.assertCountEqual(self.adapter.calls, pipeline.profile.required())

    def test_cache_requires_generation_time_and_current_schema(self):
        pipeline = self.run_job()
        path = next(pipeline.cache.glob('transcribe.*.json'))
        original = json.loads(path.read_text(encoding='utf-8'))
        cases = [
            dict(original, schema=999),
            dict(original, generated_at='2026-01-01T00:00:00'),
        ]
        missing = original.copy()
        del missing['generated_at']
        cases.append(missing)
        self.adapter.calls.clear()
        for record in cases:
            path.write_text(json.dumps(record), encoding='utf-8')
            with (
                self.subTest(record=record),
                self.assertRaisesRegex(ValueError, 'Invalid cache'),
            ):
                self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_cache_edits_with_restored_mtime_are_revalidated(self):
        pipeline = self.run_job()
        pipeline.preflight()
        path = next(pipeline.cache.glob('transcribe.*.json'))
        stat = path.stat()
        original = path.read_text(encoding='utf-8')
        changed = original.replace('"schema": 1', '"schema": 0', 1)
        self.assertNotEqual(changed, original)
        self.assertEqual(len(changed), len(original))
        path.write_text(changed, encoding='utf-8')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.adapter.calls.clear()
        with self.assertRaisesRegex(ValueError, 'Invalid cache'):
            pipeline.run()
        self.assertEqual(self.adapter.calls, [])

    def test_case_collisions_are_rejected(self):
        path = self.root / 'profiles/reviewed.toml'
        path.write_text(
            path.read_text(encoding='utf-8').replace(
                'id = "transcribe_independent"', 'id = "TRANSCRIBE"'
            ),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            load_profile(path)

    def test_bom_configs_and_duplicate_context(self):
        self.recording.write_text(
            'version=1\naudio="call.wav"\n'
            'context=["context.md", "context.md"]\n',
            encoding='utf-8-sig',
        )
        self.assertEqual(
            self.job().context, ('Known vocabulary: Kartoffelkøbing.',)
        )

    def test_missing_template_variable_fails_before_api(self):
        directory = self.root / 'templates'
        directory.mkdir()
        for kind in ('summary', 'reconcile'):
            (directory / (kind + '.j2')).write_text(
                '{{ unknown_variable }}', encoding='utf-8'
            )
        with patch('src.rendering.TEMPLATE_DIR', directory):
            with self.assertRaisesRegex(ValueError, 'Invalid .* template'):
                self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_documented_turn_bounds_are_available_during_preflight(self):
        directory = self.root / 'templates'
        directory.mkdir()
        (directory / 'reconcile.j2').write_text(
            '{% for turn in turns %}{{ turn.start }} '
            '{{ turn.end }} {{ turn.text }}{% endfor %}',
            encoding='utf-8',
        )
        with patch('src.rendering.TEMPLATE_DIR', directory):
            templates = load_templates({'reconcile'})
        self.assertIn('reconcile', templates)
        self.assertEqual(self.adapter.calls, [])

    def test_untimed_turn_template_branch_is_checked_before_api(self):
        directory = self.root / 'templates'
        directory.mkdir()
        (directory / 'reconcile.j2').write_text(
            '{% for turn in turns %}{% if turn.start is none %}'
            '{{ missing_untimed_field }}{% endif %}{% endfor %}',
            encoding='utf-8',
        )
        with (
            patch('src.rendering.TEMPLATE_DIR', directory),
            self.assertRaisesRegex(ValueError, 'Invalid reconcile template'),
        ):
            load_templates({'reconcile'})
        self.assertEqual(self.adapter.calls, [])

    def test_unknown_start_template_branch_is_checked_before_api(self):
        directory = self.root / 'templates'
        directory.mkdir()
        (directory / 'reconcile.j2').write_text(
            '{% if start is none %}'
            '{{ missing_unknown_start_field }}{% endif %}',
            encoding='utf-8',
        )
        (directory / 'summary.j2').write_text('{{ body }}', encoding='utf-8')
        with patch('src.rendering.TEMPLATE_DIR', directory):
            with self.assertRaisesRegex(
                ValueError, 'Invalid reconcile template'
            ):
                self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_reconciliation_body_branch_is_checked_before_api(self):
        directory = self.root / 'templates'
        directory.mkdir()
        (directory / 'reconcile.j2').write_text(
            '{% if body is none %}'
            '{{ missing_reconciliation_field }}{% endif %}',
            encoding='utf-8',
        )
        (directory / 'summary.j2').write_text('{{ body }}', encoding='utf-8')
        with patch('src.rendering.TEMPLATE_DIR', directory):
            with self.assertRaisesRegex(
                ValueError, 'Invalid reconcile template'
            ):
                self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_conflicting_output_directory_fails_before_api(self):
        output = self.root / 'recording'
        output.mkdir()
        (output / 'cache').write_text('not a directory', encoding='utf-8')
        with self.assertRaisesRegex(
            ValueError, 'Expected an output directory'
        ):
            self.run_job()
        self.assertEqual(self.adapter.calls, [])

    def test_output_cannot_overwrite_original_audio(self):
        from src.engine import Pipeline

        directory = self.root / 'recording'
        directory.mkdir()
        (directory / 'call.reconcile.md').symlink_to(self.audio)
        before = self.audio.read_bytes()
        with self.assertRaisesRegex(ValueError, 'overwrite.*audio'):
            Pipeline(
                self.audio, self.job(), self.adapter, log=lambda _: None
            ).preflight()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.audio.read_bytes(), before)

    def test_batch_outputs_cannot_overwrite_other_selected_audio(self):
        first_bytes = self.audio.read_bytes()
        for output in ('md', 'txt'):
            with self.subTest(output=output):
                other = (
                    self.root / 'recording' / self.audio.name
                    / f'call.reconcile.{output}'
                )
                other.parent.mkdir(parents=True, exist_ok=True)
                other.write_bytes(b'fictional second audio fixture')
                other_bytes = other.read_bytes()
                with (
                    patch('src.cli.OpenAIAdapter') as factory,
                    patch('src.engine.check_ffmpeg'),
                    contextlib.redirect_stderr(io.StringIO()) as log,
                ):
                    status = main([
                        '-c', str(self.recording), '-o', output,
                        str(self.audio), str(other),
                    ])
                self.assertEqual(status, 1)
                self.assertIn('Preflight failed', log.getvalue())
                self.assertIn('overwrite', log.getvalue())
                factory.return_value.client.assert_not_called()
                factory.return_value.execute.assert_not_called()
                self.assertEqual(self.audio.read_bytes(), first_bytes)
                self.assertEqual(other.read_bytes(), other_bytes)

    def test_unreadable_audio_is_reported_without_traceback(self):
        with (
            patch(
                'src.engine.audio_hash',
                side_effect=PermissionError('read denied'),
            ),
            contextlib.redirect_stderr(io.StringIO()) as stream,
        ):
            self.assertEqual(main(['-c', str(self.recording)]), 1)
        self.assertIn('Preflight failed', stream.getvalue())
        self.assertNotIn('Traceback', stream.getvalue())

    def test_invalid_json_response_is_retained(self):
        original = self.adapter.execute

        def invalid(stage, *args, **kwargs):
            if stage.id == 'reconcile':
                raise InvalidResponse('Invalid JSON', '{unfinished')
            return original(stage, *args, **kwargs)

        with (
            patch.object(self.adapter, 'execute', invalid),
            self.assertRaisesRegex(ValueError, 'Response saved'),
        ):
            self.run_job()
        files = list(
            (self.root / 'recording/cache').glob('reconcile.failed.*.txt')
        )
        self.assertEqual(files[0].read_text(encoding='utf-8'), '{unfinished')

    def test_no_plain_markdown_link_definitions(self):
        turns = dialogue(
            {
                'segments': [
                    {'speaker': 'A', 'start': 0, 'end': 1, 'text': 'Hej.'}
                ]
            },
            'diarize',
            markdown=True,
        )[0]
        self.assertEqual(turns[0]['label'], '\\[00:00, Speaker A]:')

    def test_filename_start_is_explicitly_unzoned(self):
        from src.rendering import recording_start

        start, source = recording_start(
            Path('delivery_20260412-090000.amr'), None
        )
        self.assertEqual(source, 'filename')
        assert start is not None
        self.assertEqual(start.isoformat(), '2026-04-12T09:00:00')
        self.assertIsNone(start.tzinfo)
        with self.assertRaisesRegex(ValueError, 'date and time'):
            self.job('--start', '2026-04-12')

    def test_bad_segment_types_are_errors(self):
        for segment in (
            [],
            {'text': 'hi', 'speaker': 'A', 'start': True, 'end': 2},
        ):
            with self.assertRaises(ValueError):
                validate_transcript(
                    {'text': 'hi', 'segments': [segment]}, True
                )

    def test_tag_normalization(self):
        self.assertEqual(
            tags(['#Øko kartofler', 'øko-kartofler', 'BÆVER', '', '123']),
            ['øko-kartofler', 'bæver'],
        )

    def test_tag_normalization_preserves_combining_marks(self):
        self.assertEqual(
            tags(['#Øko ma\u030al', 'øko-mål', 'a\u0324', '## 123']),
            ['øko-mål', 'a\u0324'],
        )
        self.assertEqual(
            tags(['a--b', 'a !! b', 'x/y']), ['a--b', 'a-b', 'x/y']
        )

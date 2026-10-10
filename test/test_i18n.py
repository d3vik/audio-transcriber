# SPDX-License-Identifier: Apache-2.0
"""Danish/English behavior and language-catalog structure."""

from dataclasses import asdict, replace
import unittest
from unittest.mock import patch

from support import WorkflowFixture
from src.api import request_rules, transcription_payload
from src.cli import parser
from src.engine import Pipeline
from src.i18n import DEFAULT_LANGUAGE, LANGUAGES, language_config
from src.rendering import dialogue, load_templates, render_stage, summary_body


class LanguageTests(WorkflowFixture, unittest.TestCase):
    def test_catalog_is_complete_and_shared_by_cli_validation(self):
        self.assertEqual(len(LANGUAGES), 26)
        self.assertTrue(
            {'bg', 'cs', 'el', 'hu', 'ro', 'sk'} <= LANGUAGES.keys()
        )
        self.assertEqual(DEFAULT_LANGUAGE, 'da')
        choices = next(
            action.choices for action in parser()._actions
            if action.dest == 'language'
        )
        self.assertEqual(tuple(choices), tuple(LANGUAGES))
        for code, locale in LANGUAGES.items():
            self.assertRegex(code, r'^[a-z]{2}$')
            self.assertEqual(len(locale.summary), 4)
            for value in asdict(locale).values():
                for text in value if isinstance(value, tuple) else (value,):
                    self.assertTrue(text.strip())
                    self.assertNotIn('\n', text)
            self.assertTrue(locale.unclear.startswith('['))
            self.assertTrue(locale.unclear.endswith(']'))

    def test_danish_english_config_and_invalid_values(self):
        self.assertEqual(self.job().language, 'da')
        for code in ('da', 'en'):
            with self.subTest(language=code):
                self.recording.write_text(
                    'version=1\naudio="call.wav"\n'
                    f'language="{code}"\n',
                    encoding='utf-8',
                )
                self.assertEqual(self.job().language, code)
                self.assertEqual(self.job('-l', code).language, code)
        for value in ('xx', 'EN', 'en-US', 'eng', '', True, ['da']):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'ISO 639-1'):
                    language_config(value)

    def test_language_rules_and_unknown_context_labels(self):
        payload = {
            'inputs': [],
            'context': [{
                'type': 'reconcile',
                'result': {
                    'speaker_names': [],
                    'turns': [{
                        'speaker': None, 'text': '80 kg',
                        'source_segments': [], 'notes': [],
                    }],
                    'topic': 'Delivery', 'notes': [],
                },
            }],
        }
        for code, name, marker in (
            ('da', 'Danish', '[uklart]'),
            ('en', 'English', '[unclear]'),
        ):
            with self.subTest(language=code):
                self.assertEqual(request_rules('diarize', code), '')
                for kind in (
                    'transcribe', 'reconcile', 'combine',
                    'research', 'summary',
                ):
                    rules = request_rules(kind, code)
                    self.assertIn(f'{name} ({code})', rules)
                    self.assertIn(marker, rules)
                compact = transcription_payload(payload)
                self.assertEqual(
                    compact['context'][0]['result']['text'],
                    'Unknown: 80 kg',
                )

    def test_localized_summary_research_notes_and_speaker_labels(self):
        for values in (
            (
                'da',
                ('Vigtige punkter', 'Beslutninger', 'Næste skridt',
                 'Usikkerheder'),
                'Taler', 'Ukendt', 'Noter', 'Baggrundsopslag',
                'Webopslag udført.', 'Webkilder',
            ),
            (
                'en',
                ('Key points', 'Decisions', 'Next steps', 'Uncertainties'),
                'Speaker', 'Unknown', 'Notes', 'Background lookup',
                'Web lookup performed.', 'Web sources',
            ),
        ):
            (
                code, headings, speaker, unknown, notes,
                research, status, sources,
            ) = values
            with self.subTest(language=code):
                job = self.job('-l', code)
                summary = {
                    'overview': '80 kg',
                    **{key: ['80 kg'] for key in (
                        'key_points', 'decisions', 'actions', 'uncertainties',
                    )},
                }
                for markdown in (True, False):
                    body = summary_body(summary, code, markdown)
                    for heading in headings:
                        self.assertIn(
                            ('## ' if markdown else '') + heading, body
                        )
                data = {
                    'topic': 'Delivery', 'tags': [], 'notes': ['80 kg'],
                    'speaker_names': [],
                    'turns': [
                        {'speaker': 'A', 'source_segments': [0],
                         'text': '80 kg', 'notes': []},
                        {'speaker': None, 'source_segments': [],
                         'text': '80 kg', 'notes': []},
                    ],
                }
                diary = {
                    'segments': [
                        {'speaker': 'A', 'start': 1, 'end': 2,
                         'text': '80 kg'},
                    ],
                }
                turns, participants, _ = dialogue(
                    data, 'reconcile', diary, True, code
                )
                self.assertEqual(turns[0]['label'], f'\\[00:01, {speaker} A]:')
                self.assertEqual(
                    participants, [f'{speaker} A', f'{speaker} {unknown}']
                )
                text = render_stage(
                    load_templates({'reconcile'}), 'reconcile', data,
                    'reconcile', data, diary, self.audio, job, 10,
                    '2026-01-01T00:00:00+00:00',
                )
                self.assertIn(f'\n{notes}:\n- 80 kg', text)
                for searched in (True, False):
                    result = {
                        'text': '80 kg', 'searched': searched,
                        'sources': [{
                            'url': 'https://example.com', 'title': 'Example',
                        }],
                    }
                    text = render_stage(
                        {}, 'research', result, 'research', result, None,
                        self.audio, job, 10, '2026-01-01T00:00:00+00:00',
                    )
                    self.assertIn(f'## {research}', text)
                    self.assertIn(
                        status if searched else LANGUAGES[code].not_searched,
                        text,
                    )
                    self.assertIn(f'### {sources}', text)

    def test_local_label_change_rerenders_without_model_calls(self):
        original = self.adapter.execute

        def noted(stage, *args):
            result = original(stage, *args)
            if stage.type == 'reconcile':
                result['notes'] = ['Vigtig note.']
            return result

        with patch.object(self.adapter, 'execute', noted):
            pipeline = self.run_job()
        output = pipeline.root / 'call.reconcile.md'
        self.assertIn('Noter:\n- Vigtig note.', output.read_text())
        changed = replace(LANGUAGES['da'], notes='Testnoter')
        self.adapter.calls.clear()
        with patch.dict(LANGUAGES, {'da': changed}):
            pipeline.preflight()
            self.assertFalse(pipeline.needs_api)
            pipeline.run()
        self.assertEqual(self.adapter.calls, [])
        self.assertIn('Testnoter:\n- Vigtig note.', output.read_text())

    def test_unknown_display_label_changes_do_not_repeat_model_calls(self):
        original = self.adapter.execute

        def unnamed(stage, *args):
            result = original(stage, *args)
            if stage.type == 'reconcile':
                result['speaker_names'] = []
                result['turns'][0]['speaker'] = None
            return result

        with patch.object(self.adapter, 'execute', unnamed):
            pipeline = self.run_job()
            self.adapter.calls.clear()
            changed = replace(LANGUAGES['da'], unknown='Uidentificeret')
            with patch.dict(LANGUAGES, {'da': changed}):
                self.run_job()
        self.assertEqual(self.adapter.calls, [])
        output = pipeline.root / 'call.reconcile.md'
        self.assertIn('Taler Uidentificeret', output.read_text())

    def test_declared_names_cannot_hide_fallback_speakers(self):
        for code in ('da', 'en'):
            with self.subTest(language=code):
                locale = LANGUAGES[code]
                data = {
                    'notes': [],
                    'speaker_names': [
                        {'speaker': 'B', 'name': f'{locale.speaker} A'},
                    ],
                    'turns': [
                        {'speaker': speaker, 'source_segments': [],
                         'text': '80 kg', 'notes': []}
                        for speaker in ('A', 'B')
                    ],
                }
                turns, participants, _ = dialogue(
                    data, 'reconcile', {'segments': []}, False, code
                )
                self.assertEqual(len(participants), 2)
                self.assertNotEqual(turns[0]['speaker'], turns[1]['speaker'])
                self.assertIn(f'({locale.speaker} A)', participants[0])
                self.assertIn(f'({locale.speaker} B)', participants[1])

    def test_disambiguation_cannot_collide_with_another_declared_name(self):
        for code in ('da', 'en'):
            with self.subTest(language=code):
                speaker = LANGUAGES[code].speaker
                data = {
                    'notes': [],
                    'speaker_names': [
                        {'speaker': 'A', 'name': 'Morgan'},
                        {'speaker': 'B', 'name': 'Morgan'},
                        {'speaker': 'C', 'name': f'Morgan ({speaker} A)'},
                    ],
                    'turns': [
                        {'speaker': identity, 'source_segments': [],
                         'text': '80 kg', 'notes': []}
                        for identity in ('A', 'B', 'C', 'Unknown', None)
                    ],
                }
                turns, participants, _ = dialogue(
                    data, 'reconcile', {'segments': []}, False, code
                )
                self.assertEqual(len(participants), 5)
                self.assertEqual(
                    len({turn['speaker'] for turn in turns}), 5
                )
                self.assertEqual(turns[2]['speaker'], f'Morgan ({speaker} A)')

    def test_localized_outputs_reuse_caches_across_formats(self):
        original = self.adapter.execute

        def unnamed(stage, *args):
            result = original(stage, *args)
            if stage.type == 'reconcile':
                result['speaker_names'] = []
                result['turns'][0]['speaker'] = None
                result['notes'] = ['80 kg']
            return result

        for code in ('da', 'en'):
            with self.subTest(language=code):
                locale = LANGUAGES[code]
                args = ('-p', 'researched', '-l', code, '--name', code)
                with patch.object(self.adapter, 'execute', unnamed):
                    pipeline = self.run_job(*args)
                    self.adapter.calls.clear()
                    self.run_job(*args, '-o', 'txt')
                    stdout = Pipeline(
                        self.audio, self.job(*args, '-o', 'stdout'),
                        self.adapter, log=lambda _: None,
                    ).run()
                self.assertEqual(self.adapter.calls, [])
                for method in ('md', 'txt'):
                    text = (
                        pipeline.root / f'call.reconcile.{method}'
                    ).read_text(encoding='utf-8')
                    self.assertIn(f'{locale.speaker} {locale.unknown}', text)
                    self.assertIn(f'{locale.notes}:\n- 80 kg', text)
                    research = (
                        pipeline.root / f'call.research.{method}'
                    ).read_text(encoding='utf-8')
                    self.assertIn(locale.research, research)
                    self.assertIn(locale.not_searched, research)
                self.assertIn(f'{locale.speaker} {locale.unknown}', stdout)
                self.assertIn(locale.not_searched, stdout)

    def test_marker_changes_invalidate_prompt_stages_not_diarization(self):
        self.run_job()
        self.adapter.calls.clear()
        changed = replace(LANGUAGES['da'], unclear='[uafklaret]')
        with patch.dict(LANGUAGES, {'da': changed}):
            self.run_job()
        self.assertCountEqual(
            self.adapter.calls,
            self.job().profile.required() - {'diarize'},
        )

    def test_unsupported_toml_language_fails_before_requests(self):
        self.recording.write_text(
            'version=1\naudio="call.wav"\nlanguage="xx"\n',
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'ISO 639-1'):
            Pipeline(self.audio, self.job(), self.adapter)
        self.assertEqual(self.adapter.calls, [])

# SPDX-License-Identifier: Apache-2.0
"""Workflow names, model controls, and isolated web-research lineage."""

import unittest
import tomllib

from support import WorkflowFixture
from src.config import (
    BUNDLED_PROFILES,
    DEFAULT_PROFILE,
    load_model,
    load_profile,
)
from src.formats import validate_research


class ProfileTests(WorkflowFixture, unittest.TestCase):
    def test_naming_policy_uses_context_but_preserves_local_identity(self):
        for role in ('reconcile-draft', 'reconcile-final'):
            with self.subTest(role=role):
                model = load_model(self.root / f'models/{role}.toml')
                self.assertIn('self-introduction is sufficient but not',
                              model.prompt.lower())
                self.assertIn('role corroborated by this part', model.prompt)
                self.assertIn('Identify each part independently', model.prompt)
                self.assertIn('A/B labels restart', model.prompt)
                self.assertIn('Research cannot establish', model.prompt)
                self.assertIn('cannot supply unspoken words', model.prompt)
        combine = load_model(self.root / 'models/combine-parts.toml').prompt
        self.assertIn('Self-introduction is not required', combine)
        self.assertIn('provisional part-stage', combine)
        self.assertIn('Never merge two different', combine)
        self.assertIn(
            'Set name to null when identity remains unclear', combine
        )

    def test_bundled_catalog_defaults_and_model_references(self):
        self.assertEqual(
            {p.stem for p in (self.root / 'profiles').glob('*.toml')},
            set(BUNDLED_PROFILES),
        )
        self.recording.write_text('version=1\naudio="call.wav"\n')
        self.assertEqual(self.job().profile.id, DEFAULT_PROFILE)
        self.assertEqual(DEFAULT_PROFILE, 'reviewed')
        referenced = set()
        for name in BUNDLED_PROFILES:
            path = self.root / f'profiles/{name}.toml'
            raw = tomllib.loads(path.read_text())
            profile = load_profile(path)
            self.assertEqual(profile.id, name)
            self.assertEqual(profile.parts.max_seconds, 300)
            referenced.add((path.parent / raw['parts']['model']).resolve())
            referenced.update(
                (path.parent / stage['model']).resolve()
                for stage in raw['stages']
            )
        self.assertEqual(
            referenced, set((self.root / 'models').glob('*.toml'))
        )

    def test_researched_reuses_reviewed_initial_listens_and_draft(self):
        self.run_job()
        self.adapter.calls.clear()
        self.run_job('-p', self.root / 'profiles/researched.toml')
        self.assertEqual(
            self.adapter.calls,
            ['research', 'transcribe_verify', 'reconcile', 'summary'],
        )

    def test_redo_all_reruns_cached_workflow_even_with_identical_results(self):
        path = self.root / 'profiles/researched.toml'
        # Unused nodes must not add API work, even with --redo all.
        with path.open('a', encoding='utf-8') as stream:
            stream.write(
                '\n[[stages]]\nid="unused"\n'
                'model="../models/transcribe-primary.toml"\n'
            )
        self.run_job('-p', path)
        self.adapter.calls.clear()
        pipeline = self.run_job(
            '-p', path, '--redo', 'all', '--redo', 'transcribe'
        )
        self.assertCountEqual(self.adapter.calls, pipeline.profile.required())
        self.assertEqual(len(self.adapter.calls), 8)
        self.assertNotIn('unused', self.adapter.calls)
        self.adapter.calls.clear()
        self.run_job('-p', path)
        self.assertEqual(self.adapter.calls, [])

    def test_redo_all_does_not_hide_invalid_stage_ids(self):
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.job('--redo', 'all', '--redo', 'unknown')
        path = self.root / 'profiles/reviewed.toml'
        path.write_text(
            path.read_text(encoding='utf-8').replace(
                'id = "transcribe_independent"', 'id = "all"'
            ),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'reserved'):
            load_profile(path)

    def test_research_lineage_and_independent_caches(self):
        pipeline = self.run_job('-p', self.root / 'profiles/researched.toml')
        self.assertEqual(len(self.adapter.calls), 8)
        self.assertLess(
            self.adapter.calls.index('transcribe_independent'),
            self.adapter.calls.index('research'),
        )
        self.assertLess(
            self.adapter.calls.index('research'),
            self.adapter.calls.index('reconcile'),
        )
        self.assertEqual(
            [x['id'] for x in self.adapter.payloads['research']['inputs']],
            ['transcribe', 'transcribe_independent'],
        )
        context = self.adapter.payloads['reconcile']['context']
        self.assertEqual(context[-1]['type'], 'research')
        self.assertLess(
            self.adapter.calls.index('reconcile_draft'),
            self.adapter.calls.index('research'),
        )
        self.assertLess(
            self.adapter.calls.index('research'),
            self.adapter.calls.index('transcribe_verify'),
        )
        self.assertEqual(
            self.adapter.payloads['transcribe_verify']['context'][-1]['id'],
            'research',
        )
        self.assertEqual(
            self.adapter.payloads['summary']['inputs'][0]['id'], 'reconcile'
        )
        self.assertEqual(
            [
                x.get('source')
                for x in self.adapter.payloads['summary']['context']
            ],
            ['user'],
        )
        self.assertIn(
            'Intet webopslag udført.',
            (pipeline.root / 'call.research.md').read_text(
                encoding='utf-8'
            ),
        )
        self.adapter.calls.clear()
        self.run_job('-p', self.root / 'profiles/researched.toml')
        self.assertEqual(self.adapter.calls, [])
        path = self.root / 'models/research-terms.toml'
        path.write_text(
            path.read_text(encoding='utf-8').replace(
                'max_tool_calls = 3', 'max_tool_calls = 2'
            ),
            encoding='utf-8',
        )
        self.run_job('-p', self.root / 'profiles/researched.toml')
        self.assertEqual(
            self.adapter.calls,
            ['research', 'transcribe_verify', 'reconcile', 'summary'],
        )

    def test_reasoning_invalidates_only_affected_branch(self):
        self.run_job()
        self.adapter.calls.clear()
        path = self.root / 'models/reconcile-final.toml'
        path.write_text(
            path.read_text(encoding='utf-8').replace(
                'effort = "high"', 'effort = "medium"'
            ),
            encoding='utf-8',
        )
        self.run_job()
        self.assertEqual(self.adapter.calls, ['reconcile', 'summary'])

    def test_model_controls_are_checked_before_api_work(self):
        path = self.root / 'model.toml'
        base = (
            'version=1\ntype="summary"\n'
            'api_model="gpt-6.1-sol"\nprompt="Summarize"\n'
        )
        cases = [
            base + '[reasoning]\neffort="none"\n',
            base + '[reasoning]\neffort="ultra"\n',
            base + '[reasoning]\nmode="cheap"\n',
            base + '[reasoning]\nsecret=true\n',
            base + 'max_tool_calls=3\n',
            base.replace('summary', 'diarize').replace(
                'prompt="Summarize"\n', ''
            )
            + '[reasoning]\neffort="low"\n',
            base.replace('summary', 'research') + 'max_tool_calls=true\n',
        ]
        for contents in cases:
            path.write_text(contents, encoding='utf-8')
            with (
                self.subTest(contents=contents),
                self.assertRaises(ValueError),
            ):
                load_model(path)

    def test_research_cannot_be_used_as_conversation_input(self):
        path = self.root / 'profiles/researched.toml'
        text = path.read_text(encoding='utf-8')
        path.write_text(
            text.replace('inputs = ["reconcile"]', 'inputs = ["research"]'),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'summary needs'):
            load_profile(path)

    def test_research_rejects_invalid_source_urls(self):
        with self.assertRaisesRegex(ValueError, 'HTTP'):
            validate_research(
                {
                    'text': 'Name',
                    'searched': True,
                    'sources': [
                        {'title': 'Bad', 'url': 'javascript:alert(1)'}
                    ],
                }
            )

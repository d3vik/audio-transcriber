# SPDX-License-Identifier: Apache-2.0
"""Speech-context compaction must preserve evidence and its origin."""

from copy import deepcopy
import unittest

from support import WorkflowFixture
from src.api import transcription_payload


class ContextTests(WorkflowFixture, unittest.TestCase):
    def test_compact_speech_context_preserves_words_names_and_uncertainty(
        self,
    ):
        payload = {
            'inputs': [],
            'part': {'index': 2, 'start': 30, 'end': 60},
            'context': [
                {
                    'source': 'user',
                    'documents': [
                        'Exact spelling: Tuber & Timber.',
                        'Do not change 80 kg.',
                    ],
                },
                {
                    'source': 'previous',
                    'type': 'reconcile',
                    'recipe': 'cache-only',
                    'part': {'index': 1, 'start': 0, 'end': 30},
                    'earlier_parts': [],
                    'result': {
                        'topic': 'Delivery',
                        'tags': ['delivery'],
                        'notes': ['Unclear amount.'],
                        'speaker_names': [{'speaker': 'A', 'name': 'Morgan'}],
                        'turns': [
                            {
                                'speaker': 'A',
                                'text': 'I did NOT agree.',
                                'source_segments': [0],
                                'notes': ['Unclear amount.'],
                            },
                            {
                                'speaker': None,
                                'text': 'Eighty kilograms?',
                                'source_segments': [],
                                'notes': ['Unclear speaker.'],
                            },
                        ],
                    },
                },
            ],
        }
        original = deepcopy(payload)
        compact = transcription_payload(payload)
        self.assertEqual(payload, original)
        self.assertEqual(compact['context'][0], original['context'][0])
        previous = compact['context'][1]
        self.assertEqual(previous['source'], 'previous')
        self.assertEqual(previous['part'], original['context'][1]['part'])
        self.assertNotIn('recipe', previous)
        self.assertEqual(
            previous['result'],
            {
                'text': 'Morgan: I did NOT agree.\nUnknown: Eighty kilograms?',
                'topic': 'Delivery',
                'notes': ['Unclear amount.', 'Unclear speaker.'],
            },
        )

    def test_full_text_and_research_links_are_not_clipped(self):
        transcript = 'Potatoes, not promises. ' * 10000
        notes = (
            'Spelling evidence [1](<https://example.com/terms>); '
            'match uncertain.'
        )
        payload = {
            'context': [
                {
                    'id': 'transcribe',
                    'type': 'transcribe',
                    'result': {'text': transcript, 'usage': {'tokens': 1}},
                    'provenance': {
                        'inputs': [],
                        'context': ['user', 'previous'],
                    },
                },
                {
                    'id': 'research',
                    'type': 'research',
                    'result': {'text': notes, 'sources': [], 'searched': True},
                },
            ]
        }
        compact = transcription_payload(payload)
        self.assertEqual(compact['context'][0]['result'], {'text': transcript})
        self.assertEqual(
            compact['context'][0]['provenance'],
            payload['context'][0]['provenance'],
        )
        self.assertEqual(compact['context'][1]['result'], {'text': notes})

    def test_final_reconciliation_keeps_original_evidence_and_draft(self):
        self.run_job()
        final = self.adapter.payloads['reconcile']
        self.assertEqual(
            [entry['id'] for entry in final['inputs']],
            [
                'transcribe',
                'transcribe_independent',
                'transcribe_verify',
                'diarize',
            ],
        )
        draft = next(
            entry
            for entry in final['context']
            if entry.get('id') == 'reconcile_draft'
        )
        self.assertIn('source_segments', draft['result']['turns'][0])
        verification = self.adapter.payloads['transcribe_verify']['context'][1]
        self.assertIn('text', verification['result'])
        self.assertNotIn('turns', verification['result'])
        self.assertEqual(
            final['inputs'][2]['provenance']['context'],
            ['user', 'previous', 'reconcile_draft'],
        )

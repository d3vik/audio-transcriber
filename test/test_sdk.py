# SPDX-License-Identifier: Apache-2.0
"""Exercise the installed SDK with mocked HTTP requests."""

import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from support import ROOT
from src.api import APIResult, InvalidResponse, OpenAIAdapter, token_usage
from src.config import load_profile
from src.rendering import srt_timestamp

from openai import OpenAI
import httpx2 as httpx


class SDKTests(unittest.TestCase):
    def test_provider_usage_is_captured_without_changing_result_fields(self):
        usage = {
            'input_tokens': 120, 'output_tokens': 45, 'total_tokens': 165,
            'input_tokens_details': {'cached_tokens': 20},
            'output_tokens_details': {'reasoning_tokens': 30},
        }

        def handler(request):
            if request.url.path.endswith('/audio/transcriptions'):
                return httpx.Response(200, json={
                    'text': 'Hello.', 'usage': {'type': 'tokens', **usage},
                })
            return httpx.Response(200, json={
                'id': 'resp_usage', 'object': 'response', 'created_at': 1,
                'status': 'completed', 'model': 'gpt-6.1-sol', 'usage': usage,
                'output': [{
                    'id': 'msg_usage', 'type': 'message', 'role': 'assistant',
                    'status': 'completed', 'content': [{
                        'type': 'output_text', 'text': '{"topic":"Call"}',
                        'annotations': [],
                    }],
                }],
            })

        adapter = OpenAIAdapter()
        adapter._client = OpenAI(
            api_key='offline-test',
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(adapter.close)
        stages = load_profile(ROOT / 'profiles/researched.toml').stages
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'fixture.wav'
            audio.write_bytes(b'fictional fixture')
            for stage_id in ('transcribe', 'summary', 'research'):
                with self.subTest(stage=stage_id):
                    result = adapter.execute(
                        stages[stage_id], {'inputs': [], 'context': []},
                        'en', audio,
                    )
                    self.assertIsInstance(result, APIResult)
                    assert result.usage is not None
                    self.assertEqual(result.usage.input_tokens, 120)
                    self.assertEqual(result.usage.output_tokens, 45)
                    if stage_id != 'transcribe':
                        self.assertNotIn('usage', result)
                    if stage_id == 'summary':
                        self.assertEqual(result, {'topic': 'Call'})

    def test_missing_and_duration_usage_and_invalid_counts(self):
        self.assertIsNone(token_usage(None))
        self.assertIsNone(token_usage({'type': 'duration', 'seconds': 60}))
        for usage in (
            {}, {'input_tokens': -1, 'output_tokens': 2},
            {'input_tokens': True, 'output_tokens': 2},
            {'input_tokens': 1, 'output_tokens': '2'},
        ):
            with self.subTest(usage=usage):
                with self.assertWarnsRegex(RuntimeWarning, 'Invalid provider'):
                    self.assertIsNone(token_usage(usage))

    def test_audio_request_requires_upload_before_client_initialization(self):
        stage = load_profile(ROOT / 'profiles/standard.toml').stages[
            'transcribe'
        ]
        adapter = OpenAIAdapter()
        with self.assertRaisesRegex(ValueError, 'audio upload'):
            adapter.execute(stage, {}, 'en')
        self.assertIsNone(adapter._client)

    def test_mixed_refusal_and_output_text_is_not_accepted(self):
        for kind in ('summary', 'research'):
            stage = load_profile(ROOT / 'profiles/researched.toml').stages[
                kind
            ]

            def handler(request):
                return httpx.Response(
                    200,
                    json={
                        'id': 'resp_test',
                        'object': 'response',
                        'created_at': 1,
                        'status': 'completed',
                        'model': stage.model.api_model,
                        'usage': {
                            'input_tokens': 123, 'output_tokens': 45,
                            'total_tokens': 168,
                        },
                        'output': [
                            {
                                'id': 'msg_test',
                                'type': 'message',
                                'role': 'assistant',
                                'status': 'completed',
                                'content': [
                                    {
                                        'type': 'output_text',
                                        'text': '{"topic":"Call"}',
                                        'annotations': [],
                                    },
                                    {
                                        'type': 'refusal',
                                        'refusal': 'Offline refusal fixture',
                                    },
                                ],
                            }
                        ],
                    },
                )

            adapter = OpenAIAdapter()
            adapter._client = OpenAI(
                api_key='offline-test',
                http_client=httpx.Client(
                    transport=httpx.MockTransport(handler)
                ),
            )
            self.addCleanup(adapter.close)
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(
                    InvalidResponse, 'refused'
                ) as error:
                    adapter.execute(stage, {'inputs': [], 'context': []}, 'en')
                raw = json.loads(error.exception.raw)
                self.assertEqual(
                    raw['output'][0]['content'][0]['text'], '{"topic":"Call"}'
                )
                self.assertEqual(
                    raw['output'][0]['content'][1]['refusal'],
                    'Offline refusal fixture',
                )
                assert error.exception.usage is not None
                self.assertEqual(error.exception.usage.input_tokens, 123)
                self.assertEqual(error.exception.usage.output_tokens, 45)

    def test_incomplete_refused_and_invalid_json_responses_keep_diagnostics(
        self,
    ):
        stage = load_profile(ROOT / 'profiles/reviewed.toml').stages['summary']
        cases = [
            (
                'incomplete',
                {'reason': 'max_output_tokens'},
                {
                    'type': 'output_text',
                    'text': '{unfinished',
                    'annotations': [],
                },
                'Increase max_output_tokens',
                '{unfinished',
            ),
            (
                'completed',
                None,
                {'type': 'refusal', 'refusal': 'Offline refusal fixture'},
                'incomplete or refused',
                'Offline refusal fixture',
            ),
            (
                'completed',
                None,
                {'type': 'output_text', 'text': 'Not JSON', 'annotations': []},
                'invalid JSON',
                'Not JSON',
            ),
        ]
        for status, details, content, message, diagnostic in cases:

            def handler(request):
                return httpx.Response(
                    200,
                    json={
                        'id': 'resp_test',
                        'object': 'response',
                        'created_at': 1,
                        'status': status,
                        'incomplete_details': details,
                        'model': stage.model.api_model,
                        'usage': {
                            'input_tokens': 123, 'output_tokens': 45,
                            'total_tokens': 168,
                        },
                        'output': [
                            {
                                'id': 'msg_test',
                                'type': 'message',
                                'role': 'assistant',
                                'status': 'completed',
                                'content': [content],
                            }
                        ],
                    },
                )

            adapter = OpenAIAdapter()
            adapter._client = OpenAI(
                api_key='offline-test',
                http_client=httpx.Client(
                    transport=httpx.MockTransport(handler)
                ),
            )
            self.addCleanup(adapter.close)
            with (
                self.subTest(status=status, message=message),
                self.assertRaisesRegex(InvalidResponse, message) as error,
            ):
                adapter.execute(stage, {'inputs': [], 'context': []}, 'en')
            self.assertIn(diagnostic, error.exception.raw)
            assert error.exception.usage is not None
            self.assertEqual(error.exception.usage.input_tokens, 123)
            self.assertEqual(error.exception.usage.output_tokens, 45)

    def test_research_request_reasoning_and_provider_citations(self):
        captured = []
        text = (
            'Canonical spelling: Tuber & Timber. '
            '\ue200cite\ue202turn0search0\ue201'
        )
        marker_start = text.index('\ue200')

        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    'id': 'resp_test',
                    'object': 'response',
                    'created_at': 1,
                    'status': 'completed',
                    'model': 'gpt-6.1-sol',
                    'output': [
                        {
                            'type': 'web_search_call',
                            'id': 'ws_test',
                            'status': 'completed',
                            'action': {
                                'type': 'search',
                                'query': 'Tuber & Timber official',
                                'sources': [
                                    {
                                        'type': 'url',
                                        'url': (
                                            'https://example.com/'
                                            'tuber-and-timber'
                                        ),
                                        'title': 'Official spelling',
                                    }
                                ],
                            },
                        },
                        {
                            'id': 'msg_test',
                            'type': 'message',
                            'role': 'assistant',
                            'status': 'completed',
                            'content': [
                                {
                                    'type': 'output_text',
                                    'text': text,
                                    'annotations': [
                                        {
                                            'type': 'url_citation',
                                            'start_index': marker_start,
                                            'end_index': len(text),
                                            'title': 'Official spelling',
                                            'url': (
                                                'https://example.com/'
                                                'tuber-and-timber'
                                            ),
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            )

        adapter = OpenAIAdapter()
        adapter._client = OpenAI(
            api_key='offline-test-key',
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(adapter.close)
        stage = load_profile(ROOT / 'profiles/researched.toml').stages[
            'research'
        ]
        stage = replace(
            stage,
            model=replace(
                stage.model, reasoning={'effort': 'high', 'mode': 'standard'}
            ),
        )
        result = adapter.execute(
            stage,
            {
                'context': [
                    {'source': 'user', 'documents': ['Tuber & Timber']}
                ],
                'inputs': [],
            },
            'en',
        )
        self.assertTrue(result['searched'])
        self.assertEqual(
            result['sources'],
            [
                {
                    'url': 'https://example.com/tuber-and-timber',
                    'title': 'Official spelling',
                }
            ],
        )
        self.assertIn(
            '[1](<https://example.com/tuber-and-timber>)', result['text']
        )
        self.assertNotIn('\ue200', result['text'])
        self.assertEqual(
            captured[0]['reasoning'], {'effort': 'high', 'mode': 'standard'}
        )
        self.assertEqual(captured[0]['tools'], [{'type': 'web_search'}])
        self.assertEqual(captured[0]['max_tool_calls'], 3)
        self.assertEqual(
            captured[0]['include'], ['web_search_call.action.sources']
        )
        self.assertNotIn('text', captured[0])
        self.assertIn('Tuber & Timber', captured[0]['input'])

    def test_requests_and_responses(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith('/audio/transcriptions'):
                data: dict = {'text': 'Hello'}
                if b'gpt-4o-transcribe-diarize' in request.content:
                    data['segments'] = [
                        {
                            'id': '0',
                            'speaker': 'A',
                            'start': 0.0,
                            'end': 1.0,
                            'text': 'Hello',
                        }
                    ]
                return httpx.Response(200, json=data)
            self.assertTrue(request.url.path.endswith('/responses'))
            return httpx.Response(
                200,
                json={
                    'id': 'resp_test',
                    'object': 'response',
                    'created_at': 1,
                    'status': 'completed',
                    'model': 'gpt-6.1-sol',
                    'output': [
                        {
                            'id': 'msg_test',
                            'type': 'message',
                            'role': 'assistant',
                            'status': 'completed',
                            'content': [
                                {
                                    'type': 'output_text',
                                    'text': '{"topic":"Call"}',
                                    'annotations': [],
                                }
                            ],
                        }
                    ],
                },
            )

        adapter = OpenAIAdapter()
        adapter._client = OpenAI(
            api_key='offline-test-key',
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(adapter.close)
        stages = load_profile(ROOT / 'profiles/reviewed.toml').stages
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'fixture.wav'
            audio.write_bytes(b'test audio')
            self.assertEqual(
                adapter.execute(
                    stages['transcribe'], {'context': []}, 'da', audio
                )['text'],
                'Hello',
            )
            self.assertEqual(
                adapter.execute(stages['diarize'], {}, 'da', audio)[
                    'segments'
                ][0]['speaker'],
                'A',
            )
            self.assertEqual(
                adapter.execute(stages['summary'], {'context': []}, 'da'),
                {'topic': 'Call'},
            )
            adapter.execute(stages['transcribe'], {'context': []}, 'en', audio)
            adapter.execute(stages['summary'], {'context': []}, 'en')
        self.assertIn(b'name="prompt"', requests[0].content)
        self.assertNotIn(b'name="prompt"', requests[1].content)
        payload = json.loads(requests[2].content)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['text']['format']['type'], 'json_schema')
        self.assertEqual(payload['max_output_tokens'], 6000)
        self.assertEqual(payload['reasoning'], {'effort': 'medium'})
        self.assertNotIn('tools', payload)
        self.assertIn(b'name="language"\r\n\r\nda', requests[0].content)
        self.assertIn(b'Unintelligible speech marker: [uklart].',
                      requests[0].content)
        self.assertIn('Requested language: Danish (da).',
                      payload['instructions'])
        self.assertIn('Unintelligible speech marker: [uklart].',
                      payload['instructions'])
        self.assertIn(b'name="language"\r\n\r\nen', requests[3].content)
        self.assertIn(b'Unintelligible speech marker: [unclear].',
                      requests[3].content)
        english = json.loads(requests[4].content)
        self.assertIn('Requested language: English (en).',
                      english['instructions'])
        self.assertIn('Unintelligible speech marker: [unclear].',
                      english['instructions'])

    def test_full_cli_with_real_audio_metadata_and_mock_http(self):
        import contextlib
        import io
        import os
        import shutil
        import wave
        from unittest.mock import patch
        from src.cli import main

        if not shutil.which('ffprobe'):
            self.skipTest('FFmpeg/ffprobe is not installed')
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/audio/transcriptions'):
                data: dict = {'text': 'Hej, det er Morgan.'}
                if b'gpt-4o-transcribe-diarize' in request.content:
                    data['segments'] = [
                        {
                            'id': '0',
                            'speaker': 'A',
                            'start': 0.0,
                            'end': 1.0,
                            'text': data['text'],
                        }
                    ]
                return httpx.Response(200, json=data)
            payload = json.loads(request.content)
            if payload['text']['format']['name'] == 'reconcile':
                data = {
                    'topic': 'Testopkald',
                    'tags': ['opkald'],
                    'notes': [],
                    'speaker_names': [{'speaker': 'A', 'name': 'Morgan'}],
                    'turns': [
                        {
                            'speaker': 'A',
                            'source_segments': [0],
                            'text': 'Hej.',
                            'notes': [],
                        }
                    ],
                }
            else:
                data = {
                    'topic': 'Testopkald',
                    'tags': ['opkald'],
                    'overview': 'Morgan ringer.',
                    'key_points': [],
                    'decisions': [],
                    'actions': [],
                    'uncertainties': [],
                }
            return httpx.Response(
                200,
                json={
                    'id': 'resp_test',
                    'object': 'response',
                    'created_at': 1,
                    'status': 'completed',
                    'model': 'gpt-6.1-sol',
                    'output': [
                        {
                            'id': 'msg_test',
                            'type': 'message',
                            'role': 'assistant',
                            'status': 'completed',
                            'content': [
                                {
                                    'type': 'output_text',
                                    'text': json.dumps(data),
                                    'annotations': [],
                                }
                            ],
                        }
                    ],
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'call.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                stream.writeframes(b'\0' * 48000)
            config = root / 'meeting.toml'
            config.write_text(
                'version=1\naudio="call.wav"\nprofile="reviewed"\n',
                encoding='utf-8',
            )
            adapter = OpenAIAdapter()
            adapter._client = OpenAI(
                api_key='offline-test-key',
                http_client=httpx.Client(
                    transport=httpx.MockTransport(handler)
                ),
            )
            with (
                patch('src.cli.OpenAIAdapter', return_value=adapter),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(['-c', str(config)]), 0)
            self.assertEqual(len(calls), 7)
            output = root / 'meeting/call.reconcile.md'
            text = output.read_text(encoding='utf-8')
            self.assertIn('duration_seconds: 3.0', text)
            self.assertIn('\\[00:00, Morgan]: Hej.', text)
            self.assertEqual(
                (output.parent / 'call.reconcile.srt').read_text(),
                '1\n00:00:00,000 --> 00:00:01,000\nMorgan: Hej.\n\n',
            )
            before = output.stat().st_mtime_ns
            with (
                patch.dict(os.environ, {'OPENAI_API_KEY': ''}),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(['-c', str(config)]), 0)
            self.assertEqual(output.stat().st_mtime_ns, before)

    def test_multipart_cli_real_ffmpeg_and_mocked_sdk(self):
        import contextlib
        import io
        import shutil
        import wave
        from unittest.mock import patch
        from src.cli import main

        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('FFmpeg/ffprobe is not installed')
        requests = []

        def handler(request):
            if request.url.path.endswith('/audio/transcriptions'):
                requests.append(('audio', bytes(request.content)))
                data: dict = {'text': 'Hello from this part.'}
                if b'gpt-4o-transcribe-diarize' in request.content:
                    data['segments'] = [
                        {
                            'speaker': 'A',
                            'start': 0.2,
                            'end': 1.0,
                            'text': data['text'],
                        }
                    ]
                return httpx.Response(200, json=data)
            payload = json.loads(request.content)
            kind = payload['text']['format']['name']
            material = json.loads(payload['input'])
            requests.append((kind, material))
            if kind == 'reconcile':
                data = {
                    'topic': 'Delivery',
                    'tags': ['delivery'],
                    'notes': [],
                    'speaker_names': [],
                    'turns': [
                        {
                            'speaker': 'A',
                            'source_segments': [0],
                            'text': 'A delivery.',
                            'notes': [],
                        }
                    ],
                }
            elif kind == 'combine':
                data = {
                    'topic': 'Whole delivery',
                    'tags': ['delivery'],
                    'notes': [],
                    'edits': [],
                    'speaker_groups': [
                        {'speakers': [s['id']], 'name': None}
                        for s in material['inputs']['speakers']
                    ],
                }
                self.assertEqual(len(material['inputs']['units']), 2)
            else:
                self.assertEqual(
                    len(material['inputs'][0]['result']['turns']), 2
                )
                data = {
                    'topic': 'Delivery',
                    'tags': ['delivery'],
                    'overview': 'A full delivery call.',
                    'key_points': [],
                    'decisions': [],
                    'actions': [],
                    'uncertainties': [],
                }
            return httpx.Response(
                200,
                json={
                    'id': 'resp_test',
                    'object': 'response',
                    'created_at': 1,
                    'status': 'completed',
                    'model': payload['model'],
                    'output': [
                        {
                            'id': 'msg_test',
                            'type': 'message',
                            'role': 'assistant',
                            'status': 'completed',
                            'content': [
                                {
                                    'type': 'output_text',
                                    'text': json.dumps(data),
                                    'annotations': [],
                                }
                            ],
                        }
                    ],
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'call.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                stream.writeframes(
                    b'\0' * 240000
                )  # 15 seconds, two parts with a 10-second cap.
            shutil.copytree(ROOT / 'models', root / 'models')
            shutil.copytree(ROOT / 'profiles', root / 'profiles')
            profile = root / 'profiles/reviewed.toml'
            profile.write_text(
                profile.read_text().replace(
                    'max_seconds = 300', 'max_seconds = 10'
                )
            )
            adapter = OpenAIAdapter()
            adapter._client = OpenAI(
                api_key='offline-test',
                http_client=httpx.Client(
                    transport=httpx.MockTransport(handler)
                ),
            )
            args = [str(audio), '-p', str(profile), '-l', 'en']
            with (
                patch('src.cli.OpenAIAdapter', return_value=adapter),
                contextlib.redirect_stderr(io.StringIO()) as log,
            ):
                self.assertEqual(main(args), 0, log.getvalue())
            self.assertEqual([kind for kind, _ in requests].count('audio'), 8)
            self.assertEqual(
                [kind for kind, _ in requests].count('reconcile'), 4
            )
            self.assertEqual(
                [kind for kind, _ in requests].count('combine'), 1
            )
            self.assertEqual(
                [kind for kind, _ in requests].count('summary'), 1
            )
            speech_contexts = [
                json.loads(
                    data.split(b'Context JSON:\n', 1)[1].split(b'\r\n', 1)[0]
                )
                for kind, data in requests
                if kind == 'audio' and b'Context JSON:\n' in data
            ]
            verification = [
                payload
                for payload in speech_contexts
                if any(
                    entry.get('id') == 'reconcile_draft'
                    for entry in payload['context']
                )
            ]
            self.assertEqual(len(verification), 2)
            for payload in verification:
                draft = next(
                    entry
                    for entry in payload['context']
                    if entry.get('id') == 'reconcile_draft'
                )
                self.assertEqual(draft['result']['text'], 'A: A delivery.')
                self.assertNotIn('source_segments', json.dumps(draft))
            previous = [
                entry
                for payload in speech_contexts
                for entry in payload['context']
                if entry.get('source') == 'previous'
            ]
            self.assertEqual(len(previous), 3)
            self.assertTrue(all('recipe' not in entry for entry in previous))
            document = (root / 'call/call.reconcile.md').read_text()
            self.assertIn('duration_seconds: 15.0', document)
            self.assertIn('Speaker P0002:A', document)
            subtitles = (root / 'call/call.reconcile.srt').read_text()
            self.assertIn('00:00:00,200 --> 00:00:01,000', subtitles)
            second_start = max(
                material['part']['start']
                for kind, material in requests if kind == 'reconcile'
            )
            start = srt_timestamp(round((second_start + 0.2) * 1000))
            end = srt_timestamp(round((second_start + 1.0) * 1000))
            self.assertIn(f'{start} --> {end}', subtitles)
            self.assertIn('Speaker P0002:A: A delivery.', subtitles)
            with (
                patch('src.cli.OpenAIAdapter') as factory,
                patch('src.engine.audio_duration', side_effect=AssertionError),
                patch('src.engine.check_ffmpeg', side_effect=AssertionError),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(args), 0)
                factory.return_value.client.assert_not_called()
                factory.return_value.execute.assert_not_called()

# SPDX-License-Identifier: Apache-2.0
"""Shared offline fixtures and source-tree import setup."""

from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.payloads = {}
        self.fail: str | None = None
        self.coordinate = False
        self.started = threading.Barrier(3)

    def client(self):
        return self

    def close(self):
        pass

    def execute(self, stage, payload, language, upload=None) -> dict:
        self.calls.append(stage.id)
        self.payloads[stage.id] = payload
        if self.coordinate and stage.id in (
            'transcribe',
            'transcribe_independent',
            'diarize',
        ):
            self.started.wait(timeout=3)
        if stage.id == self.fail:
            raise RuntimeError('Simulated API failure')
        if stage.type == 'combine':
            return {
                'topic': 'Full recording',
                'tags': ['recording'],
                'notes': [],
                'edits': [],
                'speaker_groups': [
                    {'speakers': [s['id']], 'name': s['declared_name']}
                    for s in payload['inputs']['speakers']
                ],
            }
        if stage.type == 'transcribe':
            assert upload is not None
            assert upload.is_file()
            return {'text': 'Hej, det er Morgan.'}
        if stage.type == 'diarize':
            return {
                'text': 'Hej, det er Morgan.',
                'segments': [
                    {
                        'speaker': 'A',
                        'start': 0.2,
                        'end': 2.4,
                        'text': 'Hej, det er Morgan.',
                    }
                ],
            }
        if stage.type == 'research':
            return {
                'text': 'No lookup needed; user supplied the spelling.',
                'sources': [],
                'searched': False,
            }
        if stage.type == 'reconcile':
            return {
                'topic': 'Samtale med Morgan',
                'tags': ['telefon'],
                'turns': [
                    {
                        'source_segments': [0],
                        'speaker': 'A',
                        'text': 'Hej, det er Morgan.',
                        'notes': [],
                    }
                ],
                'notes': [],
                'speaker_names': [{'speaker': 'A', 'name': 'Morgan'}],
            }
        return {
            'topic': 'Telefonopkald',
            'tags': ['telefon'],
            'overview': 'Morgan ringer.',
            'key_points': [],
            'decisions': [],
            'actions': [],
            'uncertainties': [],
        }


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('profiles', 'models'):
            shutil.copytree(ROOT / directory, self.root / directory)
        self.audio = self.root / 'call.wav'
        self.audio.write_bytes(b'audio fixture')
        self.context = self.root / 'context.md'
        self.context.write_text(
            'Known vocabulary: Kartoffelkøbing.', encoding='utf-8'
        )
        self.recording = self.root / 'recording.toml'
        self.recording.write_text(
            'version=1\naudio="call.wav"\ncontext=["context.md"]\n'
            'language="da"\nprofile="profiles/reviewed.toml"\n',
            encoding='utf-8',
        )
        self.adapter = FakeAdapter()
        self.duration_patch = patch(
            'src.engine.audio_duration', return_value=65.7
        )
        self.duration_patch.start()
        self.addCleanup(self.duration_patch.stop)

    def job(self, *extra):
        from src.cli import parser
        from src.config import load_job

        return load_job(
            parser().parse_args(['-c', str(self.recording), *map(str, extra)])
        )

    def run_job(self, *extra):
        from src.engine import Pipeline

        pipeline = Pipeline(
            self.audio, self.job(*extra), self.adapter, log=lambda value: None
        )
        pipeline.run()
        return pipeline

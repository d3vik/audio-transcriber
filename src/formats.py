# SPDX-License-Identifier: Apache-2.0
"""Validate structured response contracts without presentation logic."""

import math
from urllib.parse import urlsplit


def validate_research(data):
    if not isinstance(data, dict) or set(data) != {
        'text',
        'sources',
        'searched',
    }:
        raise ValueError('Invalid research result fields.')
    if (
        not isinstance(data['text'], str)
        or not data['text'].strip()
        or type(data['searched']) is not bool
    ):
        raise ValueError('Research needs text and a searched flag.')
    if not isinstance(data['sources'], list):
        raise ValueError('Research sources must be a list.')
    for source in data['sources']:
        if (
            not isinstance(source, dict)
            or set(source) != {'url', 'title'}
            or not all(isinstance(source[k], str) for k in ('url', 'title'))
        ):
            raise ValueError('Invalid research source.')
        if (
            urlsplit(source['url']).scheme not in ('http', 'https')
            or not urlsplit(source['url']).hostname
        ):
            raise ValueError('Research source must be an HTTP(S) URL.')


SUMMARY_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'topic',
        'tags',
        'overview',
        'key_points',
        'decisions',
        'actions',
        'uncertainties',
    ],
    'properties': {
        'topic': {'type': 'string'},
        'tags': {'type': 'array', 'items': {'type': 'string'}},
        'overview': {'type': 'string'},
        **{
            key: {'type': 'array', 'items': {'type': 'string'}}
            for key in ('key_points', 'decisions', 'actions', 'uncertainties')
        },
    },
}


RECONCILE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['turns', 'notes', 'speaker_names', 'topic', 'tags'],
    'properties': {
        'topic': {'type': 'string'},
        'tags': {'type': 'array', 'items': {'type': 'string'}},
        'speaker_names': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['speaker', 'name'],
                'properties': {
                    'speaker': {'type': 'string'},
                    'name': {'type': 'string'},
                },
            },
        },
        'turns': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['source_segments', 'speaker', 'text', 'notes'],
                'properties': {
                    'source_segments': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                    },
                    'speaker': {'type': ['string', 'null']},
                    'text': {'type': 'string'},
                    'notes': {'type': 'array', 'items': {'type': 'string'}},
                },
            },
        },
        'notes': {'type': 'array', 'items': {'type': 'string'}},
    },
}

COMBINE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['topic', 'tags', 'speaker_groups', 'edits', 'notes'],
    'properties': {
        'topic': {'type': 'string'},
        'tags': {'type': 'array', 'items': {'type': 'string'}},
        'notes': {'type': 'array', 'items': {'type': 'string'}},
        'speaker_groups': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['speakers', 'name'],
                'properties': {
                    'speakers': {'type': 'array', 'items': {'type': 'string'}},
                    'name': {'type': ['string', 'null']},
                },
            },
        },
        'edits': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['unit', 'text'],
                'properties': {
                    'unit': {'type': 'integer'},
                    'text': {'type': 'string'},
                },
            },
        },
    },
}

RESULT_SCHEMAS = {
    'reconcile': RECONCILE_SCHEMA,
    'summary': SUMMARY_SCHEMA,
    'combine': COMBINE_SCHEMA,
}


def validate_transcript(data, diarize, *, duration=None):
    if not isinstance(data, dict) or not isinstance(data.get('text'), str):
        raise ValueError('Transcript must contain a text string.')
    if not diarize:
        return
    if not isinstance(data.get('segments'), list):
        raise ValueError('Diarized transcript must contain a segments array.')
    if data['text'].strip() and not data['segments']:
        raise ValueError('Diarized transcript has text but no segments.')
    previous_start = 0.0
    for segment in data['segments']:
        if not isinstance(segment, dict) or not isinstance(
            segment.get('text'), str
        ):
            raise ValueError('Invalid segment text.')
        speaker = segment.get('speaker')
        if (
            not isinstance(speaker, str)
            or not speaker.strip()
            or '\n' in speaker
            or '\r' in speaker
        ):
            raise ValueError('Invalid diarization speaker ID.')
        start, end = segment.get('start'), segment.get('end')
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(start)
            or not math.isfinite(end)
        ):
            raise ValueError('Invalid segment timestamps.')
        if not 0 <= start <= end:
            raise ValueError('Invalid segment time range.')
        if start < previous_start:
            raise ValueError(
                'Diarization segments must be in start-time order.'
            )
        # Codec padding and rounded times can cause small overruns.
        if duration is not None and (start > duration or end > duration + 1.0):
            raise ValueError(
                'Diarization timestamps exceed the audio part duration.'
            )
        previous_start = start


def validate_reconciliation(data, source):
    turns = data.get('turns')
    if not isinstance(turns, list) or not isinstance(data.get('notes'), list):
        raise ValueError('Invalid reconciliation structure.')
    covered = []
    speakers = {s.get('speaker') for s in source['segments']}
    names = data.get('speaker_names', [])
    if not isinstance(names, list):
        raise ValueError('Invalid speaker name mapping.')
    named = set()
    for entry in names:
        if not isinstance(entry, dict):
            raise ValueError('Invalid speaker name mapping.')
        speaker, name = entry.get('speaker'), entry.get('name')
        if (
            not isinstance(speaker, str)
            or speaker not in speakers
            or speaker in named
        ):
            raise ValueError('Unknown or duplicate named speaker ID.')
        if (
            not isinstance(name, str)
            or not name.strip()
            or '\n' in name
            or '\r' in name
        ):
            raise ValueError('Invalid speaker name.')
        named.add(speaker)
    previous_start = 0.0
    for turn in turns:
        indices = turn['source_segments']
        if not isinstance(indices, list) or any(
            type(i) is not int for i in indices
        ):
            raise ValueError('Invalid reconciliation source indices.')
        covered.extend(indices)
        if turn['speaker'] is not None and turn['speaker'] not in speakers:
            raise ValueError('Reconciliation invented a speaker ID.')
        if indices != sorted(set(indices)):
            raise ValueError(
                'Source indices within each turn must be '
                'unique and increasing.'
            )
        if any(not 0 <= i < len(source['segments']) for i in indices):
            raise ValueError('Reconciliation source index is out of range.')
        if not isinstance(turn['text'], str) or not isinstance(
            turn['notes'], list
        ):
            raise ValueError('Invalid reconciliation text/notes.')
        if indices:
            segments = [source['segments'][i] for i in indices]
            if not turn['text'].strip() and any(
                s['text'].strip() for s in segments
            ):
                raise ValueError(
                    'Reconciliation cannot replace spoken segments '
                    'with empty text.'
                )
            start = min(s['start'] for s in segments)
            if start < previous_start:
                raise ValueError(
                    'Reconciliation turns must be in start-time order.'
                )
            previous_start = start
    if sorted(covered) != list(range(len(source['segments']))):
        raise ValueError(
            'Reconciliation omitted or duplicated source segments.'
        )


def validate_schema(value, schema, location='result'):
    """Validate the JSON-schema subset for output contracts."""
    allowed = schema['type']
    allowed = allowed if isinstance(allowed, list) else [allowed]
    matches = {
        'object': isinstance(value, dict),
        'array': isinstance(value, list),
        'string': isinstance(value, str),
        'integer': type(value) is int,
        'null': value is None,
    }
    if not any(matches[kind] for kind in allowed):
        raise ValueError(f'{location}: expected {allowed}.')
    if isinstance(value, dict):
        properties = schema['properties']
        if set(value) != set(properties):
            raise ValueError(f'{location}: incorrect fields.')
        for key, item in value.items():
            validate_schema(item, properties[key], f'{location}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate_schema(item, schema['items'], f'{location}[{index}]')

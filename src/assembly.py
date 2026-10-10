# SPDX-License-Identifier: Apache-2.0
"""Lossless assembly and validated, sparse model corrections."""

from copy import deepcopy

from .formats import RESULT_SCHEMAS, validate_schema


def local_speaker(part, speaker):
    return f'P{part["index"]:04d}:{speaker}' if speaker is not None else None


def collect(stage, parts, records, profile):
    """Keep material, globalize offsets and namespace identities."""
    values = [record[stage.id].data for record in records]
    if stage.type == 'transcribe':
        return {'text': '\n\n'.join(value['text'].strip() for value in values)}
    if stage.type == 'research':
        sources = {s['url']: s for value in values for s in value['sources']}
        return {
            'text': '\n\n'.join(
                f'Part {part["index"]}:\n{value["text"]}'
                for part, value in zip(parts, values)
            ),
            'sources': list(sources.values()),
            'searched': any(value['searched'] for value in values),
        }
    if stage.type == 'diarize':
        segments = []
        for part, value in zip(parts, values):
            for segment in value['segments']:
                segments.append(
                    {
                        **segment,
                        'speaker': local_speaker(part, segment['speaker']),
                        'start': segment['start'] + part['start'],
                        'end': segment['end'] + part['start'],
                    }
                )
        return {
            'text': '\n\n'.join(value['text'] for value in values),
            'segments': segments,
        }
    diary_id = next(
        ref for ref in stage.inputs if profile.stages[ref].type == 'diarize'
    )
    turns, names, notes, offset = [], [], [], 0
    for part, value, record in zip(parts, values, records):
        for turn in value['turns']:
            turns.append(
                {
                    **turn,
                    'speaker': local_speaker(part, turn['speaker']),
                    'source_segments': [
                        index + offset for index in turn['source_segments']
                    ],
                }
            )
        names.extend(
            {
                'speaker': local_speaker(part, item['speaker']),
                'name': item['name'],
            }
            for item in value['speaker_names']
        )
        notes.extend(value['notes'])
        offset += len(record[diary_id].data['segments'])
    return {
        'turns': turns,
        'speaker_names': names,
        'notes': list(dict.fromkeys(notes)),
        'topic': values[0]['topic'],
        'tags': list(dict.fromkeys(t for v in values for t in v['tags'])),
    }


def combination_input(stage, source, diary, parts, records):
    units, speakers = [], {}
    if stage.type == 'transcribe':
        # Correct paragraph units without repeating entire parts.
        for part, record in zip(parts, records):
            for paragraph in record[stage.id].data['text'].split('\n'):
                if paragraph.strip():
                    units.append(
                        {
                            'id': len(units),
                            'part': part['index'],
                            'text': paragraph.strip(),
                        }
                    )
    else:
        names = {
            item['speaker']: item['name'] for item in source['speaker_names']
        }
        for segment in diary['segments']:
            speaker = segment['speaker']
            speakers[speaker] = {
                'id': speaker,
                'part': int(speaker.split(':', 1)[0][1:]),
                'declared_name': names.get(speaker),
            }
        for turn in source['turns']:
            times = [
                diary['segments'][i]['start'] for i in turn['source_segments']
            ]
            units.append(
                {
                    'id': len(units),
                    'speaker': turn['speaker'],
                    'text': turn['text'],
                    'start': min(times) if times else None,
                    'notes': turn['notes'],
                }
            )
    return {
        'units': units,
        'speakers': list(speakers.values()),
        'parts': parts,
        'source_type': stage.type,
        'notes': source.get('notes', []),
    }


def validate_combination(data, material):
    validate_schema(data, RESULT_SCHEMAS['combine'])
    supplied = {s['id']: s['part'] for s in material['speakers']}
    seen = set()
    for group in data['speaker_groups']:
        ids = group['speakers']
        if (
            not ids
            or len(set(ids)) != len(ids)
            or any(s not in supplied or s in seen for s in ids)
        ):
            raise ValueError(
                'Combine must include each supplied speaker exactly once.'
            )
        if len({supplied[s] for s in ids}) != len(ids):
            raise ValueError(
                'Combine cannot merge distinct speakers from the same part.'
            )
        seen.update(ids)
        name = group['name']
        if name is not None and (
            not name.strip() or '\n' in name or '\r' in name
        ):
            raise ValueError('Invalid combined speaker name.')
    if seen != set(supplied):
        raise ValueError('Combine omitted a speaker.')
    edited = set()
    for edit in data['edits']:
        i = edit['unit']
        if (
            not 0 <= i < len(material['units'])
            or i in edited
            or not edit['text'].strip()
        ):
            raise ValueError(
                'Combine edits must target unique existing units '
                'and cannot delete them.'
            )
        edited.add(i)


def apply_combination(data, material, source, diary):
    validate_combination(data, material)
    edits = {edit['unit']: edit['text'] for edit in data['edits']}
    if material['source_type'] == 'transcribe':
        return (
            {
                'text': '\n\n'.join(
                    edits.get(i, unit['text'])
                    for i, unit in enumerate(material['units'])
                )
            },
            None,
        )
    result, diary = deepcopy(source), deepcopy(diary)
    mapping, names = {}, []
    for group in data['speaker_groups']:
        canonical = group['speakers'][0]
        mapping.update({speaker: canonical for speaker in group['speakers']})
        if group['name'] is not None:
            names.append({'speaker': canonical, 'name': group['name'].strip()})
    for i, turn in enumerate(result['turns']):
        turn['text'] = edits.get(i, turn['text'])
        turn['notes'] = []
        turn['speaker'] = mapping.get(turn['speaker'], turn['speaker'])
    for segment in diary['segments']:
        segment['speaker'] = mapping[segment['speaker']]
    result.update(
        topic=data['topic'],
        tags=data['tags'],
        speaker_names=names,
        notes=data['notes'],
    )
    return result, diary


def previous_context(profile, parts, records):
    """Carry the previous result and headings, never nested drafts."""
    if not records:
        return None
    source = profile.stages[profile.parts.source]
    previous = records[-1].get(source.id)
    if previous is None:
        return None
    earlier = []
    for part, record in zip(parts, records[:-1]):
        result = record.get(source.id)
        if result and source.type == 'reconcile':
            earlier.append(
                {
                    'part': part['index'],
                    'topic': result.data['topic'],
                    'declared_names': [
                        item['name'] for item in result.data['speaker_names']
                    ],
                }
            )
    return {
        'source': 'previous',
        'part': parts[len(records) - 1],
        'type': source.type,
        'result': previous.data,
        'recipe': previous.key,
        'earlier_parts': earlier,
    }

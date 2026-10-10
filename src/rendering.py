# SPDX-License-Identifier: Apache-2.0
"""Render documents and metadata without changing model requests."""

from collections import Counter
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote

from .i18n import language_config

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / 'templates'
SUBTITLE_RECIPE = 'reconciled-turns-ms-gap-v1'


def elapsed(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f'{minutes:02d}:{seconds:02d}'


def recording_start(audio, explicit):
    if explicit is not None:
        return explicit, 'configured'
    match = re.search(r'(?<!\d)(\d{8})[-_](\d{6})(?!\d)', audio.stem)
    if match:
        try:
            return (
                datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S'),
                'filename',
            )
        except ValueError:
            pass
    return None, 'unknown'


def tags(values):
    result = []
    for value in values:
        characters = []
        separator = False
        for char in unicodedata.normalize('NFC', value.lower()):
            if (
                char.isalnum() or char in '_/-'
                or unicodedata.category(char).startswith('M')
            ):
                characters.append(char)
                separator = False
            elif not separator:
                characters.append('-')
                separator = True
        tag = ''.join(characters).strip('-_/#')
        if tag and not tag.isdecimal() and tag not in result:
            result.append(tag)
    return result


def inline(text):
    return re.sub(
        r'([\\`*_\[\]<>])', r'\\\1', text.replace('\r', ' ').replace('\n', ' ')
    )


def dialogue(data, kind, diary=None, markdown=False, language='en'):
    """Share a speaker/timing view across transcripts and summaries."""
    locale = language_config(language)
    if kind == 'transcribe':
        return [], [], []
    if kind == 'diarize':
        diary = data
        raw = [
            {
                'speaker': s['speaker'],
                'source_segments': [i],
                'text': s['text'],
                'notes': [],
            }
            for i, s in enumerate(diary['segments'])
        ]
        names, notes = {}, []
    else:
        if diary is None:
            raise ValueError(
                'Reconciliation rendering requires its diarization result.'
            )
        raw = data['turns']
        names = {
            entry['speaker']: entry['name'].strip()
            for entry in data['speaker_names']
        }
        notes = list(data['notes'])
    labels = {
        **names,
        **{
            turn['speaker']: names.get(
                turn['speaker'],
                f'{locale.speaker} {turn["speaker"] or locale.unknown}',
            )
            for turn in raw if turn['text'].strip()
        },
    }
    counts = Counter(labels.values())
    reserved = set(labels.values())
    for speaker, label in labels.items():
        if counts[label] > 1:
            identity = speaker if speaker is not None else '?'
            base = f'{label} ({locale.speaker} {identity})'
            candidate, number = base, 2
            while candidate in reserved:
                candidate = f'{base} ({number})'
                number += 1
            labels[speaker] = candidate
            reserved.add(candidate)
    turns, participants = [], []
    for turn in raw:
        notes.extend(turn['notes'])
        if not turn['text'].strip():
            continue
        speaker = turn['speaker']
        label = labels[speaker]
        if label not in participants:
            participants.append(label)
        indices = turn['source_segments']
        start = (
            min(diary['segments'][i]['start'] for i in indices)
            if indices else None
        )
        end = (
            max(diary['segments'][i]['end'] for i in indices)
            if indices else None
        )
        time = (
            elapsed(start)
            if start is not None
            else '??:??'
        )
        # Unescaped [label]: word can become an invisible Markdown link.
        prefix = (
            f'\\[{time}, {inline(label)}]:'
            if markdown
            else f'[{time}, {label}]:'
        )
        turns.append(
            {
                'time': time,
                'speaker': label,
                'label': prefix,
                'text': turn['text'].strip(),
                'start': start,
                'end': end,
            }
        )
    return turns, participants, list(dict.fromkeys(notes))


def srt_timestamp(milliseconds):
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def render_srt(data, diary, duration, language, *, merge_overlaps=False):
    """Keep source timing; interpolate untimed speech into free gaps."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('SRT requires a finite, positive recording duration.')
    turns, _, _ = dialogue(data, 'reconcile', diary, language=language)
    limit = round(duration * 1000)
    bounds = []
    for index, turn in enumerate(turns, 1):
        if turn['start'] is None:
            bounds.append(None)
            continue
        start = round(turn['start'] * 1000)
        end = min(limit, round(turn['end'] * 1000))
        if not 0 <= start < end <= limit:
            raise ValueError(
                f'SRT turn {index} has no positive source duration '
                'at millisecond precision; review its diarization timing.'
            )
        bounds.append((start, end))

    index, previous_end = 0, 0
    while index < len(bounds):
        if bounds[index] is not None:
            previous_end = max(previous_end, bounds[index][1])
            index += 1
            continue
        first = index
        while index < len(bounds) and bounds[index] is None:
            index += 1
        next_start = bounds[index][0] if index < len(bounds) else limit
        count = index - first
        interval = next_start - previous_end
        if interval < count:
            raise ValueError(
                f'SRT turns {first + 1}-{index} cannot be interpolated: '
                'no sufficient gap between source timings. '
                'Review the reconciliation/diarization; '
                'timed cues have not been shifted.'
            )
        for offset in range(count):
            bounds[first + offset] = (
                previous_end + interval * offset // count,
                previous_end + interval * (offset + 1) // count,
            )
        previous_end = next_start

    cues = []
    for turn, (start, end) in zip(turns, bounds):
        text = re.sub(r'\s+', ' ', turn['text']).strip()
        cues.append((start, end, f'{turn["speaker"]}: {text}'))
    if merge_overlaps:
        events = {}
        for index, (start, end, text) in enumerate(cues):
            events.setdefault(start, []).append((index, text))
            events.setdefault(end, []).append((index, None))
        cues, active, previous = [], {}, None
        for time, updates in sorted(events.items()):
            if previous is not None and active:
                text = '\n'.join(active[index] for index in sorted(active))
                cues.append((previous, time, text))
            for index, text in updates:
                if text is None:
                    del active[index]
                else:
                    active[index] = text
            previous = time
    blocks = []
    for index, (start, end, text) in enumerate(cues, 1):
        blocks.append(
            f'{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n'
            f'{text}'
        )
    return '\n\n'.join(blocks) + '\n\n' if blocks else ''


def document_fields(
    audio, start, duration, profile, generated_at, data, participants
):
    start, source = recording_start(audio, start)
    return {
        'start': (
            start.isoformat(sep=' ', timespec='seconds') if start else None
        ),
        'end': (
            (start + timedelta(seconds=duration)).isoformat(
                sep=' ', timespec='seconds'
            )
            if start
            else None
        ),
        'start_source': source,
        'participants': participants,
        'topic': data['topic'],
        'tags': tags(data['tags']),
        'length': elapsed(duration),
        'duration_seconds': round(duration, 3),
        'profile': profile,
        'generated_at': generated_at,
        'parts': 1,
    }


def load_templates(kinds):
    kinds = set(kinds) & {'reconcile', 'summary'}
    if not kinds:
        return {}
    try:
        from jinja2 import Environment, StrictUndefined, FileSystemLoader
    except ImportError as exc:
        raise ValueError(
            'Jinja2 is required for document output: run pipenv sync.'
        ) from exc
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    environment.filters['yaml'] = lambda value: json.dumps(
        value, ensure_ascii=False, allow_nan=False
    )
    # Exercise populated branches to catch template errors before calls.
    example = document_fields(
        Path('recording.wav'),
        datetime(2026, 1, 1),
        60,
        'standard',
        '2026-01-01T00:00:00+00:00',
        {'topic': 'Example', 'tags': ['example']},
        ['Speaker A'],
    )
    example.update(
        turns=[
            {
                'time': '00:00',
                'speaker': 'Speaker A',
                'label': '\\[00:00, Speaker A]:',
                'text': 'Example',
                'start': 0.0,
                'end': 1.0,
            }
        ],
        notes=['Example note'],
        notes_heading=language_config('en').notes,
        body='Example summary',
    )
    templates = {}
    for kind in sorted(kinds):
        try:
            template = environment.get_template(kind + '.j2')
            for source in ('configured', 'filename', 'unknown'):
                for timed in (True, False):
                    turn = dict(example['turns'][0])
                    if not timed:
                        turn.update(
                            start=None, end=None, time='??:??',
                            label='\\[??:??, Speaker A]:',
                        )
                    fields = {
                        **example,
                        'start_source': source,
                        'body': (
                            None if kind == 'reconcile' else example['body']
                        ),
                        'turns': [turn],
                    }
                    if source == 'unknown':
                        fields.update(start=None, end=None)
                    template.render(**fields)
            templates[kind] = template
        except Exception as exc:
            raise ValueError(f'Invalid {kind} template: {exc}') from exc
    return templates


def summary_body(data, language, markdown):
    labels = language_config(language).summary
    blocks = [data['overview'].strip()]
    for field, label in zip(
        ('key_points', 'decisions', 'actions', 'uncertainties'), labels
    ):
        if data[field]:
            blocks.extend(
                [
                    ('## ' if markdown else '') + label,
                    '\n'.join('- ' + item for item in data[field]),
                ]
            )
    return '\n\n'.join(blocks) + '\n'


def render_stage(
    templates,
    kind,
    data,
    source_kind,
    source,
    diary,
    audio,
    job,
    duration,
    date,
    part_count=1,
    method='md',
):
    markdown = method == 'md'
    locale = language_config(job.language)
    if kind == 'research':
        status = locale.searched if data['searched'] else locale.not_searched
        lines = [
            ('## ' if markdown else '') + locale.research,
            status,
            data['text'].strip(),
        ]
        if data['sources']:
            lines.append(('### ' if markdown else '') + locale.sources)
            for ref in data['sources']:
                url = quote(ref['url'], safe='/:#?&=%+@;,-._~!$\'()*[]')
                lines.append(
                    f'- {inline(ref["title"])}: <{url}>'
                    if markdown
                    else f'- {ref["title"]}: {ref["url"]}'
                )
        return '\n\n'.join(lines) + '\n'
    turns, participants, notes = dialogue(
        source, source_kind, diary, markdown, job.language
    )
    if kind in ('reconcile', 'summary'):
        fields = document_fields(
            audio,
            job.start,
            duration,
            job.profile.id,
            date,
            data,
            participants,
        )
        fields['parts'] = part_count
        body = (
            summary_body(data, job.language, markdown)
            if kind == 'summary'
            else None
        )
        return (
            templates[kind]
            .render(
                **fields,
                turns=turns,
                notes=notes if kind == 'reconcile' else [],
                notes_heading=locale.notes,
                body=body,
            )
            .rstrip()
            + '\n'
        )
    content = (
        data['text'].strip()
        if kind == 'transcribe'
        else '\n\n'.join(f"{turn['label']} {turn['text']}" for turn in turns)
    )
    if markdown:
        content = f'# {inline(audio.name)}\n\n' + content
    return content.rstrip() + '\n'

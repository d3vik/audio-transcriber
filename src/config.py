# SPDX-License-Identifier: Apache-2.0
"""Validate configuration without API calls or output writes."""

from dataclasses import dataclass, field
from datetime import datetime
from graphlib import CycleError, TopologicalSorter
import os
from pathlib import Path
import re
import tomllib

from .i18n import DEFAULT_LANGUAGE, language_config

TYPES = {
    'transcribe',
    'diarize',
    'research',
    'reconcile',
    'summary',
    'combine',
}
PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = 'reviewed'
BUNDLED_PROFILES = ('quick', 'standard', 'reviewed', 'researched')
DEFAULT_PART_SECONDS = 300
WINDOWS_RESERVED_NAMES = frozenset({
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
})
OUTPUT_METHODS = ('md', 'srt', 'mp4', 'txt', 'stdout')


def output_methods(value):
    if isinstance(value, str):
        methods = tuple(method.strip() for method in value.split(','))
    elif isinstance(value, list):
        methods = strings(value, 'output', nonempty=True)
    else:
        raise ValueError('output must be a string or an array of methods.')
    if (
        not methods or any(method not in OUTPUT_METHODS for method in methods)
        or len(set(methods)) != len(methods)
    ):
        raise ValueError(
            'output must contain unique methods from: '
            + ', '.join(OUTPUT_METHODS)
            + '.'
        )
    return methods


def identifier(value, label):
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', value)
    ):
        raise ValueError(
            f'{label} must start with a letter '
            'and contain letters, digits, _ or -.'
        )
    # Avoid reserved device names on Windows.
    if value.upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError(f'{label} is a reserved Windows filename: {value}')
    return value


def read_toml(path, allowed):
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8-sig'))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f'{path}: {exc}') from exc
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f'{path}: unknown keys: {", ".join(sorted(unknown))}')
    if type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError(f'{path}: version must be 1.')
    return data


def strings(value, label, *, nonempty=False, unique=True):
    if not isinstance(value, list) or any(
        not isinstance(s, str) or not s.strip() for s in value
    ):
        raise ValueError(f'{label} must be an array of nonempty strings.')
    if nonempty and not value:
        raise ValueError(f'{label} cannot be empty.')
    if unique and len(set(value)) != len(value):
        raise ValueError(f'{label} contains duplicate entries.')
    return tuple(value)


def path_value(value, label, base):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty path.')
    return (base / value).resolve()


@dataclass(frozen=True)
class Model:
    type: str
    api_model: str
    prompt: str
    max_output_tokens: int | None
    reasoning: dict[str, str] = field(default_factory=dict)
    max_tool_calls: int | None = None


def load_model(path):
    data = read_toml(
        path,
        {
            'version',
            'type',
            'api_model',
            'prompt',
            'prompt_file',
            'max_output_tokens',
            'reasoning',
            'max_tool_calls',
        },
    )
    kind, api = data.get('type'), data.get('api_model')
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValueError(f'{path}: invalid model type.')
    if (
        not isinstance(api, str)
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', api)
        or len(api) > 100
    ):
        raise ValueError(f'{path}: invalid api_model identifier.')
    if 'prompt' in data and 'prompt_file' in data:
        raise ValueError(f'{path}: choose prompt or prompt_file, not both.')
    prompt = data.get('prompt', '')
    if 'prompt_file' in data:
        prompt = path_value(
            data['prompt_file'], 'prompt_file', path.parent
        ).read_text(encoding='utf-8-sig')
    if not isinstance(prompt, str):
        raise ValueError(f'{path}: prompt must be text.')
    if kind == 'diarize' and ('prompt' in data or 'prompt_file' in data):
        raise ValueError(f'{path}: diarization does not support a prompt.')
    if kind != 'diarize' and not prompt.strip():
        raise ValueError(
            f'{path}: a nonempty prompt or prompt_file is required.'
        )
    tokens = data.get('max_output_tokens')
    if kind in ('reconcile', 'summary', 'research', 'combine'):
        tokens = (
            tokens
            if tokens is not None
            else (16000 if kind == 'reconcile' else 6000)
        )
        if type(tokens) is not int or not 1 <= tokens <= 100000:
            raise ValueError(
                f'{path}: max_output_tokens must be an integer '
                'from 1 to 100000.'
            )
    elif tokens is not None:
        raise ValueError(
            f'{path}: max_output_tokens applies only to text models.'
        )
    reasoning = data.get('reasoning', {})
    if not isinstance(reasoning, dict) or set(reasoning) - {'effort', 'mode'}:
        raise ValueError(f'{path}: reasoning accepts only effort and mode.')
    for name, choices in {
        'effort': {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'},
        'mode': {'standard', 'pro'},
    }.items():
        if name in reasoning and (
            not isinstance(reasoning[name], str)
            or reasoning[name] not in choices
        ):
            raise ValueError(
                f'{path}: reasoning.{name} must be one of '
                f'{", ".join(sorted(choices))}.'
            )
    if kind in ('transcribe', 'diarize') and 'reasoning' in data:
        raise ValueError(f'{path}: reasoning applies only to text models.')
    if api.startswith('gpt-6.1-sol') and reasoning.get('effort') in (
        'none',
        'minimal',
    ):
        raise ValueError(
            f'{path}: gpt-6.1-sol reasoning effort starts at low.'
        )
    calls = data.get('max_tool_calls')
    if kind == 'research':
        calls = calls if calls is not None else 3
        if type(calls) is not int or not 1 <= calls <= 20:
            raise ValueError(
                f'{path}: max_tool_calls must be an integer from 1 to 20.'
            )
    elif calls is not None:
        raise ValueError(
            f'{path}: max_tool_calls applies only to research models.'
        )
    return Model(kind, api, prompt.strip(), tokens, reasoning, calls)


@dataclass(frozen=True)
class Stage:
    id: str
    type: str
    model: Model
    inputs: tuple[str, ...]
    context: tuple[str, ...]

    @property
    def dependencies(self):
        return set(self.inputs) | (set(self.context) - {'user', 'previous'})


@dataclass(frozen=True)
class Parts:
    source: str
    model: Model
    max_seconds: int


@dataclass(frozen=True)
class Profile:
    id: str
    stages: dict[str, Stage]
    outputs: tuple[str, ...]
    order: tuple[str, ...]
    parts: Parts

    def required(self):
        selected = set()

        def visit(name):
            if name not in selected:
                selected.add(name)
                for dependency in self.stages[name].dependencies:
                    visit(dependency)

        for output in self.outputs:
            visit(output)
        return selected


def load_profile(path):
    data = read_toml(path, {'version', 'id', 'outputs', 'stages', 'parts'})
    profile_id = identifier(data.get('id'), 'Profile id')
    outputs = strings(data.get('outputs'), 'outputs', nonempty=True)
    raw = data.get('stages')
    if not isinstance(raw, list) or not raw:
        raise ValueError(f'{path}: define at least one [[stages]] table.')
    stages = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) - {
            'id',
            'type',
            'model',
            'inputs',
            'context',
        }:
            raise ValueError(f'{path}: invalid stage table.')
        name = identifier(item.get('id'), 'Stage id')
        if name.casefold() in {
            'user',
            'previous',
            'combine',
            'all',
        } or name.casefold() in {key.casefold() for key in stages}:
            raise ValueError(f'{path}: reserved or duplicate stage id: {name}')
        model = load_model(
            path_value(item.get('model'), f'{name}.model', path.parent)
        )
        kind = item.get('type', model.type)
        if kind == 'combine':
            raise ValueError(
                'Combine models belong in [parts], not [[stages]].'
            )
        if kind != model.type:
            raise ValueError(
                f'{name}: stage type does not match model type {model.type}.'
            )
        stages[name] = Stage(
            name,
            kind,
            model,
            strings(item.get('inputs', []), f'{name}.inputs'),
            strings(item.get('context', []), f'{name}.context'),
        )
    for output in outputs:
        if output not in stages:
            raise ValueError(f'Unknown output stage: {output}')
    for stage in stages.values():
        if not stage.dependencies <= stages.keys():
            raise ValueError(
                f'{stage.id}: unknown dependencies: '
                f'{stage.dependencies - stages.keys()}'
            )
        types = [stages[name].type for name in stage.inputs]
        if stage.type in ('transcribe', 'diarize') and stage.inputs:
            raise ValueError(
                f'{stage.id}: audio stages read the recording; '
                'use context for draft transcripts.'
            )
        if stage.type == 'diarize' and stage.context:
            raise ValueError(
                f'{stage.id}: diarization does not support context.'
            )
        if stage.type == 'research' and (
            not types or any(t != 'transcribe' for t in types)
        ):
            raise ValueError(
                f'{stage.id}: research needs one or more transcription inputs.'
            )
        if stage.type == 'reconcile' and (
            types.count('diarize') != 1
            or not types.count('transcribe')
            or any(t not in ('transcribe', 'diarize') for t in types)
        ):
            raise ValueError(
                f'{stage.id}: reconciliation needs one diarization '
                'and at least one transcription input.'
            )
        if stage.type == 'summary' and (
            len(types) != 1
            or types[0] not in ('transcribe', 'diarize', 'reconcile')
        ):
            raise ValueError(
                f'{stage.id}: summary needs exactly one transcript '
                'or reconciliation input.'
            )
    try:
        order = tuple(
            TopologicalSorter(
                {
                    name: sorted(stage.dependencies)
                    for name, stage in stages.items()
                }
            ).static_order()
        )
    except CycleError as exc:
        raise ValueError(
            'Dependency cycle: ' + ' -> '.join(exc.args[1])
        ) from exc
    raw_parts = data.get('parts')
    if not isinstance(raw_parts, dict) or set(raw_parts) - {
        'source',
        'model',
        'max_seconds',
    }:
        raise ValueError(
            f'{path}: define [parts] with source, model '
            'and optional max_seconds.'
        )
    source = raw_parts.get('source')
    if (
        not isinstance(source, str)
        or source not in stages
        or stages[source].type not in ('transcribe', 'reconcile')
    ):
        raise ValueError(
            'parts.source must select a transcription or reconciliation stage.'
        )
    combine_model = load_model(
        path_value(raw_parts.get('model'), 'parts.model', path.parent)
    )
    if combine_model.type != 'combine':
        raise ValueError('parts.model must have type = "combine".')
    maximum = raw_parts.get('max_seconds', DEFAULT_PART_SECONDS)
    if type(maximum) is not int or not 10 <= maximum <= 2400:
        raise ValueError(
            'parts.max_seconds must be an integer from 10 to 2400.'
        )
    for stage in stages.values():
        if stage.type != 'summary' and any(
            stages[ref].type == 'summary' for ref in stage.dependencies
        ):
            raise ValueError(
                f'{stage.id}: part stages cannot depend '
                'on recording summaries.'
            )
        if stage.type == 'summary' and (
            'previous' in stage.context or stage.inputs != (source,)
        ):
            raise ValueError(
                f'{stage.id}: summaries read parts.source '
                'and cannot use previous context.'
            )
    profile = Profile(
        profile_id,
        stages,
        outputs,
        order,
        Parts(source, combine_model, maximum),
    )
    if source not in profile.required():
        raise ValueError(
            'parts.source must be required by the selected outputs.'
        )
    return profile


@dataclass(frozen=True)
class Job:
    audio: tuple[Path, ...]
    context: tuple[str, ...]
    language: str
    output: tuple[str, ...]
    profile: Profile
    redo: frozenset[str]
    config_path: Path | None = None
    name: str | None = None
    start: datetime | None = None

    def saved_stages(self):
        source = self.profile.stages[self.profile.parts.source]
        return tuple(dict.fromkeys((
            *self.profile.outputs,
            *((source.id,) if source.type == 'reconcile' else ()),
        )))

    def methods_for(self, stage_id):
        methods = ['md']
        if (
            stage_id == self.profile.parts.source
            and self.profile.stages[stage_id].type == 'reconcile'
        ):
            methods.append('srt')
            if 'mp4' in self.output:
                methods.append('mp4')
        methods.extend(
            method for method in self.output if method in ('txt', 'stdout')
        )
        return tuple(methods)

    def output_root(self, audio):
        parent = self.config_path.parent if self.config_path else audio.parent
        name = self.name or (
            self.config_path.stem if self.config_path else audio.stem
        )
        root = parent / name
        if len(self.audio) > 1 and (self.config_path or self.name):
            root /= audio.name
        return root

    def output_path(self, audio, stage_id, method):
        return self.output_root(audio) / f'{audio.stem}.{stage_id}.{method}'


def resolve_profile(value, base):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('profile must be a bundled name or a TOML path.')
    path = Path(value)
    if '/' in value or '\\' in value or path.suffix or path.is_absolute():
        return (base / path).resolve()
    bundled = PROJECT / 'profiles' / (value + '.toml')
    if not bundled.is_file():
        raise ValueError(
            f'Unknown bundled profile: {value}. '
            f'Use {", ".join(BUNDLED_PROFILES)}, '
            'or an explicit custom path such as ./custom.toml.'
        )
    return bundled.resolve()


def load_job(args):
    settings, base = {}, Path.cwd()
    if args.config:
        base = args.config.resolve().parent
        settings = read_toml(
            args.config,
            {
                'version',
                'audio',
                'context',
                'language',
                'profile',
                'output',
                'name',
                'start',
            },
        )
    language = args.language or settings.get('language', DEFAULT_LANGUAGE)
    language_config(language)
    output = (
        args.output if args.output is not None
        else output_methods(settings.get('output', 'md'))
    )
    profile_value = (
        args.profile
        if args.profile is not None
        else settings.get('profile', DEFAULT_PROFILE)
    )
    profile_base = Path.cwd() if args.profile is not None else base
    profile = load_profile(resolve_profile(profile_value, profile_base))
    if (
        {'srt', 'mp4'} & set(output)
        and profile.stages[profile.parts.source].type != 'reconcile'
    ):
        raise ValueError('SRT/MP4 require a reconciliation parts.source.')
    audio = settings.get('audio', [])
    if isinstance(audio, str):
        audio = [audio]
    audio = strings(audio, 'audio', unique=False)
    paths = args.files or [base / value for value in audio]
    if not paths:
        raise ValueError('Provide audio paths or audio in the recording TOML.')
    audio_paths = tuple(dict.fromkeys(path.resolve() for path in paths))
    for path in audio_paths:
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Audio file missing or empty: {path}')
    context_paths = [
        base / value
        for value in strings(
            settings.get('context', []), 'context', unique=False
        )
    ] + args.context
    context = []
    for path in dict.fromkeys(path.resolve() for path in context_paths):
        value = path.read_text(encoding='utf-8-sig').strip()
        if not value:
            raise ValueError(f'Empty context file: {path}')
        context.append(value)
    requested_redo = frozenset(args.redo)
    required = profile.required()
    unknown = requested_redo - required - {'all', 'combine'}
    if unknown:
        raise ValueError(
            '--redo must be all, combine, or a stage used '
            f'by the profile outputs: {unknown}'
        )
    redo = frozenset(
        required | {'combine'} if 'all' in requested_redo else requested_redo
    )
    name = args.name if args.name is not None else settings.get('name')
    if name is not None:
        if (
            not isinstance(name, str)
            or not name
            or name in ('.', '..')
            or any(c in name for c in '/\\<>:"|?*')
            or any(ord(c) < 32 for c in name)
            or name.endswith((' ', '.'))
            or len(name) > 100
            or name.split('.')[0].upper() in WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(
                'name must be a portable folder name, not a path.'
            )
    start = args.start if args.start is not None else settings.get('start')
    if start is not None and not isinstance(start, datetime):
        if not isinstance(start, str) or not any(
            separator in start for separator in ('T', ' ')
        ):
            raise ValueError(
                'start requires both date and time, '
                'e.g. 2026-04-12T09:00:00+02:00.'
            )
        try:
            start = datetime.fromisoformat(start)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                'start must be an ISO date/time, '
                'optionally with a timezone offset.'
            ) from exc
    if start is not None and len(audio_paths) != 1:
        raise ValueError('An explicit start time requires a single recording.')
    job = Job(
        audio_paths,
        tuple(context),
        language,
        output,
        profile,
        redo,
        args.config.resolve() if args.config else None,
        name,
        start,
    )
    roots = [job.output_root(audio) for audio in audio_paths]
    if (
        len({os.path.normcase(str(root.resolve())) for root in roots})
        != len(roots)
    ):
        raise ValueError(
            'Audio filenames produce the same output folder; '
            'use separate recording configs.'
        )
    for root in roots:
        if root.exists() and not root.is_dir():
            raise ValueError(
                f'Output path is not a directory: {root}; choose --name.'
            )
    return job

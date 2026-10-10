# SPDX-License-Identifier: Apache-2.0
"""Execute required DAG nodes and cache each effective request."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime
import time
import math
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import uuid

from .api import (
    APIResult,
    PROTOCOL_VERSION,
    InvalidResponse,
    TokenUsage,
    request_result,
    request_rules,
    transcription_payload,
    token_usage,
)
from .audio import (
    audio_for_upload,
    audio_duration,
    check_ffmpeg,
    check_video_tools,
    encode_video,
    decoded_audio_duration,
    part_for_upload,
    needs_conversion,
    plan_parts,
    validate_parts,
    MEDIA_RECIPE,
    UPLOAD_LIMIT,
    VIDEO_RECIPE,
    DECODED_DURATION_RECIPE,
)
from .assembly import (
    collect,
    combination_input,
    previous_context,
    validate_combination,
    apply_combination,
)
from .config import Stage
from .i18n import language_config
from .rendering import (
    SUBTITLE_RECIPE, elapsed, load_templates, render_srt, render_stage,
)
from .formats import (
    RESULT_SCHEMAS,
    validate_reconciliation,
    validate_research,
    validate_schema,
    validate_transcript,
)
from .storage import (
    atomic_write,
    atomic_output,
    audio_hash,
    backup,
    check_output_paths,
    digest,
    file_fingerprint,
)

CACHE_SCHEMA = 1


@dataclass(frozen=True)
class Result:
    key: str
    data: dict
    generated_at: str
    usage: TokenUsage | None = None


class Pipeline:
    def __init__(self, audio, job, adapter, log=print):
        self.audio, self.job, self.adapter, self.log = audio, job, adapter, log
        self.profile = job.profile
        self.root = job.output_root(audio)
        self.cache = self.root / 'cache'
        self.history = self.root / 'history'
        self.audio_snapshot = self.audio_stat()
        self.audio_digest = audio_hash(audio)
        self.check_audio()
        self._upload_lock = Lock()
        self.prepared = False
        self.needs_api = False
        self.templates = {}
        self.duration = None
        self.required = self.profile.required()
        self.order = [
            name for name in self.profile.order if name in self.required
        ]
        self._cache_reads = {}
        self._video_verified = None
        self.parts = []
        self._reset_traversal()
        self.part_names = {
            n
            for n in self.required
            if self.profile.stages[n].type != 'summary'
        }
        self.final_names = self.required - self.part_names
        source = self.profile.stages[self.profile.parts.source]
        self.output_names = job.saved_stages()
        combine_inputs = (source.id,) + tuple(
            ref for ref in source.inputs
            if self.profile.stages[ref].type == 'diarize'
        )
        self.combine = Stage(
            'combine', 'combine', self.profile.parts.model,
            combine_inputs, ('user',),
        )
        self.completed = 0
        self._usage_lock = Lock()
        self.usage = TokenUsage(0, 0)
        self.usage_unavailable = 0
        self.media_metadata = {
            'audio_sha256': self.audio_digest,
            'recipe': MEDIA_RECIPE,
            'max_seconds': self.profile.parts.max_seconds,
        }
        self.media_path = (
            self.cache / f'audio.{digest(self.media_metadata)[:20]}.json'
        )
        self.video_manifest = self.cache / 'video.json'
        self.output_duration = None
        self.decoded_metadata = {
            'audio_sha256': self.audio_digest,
            'recipe': DECODED_DURATION_RECIPE,
        }
        self.decoded_manifest = (
            self.cache
            / f'decoded-audio.{digest(self.decoded_metadata)[:20]}.json'
        )

    def audio_stat(self):
        return file_fingerprint(self.audio)

    def check_audio(self, *, full=False):
        if self.audio_stat() != self.audio_snapshot or (
            full and audio_hash(self.audio) != self.audio_digest
        ):
            raise ValueError(
                'Audio changed during processing; '
                'rerun with an unchanged recording.'
            )

    def cache_path(self, filename):
        directory = self.cache
        if self.part is not None and len(self.parts) > 1:
            directory /= f'part-{self.part["index"]:04d}'
        return directory / filename

    @property
    def task_count(self):
        return (
            len(self.part_names) * len(self.parts)
            + len(self.final_names)
            + (len(self.parts) > 1)
        )

    def record_usage(self, usage: TokenUsage | None):
        with self._usage_lock:
            if usage is None:
                self.usage_unavailable += 1
            else:
                self.usage = TokenUsage(
                    self.usage.input_tokens + usage.input_tokens,
                    self.usage.output_tokens + usage.output_tokens,
                )

    def total_usage(self):
        text = self.usage.display()
        if self.usage_unavailable:
            calls = 'call' if self.usage_unavailable == 1 else 'calls'
            text += f'; {self.usage_unavailable} {calls} unavailable'
        return text

    def report_completed(self, stage, started, cached, result):
        # The scheduler owns the shared completion counter.
        self.completed += 1
        status = 'Cached' if cached else 'Finished'
        timing = '' if cached else f' ({time.monotonic() - started:.1f}s)'
        tokens = (
            'tokens -' if cached else result.usage.display()
            if result.usage is not None else 'tokens unavailable'
        )
        self.log(
            f'[{self.completed}/{self.task_count}] '
            f'{status}: {stage.id}{timing}; {tokens}'
        )

    def cached(self, stage, metadata):
        if stage.id in self.job.redo:
            return None
        key = digest(metadata)
        filename = f'{stage.id}.{stage.model.api_model}.{key[:20]}.json'
        path = self.cache_path(filename)
        if not path.exists():
            return None
        try:
            fingerprint = file_fingerprint(path)
            previous = self._cache_reads.get(path)
            if previous and previous[:2] == (fingerprint, metadata):
                return previous[2]
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict):
                raise ValueError('Expected a JSON cache object.')
            if (
                type(record.get('schema')) is not int
                or record['schema'] != CACHE_SCHEMA
                or record.get('metadata') != metadata
            ):
                raise ValueError('Cache recipe mismatch.')
            self.validate(stage, record['result'])
            date = record['generated_at']
            if (
                not isinstance(date, str)
                or datetime.fromisoformat(date).tzinfo is None
            ):
                raise ValueError(
                    'generated_at must include an ISO timestamp with timezone.'
                )
            result = Result(key, record['result'], date)
            self._cache_reads[path] = (fingerprint, metadata, result)
            return result
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                f'Invalid cache {path}; use --redo {stage.id}: {exc}'
            ) from exc

    def select_part(self, part):
        self.part = part
        self.previous = previous_context(
            self.profile, self.parts, self.records
        )
        self.results = {}
        self._upload = None
        self._combined_diary = None

    def cached_graph(self, names):
        missing = set()
        for name in self.order:
            if name not in names:
                continue
            stage = self.profile.stages[name]
            unavailable = stage.dependencies - self.results.keys()
            unavailable_previous = (
                self.part is not None
                and self.part['index'] > 1
                and 'previous' in stage.context
                and self.previous is None
            )
            result = None
            if not unavailable and not unavailable_previous:
                payload = self.payload(stage)
                result = self.cached(stage, self.metadata(stage, payload))
            if result is None:
                missing.add(name)
            else:
                self.results[name] = result
        return missing

    def _reset_traversal(self):
        self.results = {}
        self.records = []
        self.part = None
        self.previous = None
        self.material = None
        self._combined_diary = None
        self._upload = None

    def preflight(self):
        self.prepared = False
        self.needs_api = False
        self._reset_traversal()
        try:
            self._preflight()
            self.prepared = True
        finally:
            self._reset_traversal()
            if not self.prepared:
                self.needs_api = False

    def _preflight(self):
        if (self.root / '.running').exists():
            raise ValueError(f'Recording locked: {self.root / ".running"}')
        if self.media_path.exists():
            try:
                info = json.loads(self.media_path.read_text(encoding='utf-8'))
                if info['metadata'] != self.media_metadata:
                    raise ValueError('Audio metadata mismatch.')
                self.duration = info['duration']
                if (
                    type(self.duration) not in (int, float)
                    or not math.isfinite(self.duration)
                    or self.duration <= 0
                ):
                    raise ValueError('Invalid audio duration.')
                self.parts = info['parts']
                validate_parts(
                    self.parts, self.duration, self.profile.parts.max_seconds
                )
                if (
                    self.audio.stat().st_size > UPLOAD_LIMIT
                    and len(self.parts) < 2
                ):
                    raise ValueError(
                        'Oversized audio requires multiple parts.'
                    )
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(
                    f'Invalid audio metadata: {self.media_path}; '
                    f'remove this file to re-probe: {exc}'
                ) from exc
        else:
            self.duration = audio_duration(self.audio)
            self.parts = plan_parts(
                self.audio, self.duration, self.profile.parts.max_seconds
            )
            validate_parts(
                self.parts, self.duration, self.profile.parts.max_seconds
            )
        self.templates = load_templates(
            {self.profile.stages[n].type for n in self.output_names}
        )
        outputs = [
            self.output_path(name, method)
            for name in self.output_names
            for method in self.job.methods_for(name) if method != 'stdout'
        ]
        if 'mp4' in self.job.output:
            outputs.extend((self.video_manifest, self.decoded_manifest))
        audio_paths = {
            path.resolve() for path in (*self.job.audio, self.audio)
        }
        for path in outputs:
            if path.resolve() in audio_paths:
                raise ValueError(
                    f'Output would overwrite original audio {path}; '
                    'choose a different --name.'
                )
        check_output_paths(self.root, outputs)
        if 'mp4' in self.job.output:
            self.read_video_manifest()
        missing_count, missing_audio = 0, False
        for part in self.parts:
            self.select_part(part)
            missing = self.cached_graph(self.part_names)
            missing_count += len(missing)
            missing_audio |= any(
                self.profile.stages[n].type in ('transcribe', 'diarize')
                for n in missing
            )
            if len(self.parts) > 1:
                check_output_paths(
                    self.cache / f'part-{part["index"]:04d}', []
                )
            self.records.append(self.results)
        if missing_count == 0:
            self.aggregate()
            if len(self.parts) > 1:
                payload = self.combine_payload()
                result = self.cached(
                    self.combine, self.metadata(self.combine, payload)
                )
                if result is None:
                    missing_count += 1
                else:
                    self.apply_combined(result)
            if missing_count == 0:
                missing_count += len(self.cached_graph(self.final_names))
            else:
                missing_count += len(self.final_names)
        else:
            missing_count += len(self.final_names) + (len(self.parts) > 1)
        if missing_audio and (
            len(self.parts) > 1 or needs_conversion(self.audio)
        ):
            check_ffmpeg()
        self.output_duration = self.duration
        if 'mp4' in self.job.output or self.decoded_manifest.exists():
            measured_now = not self.decoded_manifest.exists()
            if measured_now:
                check_video_tools()
                self.log('Measuring decoded audio duration for MP4...')
                self.output_duration = decoded_audio_duration(self.audio)
                self.check_audio()
            else:
                try:
                    record = json.loads(
                        self.decoded_manifest.read_text(encoding='utf-8')
                    )
                    if (
                        not isinstance(record, dict)
                        or set(record) != {'metadata', 'duration'}
                        or record['metadata'] != self.decoded_metadata
                        or type(record['duration']) not in (int, float)
                        or not math.isfinite(record['duration'])
                        or record['duration'] <= 0
                    ):
                        raise ValueError('Invalid decoded duration metadata.')
                    self.output_duration = record['duration']
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(
                        'Invalid decoded duration cache '
                        f'{self.decoded_manifest}; '
                        f'remove this file to remeasure: {exc}'
                    ) from exc
            if abs(self.output_duration - self.duration) > 0.25:
                self.log(
                    f'Duration correction for playback outputs: cached '
                    f'{self.duration:.3f}s, '
                    f'decoded {self.output_duration:.3f}s. '
                    'Existing transcription caches and part boundaries '
                    'are unchanged.'
                )
            if 'mp4' in self.job.output and not measured_now and (
                missing_count or not self.video_cached(self.video_metadata())
            ):
                check_video_tools()
        self.needs_api = bool(missing_count)
        parts = f'{len(self.parts)} parts, ' if len(self.parts) > 1 else ''
        self.log(
            f'Preflight: {parts}{self.task_count - missing_count} cached, '
            f'{missing_count} pending — {self.audio.name}'
        )

    def output_path(self, name, method):
        return self.job.output_path(self.audio, name, method)

    def video_metadata(self):
        source = self.profile.stages[self.profile.parts.source]
        data = self.results[source.id].data
        diary = self.diarization(source)
        locale = language_config(self.job.language)
        return {
            'audio_sha256': self.audio_digest,
            'recipe': VIDEO_RECIPE,
            'subtitles': SUBTITLE_RECIPE,
            'duration': self.output_duration,
            'dialogue_sha256': digest({
                'turns': [
                    (turn['text'], turn['speaker'], turn['source_segments'])
                    for turn in data['turns']
                ],
                'names': data['speaker_names'],
                'timings': [
                    (segment['start'], segment['end'])
                    for segment in diary['segments']
                ],
                'speaker_label': locale.speaker,
                'unknown_label': locale.unknown,
            }),
        }

    def read_video_manifest(self):
        if not self.video_manifest.exists():
            return None
        try:
            record = json.loads(
                self.video_manifest.read_text(encoding='utf-8')
            )
            if (
                not isinstance(record, dict)
                or set(record) != {'metadata', 'sha256'}
                or not isinstance(record['metadata'], dict)
                or set(record['metadata']) != {
                    'audio_sha256', 'recipe', 'subtitles',
                    'duration', 'dialogue_sha256',
                }
                or not isinstance(record['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
            ):
                raise ValueError('Invalid video manifest fields.')
            fields = record['metadata']
            if (
                any(
                    not isinstance(fields[key], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', fields[key])
                    for key in ('audio_sha256', 'dialogue_sha256')
                )
                or any(
                    not isinstance(fields[key], str) or not fields[key]
                    for key in ('recipe', 'subtitles')
                )
                or type(fields['duration']) not in (int, float)
                or not math.isfinite(fields['duration'])
                or fields['duration'] <= 0
            ):
                raise ValueError('Invalid video manifest metadata.')
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f'Invalid MP4 manifest {self.video_manifest}; '
                f'remove this file to regenerate the MP4: {exc}'
            ) from exc
        return record

    def video_cached(self, metadata):
        path = self.output_path(self.profile.parts.source, 'mp4')
        record = self.read_video_manifest()
        if not path.exists() or record is None:
            return False
        if record['metadata'] != metadata:
            return False
        fingerprint = file_fingerprint(path)
        verified = (path, fingerprint, record['sha256'])
        if self._video_verified == verified:
            return True
        if audio_hash(path) != record['sha256']:
            self.log(f'MP4 file changed; regenerating: {path}')
            return False
        if file_fingerprint(path) != fingerprint:
            raise ValueError(f'MP4 changed during validation: {path}; rerun.')
        self._video_verified = verified
        return True

    def render_video(self):
        path = self.output_path(self.profile.parts.source, 'mp4')
        metadata = self.video_metadata()
        if self.video_cached(metadata):
            self.log(f'Unchanged: {path}')
            return
        source = self.profile.stages[self.profile.parts.source]
        subtitles = render_srt(
            self.results[source.id].data, self.diarization(source),
            self.output_duration, self.job.language, merge_overlaps=True,
        )
        self.check_audio(full=True)
        self.log(f'Preparing MP4: {path}')
        try:
            with tempfile.TemporaryDirectory(
                prefix='transcribe-video-'
            ) as folder:
                subtitle_path = Path(folder) / 'subtitles.srt'
                subtitle_path.write_text(subtitles, encoding='utf-8')
                with atomic_output(path) as temporary:
                    encode_video(
                        self.audio, subtitle_path, temporary,
                        self.output_duration,
                    )
                    self.check_audio(full=True)
                    backup(path, self.history)
        except (ValueError, OSError) as exc:
            raise ValueError(
                f'MP4 export failed for {path}: {exc} '
                'Markdown, SRT and model caches remain saved; '
                'any existing MP4 was not replaced.'
            ) from exc
        atomic_write(self.video_manifest, json.dumps({
            'metadata': metadata, 'sha256': audio_hash(path),
        }, ensure_ascii=False, indent=2) + '\n')
        self.log(f'Saved: {path}')

    def upload(self, stack):
        with self._upload_lock:
            if self._upload is None:
                self.log(f'Preparing audio: {self.audio.name}')
                manager = (
                    part_for_upload(self.audio, self.part)
                    if len(self.parts) > 1
                    else audio_for_upload(self.audio)
                )
                path = stack.enter_context(manager)
                size = path.stat().st_size
                if not 0 < size <= UPLOAD_LIMIT:
                    raise ValueError(
                        f'Upload is {size / 1_000_000:.2f} MB; '
                        'lower parts.max_seconds.'
                    )
                self._upload = path
            return self._upload

    def envelope(self, name):
        stage = self.profile.stages[name]
        return {
            'id': name,
            'type': stage.type,
            'result': request_result(self.results[name].data),
            'provenance': {
                'inputs': list(stage.inputs),
                'context': list(stage.context),
            },
        }

    def payload(self, stage):
        context = []
        for ref in stage.context:
            if ref == 'user':
                context.append(
                    {'source': 'user', 'documents': list(self.job.context)}
                )
            elif ref == 'previous':
                if self.previous is not None:
                    context.append({
                        **self.previous,
                        'result': request_result(self.previous['result']),
                    })
            else:
                context.append(self.envelope(ref))
        payload = {
            'inputs': [self.envelope(ref) for ref in stage.inputs],
            'context': context,
        }
        if self.part is not None:
            payload['part'] = self.part
        return (
            transcription_payload(payload)
            if stage.type == 'transcribe'
            else payload
        )

    def metadata(self, stage, payload):
        metadata = {
            'protocol': PROTOCOL_VERSION,
            'rules_sha256': digest(
                request_rules(stage.type, self.job.language)
            ),
            'type': stage.type,
            'model': asdict(stage.model),
            'language': self.job.language,
            'payload_sha256': digest(payload),
            'dependencies': {
                name: self.results[name].key
                for name in sorted(stage.dependencies)
            },
        }
        if 'previous' in stage.context and self.previous is not None:
            metadata['previous_recipe'] = self.previous['recipe']
        if stage.type in ('transcribe', 'diarize'):
            converted = len(self.parts) > 1 or needs_conversion(self.audio)
            metadata.update(
                audio_sha256=self.audio_digest,
                media_recipe=MEDIA_RECIPE,
                part=self.part,
                upload_format=(
                    'mp3-mono-16k-64k'
                    if converted
                    else self.audio.suffix.lower()
                ),
            )
        if stage.type in RESULT_SCHEMAS:
            metadata['schema_sha256'] = digest(RESULT_SCHEMAS[stage.type])
        return metadata

    def diarization(self, stage):
        if (
            stage.id == self.profile.parts.source
            and self._combined_diary is not None
        ):
            return self._combined_diary
        ref = next(
            ref
            for ref in stage.inputs
            if self.profile.stages[ref].type == 'diarize'
        )
        return self.results[ref].data

    def validate(self, stage, data):
        if stage.type in ('transcribe', 'diarize'):
            duration = (
                self.part['end'] - self.part['start']
                if self.part
                else self.duration
            )
            validate_transcript(
                data, stage.type == 'diarize', duration=duration
            )
        elif stage.type == 'research':
            validate_research(data)
        elif stage.type == 'combine':
            validate_combination(data, self.material)
        elif stage.type == 'reconcile':
            validate_schema(data, RESULT_SCHEMAS[stage.type])
            validate_reconciliation(data, self.diarization(stage))
        else:
            validate_schema(data, RESULT_SCHEMAS[stage.type])
            if not data['overview'].strip():
                raise ValueError('Summary overview must not be empty.')

    def execute(self, stage, payload, metadata, stack):
        key = digest(metadata)
        path = self.cache_path(
            f'{stage.id}.{stage.model.api_model}.{key[:20]}.json'
        )
        cached = self.cached(stage, metadata)
        if cached is not None:
            return cached, True
        self.check_audio()
        self.log(f'Running: {stage.id} ({stage.model.api_model})')
        upload = (
            self.upload(stack)
            if stage.type in ('transcribe', 'diarize')
            else None
        )
        try:
            data = self.adapter.execute(
                stage, payload, self.job.language, upload
            )
        except InvalidResponse as exc:
            self.record_usage(exc.usage)
            failure = self.cache_path(
                f'{stage.id}.failed.{uuid.uuid4().hex[:12]}.txt'
            )
            atomic_write(failure, exc.raw)
            raise ValueError(f'{exc} Response saved: {failure}') from exc
        except Exception as exc:
            self.record_usage(None)
            status = getattr(exc, 'status_code', None)
            hints = {
                401: 'API authentication failed; check OPENAI_API_KEY.',
                403: 'API access denied; check project and model permissions.',
                404: (
                    f'API model unavailable: {stage.model.api_model}. '
                    'Check the model TOML.'
                ),
                429: (
                    'API quota or rate limit reached; '
                    'check billing/limits and retry later.'
                ),
            }
            if status in hints:
                raise ValueError(hints[status]) from exc
            raise
        usage = (
            data.usage if isinstance(data, APIResult)
            else token_usage(data.get('usage')) if isinstance(data, dict)
            else None
        )
        self.record_usage(usage)
        self.check_audio()
        try:
            self.validate(stage, data)
        except (ValueError, KeyError, TypeError) as exc:
            failed = self.cache_path(
                f'{stage.id}.failed.{uuid.uuid4().hex[:12]}.json'
            )
            atomic_write(
                failed, json.dumps(data, ensure_ascii=False, indent=2) + '\n'
            )
            raise ValueError(
                f'{stage.id}: invalid response saved to {failed}: {exc}'
            ) from exc
        generated_at = (
            datetime.now().astimezone().isoformat(timespec='seconds')
        )
        content = (
            json.dumps(
                {
                    'schema': CACHE_SCHEMA,
                    'metadata': metadata,
                    'result': data,
                    'generated_at': generated_at,
                    'usage': asdict(usage) if usage is not None else None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + '\n'
        )
        if path.exists() and path.read_text(encoding='utf-8') != content:
            backup(path, self.history)
        atomic_write(path, content)
        return Result(key, data, generated_at, usage), False

    def render(self, stage, method):
        result = self.results[stage.id]
        source = (
            self.profile.stages[stage.inputs[0]]
            if stage.type == 'summary'
            else stage
        )
        source_data = self.results[source.id].data
        diary = (
            self.diarization(source) if source.type == 'reconcile' else None
        )
        if method == 'srt':
            try:
                content = render_srt(
                    source_data, diary, self.output_duration, self.job.language
                )
            except ValueError as exc:
                raise ValueError(
                    'SRT export failed for '
                    f'{self.output_path(stage.id, method)}: '
                    f'{exc} Markdown documents remain saved. '
                    'Any existing SRT was not replaced.'
                ) from exc
            interpolated = sum(
                bool(turn['text'].strip()) and not turn['source_segments']
                for turn in source_data['turns']
            )
            if interpolated:
                self.log(
                    f'SRT: interpolated timing for {interpolated} '
                    'untimed spoken turns; verify against the audio.'
                )
        else:
            content = render_stage(
                self.templates,
                stage.type,
                result.data,
                source.type,
                source_data,
                diary,
                self.audio,
                self.job,
                self.output_duration,
                result.generated_at,
                len(self.parts),
                method=method,
            )
        if method == 'stdout':
            return (
                f'=== {self.audio.name} [{self.profile.id}/{stage.id}] ===\n'
                f'{content}'
            )
        path = self.output_path(stage.id, method)
        if not path.exists() or path.read_text(encoding='utf-8') != content:
            backup(path, self.history)
            atomic_write(path, content)
            self.log(f'Saved: {path}')
        else:
            self.log(f'Unchanged: {path}')
        return ''

    def run(self):
        if not self.prepared:
            self.preflight()
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / '.running'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError(
                f'Recording locked: {lock}. '
                'Remove only after confirming no process is active.'
            ) from exc
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(str(os.getpid()))
            if not self.media_path.exists():
                atomic_write(
                    self.media_path,
                    json.dumps(
                        {
                            'metadata': self.media_metadata,
                            'duration': self.duration,
                            'parts': self.parts,
                        }
                    )
                    + '\n',
                )
            if (
                'mp4' in self.job.output
                and not self.decoded_manifest.exists()
            ):
                atomic_write(self.decoded_manifest, json.dumps({
                    'metadata': self.decoded_metadata,
                    'duration': self.output_duration,
                }) + '\n')
            started = time.monotonic()
            self.usage = TokenUsage(0, 0)
            self.usage_unavailable = 0
            try:
                result = self._run()
            except (Exception, KeyboardInterrupt):
                self.log(
                    f'Stopped: {self.audio.name} '
                    f'({time.monotonic() - started:.1f}s); '
                    + self.total_usage()
                )
                raise
            self.log(
                f'Completed: {self.audio.name} '
                f'({time.monotonic() - started:.1f}s); '
                + self.total_usage()
            )
            return result
        finally:
            lock.unlink(missing_ok=True)

    def aggregate(self):
        self.part = None
        self.previous = None
        self._combined_diary = None
        if len(self.parts) == 1:
            self.results = dict(self.records[0])
            return
        self.results = {}
        for name in self.part_names:
            stage = self.profile.stages[name]
            data = collect(stage, self.parts, self.records, self.profile)
            key = digest(
                {
                    'parts': self.parts,
                    'recipes': [record[name].key for record in self.records],
                    'data': data,
                }
            )
            date = max(
                (record[name].generated_at for record in self.records),
                key=datetime.fromisoformat,
            )
            self.results[name] = Result(key, data, date)

    def combine_payload(self):
        stage = self.profile.stages[self.profile.parts.source]
        diary = self.diarization(stage) if stage.type == 'reconcile' else None
        self.material = combination_input(
            stage, self.results[stage.id].data, diary, self.parts, self.records
        )
        return {
            'inputs': self.material,
            'context': [
                {'source': 'user', 'documents': list(self.job.context)}
            ],
        }

    def apply_combined(self, result):
        stage = self.profile.stages[self.profile.parts.source]
        diary = self.diarization(stage) if stage.type == 'reconcile' else None
        data, diary = apply_combination(
            result.data, self.material, self.results[stage.id].data, diary
        )
        self.results[stage.id] = Result(result.key, data, result.generated_at)
        self._combined_diary = diary
        self.validate(stage, data)

    def run_graph(self, names, stack):
        pending = set(names)
        running, errors = {}, []
        with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
            while pending or running:
                if not errors:
                    ready = [
                        self.profile.stages[name]
                        for name in self.order
                        if name in pending
                        and self.profile.stages[name].dependencies
                        <= self.results.keys()
                    ]
                    for stage in ready:
                        payload = self.payload(stage)
                        metadata = self.metadata(stage, payload)
                        started = time.monotonic()
                        future = pool.submit(
                            self.execute, stage, payload, metadata, stack
                        )
                        running[future] = (stage.id, started)
                        pending.remove(stage.id)
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    name, started = running.pop(future)
                    try:
                        self.results[name], cached = future.result()
                        self.report_completed(
                            self.profile.stages[name], started, cached,
                            self.results[name],
                        )
                    except Exception as exc:
                        self.log(f'Failed: {name}: {exc}')
                        errors.append(f'{name}: {exc}')
        self.check_audio()
        if errors:
            raise ValueError('; '.join(errors))
        if pending:
            raise ValueError(
                'No runnable stages; dependency graph is incomplete.'
            )

    def _run(self):
        self._reset_traversal()
        self.completed = 0
        for part in self.parts:
            self.select_part(part)
            started = time.monotonic()
            if len(self.parts) > 1:
                self.log(
                    f'Processing part {part["index"]}/{len(self.parts)} '
                    f'({elapsed(part["start"])}–{elapsed(part["end"])})'
                )
            # Temporary media must outlive every worker in this part.
            with ExitStack() as stack:
                self.run_graph(self.part_names, stack)
            self.records.append(dict(self.results))
            if len(self.parts) > 1:
                self.log(
                    f'Completed part {part["index"]}/{len(self.parts)} '
                    f'({time.monotonic() - started:.1f}s)'
                )
        self.aggregate()
        with ExitStack() as stack:
            if len(self.parts) > 1:
                payload = self.combine_payload()
                started = time.monotonic()
                try:
                    result, cached = self.execute(
                        self.combine,
                        payload,
                        self.metadata(self.combine, payload),
                        stack,
                    )
                    self.apply_combined(result)
                    self.report_completed(
                        self.combine, started, cached, result
                    )
                except Exception as exc:
                    self.log(f'Failed: combine: {exc}')
                    raise
            self.run_graph(self.final_names, stack)
        self.check_audio(full=True)
        text = ''.join(
            self.render(self.profile.stages[name], method)
            for name in self.output_names
            for method in self.job.methods_for(name)
            if method not in ('srt', 'mp4')
        )
        for name in self.output_names:
            if 'srt' in self.job.methods_for(name):
                self.render(self.profile.stages[name], 'srt')
        if 'mp4' in self.job.output:
            self.render_video()
        return text

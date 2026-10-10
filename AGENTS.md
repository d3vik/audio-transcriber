# Working on Audio Transcriber

This is a Python CLI for audio transcription, speaker diarization, reconciliation, optional research, and summaries, with a curated catalog of 26 ISO 639-1 languages. Danish and English are the tested workflows; other language entries are not quality-certified. Keep the project small and its current configuration explicit. Do not add aliases, migration code, deprecated flags, or compatibility paths unless the requested change requires them.

## Setup and use

Use Python 3.11 or newer and Pipenv. Install FFmpeg, including `ffprobe`, on PATH; Conversion and multipart encoding require the `libmp3lame` encoder. Supported upload extensions pass through; other FFmpeg-decodable containers are converted.

```cmd
pipenv sync
pipenv run python transcribe.py --help
pipenv run python transcribe.py "path\to\call.amr" -l da -p reviewed --plan
```

Replace the audio path with a real local file. The bundled reference example is ready to run from the project root:

```cmd
pipenv run python transcribe.py -c example/tuber-and-timber-call.toml --plan
pipenv run python transcribe.py -c example/tuber-and-timber-call.toml
```

For another recording, copy `example/tuber-and-timber-call.toml`, set its audio/context paths, language and start time, and run:

```cmd
pipenv run python transcribe.py -c "recording.toml" --plan
pipenv run python transcribe.py -c "recording.toml"
pipenv run python transcribe.py -c "recording.toml" --redo all
```

`--plan` validates configuration without API calls or output writes. A normal run with pending stages uses `OPENAI_API_KEY` and incurs API cost. Offline validation does not require a key. Never include keys, private recordings, or transcripts in committed files or distributable archives. The public reference example is the explicit exception: `example/tuber-and-timber-call.mp3` is a fictional English call with AI-generated voices, accompanied by its recording TOML and `context.md`. Include all three in archives. Keep generated example results out of Git.

Keep examples, prompts and test fixtures fictional. Do not use names, companies, locations, phone numbers or recording details from a user's private conversations. The example characters are Morgan and Riley; the supplier is Tuber & Timber. Keep Danish fixtures where they exercise language or Unicode handling.

## Where changes belong

| Location                        | Responsibility                                                    |
| ------------------------------- | ----------------------------------------------------------------- |
| `transcribe.py`                 | Source-tree launcher and Python version check                     |
| `src/cli.py`                    | Arguments, progress, preflight coordination, exit codes           |
| `src/config.py`                 | Strict TOML validation, paths, model/profile graph                |
| `src/engine.py`                 | Dependency scheduling, stage cache, result lifecycle              |
| `src/api.py`                    | OpenAI requests and response extraction                           |
| `src/audio.py`                  | Media probing, silence-aware planning and temporary part encoding |
| `src/assembly.py`               | Chronological collection and validated sparse corrections         |
| `src/formats.py`                | Result schemas and validation                                     |
| `src/rendering.py`              | Timestamps, speakers, frontmatter, templates                      |
| `src/i18n.py`                   | Language catalog, document labels and unclear-speech markers     |
| `src/storage.py`                | Hashes, atomic writes, backups, output checks                     |
| `profiles/`                     | Reusable workflow graphs; no recording-specific data              |
| `models/`                       | API model choices, prompts, reasoning and research budget         |
| `templates/`                    | Local document presentation                                       |
| `example/`, `doc/`, `README.md` | Examples, reference documentation, user guide                     |
| `test/`, `test/support.py`      | Offline tests and shared fixtures                                 |

Recording configs own audio, context files, language, output name, and profile selection. Bundled profile names are `quick`, `standard`, `reviewed` (default), and `researched`. Custom profiles use relative or absolute paths. Keep editorial choices in model prompts and API/data contracts in Python.

Use `src/i18n.py` as the shared language catalog for CLI/TOML validation, prompt language names/markers, and document labels. Keep Danish as the default, use exact lowercase ISO 639-1 codes, and do not translate TOML keys, frontmatter keys, stage IDs, diagnostic messages or output filenames. Keep synthetic audio-context labels stable rather than using translated display labels. Document-label changes must not cause model calls; prompt-marker changes must affect cache identity. Preserve Unicode combining marks in normalized tags and distinguish different IDs even when names collide with fallback labels. Keep behavioral and mocked SDK tests focused on Danish/English; check other catalog entries structurally without claiming their translations or transcription quality are validated. The code catalog is curated, not a popularity ranking or an API capability list.

## Behaviour to preserve

- Execute selected outputs and their dependencies only. Ready stages run concurrently; reviewed workflows start both initial transcriptions and diarization together, then draft reconciliation, verification, and final reconciliation. Research, when selected, runs between draft and verification. Validate the complete declared graph, including unused stages. Complete each part before starting the next; defer summaries until final assembly.
- Each transcription listens to the same current audio part. Prior drafts are context, not additional independent votes. Keep the first two passes free of each other's current-part drafts and retain all original evidence for the final pass. Explicit user spellings and corrections take priority for matching entities. Background and web results are not spoken evidence; research cannot establish private agreements or identify voices. Previous-part context must never be reproduced as current speech.
- Check dependencies, templates, cache validity and writable output paths before paid calls. Fully cached runs need no API key or media tools when duration is cached. Keep diagnostic messages actionable and progress on stderr. Single-part progress has no part labels. One completion counter covers the whole recording, including cached stages and final combination/summary; failures do not advance it.
- Report provider input/output tokens alongside task timings and recording/stopped-run totals. Cache hits show `tokens -` and contribute no new tokens; unavailable or duration-only usage must not be represented as zero. Keep usage metadata outside structured result fields and recipe identity, avoid counting nested cached/reasoning breakdowns twice, and reset totals for every recording/run. Preserve known returned usage on failed/refused responses without claiming account billing or hidden retry totals.
- Cache only validated results. Cache identity includes effective requests, source audio, upload format, selected context, dependency recipes, prompts and schemas. Keep prior-part recipe lineage even when audio prompts compact its result. Preserve all context words and uncertainties; omit only irrelevant scaffolding. Reject empty replacements of spoken segments, backwards timed turns and timestamps outside their part, while allowing overlapping speech. Keep incomplete/refused responses as useful diagnostics rather than successful caches. Change the protocol/cache schema when a changed contract requires invalidation; do not silently interpret a different format. Read only the current output's cache. Rendering changes do not require API calls.
- `--redo all` forces every required model call, including multipart combination, even if results are identical. `--redo combine` only repeats assembly. Individual redo requests invalidate dependants when their effective inputs change. Successful sibling stages and earlier parts remain cached after failure.
- Bundled model filenames describe roles. Keep every bundled definition used, without aliases or redundant prompt variants. Audio model IDs must remain compatible with their endpoint. Current profiles cap parts at 300 seconds because audio output limits are tighter than the upload-size limit.
- Keep `[parts]` mandatory and single-part combination a no-op. Preserve every source unit and all timing during assembly. Speaker IDs are local to parts: validate explicit grouping rather than equating repeated A/B labels. Never delete a unit through an empty model edit or merge distinct IDs within one part.
- Attempt speaker names from clear current-part dialogue and explicit user context identifying participant roles; self-introduction is sufficient but not required. Keep each part's diarization IDs independent, and treat prior/draft names as hypotheses rather than inheriting identities from A/B labels. Final combination validates local attributions against the whole dialogue/context before matching and naming groups. A list of names or generic shared roles alone is insufficient; conflicting associations remain uncertain. Research cannot identify private voices, and inferred names cannot supply unspoken content.
- Preserve atomic writes, history backups, the per-output process lock, and original audio. Put final documents directly in the output directory with `<audio-stem>.<stage-id>.<format>` filenames; retain API model IDs/hashes only in cache filenames. Do not rename or remove old generated outputs automatically. Always write standard Markdown outputs; a final reconciled `parts.source` also always gets SRT, even when only summary is selected. Text/stdout methods add outputs rather than replacing files. Only the final reconciliation gets subtitles. Python owns timing and rendering.
- Preserve original timed speech bounds and overlaps in SRT. Interpolate untimed spoken turns equally within free gaps or recording-edge intervals, never silently dropping words or shifting existing timed cues. Report insufficient gaps or zero-length millisecond cues as export errors after saving Markdown; keep successful model caches. Subtitle rendering and output-method changes must not require API calls or prompt/cache schema changes.
- Optional MP4 is local playback output for the final reconciliation only. Use full original audio, black video and switchable embedded subtitles; do not stop at the last subtitle or silently shorten overlapping cues. For MP4 timed text, display all active overlapping cues in each interval while preserving the standalone SRT. Check media/encoder dependencies before paid calls, reuse unchanged videos through the local manifest/checksum, and preserve atomic output/history/source audio.
- Keep Windows Cmder/CMD examples usable. Use argument lists for subprocesses, UTF-8 for text, and paths relative to their declaring TOML files. Use Pipenv and keep `Pipfile.lock` synchronized when changing dependencies.

## Validate changes

Use `pycodestyle` to check PEP 8 conformance, then run the offline tests before completing code changes. Install the development dependencies and run these Pipenv shortcuts from the project root:

```cmd
pipenv sync --dev
pipenv run pycodestyle
pipenv run test
```

The style command only checks files; it does not format them, need an API key, or make API calls. A nonzero exit code means violations must be fixed. Keep Python code lines within 79 characters and doc lines within 72. Pycodestyle uses [setup.cfg](setup.cfg) and covers the launcher, `src/` and `test/` without project-file exclusions or `noqa`. W503 is disabled in favor of break-before-operator formatting. Do not add new suppressions instead of readable fixes.

The suite uses temporary files, fake adapters and the installed OpenAI SDK with mocked HTTP transport. It tests caching, forced reruns, graph concurrency, validation, rendering, actual splitting of an oversized WAV, and single/multipart CLI runs with real media tools, plus conversion of unsupported upload containers. Test previous-context inheritance, independent diarization caches, restart after failure, and whole-recording summary lineage. It must not make paid requests or perform live web searches. Install FFmpeg to run the media integration test; report skips rather than treating them as completed checks.

For a focused change, run the relevant module, for example:

```cmd
pipenv run python -m unittest discover -s test -p test_profiles.py -v
pipenv run python -m unittest discover -s test -p test_sdk.py -v
```

Add tests for changed behaviour or a concrete regression, not for trivial implementation details. Update affected docs/examples alongside code. A mocked test does not establish transcription quality, real account/model access, or Windows execution. State those limits when reporting validation.

Keep production prompts free of example-specific characters and vocabulary. Check model lifecycle notices when reviewing API choices; the bundled audio models currently have a February 26, 2027 retirement date (README). Model changes require endpoint/response validation and a representative quality comparison, not just a new identifier. Do not claim profile optimality from mocked tests.

## Package and license

When delivering an archive, include the launcher, `src/`, profiles, models, templates, examples (including the public reference MP3), docs, tests, README, this file, `LICENSE`, `NOTICE`, `Pipfile`, `Pipfile.lock` and `setup.cfg`. Exclude virtual environments, bytecode, credentials, private recordings, generated outputs and caches. Check that the archive contains the current source and that documentation links resolve. For packaging changes, extract it into a fresh directory and run `pipenv sync` followed by `pipenv run test`.

The project is licensed under Apache-2.0. Keep `LICENSE`, `NOTICE`, and the SPDX identifiers on Python source files. Preserve applicable third-party notices if adding third-party code. Keep project guidance current; do not add release-counter files or historical review reports in place of maintained documentation.

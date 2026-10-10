# Configuration reference

## Recording settings

The bundled `example/tuber-and-timber-call.toml` runs the included fictional MP3 with its context notes. For another recording, copy it and update the values:

```toml
version = 1
audio = 'calls\recording.amr'
context = ['vocabulary.md', 'call-context.md']
language = "da"
profile = "reviewed"
output = ["md", "srt"]
# Optional:
# name = "Potato delivery"
# start = "2026-04-12T09:00:00+02:00"
```

```cmd
pipenv run python transcribe.py -c "recording.toml"
```

Bundled names work wherever the recording config is located. TOML string values need quotes: use `profile = "reviewed"`. For a custom profile, use `profile = "custom/workflow.toml"` or an absolute path such as `profile = 'D:\profiles\workflow.toml'`.

- Audio can be one path or an array of paths. Recordings run sequentially.
- Context is an array of UTF-8 text/Markdown paths. A UTF-8 BOM is accepted.
- Relative paths inside TOML are resolved from the file that contains them; absolute paths are used directly.
- Single-quoted TOML strings preserve Windows backslashes. Forward slashes also work on Windows.
- CLI audio paths replace the config's audio list. CLI language, profile, output, name and start override config values.
- Repeated CLI `-x` options append context files. Identical resolved file paths are read once.
- Both TOML `profile` and CLI `-p` accept bundled names: `quick`, `standard`, `reviewed`, or `researched`. Custom paths may be relative or absolute. Relative CLI paths use the current working directory; relative TOML profile paths use the recording config directory.
- A bare name selects a bundled profile. Use `custom.toml` or `./custom` for a custom file. An unknown bare name is rejected rather than silently treated as a path.
- Defaults are `reviewed`, language `da`, and standard Markdown output plus final-reconciliation SRT.
- `output` accepts a method string or a nonempty array of unique methods: `md`, `srt`, `mp4`, `txt`, `stdout`. CLI `-o` accepts a comma-separated string and replaces configured optional methods. Markdown is always saved; a reconciled `parts.source` is always saved as Markdown and SRT. `txt` and `stdout` add text files or terminal documents without suppressing standard files. Optional `mp4` creates black-background video with full audio and an embedded, switchable subtitle track. Explicit `srt` or `mp4` requires a reconciled `parts.source`.
- `language` and CLI `-l` use one curated catalog of 26 ISO 639-1 two-letter codes. Use a listed lowercase code; unsupported codes, three-letter codes and region variants are rejected before processing. See [Languages](../README.md#languages) for the full catalog and validation limits.
- There is no automatic config discovery. Use `-c` explicitly. All configs declare `version = 1`.

Audio and context must exist before processing. Invalid configuration is rejected before API calls. One recording config uses the same settings for every listed audio file; use separate configs for different recording contexts or languages.

For direct use without a recording TOML:

```cmd
pipenv run python transcribe.py "call.amr" -p standard -l en -x "names.md" -x "call-context.md"
```

Only files you select are read. Chat history is not collected automatically. Context files should contain known background and spellings, not an invented account of what people will say. Their contents are sent to the models for stages referencing `user`.

`src/i18n.py` owns language names, document labels and unclear-speech markers. Each accepted code has a complete translation entry; missing languages do not silently fall back to English. Configuration keys and diagnostic messages remain English. The selected code is passed to audio requests and its name/marker is supplied alongside model prompts. The built-in prompts refer to that marker rather than containing separate Danish/English branches. Compact model context uses stable English scaffolding for unknown speakers rather than translated display labels, so changing local document labels requires rendering only; changing a marker changes effective prompt rules and invalidates affected stages. Translation quality and model support beyond Danish/English have not been tested.

## Write a reusable profile

The `reviewed` profile contains this structure:

```toml
version = 1
id = "reviewed"
outputs = ["reconcile","summary"]

[parts]
source = "reconcile"
model = "../models/combine-parts.toml"
max_seconds = 300

[[stages]]
id = "transcribe"
model = "../models/transcribe-primary.toml"
context = ["user","previous"]

[[stages]]
id = "transcribe_independent"
model = "../models/transcribe-independent.toml"
context = ["user","previous"]

[[stages]]
id = "diarize"
model = "../models/diarize-speakers.toml"

[[stages]]
id = "reconcile_draft"
model = "../models/reconcile-draft.toml"
inputs = ["transcribe","transcribe_independent","diarize"]
context = ["user","previous"]

[[stages]]
id = "transcribe_verify"
model = "../models/transcribe-verify.toml"
context = ["user","previous","reconcile_draft"]

[[stages]]
id = "reconcile"
model = "../models/reconcile-final.toml"
inputs = ["transcribe","transcribe_independent","transcribe_verify","diarize"]
context = ["user","previous","reconcile_draft"]

[[stages]]
id = "summary"
model = "../models/summarize-recording.toml"
inputs = ["reconcile"]
context = ["user"]
```

`inputs` is primary material to process; `context` is background. The reserved context reference `user` supplies the recording's context files; `previous` supplies the previous part's completed `parts.source` result. `previous` is absent in part 1. Any other reference names a stage in the current part (or an assembled stage for summary context). Stage references establish graph dependencies; previous-part context establishes sequential processing across parts. No separate parallel flag is needed.

Only outputs and their transitive dependencies execute. An unused stage may be defined but is not run. Add a stage ID to `outputs` to save its readable transcript as well as its cache. `outputs` order controls display order, not execution order. When `parts.source` is a reconciliation, its Markdown/SRT documents are always written even if it is selected only as a summary dependency. This does not execute additional model stages.

Stage types have fixed contracts:

- `transcribe`: current part as audio, optional user/prior-stage/previous-part context. No `inputs`.
- `diarize`: current part as audio. No `inputs`, prompt, or context.
- `research`: one or more transcription inputs; optional user context. Uses web search.
- `reconcile`: one diarization input and one or more transcription inputs.
- `summary`: once per recording; its sole primary input must be `parts.source` after assembly. It cannot use `previous` context. Part stages cannot depend on summaries.

The explicit `type` may be omitted when it can be read from the model file. If provided, it must match. IDs must start with a letter and contain letters, digits, hyphens, or underscores. `user`, `previous`, `combine`, and `all` are reserved stage IDs. Unknown references, cycles, duplicate IDs (including case-only differences), and unsupported fields are errors. TOML files can include a UTF-8 BOM.

Use `--redo STAGE_ID` to force that stage in every part, `--redo combine` to force final assembly only, or `--redo all` to force the entire selected workflow, including its dependencies and the final combination when split. `combine` is skipped for one-part files. The latter reruns all used stages even if upstream results are identical; unused profile stages still do not run. Repeat `--redo` to select several stages. Unknown IDs remain errors even when combined with `all`.

The two initial transcription stages have no current-part dependency and run concurrently with diarization. `transcribe_verify` waits for `reconcile_draft`. The final `reconcile` retains the original word transcripts and diarization in `inputs`, with the provisional reconciliation as `context`. Do not substitute the draft for the original diarization: source indices must retain one coordinate system. `parts.source`, `outputs`, and summary input all select the final stage.

For a smaller graph, inspect [standard.toml](../profiles/standard.toml). For terminology research before verification, inspect [researched.toml](../profiles/researched.toml). Definitions are complete and editable; they do not inherit hidden stages from another profile.

## Multipart policy

Every profile defines `[parts]`:

| Field         | Meaning                                                                                             |
| ------------- | --------------------------------------------------------------------------------------------------- |
| `source`      | Required transcription or reconciliation stage whose result is carried forward and finally combined |
| `model`       | Relative/absolute model TOML with `type = "combine"`                                                |
| `max_seconds` | Maximum part duration, 10–2400 seconds; default 300 (five minutes)                                  |

The selected outputs must require `source`. All summaries take it as their primary input. All other required stages run per part. The final combine model runs only for multiple parts, outside the per-part graph; do not add it as a `[[stages]]` node. It always receives the user context plus all assembled source units. Its sparse structured response lets Python retain the full transcript without asking the model to repeat it. The combine model has the same configurable prompt, reasoning, and token-budget fields as other text models.

Use `context = ["user", "previous"]` for initial transcription passes and draft reconciliation. Verification adds `reconcile_draft`; final reconciliation also uses it as context. In `researched`, verification and final reconciliation add `research` as context. Research may also use `previous`; diarization cannot. The previous result is a fallible draft from different audio, not evidence of current speech. It contains only the immediately previous source result, plus topics and declared names from earlier reconciled parts. Prior payloads are never nested recursively. Audio prompts preserve all words and material uncertainty from prior results, but omit source indices, timing and cache keys. User documents remain intact. Text stages receive full structured results. No text is silently clipped. Remove `previous` from a stage to omit that input; all parts still finish sequentially before the next begins.

The engine also splits files over 25,000,000 bytes even if they are short. The current audio model has a 2,000-token output limit; the five-minute cap provides headroom, not a completeness guarantee. Larger custom values may exceed a model limit even when uploads fit. Use shorter parts for dense speech or large context. The complete assembled transcript must still fit the final text model.

A larger raw WAV may compress easily, but still receives at least two parts under this policy. Parts are balanced in duration and adjusted toward nearby silence with room below the duration cap. Byte slicing and approximate compressed-file copying are not used. An AMR file that fits the duration/size limits keeps single-part conversion; longer AMR recordings follow the same multipart flow as other formats.

Stage caches include part boundaries, audio identity and selected previous context. Changing an earlier result can therefore invalidate later text stages, while their independent diarization caches remain valid. Final combination tracks only the assembled source and its diarization evidence, including their upstream recipe lineage; unrelated additional outputs do not invalidate it. Changing `parts.model` reruns final assembly and its dependants, not transcription. Changing `max_seconds` remeasures boundaries; matching effective part requests can still reuse caches. `--redo all` reruns model stages, not unchanged boundary measurements.

Additional per-part outputs are assembled locally: ordinary transcripts and research notes are concatenated; diarization timestamps and IDs are globalized. Only `parts.source` receives the final model editing pass. Its mapped diarization view is kept separate: additional diarization and reconciliation outputs retain their original part-local identities and name mappings. For one-part recordings, all selected results are used directly. All readable filenames use the original audio stem and stage ID, such as `call.reconcile.md`, `call.reconcile.srt`, `call.reconcile.mp4` and `call.summary.md`; model IDs remain in cache filenames only. Output directory naming still follows the recording TOML/`--name` rules. Existing files under earlier output names are not moved or removed. SRT is written only for the final reconciled source, using its mapped speakers and global timing, never for draft reconciliations, summaries, research or raw diarization.

SRT timing uses each turn's source-segment start/end bounds, rounded to milliseconds, with padded ends capped at recording duration. Untimed spoken turns share available gaps equally, including recording-edge gaps or the full duration when no turns are timed. Insufficient gaps and nonpositive cue durations are explicit errors, not permission to drop text or alter timed speech. Standard Markdown documents are written before SRT so they remain available after subtitle-export failure. See [Reconciled subtitles](../README.md#reconciled-subtitles) for details.

MP4 is an optional local export after SRT. It uses full original audio encoded as AAC, black H.264 video, and `mov_text` subtitles; overlapping cues are flattened into intervals containing all active speakers' text for MP4 only. It requires `ffmpeg` with `libx264`/`aac`/`mov_text` and the `color` filter, plus `ffprobe`. The video's duration follows the recording rather than subtitle length. Its local cache manifest/checksum lets unchanged exports skip encoding and media-tool checks; changed dialogue/rendering recipes or missing/modified video regenerate it without invalidating model stages. See [Optional MP4 playback](../README.md#optional-mp4-playback).

MP4 preparation measures full decoded audio samples and caches that duration under `cache/decoded-audio.*.json`. This can correct inaccurate bitrate-based AMR duration metadata while preserving existing transcription caches and part boundaries. The corrected value drives document duration, trailing subtitle interpolation and MP4 length, and is reused on subsequent runs even when MP4 is not selected. Invalid decoded-duration metadata is reported with its path and a remeasurement instruction; removing that indicated local measurement file does not force model calls.

## Reusable models and prompts

Example model definition:

```toml
version = 1
type = "transcribe"
api_model = "gpt-4o-transcribe"
prompt = '''
Transcribe speech faithfully. Preserve names, numbers and negations.
Use supplied context only to clarify wording supported by the audio.
Do not summarize, translate or invent speech.
'''
```

Each transcription role points to a separate file, so its `api_model` and prompt can be changed independently. To share a definition, point several stages at the same file. To experiment without affecting other profiles, copy the model file and change the relevant profile reference.

For a longer prompt, use `prompt_file = "prompts/reconcile.md"` instead of `prompt`. Its path is relative to the model TOML. Specifying both is an error. Prompt changes are detected by content, including external prompt files.

Reconciliation, summary, combine and research definitions also accept `max_output_tokens`. Reconciliation, summary and combine use the Responses API with a fixed structured-output schema; research uses text output with the built-in web-search tool. Transcription definitions use the audio transcription endpoint. **Changing the API model requires a model that supports that stage's endpoint and required response format**; configuration alone does not make arbitrary models compatible.

Bundled definitions are named for their role, not their execution number:

| Model TOML in `models/`       | API model                   | Role                                                                  |
| ----------------------------- | --------------------------- | --------------------------------------------------------------------- |
| `transcribe-primary.toml`     | `gpt-4o-transcribe`         | Faithful initial transcription                                        |
| `transcribe-independent.toml` | `gpt-4o-transcribe`         | Detail-focused listen without a current-part draft                    |
| `transcribe-verify.toml`      | `gpt-4o-transcribe`         | Check the draft against audio; return a complete transcript           |
| `diarize-speakers.toml`       | `gpt-4o-transcribe-diarize` | Speaker IDs and timing; no prompt                                     |
| `reconcile-draft.toml`        | `gpt-6.1-sol`               | Provisional conversation and essential uncertainties for verification |
| `reconcile-final.toml`        | `gpt-6.1-sol`               | Final wording and speakers, retaining original evidence               |
| `research-terms.toml`         | `gpt-6.1-sol`               | Sourced public terminology lookup                                     |
| `combine-parts.toml`          | `gpt-6.1-sol`               | Whole-recording speaker matching and sparse text corrections          |
| `summarize-recording.toml`    | `gpt-6.1-sol`               | One summary of the final whole-recording source                       |

These are nine reusable configurations using three API models. `standard` uses `reconcile-final` directly without a draft or verification stage. Additional passes have distinct jobs; repeating the same prompt is not an ensemble vote.

API model availability depends on your account. Authentication remains `OPENAI_API_KEY`; do not put credentials in any TOML file.

The bundled audio models have an announced February 26, 2027 retirement date; see the [model lifecycle note](../README.md#choose-a-workflow). Evaluate a replacement's quality and endpoint/response contracts before switching it in.

Prompts control editorial behaviour; the Python adapter adds a small data-handling and structured-output contract. Reconciliation attempts names from clear dialogue evidence: self-introductions, direct address and response, or explicit user context identifying a participant's role corroborated by the current part's dialogue. Context listing names or a generic role alone is insufficient, and tentative/conflicting associations remain uncertain. Unknown speakers retain IDs, and equal names do not merge distinct speakers. These remain edited automatic transcripts, not verified verbatim records.

## Reasoning and web research

Add this table **after top-level fields and the prompt** in a text-model TOML:

```toml
[reasoning]
effort = "medium"
# mode = "pro"
```

`effort` accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; actual support depends on the API model. GPT-6.1 Sol accepts `low` through `max` and rejects `none`/`minimal`. `mode` accepts `standard` or `pro` for compatible models. Omitting either option leaves the provider default. Bundled final reconciliation and combination use `high` with a 24,000-token budget. Draft reconciliation uses `medium` with 16,000 tokens; research and summary use `medium` with 6,000 tokens. None opts into pro mode. No reasoning settings are sent to audio models, and such configuration is rejected before requests.

`max_output_tokens` bounds visible output plus reasoning tokens. Higher reasoning can leave less room for the final transcript. An incomplete response is retained for diagnosis and is not cached as a success.

To add lookup to a custom profile, use a research stage and reference it as verification and final reconciliation **context**:

```toml
[[stages]]
id = "research"
model = "../models/research-terms.toml"
inputs = ["transcribe", "transcribe_independent"]
context = ["user", "previous", "reconcile_draft"]

# In your existing transcribe_verify and reconcile stages:
# context = ["user", "previous", "reconcile_draft", "research"]
```

Research is not a conversation source: it cannot replace a transcript input or become the summary's primary input. The bundled `researched` profile also includes `research` in `outputs`, so its notes and references are readable. A custom profile can omit it from `outputs` and still use its cached result as context.

The research model definition uses `type = "research"`, `api_model`, `prompt`/`prompt_file`, `max_output_tokens`, optional `[reasoning]`, and top-level `max_tool_calls` (1–20, default 3). Other model types reject `max_tool_calls`. There is no extra `web_search` toggle: selecting a required research stage is the opt-in. The model decides whether a lookup is useful, within that tool budget. Failed requests may be retried by the SDK, so the per-request cap is not a per-run spend cap.

Search results are fallible background. User spellings/corrections take priority; research cannot establish spoken commitments or identify voices. A research cache contains `text`, provider-reported `sources`, and `searched`. Clickable citations are converted to ordinary Markdown and source URLs must use HTTP(S). Tool sources include pages consulted, not only pages cited in the notes. Research is reused until its recipe changes or `--redo research` is given.

References checked for this implementation:

- [File transcription: upload limits, splitting, and previous context](https://developers.openai.com/api/docs/guides/speech-to-text)
- [GPT-6.1 Sol model](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Reasoning controls and token budgets](https://developers.openai.com/api/docs/guides/reasoning)
- [Responses web search and source metadata](https://developers.openai.com/api/docs/guides/tools-web-search)

## Template fields

Edit `templates/frontmatter.j2`, `reconcile.j2`, and `summary.j2` to change the documents. Rendering is local and does not invalidate model caches. Templates use Jinja with strict undefined-variable checks. Preflight exercises the standard populated branches and included templates before API calls, including unknown recording times with `start` and `end` set to `None`; unusual custom conditionals may still fail at render time.

| Field                        | Value                                                     |
| ---------------------------- | --------------------------------------------------------- |
| `start`, `end`               | ISO clock times or `None` when unknown                    |
| `start_source`               | `configured`, `filename`, or `unknown`                    |
| `participants`               | Names or speaker IDs in conversation order                |
| `topic`, `tags`              | Topic title and normalized, deduplicated topic tags       |
| `length`, `duration_seconds` | Displayed `mm:ss` length and numeric duration             |
| `profile`                    | Selected profile ID                                       |
| `parts`                      | Number of processed audio parts                           |
| `generated_at`               | Stage generation time, retained when using cache          |
| `turns`                      | Objects containing `time`, `speaker`, `label`, and `text` |
| `turn.start`, `turn.end`      | Source elapsed seconds, or `None` for untimed speech       |
| `notes`                      | Unique reconciliation notes                               |
| `notes_heading`              | Localized heading for reconciliation notes                 |
| `body`                       | Formatted summary, or `None` for reconciliation           |

Use the `yaml` filter for frontmatter values; it emits JSON-compatible quoted values, which are valid YAML. `turn.label` handles Markdown escaping so short spoken turns do not disappear as link definitions. Plain-text output uses literal brackets. Empty silence-only turns are omitted; a reconciliation that replaces spoken segments with empty text is rejected before caching.

Changing a model file affects all stages that reference it. Copy the file first when the change should apply to only one stage or profile. Relative model and prompt-file paths are resolved from the profile/model file respectively.

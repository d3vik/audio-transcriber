# SPDX-License-Identifier: Apache-2.0
import argparse
from pathlib import Path
import sys
from threading import Lock

from .api import OpenAIAdapter
from .config import BUNDLED_PROFILES, DEFAULT_PROFILE, load_job, output_methods
from .engine import Pipeline
from .i18n import DEFAULT_LANGUAGE, LANGUAGES


def parser():
    p = argparse.ArgumentParser(
        description=(
            'Transcribe recordings using reusable workflow '
            'and model TOML files.'
        )
    )
    p.add_argument(
        'files',
        nargs='*',
        type=Path,
        help='Audio files (replace recording config audio)',
    )
    p.add_argument('-c', '--config', type=Path, help='Recording TOML')
    p.add_argument(
        '-p',
        '--profile',
        help=(
            f'{", ".join(BUNDLED_PROFILES)}, '
            f'or TOML path (default {DEFAULT_PROFILE})'
        ),
    )
    p.add_argument(
        '-x',
        '--context',
        action='append',
        type=Path,
        default=[],
        help='UTF-8 context file; repeatable, appended to config context',
    )
    p.add_argument(
        '-l',
        '--language',
        choices=tuple(LANGUAGES),
        help=(
            'Recording language: supported ISO 639-1 code '
            f'(default {DEFAULT_LANGUAGE})'
        ),
    )
    p.add_argument(
        '-o',
        '--output',
        type=output_methods,
        metavar='md|srt|mp4|txt|stdout',
        help=(
            'Output methods, comma-separated; Markdown and final reconciled '
            'SRT are always saved. mp4/txt/stdout add extra outputs.'
        ),
    )
    p.add_argument(
        '--name',
        help='Output folder name; defaults to config stem or audio stem',
    )
    p.add_argument(
        '--start',
        help=(
            'Recording start in ISO date/time form, '
            'optionally with timezone offset'
        ),
    )
    p.add_argument(
        '--redo',
        action='append',
        default=[],
        metavar='STAGE_ID|combine|all',
        help=(
            'Force a stage in every part, final combine, '
            'or all model calls; repeatable'
        ),
    )
    p.add_argument(
        '--plan',
        action='store_true',
        help=(
            'Validate and print dependencies without API calls '
            'or writing outputs'
        ),
    )
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        job = load_job(args)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    if args.plan:
        for audio in job.audio:
            print(f'Audio: {audio}\nOutput: {job.output_root(audio)}')
        print(f'Profile: {job.profile.id}; language: {job.language}')
        print('Documents: Markdown for saved outputs; '
              'SRT for final reconciliation; '
              'MP4/txt/stdout when selected.')
        for audio in job.audio:
            for name in job.saved_stages():
                for method in job.methods_for(name):
                    if method != 'stdout':
                        print(
                            '  File: '
                            + str(job.output_path(audio, name, method))
                        )
        print(
            f'Parts: at most {job.profile.parts.max_seconds}s; '
            f'carry {job.profile.parts.source}; '
            f'combine using {job.profile.parts.model.api_model} '
            'only when split.'
        )
        print(
            'Part boundaries are measured during preflight; '
            'summaries run once after assembly.'
        )
        if 'combine' in job.redo:
            print('combine: forced rerun when multiple parts exist')
        required = job.profile.required()
        for name in job.profile.order:
            stage = job.profile.stages[name]
            saved = (
                stage.id in job.profile.outputs
                or (
                    stage.id == job.profile.parts.source
                    and stage.type == 'reconcile'
                )
            )
            status = (
                'saved output'
                if saved
                else 'dependency' if stage.id in required else 'unused'
            )
            if stage.id in job.redo:
                status += '; forced rerun'
            print(
                f'{stage.id}: {stage.type} / {stage.model.api_model}; '
                'waits for '
                f'{", ".join(sorted(stage.dependencies)) or "nothing"}; '
                f'{status}'
            )
            if stage.model.reasoning:
                print(
                    '  reasoning: '
                    + ', '.join(
                        f'{key}={value}'
                        for key, value in stage.model.reasoning.items()
                    )
                )
            if stage.type == 'research':
                print(
                    '  web lookup enabled; at most '
                    f'{stage.model.max_tool_calls} tool calls per request'
                )
        return 0
    adapter = OpenAIAdapter()
    failed = False
    log_lock = Lock()

    def report(message):
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    try:
        report('Checking dependencies and cached stages...')
        try:
            pipelines = [
                Pipeline(audio, job, adapter, log=report)
                for audio in job.audio
            ]
            for pipeline in pipelines:
                pipeline.preflight()
            if any(pipeline.needs_api for pipeline in pipelines):
                # Check the SDK and key before any stage runs.
                adapter.client()
        except Exception as exc:
            report(f'Preflight failed: {exc}')
            return 1
        report('Checks passed.')
        for pipeline in pipelines:
            report(f'Processing: {pipeline.audio}\nOutput: {pipeline.root}')
            try:
                text = pipeline.run()
                if text:
                    print(text, end='')
            except Exception as exc:
                report(f'{pipeline.audio}: {exc}')
                failed = True
    except KeyboardInterrupt:
        print('Interrupted; completed stages remain cached.', file=sys.stderr)
        return 130
    finally:
        adapter.close()
    return 1 if failed else 0

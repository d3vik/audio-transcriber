# SPDX-License-Identifier: Apache-2.0
from contextlib import contextmanager
from pathlib import Path
import json
import math
import re
import shutil
import subprocess
import tempfile

UPLOAD_LIMIT = 25_000_000
PART_TARGET = 24_000_000
# 64 kbit/s MP3; leave room for container overhead.
ENCODED_BYTES_PER_SECOND = 8000
MEDIA_RECIPE = 'mono-16k-mp3-64k-silence-v1'
VIDEO_RECIPE = 'black-720p-h264-10fps-aac-192k-mov-text-v2'
DECODED_DURATION_RECIPE = 'decoded-sample-duration-v1'
UPLOAD_EXTENSIONS = {
    '.flac',
    '.mp3',
    '.mp4',
    '.mpeg',
    '.mpga',
    '.m4a',
    '.ogg',
    '.wav',
    '.webm',
}


def needs_conversion(path):
    return path.suffix.lower() not in UPLOAD_EXTENSIONS


@contextmanager
def audio_for_upload(path):
    if not needs_conversion(path):
        yield path
        return
    with encoded_upload(path) as converted:
        yield converted


def check_ffmpeg(encoders=('libmp3lame',)):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError(
            'Media encoding requires ffmpeg on PATH. '
            'Install FFmpeg.'
        )
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-encoders'],
        capture_output=True,
        text=True,
        errors='replace',
        timeout=15,
    )
    available = {
        fields[1] for line in result.stdout.splitlines()
        if len(fields := line.split()) >= 2
    }
    if result.returncode or not set(encoders) <= available:
        raise ValueError(
            'FFmpeg must include these encoders: ' + ', '.join(encoders) + '.'
        )
    return ffmpeg


def check_video_tools():
    ffmpeg = check_ffmpeg(('libx264', 'aac', 'mov_text'))
    if not shutil.which('ffprobe'):
        raise ValueError('MP4 validation requires ffprobe on PATH.')
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-filters'], capture_output=True,
        text=True, errors='replace', timeout=15,
    )
    if result.returncode or not re.search(
        r'^\s*\S+\s+color\s', result.stdout, re.MULTILINE
    ):
        raise ValueError('MP4 output requires the FFmpeg color video filter.')


def decoded_audio_duration(path):
    """Measure decoded samples, not an estimated container bitrate."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('Accurate decoded duration requires ffmpeg on PATH.')
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(
            [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
             '-xerror', '-i', str(path.resolve()), '-map', '0:a:0',
             '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
             '-f', 's16le', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=errors,
        ) as process:
            if process.stdout is None:
                raise ValueError('Decoded audio stream is unavailable.')
            size = sum(
                len(block)
                for block in iter(lambda: process.stdout.read(65536), b'')
            )
            if process.wait():
                errors.seek(0)
                diagnostic = errors.read().decode('utf-8', errors='replace')
                raise ValueError(
                    'Cannot measure decoded audio duration: '
                    + diagnostic.strip()
                )
    if not size or size % 2:
        raise ValueError('Decoded audio duration is unavailable or empty.')
    return size / (16000 * 2)


def encode_video(audio, subtitles, output, duration):
    """Encode full audio with black video and switchable subtitles."""
    has_subtitles = bool(subtitles.stat().st_size)
    subtitle_input = ['-i', str(subtitles.resolve())] if has_subtitles else []
    subtitle_output = (
        ['-map', '2:s:0', '-c:s', 'mov_text',
         '-metadata:s:s:0', 'title=Reconciled subtitles',
         '-disposition:s:0', 'default']
        if has_subtitles else []
    )
    result = subprocess.run(
        ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'color=c=black:s=1280x720:r=10',
         '-i', str(audio.resolve()), *subtitle_input,
         '-map', '0:v:0', '-map', '1:a:0',
         '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'stillimage',
         '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
         *subtitle_output, '-t', str(duration),
         '-movflags', '+faststart', '-y', str(output)],
        capture_output=True, text=True, errors='replace',
    )
    if result.returncode:
        raise ValueError(f'MP4 encoding failed: {result.stderr.strip()}')
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'stream=codec_type,codec_name,duration:stream_disposition=default',
         '-of', 'json', str(output)],
        capture_output=True, text=True, errors='replace', timeout=30,
    )
    if result.returncode:
        raise ValueError(f'Cannot validate MP4: {result.stderr.strip()}')
    streams = json.loads(result.stdout).get('streams', [])
    expected = [('video', 'h264'), ('audio', 'aac')]
    if has_subtitles:
        expected.append(('subtitle', 'mov_text'))
    if [(stream['codec_type'], stream['codec_name'])
            for stream in streams] != expected:
        raise ValueError('MP4 has missing or unexpected media streams.')
    if has_subtitles and streams[2].get('disposition', {}).get('default') != 1:
        raise ValueError('MP4 subtitle track is not enabled by default.')
    for stream in streams[:2]:
        try:
            measured = float(stream['duration'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                'MP4 audio/video duration is unavailable.'
            ) from exc
        if not math.isfinite(measured) or abs(measured - duration) > 0.25:
            raise ValueError(
                f'MP4 {stream["codec_type"]} duration {measured:.3f}s '
                f'does not match decoded recording duration {duration:.3f}s '
                f'(difference {measured - duration:+.3f}s).'
            )


def audio_duration(path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise ValueError(
            'ffprobe is required to read recording duration. '
            'Install FFmpeg and add it to PATH.'
        )
    result = subprocess.run(
        [
            ffprobe,
            '-v',
            'error',
            '-select_streams',
            'a:0',
            '-show_entries',
            'format=duration:stream=codec_type,codec_name,duration',
            '-of',
            'json',
            str(path),
        ],
        capture_output=True,
        text=True,
        errors='replace',
        timeout=30,
    )
    if result.returncode:
        raise ValueError(
            f'Cannot read audio metadata: {result.stderr.strip()}'
        )
    data = json.loads(result.stdout)
    streams = [
        item
        for item in data.get('streams', [])
        if item.get('codec_type') == 'audio'
    ]
    if not streams:
        raise ValueError(f'No audio stream in {path}')
    if streams[0].get('codec_name') in ('amr_nb', 'amr_wb'):
        return decoded_audio_duration(path)
    raw = streams[0].get('duration')
    if raw in (None, 'N/A'):
        raw = data.get('format', {}).get('duration')
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('Audio duration is unavailable.') from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Audio duration must be finite and positive.')
    return duration


def needs_parts(duration, size, maximum):
    return size > UPLOAD_LIMIT or duration > maximum


def silence_cut(path, earliest, latest):
    """Prefer the last complete silence midpoint before the hard cut."""
    result = subprocess.run(
        [
            'ffmpeg',
            '-nostdin',
            '-hide_banner',
            '-ss',
            str(earliest),
            '-i',
            str(path),
            '-t',
            str(latest - earliest),
            '-map',
            '0:a:0',
            '-af',
            'silencedetect=noise=-35dB:d=0.35',
            '-f',
            'null',
            '-',
        ],
        capture_output=True,
        text=True,
        errors='replace',
    )
    if result.returncode:
        raise ValueError(
            f'Cannot find audio boundaries: {result.stderr.strip()}'
        )
    start, cut = None, latest
    for kind, value in re.findall(
        r'silence_(start|end):\s*([0-9.]+)', result.stderr
    ):
        value = float(value)
        if kind == 'start':
            start = value
        elif start is not None:
            cut = earliest + (start + value) / 2
            start = None
    return min(latest, max(earliest, cut))


def plan_parts(path, duration, maximum):
    size = path.stat().st_size
    if not needs_parts(duration, size, maximum):
        return [{'index': 1, 'start': 0.0, 'end': duration}]
    check_ffmpeg()
    ceiling = min(maximum, PART_TARGET / ENCODED_BYTES_PER_SECOND)
    reserve = min(10, ceiling * 0.1)
    count = max(2, math.ceil(duration / ceiling))
    target = duration / count
    parts, start = [], 0.0
    for i in range(1, count):
        latest = min(i * target, start + ceiling)
        earliest = max(
            start + target * 0.8,
            latest - reserve,
            duration - (count - i) * ceiling,
        )
        cut = (
            silence_cut(path, earliest, latest)
            if latest - earliest > 0.000001
            else latest
        )
        end = round(cut, 6)
        if end <= start:
            raise ValueError('Cannot create a positive-length audio part.')
        parts.append({'index': len(parts) + 1, 'start': start, 'end': end})
        start = end
    parts.append({'index': len(parts) + 1, 'start': start, 'end': duration})
    return parts


def validate_parts(parts, duration, maximum):
    if not isinstance(parts, list) or not parts:
        raise ValueError('Audio parts must be a nonempty array.')
    previous = 0.0
    for i, part in enumerate(parts, 1):
        if (
            not isinstance(part, dict)
            or set(part) != {'index', 'start', 'end'}
            or type(part['index']) is not int
            or part['index'] != i
            or not all(
                type(part[k]) in (int, float) and math.isfinite(part[k])
                for k in ('start', 'end')
            )
            or abs(part['start'] - previous) > 0.000001
            or not 0 <= part['start'] < part['end'] <= duration
            or part['end'] - part['start'] > maximum + 0.001
        ):
            raise ValueError(
                'Invalid or non-contiguous audio part boundaries.'
            )
        previous = part['end']
    if abs(previous - duration) > 0.000001:
        raise ValueError('Audio parts do not cover the complete recording.')


def part_for_upload(path, part):
    """Cut decoded audio accurately, not compressed-container bytes."""
    return encoded_upload(path, part)


@contextmanager
def encoded_upload(path, part=None):
    """Share encoding and upload checks for whole files and parts."""
    with tempfile.TemporaryDirectory(prefix='transcribe-') as directory:
        output = Path(directory) / 'audio.mp3'
        seek = ['-ss', str(part['start'])] if part else []
        limit = ['-t', str(part['end'] - part['start'])] if part else []
        process = subprocess.run(
            [
                'ffmpeg',
                '-nostdin',
                '-hide_banner',
                '-loglevel',
                'error',
                *seek,
                '-i',
                str(path.resolve()),
                *limit,
                '-map',
                '0:a:0',
                '-vn',
                '-ac',
                '1',
                '-ar',
                '16000',
                '-c:a',
                'libmp3lame',
                '-b:a',
                '64k',
                str(output),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors='replace',
        )
        if process.returncode:
            raise ValueError(
                f'FFmpeg conversion failed: {process.stderr.strip()}'
            )
        if not 0 < output.stat().st_size <= UPLOAD_LIMIT:
            raise ValueError(
                'Encoded audio is empty or exceeds the upload limit; '
                'lower parts.max_seconds.'
            )
        yield output

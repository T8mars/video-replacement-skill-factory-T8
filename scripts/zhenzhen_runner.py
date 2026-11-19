"""Zhenzhen AI character replacement. Shares the factory queue and review contracts."""
from fractions import Fraction
import json
import mimetypes
from pathlib import Path
from urllib import error, parse, request
import uuid

from PIL import Image
import rh_runner as cloud

MODEL = 'minimax-h3-comfyui-charswap'
KIND = 'zhenzhen-charswap'
BASE = 'https://api.seedance.nz'
GENERATIONS = '/v1/video/generations'
# 22+17n at 24 fps, constrained by the observed two-second input minimum
# and the factory's ten-second cap on the full input and output.
COUNTS = tuple(range(56, 241, 17))
MIB = 1024 * 1024


def frame_count(needed):
    for frames in COUNTS:
        if frames >= needed:
            return frames
    raise ValueError('Split at a reviewed shot/action boundary: no frame grid point fits the 10-second cap')


def validate_window(profile, window, slots):
    allowed = {'kind','model','resolution','ratio','limits','quality_evidence','evidence','seed'}
    if set(profile) - allowed:
        raise ValueError('Use the private key file option; unknown charswap profile settings are not supported')
    if (profile.get('model') != MODEL or not 1 <= len(slots) <= 9
            or len(set(slots)) != len(slots)):
        raise ValueError('Charswap requires its supported model and 1..9 distinct ordered role slots')
    frames = window['generation_frames']
    if (type(frames) is not int or frames not in COUNTS or Fraction(window['generation_fps']) != 24
            or Fraction(str(window['duration'])) != Fraction(frames, 24)):
        raise ValueError('Charswap needs matching 24fps input on the supported 2..10s frame grid')
    resolution, ratio = profile.get('resolution'), profile.get('ratio')
    ratios = {'auto', '16:9', '9:16'}
    if resolution == '768p':
        ratios.update(('1:1', '4:3', '3:4', '21:9'))
    if resolution == 'auto':
        ratios = {'auto'}
    if resolution not in ('480p', '768p', '1088p', 'auto') or ratio not in ratios:
        raise ValueError('Unsupported charswap resolution/ratio combination')
    canvas = window.get('source_projection', {}).get('display_canvas')
    if ratio != 'auto' and canvas:
        source_ratio = Fraction(canvas['width'], canvas['height']) * Fraction(canvas['sample_aspect_ratio'])
        if source_ratio != Fraction(*map(int, ratio.split(':'))):
            raise ValueError('Choose a source-matching ratio or auto')
    seed = profile.get('seed')
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 2147483647):
        raise ValueError('Invalid charswap seed')


def validate_files(refs, video):
    sizes = [path.stat().st_size for path in refs]
    if max(sizes) > 10*MIB or sum(sizes) > 50*MIB:
        raise ValueError('Reference image exceeds the charswap upload limits')
    for path in refs:
        with Image.open(path) as picture:
            if picture.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Use PNG, JPEG or WebP character references')
    if video.suffix.lower() != '.mp4' or video.stat().st_size > 50*MIB:
        raise ValueError('Charswap needs one MP4 of at most 50 MiB')


def payload(profile, prompt, refs, video, frames):
    metadata = {'video_url': [video['url']], 'frames': frames,
                'resolution': profile['resolution'], 'ratio': profile['ratio']}
    if profile.get('seed') is not None:
        metadata['seed'] = profile['seed']
    return {'model': MODEL, 'prompt': prompt, 'images': [ref['url'] for ref in refs],
            'metadata': metadata}


class NoApiRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def data_of(response):
    data = response.get('data')
    return data if isinstance(data, dict) else response


def result_url(data):
    content = data_of(data).get('content') or {}
    for value in (data.get('result_url'), data.get('output_url'), content.get('video_url'),
                  content.get('video_urls'), (data.get('metadata') or {}).get('url')):
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, str) and value.startswith('https://'):
            return value
    return None


class Transport(cloud.Transport):
    provider = 'zhenzhen'
    download_headers = {'User-Agent': 'Mozilla/5.0'}

    def __init__(self, key):
        self.key = key
        self.opener = request.build_opener(NoApiRedirect())

    def api(self, method, path, *, body=None, mime='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        req = request.Request(BASE + path, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self.key, 'Content-Type': mime, 'User-Agent': 'Mozilla/5.0'})
        try:
            with self.opener.open(req, timeout=60) as response:
                return json.load(response)
        except error.HTTPError as exc:
            exc.close()
            failure = cloud.ApiFailure(exc.code)
            try:
                failure.retry_after = max(60, int(exc.headers.get('Retry-After', 60)))
            except (TypeError, ValueError):
                failure.retry_after = 60
            raise failure from None

    def upload(self, path):
        boundary = '----factory' + uuid.uuid4().hex
        suffix = path.suffix.lower()
        if suffix != '.mp4':
            with Image.open(path) as picture:
                suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[picture.format]
        name = cloud.sha(path) + suffix
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  f'Content-Type: {mime}\r\n\r\n').encode('ascii')
        body = header + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode('ascii')
        result = data_of(self.api('POST', '/v1/files/upload', body=body,
                                  mime='multipart/form-data; boundary=' + boundary))
        if not str(result.get('url', '')).startswith('https://'):
            raise ValueError('No HTTPS upload result')
        return {'url': result['url'], 'expires_in': int(result.get('expires_in', 86400))}

    def estimate(self, body):
        return self.api('POST', GENERATIONS + '/estimate', body=body)

    def submit(self, path, body):
        if path != GENERATIONS or body.get('model') != MODEL:
            raise ValueError('Wrong charswap route')
        data = data_of(self.api('POST', path, body=body))
        return {'taskId': data.get('task_id') or data.get('id')}

    def query(self, task_id):
        raw = self.api('GET', GENERATIONS + '/' + parse.quote(task_id, safe=''))
        data = data_of(raw)
        received = data.get('task_id') or data.get('id')
        if received is not None and str(received) != task_id:
            raise ValueError('Provider task ID differs from the saved task')
        state = str(data.get('status', '')).upper()
        if state in ('SUCCESS', 'SUCCEEDED', 'COMPLETED'):
            state = 'SUCCESS'
        elif state in ('FAILURE', 'FAILED', 'CANCELLED', 'CANCELED'):
            state = 'FAILED'
        elif state in ('PENDING', 'SUBMITTED', 'IN_QUEUE'):
            state = 'QUEUED'
        elif state in ('PROCESSING', 'IN_PROGRESS'):
            state = 'RUNNING'
        url = result_url(data) if state == 'SUCCESS' else None
        return {'taskId': str(received) if received is not None else None, 'status': state,
                'failure_reason': str(data.get('fail_reason') or '')[:1000] if state == 'FAILED' else None,
                'usage': data.get('usage', raw.get('usage')),
                'results': [{'outputType': 'mp4', 'url': url}] if url else []}


if __name__ == '__main__':
    cloud.main(provider='zhenzhen')

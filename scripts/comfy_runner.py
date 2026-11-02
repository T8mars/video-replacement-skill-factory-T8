"""Optional loopback ComfyUI runner. One durable execution slot, no visual verdicts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

import factory
import media
import comfy_workflow

ACTIVE = ('SUBMITTING', 'UNKNOWN', 'QUEUED', 'SUCCEEDED', 'RECOVERY_REQUIRED')


def queue_records(document):
    if (not isinstance(document, dict) or any(not isinstance(document.get(k), list)
                                             for k in ('queue_running', 'queue_pending'))):
        raise ValueError('ComfyUI queue response is incomplete; no new submission')
    records = document['queue_running'] + document['queue_pending']
    if any(not isinstance(r, list) or len(r) < 4 or not isinstance(r[1], str)
           or not isinstance(r[2], dict) or not isinstance(r[3], dict) for r in records):
        raise ValueError('ComfyUI queue records cannot be identified')
    return records


def endpoint(url):
    """All loopback aliases for one port share a serial admission key."""
    p = urllib.parse.urlsplit(url)
    if (p.scheme != 'http' or p.hostname not in ('localhost', '127.0.0.1', '::1')
            or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment):
        raise ValueError('Use an explicit loopback HTTP ComfyUI URL without credentials or paths')
    return 'loopback:' + str(p.port or 80)


def file_item(item, allowed_types=('output', 'temp')):
    name, folder = item.get('filename'), item.get('subfolder', '')
    if (not isinstance(name, str) or not name or '/' in name or '\\' in name or ':' in name or '\x00' in name
            or name in ('.', '..') or not isinstance(folder, str)
            or '\\' in folder or ':' in folder or '\x00' in folder or PurePosixPath(folder).is_absolute()
            or '..' in PurePosixPath(folder).parts or item.get('type', 'output') not in allowed_types):
        raise ValueError('Invalid ComfyUI file descriptor')
    return {'filename': name, 'subfolder': folder, 'type': item.get('type', 'output')}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('ComfyUI redirects are not accepted')


class Client:
    def __init__(self, url):
        self.endpoint = endpoint(url)
        self.url = url.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, route, body=None):
        data = None if body is None else json.dumps(body, allow_nan=False).encode('utf8')
        request = urllib.request.Request(self.url + route, data=data,
                                         headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=30) as result:
            return json.load(result)

    def upload(self, path, name):
        path = Path(path)
        boundary = 'factory-' + uuid.uuid4().hex
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                  f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        tail = (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput'
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\nfalse'
                f'\r\n--{boundary}--\r\n').encode()
        def blocks():
            yield header
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk
            yield tail
        request = urllib.request.Request(self.url + '/upload/image', data=blocks(), headers={
            'Content-Type': 'multipart/form-data; boundary=' + boundary,
            'Content-Length': str(len(header) + path.stat().st_size + len(tail))})
        with self.opener.open(request, timeout=120) as result:
            item = json.load(result)
        checked = file_item({'filename': item.get('name'), 'subfolder': item.get('subfolder', ''),
                             'type': item.get('type')}, ('input',))
        return '/'.join(filter(None, (checked['subfolder'], checked['filename'])))

    def download(self, item, destination):
        item = file_item(item)
        request = self.url + '/view?' + urllib.parse.urlencode(item)
        with self.opener.open(request, timeout=120) as response, Path(destination).open('wb') as output:
            shutil.copyfileobj(response, output)


def select_output(history, selection):
    status = history.get('status', {})
    messages = status.get('messages', [])
    if (status.get('completed') is not True or status.get('status_str') != 'success'
            or any(isinstance(m, list) and m and m[0] in ('execution_error', 'execution_interrupted') for m in messages)):
        raise ValueError('ComfyUI execution is not successfully complete')
    records = history.get('outputs', {}).get(selection['node_id'], {}).get(selection['field'], [])
    if not isinstance(records, list):
        raise ValueError('Selected output collection is not a file list')
    index = selection.get('index')
    if index is None:
        if len(records) != 1:
            raise ValueError('Select an explicit final-video index; output is ambiguous or missing')
        index = 0
    if type(index) is not int or not 0 <= index < len(records):
        raise ValueError('Selected video output index is out of range')
    item = file_item(records[index])
    if Path(item['filename']).suffix.lower() not in ('.mp4', '.mov', '.mkv', '.webm'):
        raise ValueError('Selected output is not a supported video file')
    return item


class Ledger:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS local_attempts(
              id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, project TEXT NOT NULL,
              window TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
              entry TEXT NOT NULL, graph TEXT, prompt_id TEXT, receipt TEXT,
              output TEXT, signature TEXT, issue TEXT, created REAL NOT NULL, selection TEXT);
            CREATE TABLE IF NOT EXISTS local_controls(endpoint TEXT PRIMARY KEY, stopped INTEGER NOT NULL DEFAULT 0);
        ''')
        if 'selection' not in {r['name'] for r in self.db.execute('PRAGMA table_info(local_attempts)')}:
            self.db.execute('ALTER TABLE local_attempts ADD COLUMN selection TEXT')

    def close(self):
        self.db.close()

    def rows(self, key):
        return [dict(r) for r in self.db.execute('SELECT * FROM local_attempts WHERE endpoint=? ORDER BY created,rowid', (key,))]

    def update(self, aid, **changes):
        allowed = {'state', 'graph', 'prompt_id', 'receipt', 'output', 'signature', 'issue', 'selection'}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError('Invalid local attempt update')
        self.db.execute('UPDATE local_attempts SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?',
                        (*changes.values(), aid))

    def stop(self, key, stopped=True):
        self.db.execute('INSERT OR REPLACE INTO local_controls VALUES(?,?)', (key, int(stopped)))

    def reserve(self, key, project, entry):
        project = str(Path(project).resolve())
        self.db.execute('BEGIN IMMEDIATE')
        try:
            stopped = self.db.execute('SELECT stopped FROM local_controls WHERE endpoint=?', (key,)).fetchone()
            rows = self.rows(key)
            if (stopped and stopped['stopped']) or any(r['state'] in ACTIVE for r in rows):
                self.db.execute('ROLLBACK')
                return None
            if any(r['project'] == project and r['window'] == entry['window_id']
                   and r['digest'] == entry['input_digest'] and r['state'] != 'PREPARATION_FAILED' for r in rows):
                self.db.execute('ROLLBACK')
                return None
            aid = uuid.uuid4().hex
            self.db.execute('INSERT INTO local_attempts(id,endpoint,project,window,digest,state,entry,created) VALUES(?,?,?,?,?,?,?,?)',
                            (aid, key, project, entry['window_id'], entry['input_digest'], 'SUBMITTING', json.dumps(entry), time.time()))
            self.db.execute('COMMIT')
            return aid
        except BaseException:
            self.db.execute('ROLLBACK')
            raise


def preflight(project, entry):
    if entry['profile'].get('kind') != 'local-comfyui':
        raise ValueError('Use the RH runner for cloud profiles')
    current = factory.compute_input_digest(project, entry)
    if current != entry['input_digest']:
        raise ValueError('Local inputs changed; prepare and review again')
    review = entry.get('semantic_review', {})
    if review.get('status') != 'passed' or review.get('input_digest') != current:
        raise ValueError('Current semantic review is required')
    if entry.get('production_authorized') is not True or factory.read(Path(project) / 'job.json').get('production_authorized') is not True:
        raise ValueError('Current production authorization is required')
    window = entry['source_contract']['window']
    source, analysis = factory.validate_analysis(project)
    plan = factory.read(Path(project) / 'plan.json')
    factory.validate_plan(source, analysis, plan)
    planned = next(w for w in plan['windows'] if w['window_id'] == entry['window_id'])
    if (factory.generation_window(window) != factory.generation_window(planned)
            or entry['generation_frames'] != window['generation_frames']
            or entry['duration'] != window['duration']):
        raise ValueError('Local request does not match the current planned window')
    factory.validate_generation_duration(window)
    factory.validate_local_window(project, entry['profile'], window, analysis)
    projection = window['source_projection']
    if projection['requires_temporal_review']:
        pr = entry.get('source_projection_review', {})
        if pr.get('status') != 'passed' or pr.get('input_digest') != current or pr.get('loss_disclosed') is not True:
            raise ValueError('Current temporal-loss review is required')
    comfy_workflow.validate_profile(project, entry['profile'])
    return current


def match_attempt(record, row):
    """Official queue/history prompt tuple retains graph and extra_data."""
    if not isinstance(record, list) or len(record) < 4 or not row.get('graph'):
        return False
    extra = record[3]
    return (isinstance(extra, dict) and extra.get('factory_attempt') == row['id']
            and extra.get('client_id') == row['id']
            and factory.digest(record[2]) == factory.digest(json.loads(row['graph'])))


class Runner:
    def __init__(self, ledger, client, deps=None, scalar_cpu=False):
        self.ledger, self.client = ledger, client
        self.key = client.endpoint
        self.deps, self.scalar_cpu = deps, scalar_cpu

    def reconcile(self, row):
        queue, history = self.client.request('/queue'), self.client.request('/history')
        matches = set()
        for record in queue_records(queue):
            if match_attempt(record, row):
                matches.add(record[1])
        for pid, record in history.items():
            if match_attempt(record.get('prompt'), row) and record['prompt'][1] == pid:
                matches.add(pid)
        if len(matches) == 1:
            self.ledger.update(row['id'], prompt_id=matches.pop(), state='QUEUED', issue=None)
        else:
            self.ledger.update(row['id'], state='UNKNOWN', issue='No unique queue/history match; original submission must be reconciled')

    def recover(self, row):
        if row['state'] in ('SUBMITTING', 'UNKNOWN'):
            self.reconcile(row)
            return
        pid = row['prompt_id']
        # Completed receipts are durable; history can be cleared while downloads need recovery.
        if row['state'] == 'SUCCEEDED' and row.get('receipt'):
            record = json.loads(row['receipt'])
        else:
            history = self.client.request('/history/' + urllib.parse.quote(pid, safe=''))
            record = history.get(pid)
        if not record:
            queue = self.client.request('/queue')
            active = [r[1] for r in queue_records(queue)]
            if pid not in active:
                self.ledger.update(row['id'], state='RECOVERY_REQUIRED', issue='Known prompt absent from queue/history; do not resubmit')
            return
        if not match_attempt(record.get('prompt'), row) or record['prompt'][1] != pid:
            self.ledger.update(row['id'], state='RECOVERY_REQUIRED', issue='History does not bind this attempt and graph; original ID retained')
            return
        status = record.get('status', {})
        if status.get('status_str') == 'error' or any(m[0] in ('execution_error', 'execution_interrupted') for m in status.get('messages', []) if isinstance(m, list) and m):
            self.ledger.update(row['id'], state='FAILED', receipt=json.dumps(record), issue='Execution failed; inspect this attempt before a changed repair input')
            self.ledger.stop(self.key)
            return
        if status.get('completed') is not True:
            return
        entry = json.loads(row['entry'])
        self.ledger.update(row['id'], state='SUCCEEDED', receipt=json.dumps(record))
        selection = entry['profile']['output']
        if row.get('selection'):
            override = json.loads(row['selection'])
            if override['receipt_digest'] != factory.digest(record) or override['prompt_id'] != pid:
                raise ValueError('Output selection no longer binds the successful receipt')
            selection = override['output']
        item = select_output(record, selection)
        selection_suffix = '-' + factory.digest(selection)[:16] if row.get('selection') else ''
        output = Path(row['project']) / 'candidates' / row['window'] / row['id'] / ('output' + selection_suffix + Path(item['filename']).suffix.lower())
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.exists():
            temporary = output.with_name(output.stem + '-' + uuid.uuid4().hex + '.part' + output.suffix)
            try:
                self.client.download(item, temporary)
                media.inspect(temporary, self.deps, self.scalar_cpu)
                os.link(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        # Verify an existing file too, including a crash after promotion but before receipt write.
        report = media.inspect(output, self.deps, self.scalar_cpu)
        if row.get('signature') and json.loads(row['signature']) != media.signature(output):
            raise ValueError('Previously downloaded local candidate changed')
        self.ledger.update(row['id'], state='DOWNLOADED', output=str(output),
                           signature=json.dumps(report['file_signature']), issue=None)

    def submit(self, project, entry):
        preflight(project, entry)
        aid = self.ledger.reserve(self.key, project, entry)
        if not aid:
            return
        try:
            uploads = {'video': self.client.upload(factory.resolve(project, entry['video_path']), aid + '-video.mp4'),
                       'references': []}
            for index, path in enumerate(entry['reference_paths']):
                uploads['references'].append(self.client.upload(factory.resolve(project, path), aid + f'-ref{index}' + Path(path).suffix.lower()))
            graph = comfy_workflow.compile_graph(project, entry, uploads, aid)
            schemas = self.client.request('/object_info')
            comfy_workflow.validate_graph(graph, schemas)
            comfy_workflow.validate_outputs(graph, schemas, entry['profile'])
            # Re-read source/graph/assets AFTER upload and schema reads, immediately before POST.
            preflight(project, entry)
            control = self.ledger.db.execute('SELECT stopped FROM local_controls WHERE endpoint=?', (self.key,)).fetchone()
            if control and control['stopped']:
                raise ValueError('Local service submissions were stopped during preparation')
            self.ledger.update(aid, graph=json.dumps(graph))
            factory.write(Path(project) / 'local_submissions' / (aid + '.json'),
                          {'attempt': aid, 'input_digest': entry['input_digest'], 'graph': graph,
                           'output_selection': entry['profile']['output']})
        except Exception:
            self.ledger.update(aid, state='PREPARATION_FAILED', issue='Preparation failed before POST; fix cause then resume without duplicating execution')
            self.ledger.stop(self.key)
            raise
        try:
            result = self.client.request('/prompt', {'prompt': graph, 'client_id': aid,
                                                    'extra_data': {'factory_attempt': aid}})
        except urllib.error.HTTPError as error:
            # Only a structured node-validation rejection proves no execution was accepted.
            try:
                body = json.loads(error.read())
            except Exception:
                body = {}
            if error.code == 400 and body.get('error') and not body.get('prompt_id'):
                self.ledger.update(aid, state='FAILED', receipt=json.dumps(body), issue='ComfyUI validation rejected submission')
                self.ledger.stop(self.key)
            else:
                self.ledger.update(aid, state='UNKNOWN', issue='Uncertain submission; reconcile before any new work')
            return
        except Exception:
            self.ledger.update(aid, state='UNKNOWN', issue='Uncertain submission; reconcile before any new work')
            return
        if not isinstance(result, dict):
            self.ledger.update(aid, state='UNKNOWN', issue='Malformed response; reconcile before new work')
        elif isinstance(result.get('prompt_id'), str) and result['prompt_id']:
            self.ledger.update(aid, prompt_id=result['prompt_id'], state='QUEUED', receipt=json.dumps(result),
                               issue='Partial output validation errors; inspect history' if result.get('node_errors') else None)
        elif result.get('error'):
            self.ledger.update(aid, state='FAILED', receipt=json.dumps(result), issue='ComfyUI rejected execution')
            self.ledger.stop(self.key)
        else:
            self.ledger.update(aid, state='UNKNOWN', receipt=json.dumps(result), issue='Missing prompt ID; reconcile before any new work')

    def recover_all(self):
        # Recover all endpoint projects before checking new authorization. Recovery is never a new render.
        for row in self.ledger.rows(self.key):
            if row['state'] in ACTIVE:
                try:
                    self.recover(row)
                except Exception as error:
                    self.ledger.update(row['id'], issue='Recovery pending: ' + type(error).__name__)
        return self.summary()

    def choose_output(self, aid, selection, notes):
        row = next((r for r in self.ledger.rows(self.key) if r['id'] == aid), None)
        if not row or row['state'] != 'SUCCEEDED' or not row['receipt'] or not isinstance(notes, str) or not notes.strip():
            raise ValueError('Select an output only for a known successful pending download, with observations')
        record, entry = json.loads(row['receipt']), json.loads(row['entry'])
        if (not match_attempt(record.get('prompt'), row) or record['prompt'][1] != row['prompt_id']
                or set(selection) - {'node_id', 'field', 'index'}
                or selection.get('node_id') != entry['profile']['output']['node_id']
                or selection.get('field') not in ('gifs', 'videos')):
            raise ValueError('Output recovery cannot change the original execution or selected saver')
        select_output(record, selection)
        override = {'output': selection, 'notes': notes.strip(), 'prompt_id': row['prompt_id'],
                    'receipt_digest': factory.digest(record)}
        self.ledger.update(aid, selection=json.dumps(override), issue=None)
        factory.write(Path(row['project']) / 'local_submissions' / (aid + '-selection-' + uuid.uuid4().hex + '.json'), override)
        return self.summary()

    def tick(self, project, queue):
        self.recover_all()
        if any(r['state'] in ACTIVE for r in self.ledger.rows(self.key)):
            return self.summary()
        local = [e for e in queue['jobs'] if not e.get('passthrough')]
        for entry in local:
            preflight(project, entry)
            if endpoint(entry['profile']['url']) != self.key:
                raise ValueError('A local tick must use one endpoint')
        service_queue = self.client.request('/queue')
        if queue_records(service_queue):
            return self.summary()
        previous = self.ledger.rows(self.key)
        for entry in local:
            if entry['profile'].get('quality_evidence') != 'case-verified' and not entry.get('pilot'):
                continue
            if any(r['project'] == str(Path(project).resolve()) and r['window'] == entry['window_id']
                   and r['digest'] == entry['input_digest'] and r['state'] != 'PREPARATION_FAILED' for r in previous):
                continue
            self.submit(project, entry)
            break  # Every loopback server is intentionally serial, independent of RH's account cap3.
        return self.summary()

    def summary(self):
        return [{k: r[k] for k in ('id', 'project', 'window', 'digest', 'state', 'prompt_id', 'output', 'issue')}
                for r in self.ledger.rows(self.key)]


def default_ledger():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'private/video-replacement-skill-factory/comfy.sqlite'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('tick', 'recover', 'select-output', 'status', 'stop', 'resume'))
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--ledger', type=Path, default=default_ledger())
    parser.add_argument('--url', help='Explicit endpoint for recovery/status if current profile was changed')
    parser.add_argument('--deps')
    parser.add_argument('--attempt')
    parser.add_argument('--selection', type=Path, help='JSON with output {node_id,field,index} and actual observations in notes')
    parser.add_argument('--scalar-cpu', action='store_true')
    args = parser.parse_args()
    project = args.project.resolve()
    if args.ledger.resolve().is_relative_to(project) or args.ledger.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Keep shared local ledger outside projects and skills')
    url = args.url or factory.read(project / 'job.json')['profile']['url']
    ledger = Ledger(args.ledger)
    try:
        runner = Runner(ledger, Client(url), args.deps, args.scalar_cpu)
        if args.command in ('stop', 'resume'):
            ledger.stop(runner.key, args.command == 'stop')
        if args.command == 'tick':
            result = runner.tick(project, factory.read(project / 'queue.json'))
        elif args.command == 'recover':
            result = runner.recover_all()
        elif args.command == 'select-output':
            choice = factory.read(args.selection)
            result = runner.choose_output(args.attempt, choice['output'], choice['notes'])
        else:
            result = runner.summary()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        ledger.close()


if __name__ == '__main__':
    main()

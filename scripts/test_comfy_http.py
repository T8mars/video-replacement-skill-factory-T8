"""Exercise the real loopback HTTP client against an isolated in-process service."""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import comfy_runner as cr

BINARY = b'\x00\x00\x00\x18ftypmp42\x00\xff\x80\xfe\x00\r\nnon-UTF8-video-payload'
GRAPH = {'1': {'class_type': 'TestSave', 'inputs': {'prefix': 'test'}}}
SCHEMAS = {'TestSave': {'input': {'required': {'prefix': ['STRING']}}, 'output': [], 'output_node': True}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        payload = json.dumps(value).encode('utf8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path)
        self.server.seen.append(('GET', self.path))
        if route.path == '/object_info':
            self.reply(SCHEMAS)
        elif route.path == '/queue':
            self.reply(self.server.queue)
        elif route.path == '/history':
            self.reply(self.server.history)
        elif route.path.startswith('/history/'):
            pid = urllib.parse.unquote(route.path.rsplit('/', 1)[1])
            self.reply({pid: self.server.history[pid]} if pid in self.server.history else {})
        elif route.path == '/view':
            self.server.view_query = urllib.parse.parse_qs(route.query)
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(BINARY)))
            self.end_headers()
            self.wfile.write(BINARY)
        elif route.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'http://external.invalid/never-access')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.reply({'error': 'unhandled'}, 404)

    def do_POST(self):
        self.server.seen.append(('POST', self.path))
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        if self.path == '/upload/image':
            self.server.upload_headers = dict(self.headers)
            self.server.upload_raw = body
            envelope = ('Content-Type: ' + self.headers['Content-Type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body
            message = BytesParser(policy=policy.default).parsebytes(envelope)
            fields = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
            self.server.upload_fields = fields
            self.reply(self.server.upload_response)
        elif self.path == '/prompt':
            data = json.loads(body)
            self.server.prompt_bodies.append(data)
            if self.server.mode == 'reject':
                self.reply({'error': {'type': 'prompt_outputs_failed_validation'}, 'node_errors': {'1': 'bad'}}, 400)
                return
            if self.server.mode == 'server_error':
                self.reply({'error': 'internal'}, 503)
                return
            pid = 'known-1'
            extra = {**data.get('extra_data', {}), 'client_id': data.get('client_id')}
            self.server.queue['queue_running'] = [[0, pid, data['prompt'], extra, ['1']]]
            if self.server.mode == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.reply({'prompt_id': pid, 'node_errors': {}})
        else:
            self.reply({'error': 'unhandled'}, 404)


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='comfy-http-')
        self.root = Path(self.temp.name)
        self.ledgers = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.seen, self.server.prompt_bodies = [], []
        self.server.queue = {'queue_running': [], 'queue_pending': []}
        self.server.history, self.server.mode = {}, 'success'
        self.server.upload_response = {'name': 'server-renamed (1).mp4', 'subfolder': 'factory-upload', 'type': 'input'}
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = cr.Client(self.url)
        self.file = self.root / '视频 with space.mp4'
        self.file.write_bytes(BINARY)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for ledger in self.ledgers:
            ledger.close()
        self.temp.cleanup()

    def test_get_actual_object_info_queue_and_history_json(self):
        self.assertEqual(self.client.request('/object_info'), SCHEMAS)
        self.assertEqual(self.client.request('/queue'), self.server.queue)
        self.assertEqual(self.client.request('/history'), {})
        self.assertEqual(self.client.request('/history/absent'), {})
        self.assertTrue(all(method == 'GET' for method, route in self.server.seen))

    def test_multipart_stream_preserves_non_utf8_and_exact_content_length(self):
        self.client.upload(self.file, 'safe-video.mp4')
        fields = self.server.upload_fields
        self.assertEqual(fields['image'].get_payload(decode=True), BINARY)
        self.assertEqual(fields['image'].get_filename(), 'safe-video.mp4')
        self.assertEqual(fields['type'].get_content().strip(), 'input')
        self.assertEqual(fields['overwrite'].get_content().strip(), 'false')
        self.assertEqual(int(self.server.upload_headers['Content-Length']), len(self.server.upload_raw))
        self.assertNotIn('Transfer-Encoding', self.server.upload_headers)

    def test_upload_uses_server_actual_renamed_path(self):
        self.assertEqual(self.client.upload(self.file, 'requested.mp4'), 'factory-upload/server-renamed (1).mp4')

    def test_bad_upload_descriptors_do_not_reach_prompt(self):
        for descriptor in ({'name': '../escape.mp4', 'type': 'input'},
                           {'name': 'ok.mp4', 'subfolder': '../escape', 'type': 'input'},
                           {'name': 'ok.mp4', 'type': 'output'}):
            with self.subTest(descriptor=descriptor):
                self.server.upload_response = descriptor
                with self.assertRaises(ValueError):
                    self.client.upload(self.file, 'safe.mp4')
        self.assertEqual(self.server.prompt_bodies, [])

    def test_view_downloads_exact_bytes_and_encodes_descriptor(self):
        output = self.root / 'download.mp4'
        self.client.download({'filename': 'a b+中.mp4', 'subfolder': 'some folder', 'type': 'output'}, output)
        self.assertEqual(output.read_bytes(), BINARY)
        self.assertEqual(self.server.view_query['filename'], ['a b+中.mp4'])
        self.assertEqual(self.server.view_query['subfolder'], ['some folder'])

    def test_invalid_download_descriptor_is_rejected_before_http(self):
        before = len(self.server.seen)
        with self.assertRaises(ValueError):
            self.client.download({'filename': 'video.mp4', 'subfolder': '../other'}, self.root / 'bad.mp4')
        self.assertEqual(len(self.server.seen), before)
        self.assertFalse((self.root / 'bad.mp4').exists())

    def test_windows_subfolder_download_is_canonical_and_exact(self):
        output = self.root / 'windows-download.mp4'
        self.client.download({'filename': 'clip.mp4', 'subfolder': 'factory\\attempt',
                              'type': 'output', 'fullpath': 'ignored'}, output)
        self.assertEqual(output.read_bytes(), BINARY)
        self.assertEqual(self.server.view_query['subfolder'], ['factory/attempt'])
        self.assertNotIn('fullpath', self.server.view_query)

    def test_redirect_is_rejected_without_opening_target(self):
        # The reserved .invalid domain must never be contacted by this client.
        with self.assertRaisesRegex(ValueError, 'redirect'):
            self.client.request('/redirect')
        self.assertEqual(self.server.seen, [('GET', '/redirect')])

    def test_prompt_serialization_and_known_id_queue_echo(self):
        body = {'prompt': GRAPH, 'client_id': 'attempt-id', 'extra_data': {'factory_attempt': 'attempt-id'}}
        self.assertEqual(self.client.request('/prompt', body)['prompt_id'], 'known-1')
        self.assertEqual(self.server.prompt_bodies, [body])
        queued = self.client.request('/queue')['queue_running'][0]
        self.assertEqual(queued[2], GRAPH)
        self.assertEqual(queued[3]['client_id'], 'attempt-id')

    def run_submission(self, mode):
        self.server.mode = mode
        project = self.root / 'project'
        project.mkdir()
        (project / 'video.mp4').write_bytes(BINARY)
        entry = {'window_id': 'w1', 'input_digest': 'reviewed-digest', 'video_path': 'video.mp4',
                 'reference_paths': [], 'profile': {'kind': 'local-comfyui', 'url': self.url,
                                                   'output': {'node_id': '1', 'field': 'gifs'}}}
        ledger = cr.Ledger(self.root / 'private.sqlite')
        self.ledgers.append(ledger)
        runner = cr.Runner(ledger, self.client)
        with patch.object(cr, 'preflight', return_value='reviewed-digest'), \
                patch.object(cr.comfy_workflow, 'compile_graph', return_value=GRAPH), \
                patch.object(cr.comfy_workflow, 'validate_graph'), \
                patch.object(cr.comfy_workflow, 'validate_outputs'):
            runner.submit(project, entry)
        return runner, ledger.rows(self.client.endpoint)[0]

    def test_structured_http400_is_definite_rejection(self):
        runner, row = self.run_submission('reject')
        self.assertEqual(row['state'], 'FAILED')
        self.assertIsNone(row['prompt_id'])
        self.assertEqual(len(self.server.prompt_bodies), 1)
        self.assertEqual(self.server.queue['queue_running'], [])

    def test_http503_retains_unknown_submission_barrier(self):
        runner, row = self.run_submission('server_error')
        self.assertEqual(row['state'], 'UNKNOWN')
        self.assertIsNone(row['prompt_id'])
        self.assertEqual(len(self.server.prompt_bodies), 1)

    def test_accepted_connection_drop_reconciles_without_second_post(self):
        runner, row = self.run_submission('disconnect')
        self.assertEqual(row['state'], 'UNKNOWN')
        runner.reconcile(row)
        updated = runner.ledger.rows(self.client.endpoint)[0]
        self.assertEqual(updated['state'], 'QUEUED')
        self.assertEqual(updated['prompt_id'], 'known-1')
        self.assertEqual(len(self.server.prompt_bodies), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Local ComfyUI regressions: simulated HTTP/service and real tiny decoded video."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import factory
import media
import test_factory
import comfy_runner as cr


def workflow_fixture(project):
    graph = {'1': {'class_type': 'FixtureGenerator', 'inputs': {
        'text': 'original', 'video': 'old.mp4', 'image': 'old.png', 'frames': 8, 'fps': 12.0}},
        '2': {'class_type': 'FixtureSaver', 'inputs': {'video': ['1', 0], 'filename_prefix': 'original'}}}
    path = project / 'local' / 'workflow.api.json'
    factory.write(path, graph)
    return {'kind': 'local-comfyui', 'url': 'http://127.0.0.1:8188',
            'workflow_path': 'local/workflow.api.json', 'workflow_sha256': media.signature(path)['sha256'],
            'bindings': [{'source': source, 'node_id': node, 'input': key, **extra}
                         for source, node, key, extra in [('prompt', '1', 'text', {}), ('video', '1', 'video', {}),
                             ('reference', '1', 'image', {'index': 0}), ('frames', '1', 'frames', {}),
                             ('fps', '1', 'fps', {}), ('output_prefix', '2', 'filename_prefix', {})]],
            'output': {'node_id': '2', 'field': 'gifs'},
            'limits': {'fps': ['12'], 'frame_counts': [8, 16], 'identity_slots': 1}}


SCHEMAS = {
    'FixtureGenerator': {'input': {'required': {'text': ['STRING', {}], 'video': ['STRING', {}],
        'image': ['STRING', {}], 'frames': ['INT', {'min': 1}], 'fps': ['FLOAT', {'min': 1}]}}, 'output': ['VIDEO']},
    'FixtureSaver': {'input': {'required': {'video': ['VIDEO', {}], 'filename_prefix': ['STRING', {}]}},
                     'output': [], 'output_node': True}}


class Fake:
    endpoint = cr.endpoint('http://localhost:8188')
    def __init__(self, video):
        self.video, self.posts, self.uploads, self.downloads = video, [], [], 0
        self.queue = {'queue_running': [], 'queue_pending': []}
        self.history, self.schemas = {}, copy.deepcopy(SCHEMAS)
        self.timeout = self.fail_download = self.partial_errors = False

    def upload(self, path, name):
        self.uploads.append((path, name))
        return 'server-returned/' + name

    def request(self, route, body=None):
        if route == '/queue':
            return copy.deepcopy(self.queue)
        if route == '/object_info':
            return copy.deepcopy(self.schemas)
        if route == '/history':
            return copy.deepcopy(self.history)
        if route.startswith('/history/'):
            pid = route.split('/')[-1]
            return {pid: copy.deepcopy(self.history[pid])} if pid in self.history else {}
        if route == '/prompt':
            self.posts.append(body)
            pid = 'prompt-' + str(len(self.posts))
            extra = {**body['extra_data'], 'client_id': body['client_id']}
            self.queue['queue_running'] = [[0, pid, body['prompt'], extra, ['2']]]
            if self.timeout:
                raise TimeoutError('Service accepted; response lost')
            return {'prompt_id': pid, 'node_errors': {'other': 'rejected extra output'} if self.partial_errors else {}}
        raise AssertionError(route)

    def complete(self, pid='prompt-1', outputs=None):
        record = next(r for r in self.queue['queue_running'] + self.queue['queue_pending'] if r[1] == pid)
        self.history[pid] = {'prompt': record, 'status': {'completed': True, 'status_str': 'success', 'messages': []},
            'outputs': outputs or {'2': {'gifs': [{'filename': 'video.mp4', 'subfolder': 'test', 'type': 'output'}]}}}
        self.queue = {'queue_running': [], 'queue_pending': []}

    def download(self, item, destination):
        self.downloads += 1
        if self.fail_download:
            Path(destination).write_bytes(b'broken')
            raise TimeoutError('download interrupted')
        shutil.copy2(self.video, destination)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        test_factory.FactoryIntegration.setUp(self)
        self.job['profile'] = workflow_fixture(self.project)
        self.job['production_authorized'] = True
        factory.write(self.project / 'job.json', self.job)
        self.queue = factory.prepare(self.project, test_factory.DEPS, test_factory.SCALAR, True, True)
        self.ledger = cr.Ledger(self.temp / 'private' / 'comfy.sqlite')
        self.client = Fake(self.video)
        self.runner = cr.Runner(self.ledger, self.client, test_factory.DEPS, test_factory.SCALAR)

    def tearDown(self):
        self.ledger.close()

    def tick(self):
        return self.runner.tick(self.project, self.queue)

    def test_submit_download_and_real_clock_review_stays_pending(self):
        self.tick()
        self.assertEqual(len(self.client.posts), 1)
        configured = self.client.posts[0]['prompt']['1']['inputs']
        self.assertEqual(configured['frames'], 8)
        self.assertTrue(configured['video'].startswith('server-returned/'))
        self.client.complete()
        result = self.tick()[0]
        self.assertEqual(result['state'], 'DOWNLOADED')
        self.tick()
        self.assertEqual(len(self.client.posts), 1)
        record = factory.inspect_candidate(self.project, result['window'], result['prompt_id'],
                                            Path(result['output']).relative_to(self.project).as_posix(),
                                            test_factory.DEPS, test_factory.SCALAR)
        self.assertEqual(record['visual']['status'], 'pending')
        self.assertEqual(record['output_clock_review']['status'], 'pending')

    def test_revoked_authorization_stops_new_submission(self):
        self.job['production_authorized'] = False
        factory.write(self.project / 'job.json', self.job)
        with self.assertRaisesRegex(ValueError, 'authorization'):
            self.tick()
        self.assertEqual(self.client.uploads, [])

    def test_stale_semantic_review_does_not_upload(self):
        self.queue['jobs'][0]['semantic_review']['status'] = 'pending'
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.client.uploads, [])

    def test_workflow_mutation_invalidates_approval(self):
        path = self.project / 'local/workflow.api.json'
        graph = factory.read(path)
        graph['1']['inputs']['text'] = 'changed'
        factory.write(path, graph)
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.client.posts, [])

    def test_quality_only_window_promotion_preserves_approved_generation_digest(self):
        plan = factory.read(self.project / 'plan.json')
        plan['windows'][0]['profile_override'] = copy.deepcopy(self.job['profile'])
        factory.write(self.project / 'plan.json', plan)
        # Introducing an override is a real contract edit; archive the initial fixture input first.
        (self.project / 'derived_inputs').rename(self.project / 'initial-derived-archive')
        queue = factory.prepare(self.project, test_factory.DEPS, test_factory.SCALAR, True, True)
        entry = queue['jobs'][0]
        plan['windows'][0]['profile_override']['quality_evidence'] = 'case-verified'
        plan['windows'][0]['profile_override']['evidence'] = {'kind': 'synthetic-test-only'}
        factory.write(self.project / 'plan.json', plan)
        self.assertEqual(cr.preflight(self.project, entry), entry['input_digest'])

    def test_external_queue_waits_without_upload(self):
        self.client.queue['queue_running'] = [[0, 'someone-else', {}, {}, []]]
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.client.uploads, [])

    def test_incomplete_service_queue_never_submits(self):
        self.client.queue = {}
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.client.posts, [])

    def test_known_history_must_bind_original_graph_and_attempt(self):
        self.tick()
        self.client.complete()
        self.client.history['prompt-1']['prompt'][3]['client_id'] = 'another-client'
        self.assertEqual(self.tick()[0]['state'], 'RECOVERY_REQUIRED')
        self.assertEqual(self.client.downloads, 0)
        self.assertEqual(len(self.client.posts), 1)

    def test_upload_failure_is_retryable_only_before_post_after_resume(self):
        with patch.object(self.client, 'upload', side_effect=TimeoutError('pre-POST failure')):
            with self.assertRaises(TimeoutError):
                self.tick()
        self.assertEqual(self.runner.summary()[0]['state'], 'PREPARATION_FAILED')
        self.assertEqual(self.client.posts, [])
        self.tick()
        self.assertEqual(self.client.posts, [])
        self.ledger.stop(self.runner.key, False)
        self.tick()
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(len(self.runner.summary()), 2)

    def test_stop_during_upload_prevents_post(self):
        upload = self.client.upload
        def upload_then_stop(path, name):
            result = upload(path, name)
            self.ledger.stop(self.runner.key)
            return result
        with patch.object(self.client, 'upload', upload_then_stop), self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.runner.summary()[0]['state'], 'PREPARATION_FAILED')

    def test_resume_does_not_repost_execution_failure(self):
        self.tick()
        self.client.complete()
        self.client.history['prompt-1']['status']['status_str'] = 'error'
        self.assertEqual(self.tick()[0]['state'], 'FAILED')
        self.ledger.stop(self.runner.key, False)
        self.tick()
        self.assertEqual(len(self.client.posts), 1)

    def test_timeout_reconciles_exact_graph_without_reposting(self):
        self.client.timeout = True
        self.assertEqual(self.tick()[0]['state'], 'UNKNOWN')
        self.assertEqual(self.tick()[0]['state'], 'QUEUED')
        self.client.complete()
        self.assertEqual(self.tick()[0]['state'], 'DOWNLOADED')
        self.assertEqual(len(self.client.posts), 1)

    def test_unknown_missing_history_stays_blocked(self):
        self.client.timeout = True
        self.tick()
        self.client.queue['queue_running'] = []
        self.assertEqual(self.tick()[0]['state'], 'UNKNOWN')
        self.assertEqual(self.tick()[0]['state'], 'UNKNOWN')
        self.assertEqual(len(self.client.posts), 1)

    def test_unknown_duplicate_matches_never_claims_arbitrary_id(self):
        self.client.timeout = True
        self.tick()
        second = copy.deepcopy(self.client.queue['queue_running'][0])
        second[1] = 'duplicate'
        self.client.queue['queue_pending'].append(second)
        self.assertEqual(self.tick()[0]['state'], 'UNKNOWN')

    def test_unknown_mismatching_graph_is_not_claimed(self):
        self.client.timeout = True
        self.tick()
        self.client.queue['queue_running'][0][2]['1']['inputs']['text'] = 'unrelated'
        self.assertEqual(self.tick()[0]['state'], 'UNKNOWN')

    def test_float_input_validation_recovers_without_reposting(self):
        self.client.timeout = True
        self.tick()
        graph = self.client.queue['queue_running'][0][2]
        graph['1']['inputs']['frames'] = 8.0
        self.assertEqual(self.tick()[0]['state'], 'QUEUED')
        self.client.complete()
        self.assertEqual(self.tick()[0]['state'], 'DOWNLOADED')
        self.assertEqual(len(self.client.posts), 1)

    def test_graph_numeric_comparison_preserves_values_and_boolean_types(self):
        self.assertTrue(cr.same_graph_value({'fps': 24}, {'fps': 24.0}))
        for changed in (23.999999, '24', True, None):
            self.assertFalse(cr.same_graph_value({'fps': 24}, {'fps': changed}))
        self.assertFalse(cr.same_graph_value({'enabled': 1}, {'enabled': True}))
        self.assertFalse(cr.same_graph_value([1, 2], [1]))
        self.assertFalse(cr.same_graph_value({'fps': 24}, {'fps': 24.0, 'extra': 0}))
        self.assertFalse(cr.same_graph_value(9007199254740993, 9007199254740992.0))

    def test_partial_node_errors_persist_accepted_id(self):
        self.client.partial_errors = True
        row = self.tick()[0]
        self.assertEqual(row['prompt_id'], 'prompt-1')
        self.assertEqual(row['state'], 'QUEUED')

    def test_missing_known_id_is_recovery_required_not_retried(self):
        self.tick()
        self.client.queue['queue_running'] = []
        self.assertEqual(self.tick()[0]['state'], 'RECOVERY_REQUIRED')
        self.assertEqual(len(self.client.posts), 1)

    def test_download_failure_recovers_id_and_cleans_partial(self):
        self.tick()
        self.client.complete()
        self.client.fail_download = True
        self.assertEqual(self.tick()[0]['state'], 'SUCCEEDED')
        self.assertFalse(list((self.project / 'candidates').rglob('*.part.mp4')))
        self.client.fail_download = False
        self.assertEqual(self.tick()[0]['state'], 'DOWNLOADED')
        self.assertEqual(len(self.client.posts), 1)

    def test_durable_success_receipt_recovers_download_after_history_clear(self):
        self.tick()
        self.client.complete()
        self.client.fail_download = True
        self.assertEqual(self.tick()[0]['state'], 'SUCCEEDED')
        self.client.history = {}
        self.client.fail_download = False
        self.assertEqual(self.runner.recover_all()[0]['state'], 'DOWNLOADED')
        self.assertEqual(len(self.client.posts), 1)

    def test_ambiguous_output_can_be_selected_without_new_render(self):
        self.tick()
        self.client.complete(outputs={'2': {'gifs': [
            {'filename': 'draft.mp4', 'type': 'output'}, {'filename': 'final.mp4', 'type': 'output'}]}})
        row = self.tick()[0]
        self.assertEqual(row['state'], 'SUCCEEDED')
        self.runner.choose_output(row['id'], {'node_id': '2', 'field': 'gifs', 'index': 1}, 'Synthetic final saver index review.')
        self.assertEqual(self.runner.recover_all()[0]['state'], 'DOWNLOADED')
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(len(self.client.uploads), 2)

    def test_output_selection_cannot_change_saver_or_hide_invalid_index(self):
        self.tick()
        self.client.complete(outputs={'2': {'gifs': [
            {'filename': 'draft.mp4', 'type': 'output'}, {'filename': 'final.mp4', 'type': 'output'}]}})
        row = self.tick()[0]
        for selection in ({'node_id': '1', 'field': 'gifs', 'index': 0},
                          {'node_id': '2', 'field': 'gifs', 'index': 9}):
            with self.assertRaises(ValueError):
                self.runner.choose_output(row['id'], selection, 'Observed test.')
        self.assertEqual(self.runner.summary()[0]['state'], 'SUCCEEDED')

    def test_selection_after_promotion_crash_keeps_old_candidate_and_downloads_new(self):
        self.tick()
        self.client.complete(outputs={'2': {'gifs': [
            {'filename': 'draft.mp4', 'type': 'output'}, {'filename': 'final.mp4', 'type': 'output'}]}})
        # Explicit first output makes one unambiguous original request selector.
        row = self.ledger.rows(self.runner.key)[0]
        entry = json.loads(row['entry'])
        entry['profile']['output']['index'] = 0
        self.ledger.db.execute('UPDATE local_attempts SET entry=? WHERE id=?', (json.dumps(entry), row['id']))
        update = self.ledger.update
        def crash_after_file(aid, **changes):
            if changes.get('state') == 'DOWNLOADED':
                raise RuntimeError('Simulated crash after atomic promotion')
            update(aid, **changes)
        with patch.object(self.ledger, 'update', crash_after_file):
            self.runner.recover_all()
        old = self.project / 'candidates' / row['window'] / row['id'] / 'output.mp4'
        self.assertTrue(old.exists())
        self.runner.choose_output(row['id'], {'node_id': '2', 'field': 'gifs', 'index': 1}, 'Final output selected after recovery.')
        latest = self.runner.recover_all()[0]
        self.assertEqual(latest['state'], 'DOWNLOADED')
        self.assertNotEqual(Path(latest['output']), old)
        self.assertTrue(old.exists())
        self.assertEqual(self.client.downloads, 2)

    def test_bad_video_does_not_become_downloaded(self):
        self.tick()
        self.client.complete()
        with patch.object(self.client, 'download', lambda item, dest: Path(dest).write_bytes(b'bad mp4')):
            self.assertEqual(self.tick()[0]['state'], 'SUCCEEDED')
        self.assertFalse(list((self.project / 'candidates').rglob('output.mp4')))

    def test_stop_still_recovers_existing_execution(self):
        self.tick()
        self.ledger.stop(self.runner.key)
        self.job['production_authorized'] = False
        factory.write(self.project / 'job.json', self.job)
        self.client.complete()
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.runner.summary()[0]['state'], 'DOWNLOADED')
        self.assertEqual(len(self.client.posts), 1)

    def test_schema_changed_before_submit_rejects_without_post(self):
        self.client.schemas['FixtureGenerator']['input']['required']['frames'] = ['INT', {'min': 99}]
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.client.posts, [])

    def test_two_ledger_connections_share_one_slot(self):
        other = cr.Ledger(self.ledger.path)
        try:
            entry = self.queue['jobs'][0]
            first = self.ledger.reserve(self.runner.key, self.project, entry)
            second = other.reserve(cr.endpoint('http://[::1]:8188'), self.project.parent / 'another', entry)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
        finally:
            other.close()

    def test_local_short_shot_uses_declared_lattice_without_five_second_padding(self):
        self.assertEqual(self.queue['jobs'][0]['generation_frames'], 8)
        self.assertEqual(self.queue['jobs'][0]['duration'], '2/3')

    def test_local_context_cannot_cross_reviewed_cut(self):
        self.analysis['shots'] = [
            {'id': 's1', 'start': 0, 'end': 4, 'roles': ['A'], 'description_en': '{{A}} moves.'},
            {'id': 's2', 'start': 4, 'end': 8, 'roles': ['A'], 'description_en': '{{A}} turns.'}]
        factory.write(self.project / 'analysis.json', self.analysis)
        plan = factory.read(self.project / 'plan.json')
        plan['analysis_digest'] = factory.digest(self.analysis)
        factory.write(self.project / 'plan.json', plan)
        with self.assertRaisesRegex(ValueError, 'one reviewed shot'):
            factory.prepare(self.project, test_factory.DEPS, test_factory.SCALAR)

    def test_local_frame_duration_mismatch_is_rejected(self):
        plan = factory.read(self.project / 'plan.json')
        plan['windows'][0]['duration'] = '1'
        factory.write(self.project / 'plan.json', plan)
        with self.assertRaisesRegex(ValueError, 'frame clock'):
            factory.prepare(self.project, test_factory.DEPS, test_factory.SCALAR)

    def test_matching_local_reference_count_required_before_any_upload(self):
        self.job['profile']['limits']['identity_slots'] = 2
        graph = factory.read(self.project / 'local/workflow.api.json')
        graph['1']['inputs']['image2'] = 'old2.png'
        factory.write(self.project / 'local/workflow.api.json', graph)
        self.job['profile']['workflow_sha256'] = media.signature(self.project / 'local/workflow.api.json')['sha256']
        self.job['profile']['bindings'].append({'source': 'reference', 'index': 1, 'node_id': '1', 'input': 'image2'})
        factory.write(self.project / 'job.json', self.job)
        with self.assertRaisesRegex(ValueError, 'reference count'):
            factory.prepare(self.project, test_factory.DEPS, test_factory.SCALAR)
        self.assertEqual(self.client.uploads, [])

    def test_long_passthrough_is_not_limited_by_generation_cap(self):
        # A 20-second source with no replacements consumes zero local/cloud slots.
        long_video = test_factory.video_fixture(self.temp / 'long.mp4', fps=12, pts=list(range(241)))
        project = self.temp / 'long-run'
        factory.ingest(project, long_video, test_factory.DEPS, test_factory.SCALAR)
        analysis = factory.read(project / 'analysis.json')
        analysis['status'] = 'passed'
        analysis['shots'][0]['description_en'] = 'An unchanged empty scene.'
        factory.write(project / 'analysis.json', analysis)
        queue = factory.prepare(project, test_factory.DEPS, test_factory.SCALAR)
        self.assertTrue(queue['jobs'][0]['passthrough'])
        self.assertEqual(len(queue['jobs']), 1)

    def test_dedicated_zip_contains_adapter_not_current_private_workflow(self):
        plan = factory.read(self.project / 'plan.json')
        plan['windows'][0]['profile_override'] = copy.deepcopy(self.job['profile'])
        factory.write(self.project / 'plan.json', plan)
        result = factory.build_skill(self.project, self.temp / 'skills', display_name='合成测试人物动作')
        import zipfile
        with zipfile.ZipFile(result['zip']['path']) as archive:
            names = archive.namelist()
            self.assertTrue(any(n.endswith('/scripts/comfy_runner.py') for n in names))
            self.assertFalse(any(n.endswith('/workflow.api.json') or 'local_submissions/' in n for n in names))
            neutral = json.loads(archive.read(result['name'] + '/assets/plan.json'))
            self.assertNotIn('profile_override', neutral['windows'][0])
        target = self.temp / 'new-run'
        factory.instantiate(result['path'], target)
        self.assertEqual(factory.read(target / 'job.json')['profile'], {})
        self.assertNotIn('profile_override', factory.read(target / 'plan.json')['windows'][0])

    def test_instantiating_legacy_template_removes_local_machine_override(self):
        result = factory.build_skill(self.project, self.temp / 'legacy-skills', display_name='旧模板迁移测试')
        plan_path = Path(result['path']) / 'assets/plan.json'
        plan = factory.read(plan_path)
        plan['windows'][0]['profile_override'] = copy.deepcopy(self.job['profile'])
        factory.write(plan_path, plan)
        target = self.temp / 'legacy-new-run'
        factory.instantiate(result['path'], target)
        self.assertNotIn('profile_override', factory.read(target / 'plan.json')['windows'][0])


class ContractTests(unittest.TestCase):
    def test_endpoint_aliases_and_restricted_urls(self):
        self.assertEqual(cr.endpoint('http://localhost:8188/'), cr.endpoint('http://[::1]:8188'))
        # Build the dummy credential-bearing URL without a publishable credential-shaped literal.
        for url in ('https://localhost:8188', 'http://elsewhere:8188', 'http://' + 'a:b@localhost',
                    'http://localhost/path', 'http://localhost?x=1', 'http://localhost#x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                cr.endpoint(url)

    def test_output_selection_is_explicit_and_strict(self):
        history = {'status': {'completed': True, 'status_str': 'success'},
                   'outputs': {'10': {'gifs': [{'filename': 'movie.mp4', 'type': 'output'}]}}}
        for index in (-1, True, 2):
            with self.subTest(index=index), self.assertRaises(ValueError):
                cr.select_output(history, {'node_id': '10', 'field': 'gifs', 'index': index})
        result = cr.select_output(history, {'node_id': '10', 'field': 'gifs'})
        self.assertEqual(result['filename'], 'movie.mp4')
        history['outputs']['10']['gifs'].append({'filename': 'another.mp4', 'type': 'output'})
        with self.assertRaises(ValueError):
            cr.select_output(history, {'node_id': '10', 'field': 'gifs'})

    def test_malicious_file_descriptors_rejected(self):
        for item in ({'filename': '../a.mp4'}, {'filename': 'a\\b.mp4'},
                     {'filename': 'a.mp4', 'subfolder': '../x'}, {'filename': 'a.mp4', 'type': 'input'}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                cr.file_item(item)

    def test_frame_grid_is_declared_not_assumed(self):
        p = {'limits': {'fps': ['24'], 'identity_slots': 1, 'frame_grid': {'step': 17, 'offset': 5, 'minimum': 22}}}
        self.assertEqual(factory.local_frame_count(p, 24, 24), 39)
        with self.assertRaises(ValueError):
            factory.local_frame_count(p, 239, 24)
        for grid in ({'step': True, 'offset': 5, 'minimum': 22}, {'step': 17, 'offset': 17, 'minimum': 22}):
            with self.assertRaises(ValueError):
                factory.local_frame_count({'limits': {**p['limits'], 'frame_grid': grid}}, 24, 24)
        for fps in ([True], ['24', 0], ['24', '-1'], ['24', 'nan']):
            with self.assertRaises(ValueError):
                factory.local_frame_count({'limits': {**p['limits'], 'fps': fps}}, 24, 24)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path)
    parser.add_argument('--deps')
    parser.add_argument('--scalar-cpu', action='store_true')
    args, rest = parser.parse_known_args()
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
    test_factory.WORK, test_factory.DEPS, test_factory.SCALAR = args.work, args.deps, args.scalar_cpu
    media.runtime(args.deps, args.scalar_cpu)
    unittest.main(argv=[__file__, *rest], verbosity=2)

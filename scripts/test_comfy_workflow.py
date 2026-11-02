"""Offline API graph contracts; no ComfyUI installation or GPU is used."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import comfy_workflow as cw


def fixture():
    graph = {
        'load': {'class_type': 'UserModel', 'inputs': {'model': 'user-original', 'lora': 'user-lora'}},
        'conditioning': {'class_type': 'UserH3', 'inputs': {'model': ['load', 0], 'caption': '',
            'video_file': '', 'portrait_a': '', 'portrait_b': '', 'length': 25, 'rate': 25,
            'seconds': 1.0, 'width': 64, 'height': 48, 'ratio': '4:3', 'seed': 0}},
        'save': {'class_type': 'UserVideoOutput', 'inputs': {'video': ['conditioning', 0], 'prefix': 'original'}},
        'unused': {'class_type': 'UserModel', 'inputs': {'model': 'unused', 'lora': 'unused'}}}
    schemas = {
        'UserModel': {'input': {'required': {'model': ['STRING'], 'lora': ['STRING']}}, 'output': ['MODEL']},
        'UserH3': {'input': {'required': {'model': ['MODEL'], 'caption': ['STRING'], 'video_file': ['STRING'],
            'portrait_a': ['STRING'], 'portrait_b': ['STRING'], 'length': ['INT', {'min': 1, 'max': 250}],
            'rate': ['FLOAT', {'min': 1, 'max': 120}], 'seconds': ['FLOAT'], 'width': ['INT'],
            'height': ['INT'], 'ratio': ['STRING'], 'seed': ['INT']}}, 'output': ['VIDEO']},
        'UserVideoOutput': {'input': {'required': {'video': ['VIDEO'], 'prefix': ['STRING']}},
                            'output': [], 'output_node': True}}
    bindings = [{'source': s, 'node_id': 'conditioning', 'input': name}
                for s, name in [('prompt', 'caption'), ('video', 'video_file'), ('frames', 'length'),
                    ('fps', 'rate'), ('seconds', 'seconds'), ('width', 'width'), ('height', 'height'),
                    ('aspect_ratio', 'ratio'), ('seed', 'seed')]]
    bindings += [{'source': 'reference', 'index': i, 'node_id': 'conditioning', 'input': name}
                 for i, name in enumerate(('portrait_a', 'portrait_b'))]
    bindings += [{'source': 'output_prefix', 'node_id': 'save', 'input': 'prefix'}]
    profile = {'kind': 'local-comfyui', 'url': 'http://127.0.0.1:8188', 'workflow_path': 'assets/workflow.json',
               'bindings': bindings, 'parameters': [], 'output': {'node_id': 'save', 'field': 'gifs'},
               'limits': {'identity_slots': 2}}
    return graph, schemas, profile


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='comfy-workflow-')
        self.root = Path(self.temp.name)
        (self.root / 'assets').mkdir()
        self.graph, self.schemas, self.profile = fixture()
        self.persist()
        (self.root / 'prompt.txt').write_text('Source-bound complete prompt\n', encoding='utf8')
        self.entry = {'profile': self.profile, 'prompt_path': 'prompt.txt', 'reference_paths': ['a.png', 'b.png'],
                      'window_id': 'w01', 'source_contract': {'window': {'generation_frames': 50,
                        'generation_fps': '25', 'source_projection': {'display_canvas': {
                            'width': 64, 'height': 48, 'sample_aspect_ratio': '1'}}}}}
        self.uploads = {'video': 'attempt/video.mp4', 'references': ['attempt/a.png', 'attempt/b.png']}

    def tearDown(self):
        self.temp.cleanup()

    def persist(self):
        path = self.root / 'assets/workflow.json'
        path.write_text(json.dumps(self.graph), encoding='utf8')
        self.profile['workflow_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()

    def compile(self):
        return cw.compile_graph(self.root, self.entry, self.uploads, 'attempt-123')

    def test_compile_binds_runtime_clock_order_and_preserves_user_models(self):
        before = copy.deepcopy(self.graph)
        actual = self.compile()
        cw.validate_graph(actual, self.schemas)
        cw.validate_outputs(actual, self.schemas, self.profile)
        self.assertEqual(actual['load'], before['load'])
        self.assertNotIn('unused', actual)
        inputs = actual['conditioning']['inputs']
        self.assertEqual((inputs['length'], inputs['rate'], inputs['seconds']), (50, 25, 2.0))
        self.assertEqual((inputs['portrait_a'], inputs['portrait_b']), tuple(self.uploads['references']))
        self.assertEqual(inputs['caption'], 'Source-bound complete prompt\n')
        self.assertEqual(actual['save']['inputs']['prefix'], 'factory/attempt-123/w01')
        self.assertEqual(self.graph, before)
        self.assertEqual(actual, self.compile())

    def test_noninteger_fps_and_display_aspect_ratio_are_explicit(self):
        window = self.entry['source_contract']['window']
        window['generation_fps'] = '24000/1001'
        window['source_projection']['display_canvas']['sample_aspect_ratio'] = '4/3'
        actual = self.compile()['conditioning']['inputs']
        self.assertAlmostEqual(actual['rate'], 24000/1001)
        self.assertAlmostEqual(actual['seconds'], 50*1001/24000)
        self.assertEqual(actual['ratio'], '16:9')

    def test_workflow_hash_change_requires_new_review(self):
        (self.root / 'assets/workflow.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            self.compile()

    def test_ui_workflow_is_rejected_with_api_export_instruction(self):
        self.graph = {'nodes': [{'id': 1, 'widgets_values': ['unknown']}]}
        self.persist()
        with self.assertRaisesRegex(ValueError, 'API-format export'):
            self.compile()

    def test_prompt_wrapper_is_supported(self):
        self.graph = {'prompt': self.graph, 'client_id': 'ignored'}
        self.persist()
        self.assertEqual(self.compile()['load']['inputs']['model'], 'user-original')

    def test_missing_upstream_cannot_be_treated_as_literal(self):
        self.graph['conditioning']['inputs']['model'] = ['missing', 0]
        self.persist()
        with self.assertRaisesRegex(ValueError, 'Missing API'):
            self.compile()

    def test_disconnected_cycle_is_rejected_before_pruning(self):
        self.graph['unused']['inputs']['model'] = ['unused', 0]
        self.persist()
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            self.compile()

    def test_negative_and_out_of_range_link_slots_are_rejected(self):
        for slot in (-1, 9):
            with self.subTest(slot=slot):
                graph = copy.deepcopy(self.graph)
                graph['conditioning']['inputs']['model'] = ['load', slot]
                with self.assertRaisesRegex(ValueError, 'slot'):
                    cw.validate_graph(graph, self.schemas)

    def test_link_types_must_match(self):
        self.schemas['UserModel']['output'] = ['IMAGE']
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            cw.validate_graph(self.graph, self.schemas)

    def test_binding_linked_input_is_rejected(self):
        self.profile['bindings'][0]['input'] = 'model'
        with self.assertRaisesRegex(ValueError, 'upstream literal'):
            self.compile()

    def test_duplicate_parameter_binding_target_is_rejected(self):
        self.profile['parameters'] = [{'node_id': 'conditioning', 'input': 'caption', 'value': 'override'}]
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.compile()

    def test_disconnected_binding_target_cannot_disappear(self):
        self.profile['parameters'] = [{'node_id': 'unused', 'input': 'model', 'value': 'override'}]
        with self.assertRaisesRegex(ValueError, 'absent from retained'):
            self.compile()

    def test_runtime_contract_requires_explicit_prompt_video_and_clock(self):
        self.profile['bindings'] = [b for b in self.profile['bindings'] if b['source'] != 'fps']
        with self.assertRaisesRegex(ValueError, 'prompt, video, frames and fps'):
            self.compile()

    def test_output_prefix_binding_is_required_for_attempt_isolation(self):
        self.profile['bindings'] = [b for b in self.profile['bindings'] if b['source'] != 'output_prefix']
        with self.assertRaisesRegex(ValueError, 'output_prefix'):
            cw.validate_profile(self.root, self.profile)

    def test_different_attempts_have_distinct_prefixes_with_same_original_models(self):
        first = cw.compile_graph(self.root, self.entry, self.uploads, 'attempt-A')
        second = cw.compile_graph(self.root, self.entry, self.uploads, 'attempt-B')
        self.assertNotEqual(first['save']['inputs']['prefix'], second['save']['inputs']['prefix'])
        self.assertEqual(first['load'], second['load'])
        self.assertEqual(first['load'], self.graph['load'])

    def test_preparation_rejects_missing_extra_and_invalid_declared_reference_slots(self):
        for slots in (None, True, 1, 3, 0):
            with self.subTest(slots=slots):
                self.profile['limits']['identity_slots'] = slots
                with self.assertRaisesRegex(ValueError, 'identity_slots'):
                    cw.validate_profile(self.root, self.profile)

    def test_one_reference_can_bind_multiple_actual_workflow_controls(self):
        self.graph['conditioning']['inputs']['portrait_again'] = ''
        self.schemas['UserH3']['input']['required']['portrait_again'] = ['STRING']
        self.profile['bindings'].append({'source': 'reference', 'index': 0,
                                        'node_id': 'conditioning', 'input': 'portrait_again'})
        self.persist()
        actual = self.compile()
        cw.validate_graph(actual, self.schemas)
        self.assertEqual(actual['conditioning']['inputs']['portrait_again'], self.uploads['references'][0])

    def test_reference_index_coverage_must_match_exact_ordered_references(self):
        for index in (2, -1, True):
            with self.subTest(index=index):
                self.profile['bindings'][-2]['index'] = index
                with self.assertRaises(ValueError):
                    self.compile()

    def test_upload_paths_reject_traversal_windows_and_absolute_names(self):
        for path in ('../a.png', 'C:/a.png', '/a.png', 'a\\b.png', ''):
            with self.subTest(path=path):
                self.uploads['references'][0] = path
                with self.assertRaisesRegex(ValueError, 'contained relative'):
                    self.compile()

    def test_project_workflow_path_cannot_escape(self):
        self.profile['workflow_path'] = '../other.json'
        with self.assertRaisesRegex(ValueError, 'relative and contained'):
            self.compile()

    def test_remote_or_credentialed_endpoint_is_rejected(self):
        for url in ('https://localhost:8188', 'http://example.com', 'http://a@localhost:8188', 'http://localhost:8188/x'):
            with self.subTest(url=url):
                self.profile['url'] = url
                with self.assertRaisesRegex(ValueError, 'loopback'):
                    self.compile()

    def test_output_index_is_strict(self):
        for index in (-1, True, 0.5, None):
            with self.subTest(index=index):
                self.profile['output']['index'] = index
                with self.assertRaisesRegex(ValueError, 'index'):
                    self.compile()

    def test_output_collection_and_installed_saver_are_explicit(self):
        self.profile['output']['field'] = 'images'
        with self.assertRaisesRegex(ValueError, 'gifs/videos'):
            self.compile()
        self.profile['output']['field'] = 'gifs'
        self.schemas['UserVideoOutput']['output_node'] = False
        with self.assertRaisesRegex(ValueError, 'installed output'):
            cw.validate_outputs(self.compile(), self.schemas, self.profile)

    def test_extra_output_keeps_original_side_effect_dependency(self):
        self.graph['unload'] = {'class_type': 'Unload', 'inputs': {'video': ['conditioning', 0]}}
        self.schemas['Unload'] = {'input': {'required': {'video': ['VIDEO']}}, 'output_node': True, 'output': []}
        self.profile['extra_output_node_ids'] = ['unload']
        self.persist()
        actual = self.compile()
        self.assertIn('unload', actual)
        cw.validate_graph(actual, self.schemas)
        cw.validate_outputs(actual, self.schemas, self.profile)

    def test_numeric_types_ranges_and_nonfinite_are_rejected(self):
        for value in (True, 25.0, 0, 251, float('nan'), float('inf')):
            with self.subTest(value=value):
                graph = copy.deepcopy(self.graph)
                graph['conditioning']['inputs']['length'] = value
                with self.assertRaisesRegex(ValueError, 'Numeric'):
                    cw.validate_graph(graph, self.schemas)

    def test_required_unknown_and_string_inputs_are_checked(self):
        for operation in ('missing', 'unknown', 'wrong-string'):
            with self.subTest(operation=operation):
                graph = copy.deepcopy(self.graph)
                if operation == 'missing':
                    del graph['conditioning']['inputs']['caption']
                elif operation == 'unknown':
                    graph['conditioning']['inputs']['invented'] = 1
                else:
                    graph['conditioning']['inputs']['caption'] = 1
                with self.assertRaises(ValueError):
                    cw.validate_graph(graph, self.schemas)

    def test_combo_choices_do_not_confuse_bool_with_integer(self):
        graph = {'x': {'class_type': 'Combo', 'inputs': {'choice': True}}}
        schemas = {'Combo': {'input': {'required': {'choice': [[1, 2]]}}}}
        with self.assertRaisesRegex(ValueError, 'combo'):
            cw.validate_graph(graph, schemas)

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / 'assets/workflow.json'
        path.write_text('{"x":1,"x":2}')
        self.profile['workflow_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
            self.compile()

    def test_inspect_cli_is_read_only_and_refuses_existing_report(self):
        schemas = self.root / 'schemas.json'
        schemas.write_text(json.dumps(self.schemas))
        report = self.root / 'inspection.json'
        command = [sys.executable, str(Path(cw.__file__)), 'inspect', '--workflow',
                   str(self.root / 'assets/workflow.json'), '--schemas', str(schemas), '--out', str(report)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text())
        self.assertTrue(data['schema_checked'])
        self.assertEqual(data['output_candidates'], ['save'])
        before = report.read_bytes()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(report.read_bytes(), before)
        self.assertFalse((self.root / 'job.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)

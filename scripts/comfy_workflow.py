"""Inspect and bind an explicitly supplied ComfyUI API workflow without executing it."""
from __future__ import annotations

import argparse
import copy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

SOURCES = {'prompt', 'video', 'reference', 'frames', 'fps', 'seconds', 'width',
           'height', 'aspect_ratio', 'output_prefix', 'seed'}
MANDATORY = {'prompt', 'video', 'frames', 'fps', 'output_prefix'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))


def _link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def _graph(document):
    if not isinstance(document, dict) or isinstance(document.get('nodes'), list):
        raise ValueError('Supply the same workflow as an API-format export; UI widgets are not executed')
    graph = document.get('prompt', document)
    if not isinstance(graph, dict) or not graph:
        raise ValueError('A nonempty ComfyUI API graph is required')
    result = {}
    for ident, node in graph.items():
        if not isinstance(ident, str) or not ident or not isinstance(node, dict):
            raise ValueError('API node IDs must be nonempty unique strings')
        if not isinstance(node.get('class_type'), str) or not node['class_type'] or not isinstance(node.get('inputs'), dict):
            raise ValueError('Every API node needs class_type and inputs')
        if any(not isinstance(key, str) or not key for key in node['inputs']):
            raise ValueError('API input names must be nonempty strings')
        result[ident] = copy.deepcopy(node)
    _prune(result, list(result))  # Also reject disconnected cycles and dangling links.
    return result


def _prune(graph, roots):
    result, visiting = {}, set()
    def visit(ident):
        if ident in result:
            return
        if ident in visiting:
            raise ValueError('Cyclic API workflow')
        if ident not in graph:
            raise ValueError('Missing API execution node: ' + str(ident))
        visiting.add(ident)
        for value in graph[ident]['inputs'].values():
            if _link(value):
                if value[1] < 0:
                    raise ValueError('Invalid negative output slot')
                visit(value[0])
        visiting.remove(ident)
        result[ident] = copy.deepcopy(graph[ident])
    for root in roots:
        visit(root)
    return result


def _target(graph, entry):
    ident, name = entry.get('node_id'), entry.get('input')
    if not isinstance(ident, str) or ident not in graph or name not in graph[ident]['inputs']:
        raise ValueError('Binding/parameter target is absent from retained graph')
    if _link(graph[ident]['inputs'][name]):
        raise ValueError('Bind the upstream literal control, not a linked input')
    return ident, name


def validate_profile(project, profile):
    """Validate immutable, run-contained API bytes and explicit literal bindings."""
    import factory
    from comfy_runner import endpoint
    if profile.get('kind') != 'local-comfyui':
        raise ValueError('Expected local-comfyui profile')
    endpoint(profile['url'])
    path = factory.resolve(project, profile['workflow_path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != profile.get('workflow_sha256'):
        raise ValueError('Workflow bytes changed; inspect and review the new API export')
    graph = _graph(_read(path))
    selection = profile.get('output', {})
    if not isinstance(selection, dict) or selection.get('field') not in ('gifs', 'videos'):
        raise ValueError('Choose the final saver node and gifs/videos collection')
    if 'index' in selection and (type(selection['index']) is not int or selection['index'] < 0):
        raise ValueError('Output index must be a nonnegative integer')
    extras = profile.get('extra_output_node_ids', [])
    if not isinstance(extras, list) or any(not isinstance(v, str) for v in extras):
        raise ValueError('Extra output IDs must be an explicit string list')
    roots = [selection.get('node_id')] + extras
    graph = _prune(graph, roots)
    bindings, parameters = profile.get('bindings'), profile.get('parameters', [])
    if not isinstance(bindings, list) or not isinstance(parameters, list):
        raise ValueError('Bindings and parameters must be lists')
    seen, sources, reference_indices = set(), set(), set()
    for entry in bindings + parameters:
        if not isinstance(entry, dict):
            raise ValueError('Binding/parameter must be an object')
        target = _target(graph, entry)
        if target in seen:
            raise ValueError('Duplicate binding/parameter target')
        seen.add(target)
        if entry in bindings:
            source = entry.get('source')
            if source not in SOURCES:
                raise ValueError('Unknown runtime source')
            if source == 'reference':
                if type(entry.get('index')) is not int or entry['index'] < 0:
                    raise ValueError('Reference needs an explicit nonnegative index')
                reference_indices.add(entry['index'])
            elif 'index' in entry:
                raise ValueError('Only reference bindings use index')
            sources.add(source)
        elif 'value' not in entry:
            raise ValueError('Parameter needs an explicit value')
    if not MANDATORY.issubset(sources):
        raise ValueError('Bind prompt, video, frames and fps explicitly, plus an output_prefix isolation control')
    slots = profile.get('limits', {}).get('identity_slots')
    if type(slots) is not int or slots < 1 or reference_indices != set(range(slots)):
        raise ValueError('Reference binding indices must exactly cover declared identity_slots, starting at zero')
    return graph


def _path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts):
        raise ValueError('Upload path must be a contained relative ComfyUI input name')
    return value


def compile_graph(project, entry, uploads, attempt_id):
    import factory
    profile = entry['profile']
    graph = validate_profile(project, profile)
    if not isinstance(attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', attempt_id):
        raise ValueError('Unsafe attempt ID')
    references = uploads.get('references')
    if not isinstance(references, list) or len(references) != len(entry['reference_paths']):
        raise ValueError('Uploaded references must match current entry exactly')
    for value in references:
        _path(value)
    required = set(range(len(references)))
    actual = {b['index'] for b in profile['bindings'] if b['source'] == 'reference'}
    if actual != required:
        raise ValueError('Reference bindings must cover exactly the current ordered references')
    window = entry['source_contract']['window']
    canvas = window['source_projection']['display_canvas']
    width, height = canvas['width'], canvas['height']
    ratio = Fraction(width, height) * Fraction(canvas.get('sample_aspect_ratio', '1'))
    fps = Fraction(window['generation_fps'])
    frames = window['generation_frames']
    if fps <= 0 or type(frames) is not int or frames <= 0:
        raise ValueError('Invalid generation clock')
    context = {'prompt': factory.resolve(project, entry['prompt_path']).read_text(encoding='utf-8-sig'),
               'video': _path(uploads['video']), 'frames': frames,
               'fps': int(fps) if fps.denominator == 1 else float(fps),
               'seconds': float(Fraction(frames, 1) / fps), 'width': width, 'height': height,
               'aspect_ratio': f'{ratio.numerator}:{ratio.denominator}',
               'output_prefix': f'factory/{attempt_id}/{entry["window_id"]}',
               'seed': int.from_bytes(hashlib.sha256(attempt_id.encode()).digest()[:4], 'big') & 0x7fffffff}
    for parameter in profile.get('parameters', []):
        ident, name = _target(graph, parameter)
        graph[ident]['inputs'][name] = copy.deepcopy(parameter['value'])
    for binding in profile['bindings']:
        ident, name = _target(graph, binding)
        value = references[binding['index']] if binding['source'] == 'reference' else context[binding['source']]
        graph[ident]['inputs'][name] = copy.deepcopy(value)
    return graph


def validate_graph(graph, schemas):
    """Validate actual installed schemas; unsupported dynamic widgets stay explicit."""
    graph = _graph(graph)
    for ident, node in graph.items():
        schema = schemas.get(node['class_type'])
        if not isinstance(schema, dict):
            raise ValueError('Local node type is not installed: ' + node['class_type'])
        specs = {}
        for category in ('required', 'optional'):
            values = schema.get('input', {}).get(category, {})
            if not isinstance(values, dict):
                raise ValueError('Unsupported input schema')
            for name, spec in values.items():
                if not isinstance(spec, (list, tuple)) or not spec:
                    raise ValueError('Unsupported field schema')
                specs[name] = spec
                if category == 'required' and name not in node['inputs']:
                    raise ValueError(f'{ident}: missing required input {name}')
        if not set(node['inputs']).issubset(specs):
            raise ValueError(f'{ident}: input absent from installed schema')
        for name, value in node['inputs'].items():
            typ = specs[name][0]
            options = specs[name][1] if len(specs[name]) > 1 and isinstance(specs[name][1], dict) else {}
            if _link(value):
                upstream = graph[value[0]]['class_type']
                outputs = schemas.get(upstream, {}).get('output', [])
                if not 0 <= value[1] < len(outputs):
                    raise ValueError('Invalid output slot')
                actual = outputs[value[1]]
                if isinstance(typ, list) or typ not in ('*', actual) and actual != '*':
                    raise ValueError('Incompatible input link')
            elif isinstance(typ, list):
                if not any(type(value) is type(choice) and value == choice for choice in typ):
                    raise ValueError('Value is not an installed combo choice')
            elif typ in ('INT', 'FLOAT'):
                if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                        or typ == 'INT' and type(value) is not int
                        or not options.get('min', -math.inf) <= value <= options.get('max', math.inf)):
                    raise ValueError('Numeric input has invalid type or range')
            elif typ == 'BOOLEAN':
                if type(value) is not bool:
                    raise ValueError('Expected boolean')
            elif typ == 'STRING':
                if not isinstance(value, str):
                    raise ValueError('Expected string')
            elif typ != '*':
                raise ValueError('Unsupported literal input type; provide a supported API graph: ' + str(typ))


def validate_outputs(graph, schemas, profile):
    for ident in [profile['output']['node_id']] + profile.get('extra_output_node_ids', []):
        if ident not in graph or schemas.get(graph[ident]['class_type'], {}).get('output_node') is not True:
            raise ValueError('Selected saver/extra root must be an installed output node')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect'])
    parser.add_argument('--workflow', type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--url')
    source.add_argument('--schemas', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    graph = _graph(_read(args.workflow))
    schemas = None
    if args.url:
        from comfy_runner import Client
        schemas = Client(args.url).request('/object_info')
    elif args.schemas:
        schemas = _read(args.schemas)
    if schemas is not None:
        validate_graph(graph, schemas)
    report = {'workflow_sha256': hashlib.sha256(args.workflow.read_bytes()).hexdigest(),
              'workflow_format': 'api', 'schema_checked': schemas is not None,
              'controls': [{'node_id': ident, 'input': name, 'value': value, 'class_type': node['class_type']}
                           for ident, node in graph.items() for name, value in node['inputs'].items() if not _link(value)],
              'output_candidates': [ident for ident, node in graph.items() if schemas is not None and
                                    schemas.get(node['class_type'], {}).get('output_node') is True],
              'status': 'inspection-only; no configuration activated or generation submitted'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(str(args.out.resolve()))


if __name__ == '__main__':
    main()

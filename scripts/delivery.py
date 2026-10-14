"""Validate an explicitly reviewed, externally postprocessed complete film.

This separate build-only contract preserves failed intermediate reviews. It
never changes candidate approval or produces a visual verdict. Run records are
private; only neutral source facts are copied into a dedicated skill.
"""
from pathlib import Path
from fractions import Fraction
import hashlib
import factory
import media


def bound_file(project, item):
    actual = factory.file_signature(project, item['path'])
    if item != actual:
        raise ValueError('Delivery artifact changed after review')
    return actual


def verify_raw_rows(project, source, proof, runtime=None):
    runtime=runtime or {}
    av=media.runtime(runtime.get('deps'),runtime.get('scalar_cpu',False))
    recorded=factory.read(factory.resolve(project,proof['proof_file']['path']))
    if recorded.get('raw_signature') != {k:v for k,v in proof['artifact'].items() if k!='path'}:
        raise ValueError('Raw ownership proof must bind the actual raw artifact')
    rows=recorded['owner_rows'];n=source['video']['frame_count']
    if len(rows)!=n or [row['source'] for row in rows]!=list(range(n)):
        raise ValueError('Raw proof rows do not cover all source owners')
    count=0
    with av.open(str(factory.resolve(project,proof['artifact']['path']))) as inp:
        inp.streams.video[0].codec_context.thread_count=1
        for i,frame in enumerate(inp.decode(video=0)):
            if i>=n:raise ValueError('Raw artifact has extra owners')
            expected=source['video']['frames'][i]
            time=Fraction(expected['time'])-Fraction(source['video']['start_time'])
            if (Fraction(frame.pts)*frame.time_base!=time or
                    hashlib.sha256(frame.to_image().tobytes()).hexdigest()!=rows[i]['rgb_sha256']):
                raise ValueError('Raw complete RGB/PTS differs from owner proof')
            count+=1
    if count!=n:raise ValueError('Raw artifact lost source owners')


def guard(project, relative):
    project = Path(project)
    source, analysis = factory.validate_analysis(project)
    record = factory.read(factory.resolve(project, relative))
    if record.get('schema_version') != 1 or record.get('kind') != 'reviewed-postprocessed-film':
        raise ValueError('Unknown reviewed delivery contract')
    if record.get('source_signature') != source['source']:
        raise ValueError('Delivery source binding is stale')
    if record.get('analysis_digest') != factory.digest(analysis):
        raise ValueError('Delivery source analysis changed')
    actual = bound_file(project, record['file_signature'])
    if record['output_path'] != actual['path']:
        raise ValueError('Delivery output path mismatch')
    if record['technical'].get('status') != 'passed':
        raise ValueError('Delivery needs a passed complete technical review')
    factory.validate_final_technical_report(source, record, actual)
    n = source['video']['frame_count']
    owners = record['owners']
    cursor = 0
    for owner in owners:
        start, end = owner['source_interval']
        if type(start) is not int or type(end) is not int or start != cursor or not start < end <= n:
            raise ValueError('Delivery owners must cover each source frame exactly once')
        if owner.get('passthrough'):
            if owner['artifact'] != source['source']:
                raise ValueError('Passthrough must bind the original source')
        else:
            bound_file(project, owner['artifact'])
            if not owner.get('provenance') or not owner.get('notes'):
                raise ValueError('Processed owner needs explicit provenance and method notes')
            for parent in owner['provenance']:
                bound_file(project, parent)
        cursor = end
    if cursor != n:
        raise ValueError('Delivery ownership is incomplete')
    proof = record['raw_owner_verification']
    bound_file(project, proof['artifact'])
    bound_file(project, proof['proof_file'])
    if proof.get('status') != 'passed' or proof.get('frames') != n or not proof.get('notes'):
        raise ValueError('Complete raw ownership RGB/PTS verification is missing')
    verify_raw_rows(project,source,proof,record.get('local_runtime'))
    identity = record['identity_review']
    if identity.get('status') != 'passed' or not identity.get('notes') or not identity.get('original_references'):
        raise ValueError('Delivery needs a review against original user identity references')
    for original in identity['original_references']:
        bound_file(project, original)
    visual = record['visual']
    if visual.get('status') != 'passed' or not visual.get('notes'):
        raise ValueError('Delivery visual observations are missing')
    if not factory.coverage(visual.get('dynamic_intervals', []), range(n)):
        raise ValueError('Complete current-film original-speed review is required')
    evidence = visual.get('original_speed_evidence', {})
    if (evidence.get('reviewer') not in ('human-user', 'codex') or
            evidence.get('speed') != '1x' or evidence.get('output_sha256') != actual['sha256'] or
            not evidence.get('observations')):
        raise ValueError('Original-speed evidence must bind the exact final movie')
    if evidence['reviewer'] == 'human-user' and (not evidence.get('question') or not evidence.get('answer')):
        raise ValueError('Human dynamic review requires the actual question and reply')
    # Risk intervals describe this postprocess (not a rejected intermediate).
    # Complete dynamic/identity review above still covers the whole source.
    required = set()
    for interval in record['native_risk_intervals']:
        start, end = interval
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= n:
            raise ValueError('Invalid native risk interval')
        required.update(range(start, end))
    for owner in owners:
        for boundary in owner['source_interval']:
            required.update(range(max(0, boundary-2), min(n, boundary+2)))
    if not required or not factory.coverage(visual.get('native_intervals', []), required):
        raise ValueError('Delivery native risks and all composition seams need actual review')
    if not visual.get('native_evidence'):
        raise ValueError('Delivery native observations need bound image evidence')
    for item in visual['native_evidence']:
        bound_file(project, item)
    return record

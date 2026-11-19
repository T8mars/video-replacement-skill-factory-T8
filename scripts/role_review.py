"""Check a human/agent's recorded observations against the original shot, never infer them."""


def expectations(analysis, window):
    start, end = window['source_interval']
    points = {start, (start + end - 1)//2, end - 1}
    overrides = {}
    for role in analysis['roles']:
        for a, b in role.get('appearances', []):
            points.update(frame for frame in (a-1, a, b-1, b) if start <= frame < end)
    for shot in analysis['shots']:
        if shot['end'] <= start or end <= shot['start']:
            continue
        points.update((max(start, shot['start']), min(end, shot['end']) - 1))
        for event in shot.get('events', []):
            frame = event['source_frame']
            if start <= frame < end:
                points.add(frame)
        for point in shot.get('role_checkpoints', []):
            frame = point['source_frame']
            if type(frame) is not int or not shot['start'] <= frame < shot['end']:
                raise ValueError('Role checkpoint lies outside its source shot')
            if start <= frame < end:
                if frame in overrides:
                    raise ValueError('Duplicate role checkpoint')
                points.add(frame)
                overrides[frame] = point
    rows = []
    for frame in sorted(points):
        shot = next(s for s in analysis['shots'] if s['start'] <= frame < s['end'])
        roles = sorted(r['slot'] for r in analysis['roles'] if r['slot'] in shot['roles'] and
                       (not r.get('appearances') or any(a <= frame < b for a,b in r['appearances'])))
        point = overrides.get(frame, {})
        visible = point.get('visible_slots', roles)
        if not isinstance(visible, list) or len(visible) != len(set(visible)) or not set(visible) <= set(roles):
            raise ValueError('Visible roles must match source appearances; record actual occlusion')
        owners = point.get('owners', {})
        if not isinstance(owners, dict) or not set(owners.values()) <= set(window['role_slots']):
            raise ValueError('Prop and garment owners must be source role slots')
        rows.append({'source_frame': frame, 'visible_slots': sorted(visible), 'owners': owners})
    return rows


def binding(record):
    import factory
    return factory.digest({'input': record['input_digest'], 'candidate': record['file_signature']['sha256'],
                           'task': record['task_id'], 'mapping': record['source_to_output']})


def pending(analysis, window, record):
    return {'binding': binding(record), 'reviewer': '', 'continuity_notes': '',
            'points': [{'source_frame': p['source_frame'],
                        'output_frame': record['source_to_output'][p['source_frame'] - window['source_interval'][0]],
                        'visible_slots': None, 'owners': None, 'unexpected_subjects': None}
                       for p in expectations(analysis, window)]}


def validate(analysis, window, record):
    review = record.get('role_review') or {}
    if (review.get('binding') != binding(record) or not str(review.get('reviewer') or '').strip()
            or not str(review.get('continuity_notes') or '').strip()):
        raise ValueError('Current candidate needs a bound role and continuity review')
    expected = expectations(analysis, window)
    rows = review.get('points')
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError('Review every source role checkpoint')
    for want, got in zip(expected, rows):
        src = want['source_frame']
        if (got.get('source_frame') != src or got.get('output_frame') != record['source_to_output'][src-window['source_interval'][0]]
                or not isinstance(got.get('visible_slots'), list) or sorted(got['visible_slots']) != want['visible_slots']
                or got.get('owners') != want['owners'] or got.get('unexpected_subjects') is not False):
            raise ValueError('Observed people, ownership or output frame disagree at source frame ' + str(src))

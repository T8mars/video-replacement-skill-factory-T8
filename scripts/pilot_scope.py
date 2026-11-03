"""Current-identity pilot scopes. This module never submits or judges visuals."""
from pathlib import Path

VERSION=1

def _factory():
    import factory
    return factory

def _risks(value):
    if value is None:return []
    if isinstance(value,str):return [value] if value and value!='ordinary' else []
    if not isinstance(value,list) or any(not isinstance(v,str) or not v for v in value):
        raise ValueError('Risk classes must be a string or a list of nonempty strings')
    return [v for v in value if v!='ordinary']

def scope(project,window,profile):
    f=_factory();project=Path(project).resolve()
    source,analysis=f.validate_analysis(project)
    job=f.read(project/'job.json')
    slots=window.get('role_slots',[])
    identities=[]
    for slot in slots:
        ref=job.get('references',{}).get(slot)
        if not isinstance(ref,dict) or not ref.get('path'):
            raise ValueError('Current pilot scope needs every actual role reference')
        signature=f.file_signature(project,ref['path'])
        identities.append({'slot':slot,'image_sha256':signature['sha256'],
            'description':{k:v for k,v in ref.items() if k not in ('path','file_signature')}})
    a,b=window.get('context_interval',window['source_interval'])
    risks=set(_risks(window.get('risk_class'))+_risks(window.get('risk_classes')))
    for shot in analysis['shots']:
        if shot['start']<b and a<shot['end']:
            risks.update(_risks(shot.get('risk_class'))+_risks(shot.get('risk_classes')))
            for event in shot.get('events',[]):
                if a<=event['source_frame']<b:
                    risks.update(_risks(event.get('risk_class'))+_risks(event.get('risk_classes')))
    route=f.generation_profile(profile)
    if profile.get('kind')=='local-comfyui':
        workflow=f.file_signature(project,profile['workflow_path'])
        route={**route,'actual_workflow_sha256':workflow['sha256']}
    return f.digest({'schema_version':VERSION,'source_sha256':source['source']['sha256'],
        'role_identities':identities,'replacement_mode':job.get('replacement_mode'),
        'appearance_policy':job.get('appearance_policy',{}),'profile':route,
        'risk_classes':sorted(risks) or ['ordinary']})

def _window(project,entry):
    f=_factory();plan=f.read(Path(project)/'plan.json')
    window=next((w for w in plan['windows'] if w['window_id']==entry['window_id']),None)
    if window is None:raise ValueError('Pilot scope has no current planned window')
    return window

def check(project,entry):
    """True only for a current, still-bound approved pilot. No visual inference."""
    f=_factory();project=Path(project).resolve()
    expected=scope(project,_window(project,entry),entry['profile'])
    if entry.get('pilot_scope')!=expected:
        raise ValueError('Pilot identity/policy scope changed; prepare and review current inputs')
    path=project/'pilot-approvals.json'
    if not path.exists():return False
    approvals=f.read(path)
    record=approvals.get('scopes',{}).get(expected)
    if not record:return False
    if record.get('schema_version')!=VERSION or record.get('status')!='passed':return False
    reviews_path=project/'reviews.json'
    if not reviews_path.exists():return False
    current=f.read(reviews_path).get('windows',{}).get(record.get('window_id'))
    if not current or f.digest(current)!=record.get('review_digest'):return False
    if current.get('input_digest')!=record.get('input_digest') or current.get('visual',{}).get('status')!='passed':return False
    try:
        pilot_entry=next(e for e in f.read(project/'queue.json')['jobs'] if e['window_id']==record['window_id'])
        if (pilot_entry.get('input_digest')!=record.get('input_digest') or
                f.compute_input_digest(project,pilot_entry)!=record.get('input_digest')):return False
    except (OSError,KeyError,ValueError,StopIteration):return False
    try:
        signature=f.file_signature(project,record['candidate_signature']['path'])
    except (OSError,KeyError,ValueError):return False
    return signature==record.get('candidate_signature') and signature==current.get('file_signature')

def approval(project,entry):
    approved=check(project,entry)
    if not approved and entry.get('pilot') is not True:
        raise ValueError('Current identity/backend/risk scope requires an actually reviewed pilot before batching')
    return approved

def promote(project,window_id):
    """Record an actually reviewed window; does not change any generation input."""
    f=_factory();project=Path(project).resolve()
    selected=f.review_guard(project,window_ids=[window_id])
    if len(selected)!=1:raise ValueError('Pilot promotion needs exactly one reviewed generated window')
    window,record=selected[0]
    if window.get('passthrough') or record.get('visual',{}).get('status')!='passed':
        raise ValueError('Original passthrough cannot approve a generation route')
    queue=f.read(project/'queue.json')
    entry=next(e for e in queue['jobs'] if e['window_id']==window_id)
    expected=scope(project,window,entry['profile'])
    if entry.get('pilot_scope')!=expected:raise ValueError('Pilot does not bind current identities and policy')
    path=project/'pilot-approvals.json'
    values=f.read(path) if path.exists() else {'schema_version':VERSION,'kind':'pilot-scope-approvals','scopes':{}}
    values['scopes'][expected]={'schema_version':VERSION,'status':'passed','window_id':window_id,
        'input_digest':entry['input_digest'],'candidate_signature':record['file_signature'],
        'review_digest':f.digest(record),'visual_status':'passed-current-review',
        'note':'Review guard verified current candidate, source motion, native risks and retained natural-speed coverage.'}
    f.write(path,values)
    return {'pilot_scope':expected,'window_id':window_id,'status':'passed','cloud_calls':0}

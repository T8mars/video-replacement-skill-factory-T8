"""Chenyu's published Wuwu character-swap workflow, using the factory queue."""
from fractions import Fraction
import json
import mimetypes
from urllib import error, parse, request
import uuid

import rh_runner as cloud

BASE = 'https://www.chenyu.cn/api/open/v2'
WORKFLOW = 'wf_04a30714468c59fd78f5'
KIND = 'chenyu-workflow'
SUBMIT = '/workflow/run/submit'
OUTPUT = 'result_168_gifs_1'
# The published label gives 17 + 5*N, minimum 22. Its 73-frame default
# also completed a live trial. Other lengths require their own pilot.
COUNTS = tuple(sorted({73, *range(22, 241, 5)}))


def frame_count(needed):
    for frames in COUNTS:
        if frames >= needed:
            return frames
    raise ValueError('Split this shot at a reviewed action boundary within ten seconds')


def validate_window(profile, window, slots):
    if (profile.get('workflow_id') != WORKFLOW or not profile.get('revision_id')
            or profile.get('output_name') != OUTPUT
            or set(profile) - {'kind','workflow_id','revision_id','output_name','limits','quality_evidence','evidence'}):
        raise ValueError('Unsupported Chenyu workflow profile')
    if not 1 <= len(slots) <= 5 or len(set(slots)) != len(slots):
        raise ValueError('Chenyu requires 1..5 distinct ordered role slots')
    frames = window['generation_frames']
    if (type(frames) is not int or frames not in COUNTS or Fraction(window['generation_fps']) != 24
            or Fraction(str(window['duration'])) != Fraction(frames, 24)):
        raise ValueError('Invalid frame count, cadence or duration for this workflow')


def verify_schema(data, profile, refs):
    if not 1 <= refs <= 5:
        raise ValueError('Expected 1..5 reference images')
    if (data.get('workflow_id') != WORKFLOW or data.get('revision_id') != profile['revision_id']
            or data.get('may_incur_external_model_cost') is not False):
        raise ValueError('Chenyu revision or external-cost terms changed; review the published workflow')
    fields = {item['name']: item for item in data['editable_parameter_manifest']}
    expected = {'node_138_value':'string', 'node_342_video':'video_input', 'node_326_value':'int'}
    expected.update({f'node_{386+i}_image':'image_input' for i in range(refs)})
    if (any(fields.get(k, {}).get('value_type') != v for k,v in expected.items())
            or any(item.get('required') and k not in expected for k,item in fields.items())
            or [o['name'] for o in data['candidate_output_manifest'] if o.get('type') == 'video'] != [OUTPUT]):
        raise ValueError('Chenyu input slots or named output changed')
    return {k:data.get(k) for k in ('quote_amount','quote_currency')}


def payload(profile, prompt, refs, video, frames):
    inputs = {'node_138_value':prompt, 'node_342_video':video['asset_uri'], 'node_326_value':frames}
    inputs.update({f'node_{386+i}_image':ref['asset_uri'] for i,ref in enumerate(refs)})
    return {'workflow_id':WORKFLOW, 'revision_id':profile['revision_id'], 'inputs':inputs,
            'idempotency_key':uuid.uuid4().hex, 'accept_external_cost_risk':False}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Transport(cloud.Transport):
    provider = 'chenyu'
    download_headers = {'User-Agent':'Mozilla/5.0'}

    def __init__(self, key):
        if not key:
            raise ValueError('Missing Chenyu credential')
        self.key = key
        self.opener = request.build_opener(NoRedirect())

    def api(self, path, body=None, mime='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body,ensure_ascii=False).encode()
        req = request.Request(BASE + path, data=body, headers={'Authorization':'Bearer ' + self.key,
                              'Content-Type':mime, 'User-Agent':'Video-Replacement-Factory'})
        try:
            with self.opener.open(req, timeout=120) as response:
                envelope = json.load(response)
        except error.HTTPError as exc:
            exc.close()
            raise cloud.ApiFailure(exc.code) from None
        if envelope.get('code') != 0:
            raise cloud.ApiFailure(envelope.get('code'))
        return envelope['data']

    def inspect_route(self, profile, refs):
        return verify_schema(self.api('/workflow/market/info?workflow_id='+WORKFLOW), profile, refs)

    def upload(self, path):
        kind = 'video' if path.suffix.lower() == '.mp4' else 'image'
        name = cloud.sha(path) + path.suffix.lower()
        boundary = uuid.uuid4().hex
        def field(key, value):
            return f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        header = (field('media_type',kind) + field('purpose','temp_input')
                  + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  + f'Content-Type: {mimetypes.guess_type(name)[0] or "application/octet-stream"}\r\n\r\n')
        body = header.encode() + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        uri = self.api('/assets',body,'multipart/form-data; boundary='+boundary).get('asset_uri')
        if not isinstance(uri,str) or not uri.startswith('asset://'):
            raise ValueError('Missing uploaded asset URI')
        return {'asset_uri':uri}

    def submit(self, path, body):
        if (path != SUBMIT or body.get('workflow_id') != WORKFLOW or not body.get('idempotency_key')
                or body.get('accept_external_cost_risk') is not False):
            raise ValueError('Invalid Chenyu submission intent')
        return {'taskId':self.api(path,body)['run_order_id']}

    def query(self, task_id, revision=None):
        data = self.api('/workflow/run/info?' + parse.urlencode({'run_order_id':task_id}))
        if data.get('run_order_id') != task_id or (revision and data.get('revision_id') != revision):
            raise ValueError('Unexpected Chenyu order or revision')
        status = str(data.get('run_status','')).lower()
        state = {'queued':'QUEUED','pending':'QUEUED','running':'RUNNING','succeeded':'SUCCESS',
                 'failed':'FAILED','canceled':'FAILED','cancelled':'FAILED','error':'FAILED'}.get(status,status.upper())
        outputs = data.get('outputs')
        value = outputs.get(OUTPUT) if isinstance(outputs,dict) and state=='SUCCESS' else None
        if isinstance(value,list) and len(value)==1:
            value=value[0]
        url = value if isinstance(value,str) and value.startswith('https://') else None
        usage = None
        if data.get('settlement_status')=='settled' and data.get('final_amount') is not None and data.get('quote_currency'):
            usage={'amount':data['final_amount'],'currency':data['quote_currency']}
        return {'taskId':task_id,'status':state,'usage':usage,'settlement_status':data.get('settlement_status'),
                'results':[{'outputType':'mp4','url':url}] if url else []}


if __name__ == '__main__':
    cloud.main(provider='chenyu')

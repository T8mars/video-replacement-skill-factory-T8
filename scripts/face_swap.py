"""Original-photo identity correction via live-schema-verified RH applications.

Uses the existing private ledger, upload cache and query/download recovery.
No visual approval is produced. Explicit input authorization and current hashes
are required before the first paid submit. The factory's H3 adapter is unchanged.
"""
from pathlib import Path
from fractions import Fraction
import sys
import json
import subprocess
import argparse
import shutil
import os
import media

ROOT=Path(__file__).parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory
import rh_runner as rh

APP_NODES={
    '1911332072277929986':('10','11',[]),
    '1891737138617069570':('2','3',[('frame_load_cap','0'),('select_every_nth','1'),('skip_first_frames','0')]),
    '1872958921492389890':('3','51',[]),
    '1931791712199618561':('5','4',[]),
}

def nodes_for(profile, image, video):
    if profile.get('kind')!='reviewed-framewise-face-swap' or profile.get('app_id') not in APP_NODES:
        raise ValueError('Unknown face-swap application')
    image_node,video_node,controls=APP_NODES[profile['app_id']]
    return [{'nodeId':image_node,'fieldName':'image','fieldValue':image},
            {'nodeId':video_node,'fieldName':'video','fieldValue':video}]+[
            {'nodeId':video_node,'fieldName':name,'fieldValue':value} for name,value in controls]

def preflight(project,entry):
    project=Path(project)
    actual=factory.compute_input_digest(project,entry)
    if actual!=entry['input_digest']:
        raise ValueError('Face-swap inputs changed')
    semantic=entry.get('semantic_review',{})
    if semantic.get('status')!='passed' or semantic.get('input_digest')!=actual or not semantic.get('notes'):
        raise ValueError('Face-swap input semantics not reviewed')
    job=factory.read(project/'job.json')
    if entry.get('production_authorized') is not True or job.get('production_authorized') is not True:
        raise ValueError('No current production authorization')
    profile=entry['profile']
    if factory.generation_profile(profile)!=factory.generation_profile(job['profile']):
        raise ValueError('Face-swap queue/profile mismatch')
    nodes_for(profile,'image-placeholder','video-placeholder')
    if len(entry['reference_paths'])!=1 or entry['reference_paths'][0]!=job['references']['A']['path']:
        raise ValueError('Exactly one current identity photograph required')
    window=entry['source_contract']['window']
    factory.validate_generation_duration({**window,'generation_frames':entry['generation_frames'],'duration':entry['duration']})
    if Fraction(window['generation_fps'])!=24:
        raise ValueError('This measured route currently supports explicit24fps input only')
    for name in ('video_path','prompt_path'):
        rh.asset(project,entry[name])
    for name in entry['reference_paths']:
        rh.asset(project,name)
    schema_path=rh.asset(project,profile['schema_path'])
    if rh.sha(schema_path)!=profile['schema_sha256']:
        raise ValueError('Face-swap schema snapshot changed')
    schema=factory.read(schema_path)
    if str(schema.get('code')) not in ('0','200'):
        raise ValueError('Face-swap schema did not succeed')
    live={(n['nodeId'],n['fieldName']):n for n in schema['data']['nodeInfoList']}
    for node in nodes_for(profile,'image-placeholder','video-placeholder'):
        if (node['nodeId'],node['fieldName']) not in live:
            raise ValueError('Requested face-swap field not published')
    if entry['source_projection_review'].get('status')!='passed' or entry['source_projection_review'].get('input_digest')!=actual:
        raise ValueError('Face-swap frame ownership/input clock not reviewed')
    input_report=factory.read(project/'derived_inputs/current-input-proof.json')
    if input_report['input_signature']!=factory.file_signature(project,entry['video_path']):
        raise ValueError('Face-swap complete input report is stale')
    video=input_report['report']['video']
    if video['frame_count']!=entry['generation_frames'] or video['fps']!='24' or Fraction(video['end_time'])>10:
        raise ValueError('Actual complete face-swap input frame clock invalid')
    return actual

class ScalarTransport(rh.Transport):
    @staticmethod
    def _verify_download(temp):
        ffmpeg = os.environ.get('VIDEO_FACTORY_FFMPEG') or shutil.which('ffmpeg')
        if not ffmpeg:
            raise ValueError('ffmpeg is required for strict complete decode')
        command=[ffmpeg,'-nostdin','-v','error','-xerror','-cpuflags','0','-threads','1',
                 '-i',str(temp),'-map','0:v:0','-threads','1','-f','null','-']
        check=subprocess.run(command,capture_output=True,timeout=120)
        if check.returncode or check.stderr.strip():
            raise ValueError('Face-swap result failed strict scalar complete decode; .part preserved')

class Runner(rh.Runner):
    def submit_one(self,project,entry,validator=None):
        validator=validator or preflight
        digest=validator(project,entry)
        account=self.ledger.account(self.account)
        if account['stopped'] or account['cooldown']>self.ledger.clock():
            return None
        refs=[self.upload(rh.asset(project,p),'reviewed-framewise-face-swap') for p in entry['reference_paths']]
        video=self.upload(rh.asset(project,entry['video_path']),'reviewed-framewise-face-swap')
        profile=entry['profile']
        path='/openapi/v2/run/ai-app/'+profile['app_id']
        body={'nodeInfoList':nodes_for(profile,refs[0]['filename'],video['filename']),
              'instanceType':profile.get('instance_type','default'),'usePersonalQueue':False}
        if validator(project,entry)!=digest:
            raise ValueError('Face-swap assets changed during upload')
        aid=self.ledger.reserve(self.account,project,entry['window_id'],digest,entry.get('retry_after'))
        if not aid:
            return None
        self.ledger.update(aid,request=json.dumps({'path':path,'body':body},ensure_ascii=False))
        try:
            result=self.transport.submit(path,body)
        except rh.ApiFailure as error:
            if error.code in ('400','401','402','403','1913','1014')+rh.DEFERRED_CODES:
                self.submission_rejected(aid,error.code)
            else:
                self.ledger.update(aid,state='SUBMISSION_UNKNOWN')
            return aid
        except Exception:
            self.ledger.update(aid,state='SUBMISSION_UNKNOWN')
            return aid
        task=result.get('taskId') or (result.get('data') or {}).get('taskId')
        if not task:
            code=result.get('errorCode') or result.get('code')
            if str(code) in ('400','401','402','403','1913','1014')+rh.DEFERRED_CODES:
                self.submission_rejected(aid,code)
                self.ledger.update(aid,receipt=json.dumps(result))
            else:
                self.ledger.update(aid,state='SUBMISSION_UNKNOWN',receipt=json.dumps(result))
            return aid
        self.ledger.update(aid,task_id=str(task),receipt=json.dumps(result),state='RUNNING',next_poll=self.ledger.clock()+5)
        return aid

    def tick_one_pilot(self,project,entry):
        rows=self.ledger.db.execute('SELECT id FROM attempts WHERE account=? AND state IN (?,?,?,?,?,?) ORDER BY created',
                                   (self.account,*rh.POLLABLE)).fetchall()
        for row in rows:
            self.recover(row['id'])
        preflight(project,entry)
        if entry.get('pilot') is not True:
            raise ValueError('Prototype permits only isolated representative pilots')
        prior=self.ledger.db.execute('SELECT window FROM attempts WHERE account=? AND project=? AND window<>?',
                                    (self.account,str(Path(project).resolve()),entry['window_id'])).fetchone()
        if prior:
            raise ValueError('New app cannot expand before actual pilot qualification')
        self.submit_one(project,entry)
        return self.ledger.summary(self.account)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=('tick-pilot',))
    ap.add_argument('--project',type=Path,required=True)
    ap.add_argument('--ledger',type=Path,default=rh.default_private()/'tasks.sqlite')
    ap.add_argument('--key-file',type=Path,default=rh.default_key());ap.add_argument('--account',default='default')
    args=ap.parse_args()
    queue=factory.read(args.project/'queue.json')
    if len(queue['jobs'])!=1:raise ValueError('Each isolated correction pilot has exactly one approved job')
    led=rh.Ledger(args.ledger)
    try:
        key=args.key_file.read_text(encoding='utf8').strip()
        result=Runner(led,ScalarTransport(key),args.account).tick_one_pilot(args.project,queue['jobs'][0])
        print(json.dumps(result,ensure_ascii=False))
    finally:led.close()

if __name__=='__main__':main()

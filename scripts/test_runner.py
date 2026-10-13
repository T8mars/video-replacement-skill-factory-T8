"""Offline behavioral tests. Network access is forbidden in this process."""
from pathlib import Path
import concurrent.futures
import io
import json
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from rh_runner import Ledger, Runner, Transport, ApiFailure, sha, preflight, OBSERVED_RESULT_HOST, legacy_resolution_nodes, same_profile_route


class Clock:
    now = 1000.0
    def __call__(self):
        return self.now


class Fake:
    def __init__(self, submit_result=None, queries=None, download_fails=0):
        self.submits = 0
        self.queries = 0
        self.downloads = 0
        self.result = submit_result or {'taskId': 'task1', 'status': 'RUNNING'}
        self.query_results = queries or [{'taskId': 'task1', 'status': 'RUNNING'}]
        self.download_fails = download_fails
    def upload(self, path):
        return {'filename': sha(path), 'url': 'https://example.test/input?PRIVATE_SENTINEL'}
    def submit(self, path, body):
        self.submits += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
    def query(self, task_id):
        self.queries += 1
        r = self.query_results[min(self.queries - 1, len(self.query_results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    def download(self, url, dest):
        self.downloads += 1
        if self.downloads <= self.download_fails:
            raise TimeoutError('PRIVATE_SENTINEL')
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b'offline fake output; media correctness is tested separately')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        for name in ('video.mp4', 'person.png', 'prompt.txt'):
            (self.project / name).write_text(name)
        (self.project/'job.json').write_text(json.dumps({'production_authorized':True}))
        self.clock = Clock()
        self.ledger = Ledger(self.root / 'private' / 'tasks.sqlite', self.clock)
        self.entry = {'window_id': 'w001', 'input_digest': 'input1', 'video_path': 'video.mp4',
                      'reference_paths': ['person.png'], 'prompt_path': 'prompt.txt', 'generation_frames': 22,
                      'duration':'11/12','source_contract':{'window':{'generation_fps':'24'}},
                      'profile': {'kind': 'legacy-ai-app', 'app_id': '2107844752011010050', 'quality_evidence': 'case-verified'}}
        self.validate = lambda p, e: e['input_digest']
    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()
    def submit(self, fake):
        return Runner(self.ledger, fake).submit_one(self.project, self.entry, self.validate)
    def test_explicit_portrait_resolution_reaches_immutable_request(self):
        self.entry['profile']['resolution_selector']={'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2}
        aid=self.submit(Fake())
        request=json.loads(self.ledger.row(aid)['request'])
        nodes=request['body']['nodeInfoList']
        self.assertIn({'nodeId':'115','fieldName':'aspect_ratio','fieldValue':'9:16 (Portrait Widescreen)'},nodes)
        self.assertIn({'nodeId':'115','fieldName':'megapixels','fieldValue':'2'},nodes)
        self.assertEqual(next(n['fieldValue'] for n in nodes if n['nodeId']=='326'),'22')
    def test_standard_preflight_never_accepts_duration_above_ten_seconds(self):
        self.entry.update(duration='10',generation_frames=240,profile={'kind':'standard-h3'},production_authorized=True,
                          source_contract={'window':{'generation_fps':'24'}},
                          semantic_review={'status':'passed','input_digest':'input1'})
        with patch('factory.compute_input_digest',return_value='input1'):
            self.assertEqual(preflight(self.project,self.entry),'input1')
            self.entry.update(duration='11',generation_frames=264)
            with self.assertRaisesRegex(ValueError,'10-second production cap'):preflight(self.project,self.entry)
    def test_legacy_preflight_checks_actual_frame_clock_not_only_declared_seconds(self):
        self.entry.update(duration='4',generation_frames=22,production_authorized=True,
                          source_contract={'window':{'generation_fps':'1'}},
                          semantic_review={'status':'passed','input_digest':'input1'})
        with patch('factory.compute_input_digest',return_value='input1'):
            with self.assertRaisesRegex(ValueError,'10-second production cap'):preflight(self.project,self.entry)
    def test_unset_resolution_preserves_existing_request(self):
        aid=self.submit(Fake())
        request=json.loads(self.ledger.row(aid)['request'])
        self.assertFalse(any(n['nodeId']=='115' for n in request['body']['nodeInfoList']))
        self.assertTrue(same_profile_route(self.entry['profile'],self.ledger.row(aid)['request']))
    def test_both_published_aspects_reach_immutable_request(self):
        self.entry['profile']['resolution_selector']={'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2,'aspect_node_ids':['115','214']}
        aid=self.submit(Fake());raw=self.ledger.row(aid)['request'];nodes=json.loads(raw)['body']['nodeInfoList']
        self.assertEqual({n['nodeId'] for n in nodes if n['fieldName']=='aspect_ratio'},{'115','214'})
        self.assertTrue(all(n['fieldValue']=='9:16 (Portrait Widescreen)' for n in nodes if n['fieldName']=='aspect_ratio'))
        self.assertTrue(same_profile_route(self.entry['profile'],raw))
        self.entry['profile']['resolution_selector'].pop('aspect_node_ids')
        self.assertFalse(same_profile_route(self.entry['profile'],raw))
    def test_new_second_selector_does_not_inherit_first_selector_pilot(self):
        self.entry['profile']['resolution_selector']={'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2}
        aid=self.submit(Fake());raw=self.ledger.row(aid)['request']
        self.entry['profile']['resolution_selector']['aspect_node_ids']=['214','115']
        self.assertFalse(same_profile_route(self.entry['profile'],raw))
    def test_invalid_aspect_node_lists_never_submit(self):
        for ids in ([],['214'],['115','115'],['115','arbitrary'],'115',[115],['115','214','214']):
            with self.subTest(ids=ids):
                self.entry['profile']['resolution_selector']={'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2,'aspect_node_ids':ids}
                fake=Fake()
                with self.assertRaises(ValueError):self.submit(fake)
                self.assertEqual(fake.submits,0)
    def test_two_image_app_published_selectors_and_reference_order(self):
        self.entry['profile'].update({'app_id':'2107845954833907713','resolution_selector':{'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2,'aspect_node_ids':['115','214']}})
        (self.project/'second.png').write_text('distinct second expression of the same person')
        self.entry['reference_paths']=['person.png','second.png']
        aid=self.submit(Fake());raw=self.ledger.row(aid)['request'];request=json.loads(raw)
        self.assertEqual(request['path'],'/openapi/v2/run/ai-app/2107845954833907713')
        nodes=request['body']['nodeInfoList']
        self.assertEqual({n['nodeId'] for n in nodes if n['fieldName']=='aspect_ratio'},{'115','214'})
        self.assertEqual(next(n['fieldValue'] for n in nodes if n['nodeId']=='137'),sha(self.project/'person.png'))
        self.assertEqual(next(n['fieldValue'] for n in nodes if n['nodeId']=='139'),sha(self.project/'second.png'))
        self.assertTrue(same_profile_route(self.entry['profile'],raw))
        old_route={**self.entry['profile'],'app_id':'2107844752011010050'}
        self.assertFalse(same_profile_route(old_route,raw))
    def test_second_selector_still_rejects_unrecognized_application(self):
        self.entry['profile'].update({'app_id':'unreviewed-app','resolution_selector':{'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2,'aspect_node_ids':['115','214']}})
        fake=Fake()
        with self.assertRaisesRegex(ValueError,'only documented'):self.submit(fake)
        self.assertEqual(fake.submits,0)
    def test_resolution_route_does_not_inherit_landscape_pilot(self):
        aid=self.submit(Fake());request=self.ledger.row(aid)['request']
        self.entry['profile']['resolution_selector']={'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2}
        self.assertFalse(same_profile_route(self.entry['profile'],request))
    def test_invalid_resolution_never_submits(self):
        for value in ({'aspect_ratio':'9:16','megapixels':2},{'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':True},
                      {'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':float('nan')},
                      {'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':17},
                      {'aspect_ratio':'9:16 (Portrait Widescreen)'},[],
                      {'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2,'nodeId':'arbitrary'}):
            with self.subTest(value=value):
                self.entry['profile']['resolution_selector']=value;fake=Fake()
                with self.assertRaises(ValueError):self.submit(fake)
                self.assertEqual(fake.submits,0)
    def test_timeout_is_unknown_and_never_resubmits_after_restart(self):
        # Existing recovery contract remains unchanged by optional canvas settings.
        fake = Fake(TimeoutError('request may have been accepted'))
        aid = self.submit(fake)
        self.assertEqual(self.ledger.row(aid)['state'], 'SUBMISSION_UNKNOWN')
        self.assertEqual(self.ledger.summary('default')['active'], 1)
        self.assertIsNone(self.submit(fake))
        self.ledger.close()
        self.ledger = Ledger(self.root / 'private' / 'tasks.sqlite', self.clock)
        self.assertIsNone(self.submit(fake))
        self.assertEqual(fake.submits, 1)
    def test_portrait_without_selector_is_rejected_before_submit(self):
        self.entry['source_contract']={'window':{'source_projection':{'display_canvas':{'width':720,'height':1280,'sample_aspect_ratio':'1'}}}}
        fake=Fake()
        with self.assertRaisesRegex(ValueError,'Portrait source'):self.submit(fake)
        self.assertEqual(fake.submits,0)
    def test_source_matching_portrait_selector_can_submit(self):
        self.entry['source_contract']={'window':{'source_projection':{'display_canvas':{'width':720,'height':1280,'sample_aspect_ratio':'1'}}}}
        self.entry['profile']['resolution_selector']={'aspect_ratio':'9:16 (Portrait Widescreen)','megapixels':2}
        fake=Fake();self.assertIsNotNone(self.submit(fake));self.assertEqual(fake.submits,1)
    def test_explicit_wrong_aspect_is_rejected_before_submit(self):
        self.entry['source_contract']={'window':{'source_projection':{'display_canvas':{'width':720,'height':1280,'sample_aspect_ratio':'1'}}}}
        self.entry['profile']['resolution_selector']={'aspect_ratio':'16:9 (Widescreen)','megapixels':2}
        fake=Fake()
        with self.assertRaisesRegex(ValueError,'differs'):self.submit(fake)
        self.assertEqual(fake.submits,0)
    def test_http_500_is_also_uncertain(self):
        aid = self.submit(Fake(ApiFailure(500)))
        self.assertEqual(self.ledger.row(aid)['state'], 'SUBMISSION_UNKNOWN')
    def test_success_without_results_only_queries(self):
        fake = Fake(queries=[{'status': 'SUCCESS', 'results': None}])
        aid = self.submit(fake)
        runner = Runner(self.ledger, fake)
        for _ in range(4):
            self.clock.now += 60
            runner.recover(aid)
        self.assertEqual(self.ledger.row(aid)['state'], 'RESULTS_MISSING')
        self.assertEqual(self.ledger.summary('default')['active'], 0)
        self.assertIsNone(self.submit(fake))
        self.assertEqual(fake.submits, 1)
        self.assertEqual(fake.queries, 3)
    def test_failed_download_recovers_same_task(self):
        fake = Fake(queries=[{'status': 'SUCCESS', 'results': [{'url': 'https://example.test/output.mp4?PRIVATE_SENTINEL', 'outputType': 'mp4'}]}], download_fails=1)
        aid = self.submit(fake)
        self.clock.now += 60
        Runner(self.ledger, fake).recover(aid)
        self.assertEqual(self.ledger.row(aid)['state'], 'DOWNLOAD_FAILED')
        self.clock.now += 60
        Runner(self.ledger, fake).recover(aid)
        self.assertEqual(self.ledger.row(aid)['state'], 'DOWNLOADED')
        self.assertEqual(fake.submits, 1)
        self.assertEqual(fake.downloads, 2)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(self.ledger.summary('default')))
    def test_cross_project_concurrency_and_restart_no_lease_release(self):
        ids = [self.ledger.reserve('default', self.root / ('project' + str(i)), 'w', 'd') for i in range(4)]
        self.assertEqual(sum(x is not None for x in ids), 3)
        self.clock.now += 100000
        self.ledger.close()
        self.ledger = Ledger(self.root / 'private' / 'tasks.sqlite', self.clock)
        self.assertIsNone(self.ledger.reserve('default', self.root / 'newproject', 'w', 'd'))
        self.assertEqual(self.ledger.summary('default')['active'], 3)
    def test_sqlite_concurrent_workers_do_not_exceed_three(self):
        self.ledger.account('default')
        def reserve(i):
            l = Ledger(self.root / 'private' / 'tasks.sqlite', self.clock)
            try:
                return l.reserve('default', self.root / str(i), 'w', 'd')
            finally:
                l.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(x is not None for x in ids), 3)
    def test_separate_processes_share_the_same_global_cap(self):
        self.ledger.account('default')
        code = 'from rh_runner import Ledger; import sys; l=Ledger(sys.argv[1]); print(bool(l.reserve("default",sys.argv[2],"w","d"))); l.close()'
        children = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code,
                       str(self.root / 'private' / 'tasks.sqlite'), str(self.root / ('process-' + str(i)))],
                       cwd=str(Path(__file__).parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for i in range(6)]
        results = []
        for child in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, err)
            results.append(out.strip())
        self.assertEqual(results.count('True'), 3)
        self.assertEqual(self.ledger.summary('default')['active'], 3)
    def test_production_authorization_is_required_before_any_submit(self):
        entry = dict(self.entry, input_digest='digest', semantic_review={'status':'passed','input_digest':'digest'}, production_authorized=False)
        fake = Fake()
        with patch('factory.compute_input_digest', return_value='digest'):
            with self.assertRaises(ValueError):
                Runner(self.ledger, fake).tick(self.project, {'schema_version':1, 'jobs':[entry]})
        self.assertEqual(fake.submits, 0)
        self.assertEqual(self.ledger.summary('default')['attempts'], [])
    def test_projection_review_is_independent_of_semantic_review(self):
        entry = dict(self.entry, input_digest='digest', semantic_review={'status':'passed','input_digest':'digest'}, production_authorized=True,
                     source_contract={'window':{'generation_fps':'24','source_projection':{'requires_temporal_review':True}}})
        with patch('factory.compute_input_digest', return_value='digest'):
            with self.assertRaises(ValueError):
                preflight(self.project, entry)
            entry['source_projection_review']={'status':'passed','input_digest':'digest','loss_disclosed':True}
            self.assertEqual(preflight(self.project, entry), 'digest')
    def test_current_job_revocation_blocks_stale_queue_authorization(self):
        entry=dict(self.entry,input_digest='digest',semantic_review={'status':'passed','input_digest':'digest'},production_authorized=True)
        (self.project/'job.json').write_text(json.dumps({'production_authorized':False}))
        with patch('factory.compute_input_digest',return_value='digest'):
            with self.assertRaises(ValueError):
                preflight(self.project,entry)
    def test_passthrough_never_creates_a_cloud_attempt(self):
        fake=Fake()
        Runner(self.ledger,fake).tick(self.project,{'schema_version':1,'jobs':[{'passthrough':True,'window_id':'empty'}]})
        self.assertEqual(fake.submits,0)
        self.assertEqual(self.ledger.summary('default')['attempts'],[])
    def test_each_backend_route_gets_one_pilot_and_no_unreviewed_batch(self):
        fake=Fake()
        first=dict(self.entry,pilot=True,production_authorized=True)
        first['profile']=dict(first['profile'],quality_evidence='documented-only')
        second=dict(first,window_id='w002')
        dual=dict(first,window_id='w003',reference_paths=['person.png','person.png'])
        dual['profile']=dict(first['profile'],app_id='2107845954833907713')
        for entry in (first,second,dual):
            entry['input_digest']=entry['window_id']
        with patch('rh_runner.preflight',side_effect=lambda p,e:e['input_digest']):
            Runner(self.ledger,fake).tick(self.project,{'jobs':[first,second,dual]})
        self.assertEqual(fake.submits,2)
        windows={r['window'] for r in self.ledger.summary('default')['attempts']}
        self.assertEqual(windows,{'w001','w003'})
    def test_three_isolated_repair_pilots_fill_one_shared_account_and_resume_ids(self):
        class UniqueTasks(Fake):
            def submit(self,path,body):
                self.submits+=1
                return {'taskId':'repair-'+str(self.submits),'status':'RUNNING'}
        fake=UniqueTasks()
        runner=Runner(self.ledger,fake)
        repairs=[]
        for i in range(4):
            project=self.root/('repair-hypothesis-'+str(i))
            project.mkdir()
            for name in ('video.mp4','person.png','prompt.txt'):
                (project/name).write_text(name+' hypothesis '+str(i))
            (project/'job.json').write_text(json.dumps({'production_authorized':True}))
            entry=dict(self.entry,input_digest='hypothesis-'+str(i),pilot=True,production_authorized=True)
            entry['profile']=dict(entry['profile'],quality_evidence='documented-only')
            repairs.append((project,{'jobs':[entry]}))
        with patch('rh_runner.preflight',side_effect=lambda p,e:e['input_digest']):
            for project,queue in repairs:runner.tick(project,queue)
            self.assertEqual(fake.submits,3)
            first=self.ledger.summary('default')
            self.assertEqual(first['active'],3)
            self.assertEqual({r['task_id'] for r in first['attempts']},{'repair-1','repair-2','repair-3'})
            self.clock.now+=60
            for project,queue in repairs:runner.tick(project,queue)
            self.assertEqual(fake.submits,3)
            self.assertGreaterEqual(fake.queries,3)
            self.assertEqual({r['task_id'] for r in self.ledger.summary('default')['attempts']},
                             {'repair-1','repair-2','repair-3'})
            self.ledger.update(first['attempts'][0]['id'],state='EXECUTION_FAILED')
            runner.tick(*repairs[3])
        self.assertEqual(fake.submits,4)
        current=self.ledger.summary('default')
        self.assertEqual(current['active'],3)
        self.assertEqual({r['task_id'] for r in current['attempts']},
                         {'repair-1','repair-2','repair-3','repair-4'})
    def test_401_stops_account_immediately(self):
        fake = Fake(ApiFailure(401))
        aid = self.submit(fake)
        self.assertEqual(self.ledger.row(aid)['state'], 'EXECUTION_FAILED')
        self.assertTrue(self.ledger.summary('default')['stopped'])
        self.entry['window_id'] = 'w002'
        self.assertIsNone(self.submit(fake))
        self.assertEqual(fake.submits, 1)
    def test_model_key_denial_is_terminal_and_blocks_other_model_windows(self):
        for rejection in ({'code':1014}, ApiFailure(1014)):
            with self.subTest(rejection=str(rejection)):
                fake=Fake(rejection)
                fake.key='test-key-'+str(type(rejection))
                self.entry.update(window_id='denied-model-'+str(type(rejection)),duration='5',profile={'kind':'standard-h3'})
                aid=self.submit(fake)
                self.assertEqual(self.ledger.row(aid)['state'],'EXECUTION_FAILED')
                self.assertEqual(self.ledger.summary('default')['active'],0)
                self.entry['window_id']='new-model-window-'+str(type(rejection))
                self.assertIsNone(self.submit(fake))
                self.assertEqual(fake.submits,1)
                self.assertFalse(self.ledger.summary('default')['stopped'])
                self.ledger.close()
                self.ledger=Ledger(self.root/'private'/'tasks.sqlite',self.clock)
                self.assertIsNone(self.submit(fake))
                self.entry.update(window_id='legacy-'+str(type(rejection)),profile={'kind':'legacy-ai-app','app_id':'2107844752011010050'})
                fake.result={'taskId':'legacy-task','status':'RUNNING'}
                legacy_aid=self.submit(fake)
                self.assertIsNotNone(legacy_aid)
                self.ledger.update(legacy_aid,state='DOWNLOADED')
    def test_421_cooldown_is_shared_persistent_and_poll_continues(self):
        existing = self.ledger.reserve('default', self.project, 'old', 'old')
        self.ledger.update(existing, state='RUNNING', task_id='existing')
        fake = Fake(ApiFailure(421))
        self.submit(fake)
        self.entry['window_id'] = 'next'
        self.assertIsNone(self.submit(fake))
        self.assertGreater(self.ledger.summary('default')['cooldown_remaining'], 0)
        Runner(self.ledger, fake).recover(existing)
        self.assertEqual(fake.queries, 1)
        self.ledger.close()
        self.ledger = Ledger(self.root / 'private' / 'tasks.sqlite', self.clock)
        self.assertIsNone(self.submit(fake))
    def test_three_execution_failures_stop_this_window(self):
        fake = Fake(ApiFailure(400))
        for _ in range(3):
            self.assertIsNotNone(self.submit(fake))
        self.assertIsNone(self.submit(fake))
        self.assertEqual(fake.submits, 3)
    def test_explicit_queue_busy_rejections_wait_past_three_and_resume(self):
        for code in ('415','421','429','1003','1010','1011'):
            for response in ({'code':code},ApiFailure(code)):
                self.entry['window_id']='busy-'+code+'-'+str(type(response))
                fake=Fake(response)
                for _ in range(4):
                    aid=self.submit(fake)
                    self.assertEqual(self.ledger.row(aid)['state'],'DEFERRED')
                    self.assertEqual(self.ledger.summary('default')['active'],0)
                    self.assertFalse(self.ledger.summary('default')['stopped'])
                    self.assertIsNone(self.submit(fake))
                    self.clock.now+=65
                self.assertIsNone(self.ledger.db.execute('SELECT count FROM failures WHERE account=? AND project=? AND window=?',
                                  ('default',str(self.project.resolve()),self.entry['window_id'])).fetchone())
                fake.result={'taskId':'resumed-task','status':'RUNNING'}
                aid=self.submit(fake)
                self.assertEqual(fake.submits,5)
                self.assertEqual(self.ledger.row(aid)['task_id'],'resumed-task')
                self.ledger.update(aid,state='DOWNLOADED')
    def test_known_task_busy_queries_keep_same_task_without_resubmission(self):
        fake=Fake(queries=[ApiFailure(1011)]*4+[{'taskId':'task1','status':'SUCCESS','results':[{'url':'https://example.test/result.mp4','outputType':'mp4'}]}])
        aid=self.submit(fake)
        for _ in range(5):
            self.clock.now+=65
            Runner(self.ledger,fake).recover(aid)
        self.assertEqual(fake.submits,1)
        self.assertEqual(fake.queries,5)
        self.assertEqual(self.ledger.row(aid)['state'],'DOWNLOADED')
        self.assertEqual(self.ledger.row(aid)['query_errors'],0)
    def test_known_failed_busy_receipts_wait_on_original_task_through_restart(self):
        for code in ('415','421','429','1003','1010','1011'):
            for field in ('errorCode','code'):
                with self.subTest(code=code,field=field):
                    self.entry['window_id']='known-failed-busy-'+code+'-'+field
                    task_id='accepted-'+self.entry['window_id']
                    busy={'taskId':task_id,'status':'FAILED',field:code,'detail':'PRIVATE_SENTINEL'}
                    success={'taskId':task_id,'status':'SUCCESS','results':[{'url':'https://example.test/result.mp4','outputType':'mp4'}]}
                    class TracedFake(Fake):
                        def query(inner,tid):
                            inner.queried_ids.append(tid)
                            return super().query(tid)
                    fake=TracedFake({'taskId':task_id,'status':'RUNNING'},queries=[busy]*4+[success])
                    fake.queried_ids=[]
                    aid=self.submit(fake)
                    try:
                        self.clock.now+=5
                        for n in range(4):
                            if n:self.clock.now+=1
                            Runner(self.ledger,fake).recover(aid)
                            row=self.ledger.row(aid)
                            self.assertEqual(row['state'],'RUNNING')
                            self.assertEqual(row['task_id'],task_id)
                            self.assertEqual(json.loads(row['receipt']),busy)
                            self.assertEqual(row['next_poll'],self.clock.now+60)
                            self.assertEqual(row['query_errors'],0)
                            self.assertEqual(self.ledger.summary('default')['active'],1)
                            self.assertGreaterEqual(self.ledger.summary('default')['cooldown_remaining'],60)
                            self.assertIsNone(self.submit(fake))
                            before=fake.queries
                            self.clock.now+=59
                            Runner(self.ledger,fake).recover(aid)
                            self.assertEqual(fake.queries,before)
                            if n==1:
                                self.ledger.close()
                                self.ledger=Ledger(self.root/'private'/'tasks.sqlite',self.clock)
                                self.assertEqual(self.ledger.row(aid)['next_poll'],self.clock.now+1)
                                self.assertEqual(self.ledger.summary('default')['active'],1)
                                self.assertGreater(self.ledger.summary('default')['cooldown_remaining'],0)
                        self.assertIsNone(self.ledger.db.execute('SELECT count FROM failures WHERE account=? AND project=? AND window=?',
                                          ('default',str(self.project.resolve()),self.entry['window_id'])).fetchone())
                        self.clock.now+=1
                        Runner(self.ledger,fake).recover(aid)
                        self.assertEqual(self.ledger.row(aid)['state'],'DOWNLOADED')
                        self.assertEqual(fake.submits,1)
                        self.assertEqual(fake.queries,5)
                        self.assertEqual(fake.downloads,1)
                        self.assertEqual(fake.queried_ids,[task_id]*5)
                        self.assertIsNone(self.submit(fake))
                        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.ledger.summary('default')))
                    finally:
                        # Each subcase has its own fixture window; old-core negative runs also release it.
                        if aid:self.ledger.update(aid,state='DOWNLOADED')
    def test_known_busy_query_exceptions_keep_receipt_state_and_next_poll(self):
        for code in ('415','421','429','1003','1010','1011'):
            with self.subTest(code=code):
                self.entry['window_id']='known-query-exception-'+code
                fake=Fake(queries=[ApiFailure(code)])
                aid=self.submit(fake)
                self.ledger.update(aid,state='QUEUED')
                receipt=self.ledger.row(aid)['receipt']
                self.clock.now+=5
                Runner(self.ledger,fake).recover(aid)
                row=self.ledger.row(aid)
                self.assertEqual(row['state'],'QUEUED')
                self.assertEqual(row['task_id'],'task1')
                self.assertEqual(row['receipt'],receipt)
                self.assertEqual(row['next_poll'],self.clock.now+60)
                self.assertEqual(row['query_errors'],0)
                event=self.ledger.db.execute("SELECT data FROM events WHERE attempt=? AND kind='QUERY_DEFERRED' ORDER BY id DESC LIMIT 1",(aid,)).fetchone()
                self.assertEqual(json.loads(event['data']),{'code':code})
                self.assertEqual(self.ledger.summary('default')['active'],1)
                self.assertIsNone(self.submit(fake))
                self.ledger.update(aid,state='DOWNLOADED')
                self.clock.now+=60
    def test_query_busy_cooldown_blocks_new_submits_but_not_other_known_polls(self):
        fake=Fake(queries=[{'taskId':'task1','code':'1003','detail':'PRIVATE_SENTINEL'},
                           {'taskId':'known-other','status':'RUNNING'}])
        aid=self.submit(fake)
        other=self.ledger.reserve('default',self.root/'other-project','other-window','other-digest')
        self.ledger.update(other,state='QUEUED',task_id='known-other',next_poll=0)
        self.clock.now+=5
        Runner(self.ledger,fake).recover(aid)
        self.assertEqual(json.loads(self.ledger.row(aid)['receipt']),fake.query_results[0])
        self.entry['window_id']='new-window-during-busy'
        self.assertIsNone(self.submit(fake))
        Runner(self.ledger,fake).recover(other)
        self.assertEqual(fake.queries,2)
        self.assertEqual(fake.submits,1)
        self.assertEqual(self.ledger.row(other)['state'],'RUNNING')
        self.assertEqual(self.ledger.summary('default')['active'],2)
        self.ledger.close()
        self.ledger=Ledger(self.root/'private'/'tasks.sqlite',self.clock)
        self.assertGreater(self.ledger.summary('default')['cooldown_remaining'],0)
        Runner(self.ledger,fake).recover(aid)
        self.assertEqual(fake.queries,2)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.ledger.summary('default')))
    def test_defer_submission_rejects_an_already_accepted_task(self):
        fake=Fake()
        aid=self.submit(fake)
        before=self.ledger.row(aid)
        with self.assertRaisesRegex(ValueError,'accepted task'):
            self.ledger.defer_submission(aid,'421')
        after=self.ledger.row(aid)
        self.assertEqual(after,before)
        self.assertEqual(self.ledger.summary('default')['active'],1)
        self.assertEqual(self.ledger.summary('default')['cooldown_remaining'],0)
        self.assertEqual(fake.submits,1)
    def test_known_nonbusy_failure_remains_terminal_execution_failure(self):
        for code in ('400','401'):
            self.entry['window_id']='known-terminal-'+code
            fake=Fake(queries=[{'taskId':'task1','status':'FAILED','errorCode':code}])
            aid=self.submit(fake)
            self.clock.now+=5
            Runner(self.ledger,fake).recover(aid)
            self.assertEqual(self.ledger.row(aid)['state'],'EXECUTION_FAILED')
            self.assertEqual(json.loads(self.ledger.row(aid)['receipt']),fake.query_results[0])
            self.assertEqual(self.ledger.summary('default')['active'],0)
            self.assertEqual(self.ledger.summary('default')['stopped'],code=='401')
            self.assertEqual(fake.submits,1)
    def test_busy_upload_waits_without_reserving_or_counting_execution_failures(self):
        class BusyUpload(Fake):
            upload_calls=0
            def upload(self,path):
                self.upload_calls+=1
                if self.upload_calls<=4:raise ApiFailure(1011)
                return super().upload(path)
        fake=BusyUpload()
        entry=dict(self.entry,production_authorized=True,semantic_review={'status':'passed','input_digest':'input1'})
        with patch('factory.compute_input_digest',return_value='input1'):
            for _ in range(4):
                Runner(self.ledger,fake).tick(self.project,{'jobs':[entry]})
                self.assertEqual(self.ledger.summary('default')['attempts'],[])
                self.clock.now+=65
            Runner(self.ledger,fake).tick(self.project,{'jobs':[entry]})
        self.assertEqual(fake.submits,1)
        self.assertEqual(self.ledger.summary('default')['active'],1)
    def test_accepted_queue_codes_without_task_id_never_trigger_a_new_post(self):
        for code in ('804','813'):
            self.entry['window_id']='accepted-'+code
            fake=Fake({'code':code})
            aid=self.submit(fake)
            self.assertEqual(self.ledger.row(aid)['state'],'SUBMISSION_UNKNOWN')
            self.clock.now+=600
            self.assertIsNone(self.submit(fake))
            self.assertEqual(fake.submits,1)
    def test_quality_failures_have_no_three_attempt_limit(self):
        fake = Fake()
        for n in range(5):
            self.entry['input_digest'] = 'revised-input-' + str(n)
            aid = self.submit(fake)
            self.assertIsNotNone(aid)
            self.ledger.update(aid, state='VISUAL_FAILED')
            self.clock.now += 1
        self.assertEqual(fake.submits, 5)
    def test_stop_persists_but_known_tasks_can_recover(self):
        fake = Fake()
        aid = self.submit(fake)
        self.ledger.stop('default')
        self.clock.now += 60
        Runner(self.ledger, fake).recover(aid)
        self.assertEqual(fake.queries, 1)
        self.entry['window_id'] = 'next'
        self.assertIsNone(self.submit(fake))
    def test_changed_input_cannot_mix_with_running_window(self):
        fake = Fake()
        self.submit(fake)
        self.entry['input_digest'] = 'changed-ref'
        self.assertIsNone(self.submit(fake))
        self.assertEqual(fake.submits, 1)


class DownloadTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __init__(self, body):
            super().__init__(body)
            self.headers={'Content-Length':str(len(body))}

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.output=Path(self.tmp.name)/'candidate.mp4'
        self.url='https://'+OBSERVED_RESULT_HOST+'/result.mp4?private-result-token'
        self.transport=Transport('private-key-sentinel')

    def tearDown(self):
        self.tmp.cleanup()

    def answer(self, *addresses):
        return self.Response(json.dumps({'Answer':[{'type':1,'data':x} for x in addresses]}).encode())

    def test_normal_https_download_decodes_before_atomic_promotion(self):
        with patch('rh_runner.urllib.request.urlopen',return_value=self.Response(b'actual-transfer')) as opened, \
             patch('rh_runner.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as checked:
            self.transport.download(self.url,self.output)
        self.assertEqual(self.output.read_bytes(),b'actual-transfer')
        self.assertEqual(opened.call_count,1)
        self.assertEqual(checked.call_args.args[0][0],'ffmpeg')
        checked_path=Path(checked.call_args.args[0][checked.call_args.args[0].index('-i')+1])
        self.assertEqual(checked_path.parent,self.output.parent)
        self.assertTrue(checked_path.name.startswith(self.output.name+'.'))
        self.assertTrue(checked_path.name.endswith('.part'))
        self.assertFalse(checked_path.exists())

    def test_tls_eof_recovers_with_live_dns_agreement_and_original_https_hostname(self):
        replies=[urllib.error.URLError('TLS EOF'),self.answer('1.1.1.1','8.8.8.8'),self.answer('8.8.8.8')]
        commands=[]
        def invoke(args,**kwargs):
            commands.append(args)
            if args[0]=='curl':
                Path(args[args.index('--output')+1]).write_bytes(b'verified-transfer')
            return subprocess.CompletedProcess(args,0,b'',b'')
        with patch('rh_runner.urllib.request.urlopen',side_effect=replies) as opened, \
             patch('rh_runner.subprocess.run',side_effect=invoke):
            self.transport.download(self.url,self.output)
        self.assertEqual(self.output.read_bytes(),b'verified-transfer')
        curl,decode=commands
        self.assertEqual(curl[-1],self.url)
        self.assertEqual(curl[curl.index('--resolve')+1],OBSERVED_RESULT_HOST+':443:8.8.8.8')
        self.assertEqual(curl[curl.index('--proto')+1],'=https')
        self.assertEqual(curl[curl.index('--proto-redir')+1],'=https')
        self.assertNotIn('--insecure',curl)
        self.assertNotIn('-k',curl)
        self.assertEqual(decode[0],'ffmpeg')
        for call in opened.call_args_list[1:]:
            self.assertNotIn('Authorization',call.args[0].headers)
            self.assertNotIn('private-key-sentinel',str(call))
            self.assertNotIn('private-result-token',str(call))

    def test_dns_disagreement_never_promotes_or_invokes_curl(self):
        with patch('rh_runner.urllib.request.urlopen',side_effect=[urllib.error.URLError('EOF'),self.answer('1.1.1.1'),self.answer('8.8.8.8')]), \
             patch('rh_runner.subprocess.run') as command:
            with self.assertRaisesRegex(ValueError,'did not agree'):
                self.transport.download(self.url,self.output)
        command.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_private_or_other_host_is_not_a_dns_recovery_destination(self):
        with patch('rh_runner.urllib.request.urlopen',side_effect=urllib.error.URLError('EOF')) as opened, \
             patch('rh_runner.subprocess.run') as command:
            with self.assertRaisesRegex(ValueError,'restricted'):
                self.transport.download('https://another.test/result.mp4',self.output)
        self.assertEqual(opened.call_count,1)
        command.assert_not_called()
        with patch('rh_runner.urllib.request.urlopen',side_effect=[urllib.error.URLError('EOF'),self.answer('127.0.0.1'),self.answer('127.0.0.1')]), \
             patch('rh_runner.subprocess.run') as command:
            with self.assertRaisesRegex(ValueError,'public address'):
                self.transport.download(self.url,self.output)
        command.assert_not_called()

    def test_corrupt_recovered_result_is_preserved_as_part_without_promotion(self):
        def invoke(args,**kwargs):
            if args[0]=='curl':
                Path(args[args.index('--output')+1]).write_bytes(b'corrupt')
                return subprocess.CompletedProcess(args,0,b'',b'')
            return subprocess.CompletedProcess(args,1,b'',b'decoding error')
        with patch('rh_runner.urllib.request.urlopen',side_effect=[urllib.error.URLError('EOF'),self.answer('1.1.1.1'),self.answer('1.1.1.1')]), \
             patch('rh_runner.subprocess.run',side_effect=invoke):
            with self.assertRaisesRegex(ValueError,'complete decode'):
                self.transport.download(self.url,self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(next(self.output.parent.glob('candidate.mp4.*.part')).read_bytes(),b'corrupt')

    def test_destination_created_during_decode_is_never_overwritten(self):
        def decode(args,**kwargs):
            self.output.write_bytes(b'previously-promoted-candidate')
            return subprocess.CompletedProcess(args,0,b'',b'')
        with patch('rh_runner.urllib.request.urlopen',return_value=self.Response(b'new-download')), \
             patch('rh_runner.subprocess.run',side_effect=decode):
            with self.assertRaises(FileExistsError):
                self.transport.download(self.url,self.output)
        self.assertEqual(self.output.read_bytes(),b'previously-promoted-candidate')
        self.assertEqual(next(self.output.parent.glob('candidate.mp4.*.part')).read_bytes(),b'new-download')

    def test_simultaneous_downloads_use_distinct_parts_and_only_one_can_promote(self):
        import threading
        barrier=threading.Barrier(2)
        parts=[]
        def decode(args,**kwargs):
            path=Path(args[args.index('-i')+1]);parts.append(path)
            self.assertEqual(path.read_bytes(),b'complete')
            barrier.wait(timeout=5)
            return subprocess.CompletedProcess(args,0,b'',b'')
        def run():
            try:
                self.transport.download(self.url,self.output)
                return 'promoted'
            except FileExistsError:
                return 'already-promoted'
        with patch('rh_runner.urllib.request.urlopen',side_effect=lambda *a,**k:self.Response(b'complete')), \
             patch('rh_runner.subprocess.run',side_effect=decode), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(sorted(results),['already-promoted','promoted'])
        self.assertEqual(len(set(parts)),2)
        self.assertEqual(self.output.read_bytes(),b'complete')

    def test_existing_candidate_and_non_https_never_start_network(self):
        self.output.write_bytes(b'immutable')
        with patch('rh_runner.urllib.request.urlopen') as opened:
            with self.assertRaises(FileExistsError):
                self.transport.download(self.url,self.output)
            with self.assertRaisesRegex(ValueError,'HTTPS'):
                self.transport.download('http://another.test/file',self.output)
        opened.assert_not_called()
        self.assertEqual(self.output.read_bytes(),b'immutable')


if __name__ == '__main__':
    with patch.object(socket, 'create_connection', side_effect=AssertionError('Real network forbidden')):
        unittest.main(verbosity=2)

"""Offline identity-scoped pilot and receipt bookkeeping regressions."""
from pathlib import Path
from fractions import Fraction
import json,sqlite3,tempfile,unittest,copy
from unittest.mock import patch
import factory as f
import media,pilot_scope,cost_audit,rh_runner
from test_factory import video_fixture
from test_runner import Fake,Clock

class PilotScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='factory-pilot-scope-');self.root=Path(self.temp.name)
        self.project=self.root/'run';self.source=video_fixture(self.root/'source.mp4',fps=24)
        f.ingest(self.project,self.source)
        analysis=f.read(self.project/'analysis.json')
        analysis.update(status='passed',roles=[{'slot':r,'label':'Neutral '+r,'appearances':[[0,8]] if r!='C' else []} for r in 'ABC'])
        analysis['shots']=[{'id':'s1','start':0,'end':8,'roles':['A','B'],
            'description_en':'{{A}} and {{B}} keep measured source motion.','events':[]}]
        f.write(self.project/'analysis.json',analysis)
        from PIL import Image
        refs={}
        for r,c in zip('ABC',['red','green','blue']):
            path=self.project/'references-input'/f'{r}.png';path.parent.mkdir(exist_ok=True)
            Image.new('RGB',(32,32),c).save(path)
            refs[r]={'path':path.relative_to(self.project).as_posix(),'identity_en':'Current '+r+' identity with '+c+' clothes.'}
        job=f.read(self.project/'job.json');job.update(references=refs,production_authorized=True,
            appearance_policy={'style':'smooth','headset':'preserve-source'},
            profile={'kind':'legacy-ai-app','app_id':'2107845954833907713','quality_evidence':'case-verified',
                'limits':{'fps':['24'],'frame_counts':[22,39],'identity_slots':2}})
        f.write(self.project/'job.json',job)
        self.entry=f.prepare(self.project,approve_semantics=True)['jobs'][0]
        self.window=f.read(self.project/'plan.json')['windows'][0]
    def tearDown(self):self.temp.cleanup()
    def reviewed(self):
        out='candidates/fixture.mp4';p=self.project/out;p.parent.mkdir(exist_ok=True);p.write_bytes(self.source.read_bytes())
        f.inspect_candidate(self.project,self.entry['window_id'],'OFFLINE_TASK',out)
        reviews=f.read(self.project/'reviews.json');r=reviews['windows'][self.entry['window_id']]
        r['visual']={'status':'passed','dynamic_intervals':[[0,8]],'native_intervals':[[0,8]],'notes':'Offline synthetic fixture; not real quality approval.'}
        r['output_clock_review']={'status':'observed','intervals':[[0,8]],'notes':'All eight synthetic frames observed for guard testing.'}
        f.write(self.project/'reviews.json',reviews)
        return r
    def test_old_profile_case_verified_cannot_batch_new_identities(self):
        self.assertTrue(self.entry['pilot'])
        batch={**self.entry,'pilot':False}
        with self.assertRaisesRegex(ValueError,'reviewed pilot'):rh_runner.preflight(self.project,batch)
        self.assertEqual(rh_runner.preflight(self.project,self.entry),self.entry['input_digest'])
    def test_changed_reference_invalidates_scope_even_after_reprepare(self):
        old=self.entry['pilot_scope']
        from PIL import Image
        Image.new('RGB',(32,32),'black').save(self.project/'references-input/A.png')
        with self.assertRaisesRegex(ValueError,'scope changed'):pilot_scope.check(self.project,self.entry)
        new=f.prepare(self.project,approve_semantics=True)['jobs'][0]
        self.assertNotEqual(new['pilot_scope'],old);self.assertTrue(new['pilot'])
    def test_unrelated_reference_does_not_invalidate_ab_scope(self):
        old=self.entry['pilot_scope']
        from PIL import Image
        Image.new('RGB',(32,32),'white').save(self.project/'references-input/C.png')
        self.assertEqual(pilot_scope.scope(self.project,self.window,self.entry['profile']),old)
    def test_policy_and_native_risk_cannot_inherit_ordinary_scope(self):
        job=f.read(self.project/'job.json');job['appearance_policy']['headset']='remove'
        f.write(self.project/'job.json',job)
        self.assertNotEqual(pilot_scope.scope(self.project,self.window,self.entry['profile']),self.entry['pilot_scope'])
        analysis=f.read(self.project/'analysis.json');analysis['shots'][0]['risk_classes']=['back-turn','occlusion']
        f.write(self.project/'analysis.json',analysis)
        ordinary={**self.window,'risk_class':'ordinary'}
        self.assertNotEqual(pilot_scope.scope(self.project,ordinary,self.entry['profile']),self.entry['pilot_scope'])
    def test_real_review_promotion_is_bound_and_does_not_change_input_digest(self):
        self.reviewed();old=f.compute_input_digest(self.project,self.entry)
        result=pilot_scope.promote(self.project,self.entry['window_id'])
        self.assertEqual(result['status'],'passed');self.assertEqual(f.compute_input_digest(self.project,self.entry),old)
        self.assertTrue(pilot_scope.check(self.project,self.entry))
        rh_runner.preflight(self.project,{**self.entry,'pilot':False})
        reviews=f.read(self.project/'reviews.json');reviews['windows']['unrelated']={'visual':{'status':'pending'}}
        f.write(self.project/'reviews.json',reviews)
        self.assertTrue(pilot_scope.check(self.project,self.entry))
        reviews['windows'][self.entry['window_id']]['visual']['status']='failed';f.write(self.project/'reviews.json',reviews)
        self.assertFalse(pilot_scope.check(self.project,self.entry))
    def test_changed_candidate_bytes_revoke_promotion(self):
        record=self.reviewed();pilot_scope.promote(self.project,self.entry['window_id'])
        with (self.project/record['output_path']).open('ab') as h:h.write(b'changed')
        self.assertFalse(pilot_scope.check(self.project,self.entry))
    def test_changed_pilot_prompt_or_source_event_revokes_promotion(self):
        self.reviewed();pilot_scope.promote(self.project,self.entry['window_id'])
        prompt=self.project/self.entry['prompt_path'];old=prompt.read_text(encoding='utf-8')
        prompt.write_text(old+'\nChanged pilot action.',encoding='utf-8')
        self.assertFalse(pilot_scope.check(self.project,self.entry))
        # Restore exact file bytes and timestamp-sensitive prepared contract by
        # preparing and obtaining an actual new bound review again.
        self.entry=f.prepare(self.project,approve_semantics=True)['jobs'][0]
        self.reviewed();pilot_scope.promote(self.project,self.entry['window_id'])
        analysis=f.read(self.project/'analysis.json')
        analysis['shots'][0]['events']=[{'source_frame':2,'description_en':'A corrected measured gesture begins here.'}]
        f.write(self.project/'analysis.json',analysis)
        self.assertFalse(pilot_scope.check(self.project,self.entry))
    def test_unreviewed_or_invalid_global_plan_cannot_promote(self):
        with self.assertRaises((ValueError,KeyError)):pilot_scope.promote(self.project,self.entry['window_id'])
        self.reviewed();plan=f.read(self.project/'plan.json');plan['windows'][0]['source_interval']=[0,7]
        f.write(self.project/'plan.json',plan)
        with self.assertRaises(ValueError):pilot_scope.promote(self.project,self.entry['window_id'])
    def test_known_paid_id_recovers_without_new_scope_or_post(self):
        clock=Clock();led=rh_runner.Ledger(self.root/'private/tasks.sqlite',clock)
        try:
            runner=rh_runner.Runner(led,Fake(queries=[{'status':'SUCCESS','results':[{'url':'https://example.test/output.mp4','outputType':'mp4'}]}]))
            entry={**self.entry};entry.pop('pilot_scope')
            aid=runner.submit_one(self.project,entry,lambda p,e:e['input_digest']);clock.now+=10
            with patch('pilot_scope.approval',side_effect=AssertionError('Recovery must not require new scope')):
                runner.tick(self.project,{'jobs':[entry]})
            self.assertEqual(led.row(aid)['state'],'DOWNLOADED');self.assertEqual(runner.transport.submits,1)
        finally:led.close()

class CostAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='factory-cost-audit-');self.root=Path(self.temp.name).resolve()
        self.project=self.root/'run';self.project.mkdir();self.ledger=self.root/'private/tasks.sqlite';self.ledger.parent.mkdir()
        self.db=sqlite3.connect(self.ledger)
        self.db.execute('CREATE TABLE attempts(account TEXT,project TEXT,window TEXT,task_id TEXT,receipt TEXT,state TEXT,created REAL)')
        self.now=0
    def tearDown(self):self.db.close();self.temp.cleanup()
    def row(self,tid,coins=100,state='DOWNLOADED',window='w1',account='default',project=None,parent=None,receipt=None):
        self.now+=1
        if receipt is None:
            usage={'consumeCoins':str(coins),'consumeMoney':'0','thirdPartyConsumeMoney':'0'} if coins is not None else {}
            receipt={'taskId':tid,'status':'SUCCESS' if state!='EXECUTION_FAILED' else 'FAILED','usage':usage,
                'taskUsageList':[{'taskId':tid,'usage':usage}],
                'results':[{'url':'https://example.invalid/private-signature'}]}
            if parent:receipt['parentTaskId']=parent
        self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',(account,str(project or self.project),window,tid,json.dumps(receipt),state,self.now));self.db.commit()
    def audit(self,**kw):return cost_audit.audit(self.project,self.ledger,**kw)
    def test_receipt_duplicates_and_nested_usage_not_double_counted(self):
        self.row('PRIVATE_TASK',100);self.row('PRIVATE_TASK',100)
        self.row('other-project',999,project=self.root/'other');self.row('other-account',777,account='other')
        result=self.audit();self.assertEqual(result['total_rh_coins'],'100');self.assertEqual(result['accepted_unique_tasks'],1)
        serialized=json.dumps(result);self.assertNotIn('PRIVATE_TASK',serialized);self.assertNotIn('private-signature',serialized)
    def test_failed_and_download_pending_charges_are_included(self):
        self.row('failure',30,state='EXECUTION_FAILED');self.row('pending-download',100,state='DOWNLOAD_FAILED')
        self.assertEqual(self.audit()['total_rh_coins'],'130')
    def test_deferred_no_id_not_assumed_to_have_charge(self):
        self.row(None,receipt={'errorCode':'421','usage':None},state='DEFERRED')
        result=self.audit();self.assertEqual(result['total_rh_coins'],'0');self.assertEqual(result['unaccepted_deferred_rows'],1)
    def test_missing_conflicting_and_nonfinite_charges_are_unknown(self):
        self.row('missing',None);self.row('conflict',10);self.row('conflict',20);self.row('bad','NaN')
        result=self.audit();self.assertIsNone(result['total_rh_coins']);self.assertEqual(len(result['unknown_items']),3)
        self.assertEqual(result['known_coin_subtotal'],'0')
    def test_parent_child_overlap_requires_reconciliation(self):
        self.row('parent',100);self.row('child',100,parent='parent')
        result=self.audit();self.assertIsNone(result['total_rh_coins']);self.assertEqual(result['known_coin_subtotal'],'0')
    def test_aggregate_child_list_overlap_without_child_parent_field(self):
        self.row('parent',receipt={'taskId':'parent','status':'SUCCESS','usage':{'consumeCoins':'100'},
            'taskUsageList':[{'taskId':'child','usage':{'consumeCoins':'100'}}]})
        self.row('child',100)
        result=self.audit();self.assertIsNone(result['total_rh_coins']);self.assertEqual(result['known_coin_subtotal'],'0')
    def test_unreconciled_receipt_id_and_mismatched_id_are_unknown(self):
        self.row(None,receipt={'taskId':'unreconciled','usage':{'consumeCoins':'5'}},state='SUBMISSION_UNKNOWN')
        self.row('ledger-id',receipt={'taskId':'different','usage':{'consumeCoins':'7'}})
        result=self.audit();self.assertIsNone(result['total_rh_coins']);self.assertEqual(result['known_coin_subtotal'],'0')
    def test_owner_groups_and_conditional_scenario(self):
        f.write(self.project/'queue.json',{'jobs':[{'window_id':w,'source_contract':{'timeline_digest':'same-source-clock','window':{'source_interval':span}}}
            for w,span in [('w1',[0,5]),('w2',[0,5]),('w3',[5,10])]]})
        self.row('first',100,window='w1');self.row('repair',80,window='w2');self.row('second-shot',50,window='w3')
        result=self.audit(reduction_assumptions=['0.5'])
        self.assertEqual(result['first_success_baseline_coins'],'150');self.assertEqual(result['extra_attempt_cost_coins'],'80')
        self.assertEqual(result['scenario_assumptions'][0]['coins_saved'],'40.0')
        self.assertFalse(result['scenario_assumptions'][0]['measured_saving'])
    def test_overlapping_refinement_ranges_do_not_invent_baseline(self):
        f.write(self.project/'queue.json',{'jobs':[{'window_id':w,'source_contract':{'timeline_digest':'source','window':{'source_interval':span}}}
            for w,span in [('w1',[0,10]),('w2',[0,5])]]})
        self.row('wide',100,window='w1');self.row('partial',50,window='w2')
        result=self.audit(reduction_assumptions=['0.5']);self.assertIsNone(result['first_success_baseline_coins'])
        self.assertEqual(result['scenario_assumptions'],[])
    def test_missing_database_is_not_created(self):
        missing=self.root/'never-created.sqlite'
        with self.assertRaises(ValueError):cost_audit.audit(self.project,missing)
        self.assertFalse(missing.exists())

if __name__=='__main__':
    with patch('urllib.request.urlopen',side_effect=AssertionError('Offline tests prohibit network')):unittest.main(verbosity=2)

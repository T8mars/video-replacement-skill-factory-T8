"""Offline provider, order recovery and extracted-skill regressions."""
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

import chenyu_runner as cy
import factory
import rh_runner as cloud
import test_factory as fixtures
import zhenzhen_cost


def profile():
    return factory.read(Path(factory.__file__).parents[1]/'assets/backend-profiles.json')['profiles'][cy.KIND]


def schema():
    fields = [('node_138_value','string',True),('node_342_video','video_input',True),
              ('node_326_value','int',True)]
    fields.extend((f'node_{386+i}_image','image_input',i==0) for i in range(5))
    return {'workflow_id':cy.WORKFLOW,'revision_id':profile()['revision_id'],
            'may_incur_external_model_cost':False,'quote_amount':'0.02','quote_currency':'amount',
            'editable_parameter_manifest':[dict(name=k,value_type=t,required=r) for k,t,r in fields],
            'candidate_output_manifest':[{'name':cy.OUTPUT,'type':'video'}]}


class WireTests(unittest.TestCase):
    def test_five_ordered_slots_and_integer_frame_count(self):
        self.assertEqual(cy.verify_schema(schema(),profile(),5)['quote_currency'],'amount')
        refs=[{'asset_uri':'asset://ref'+str(i)} for i in range(5)]
        body=cy.payload(profile(),'ordered references',refs,{'asset_uri':'asset://motion'},73)
        for i,ref in enumerate(refs):
            self.assertEqual(body['inputs'][f'node_{386+i}_image'],ref['asset_uri'])
        self.assertEqual(body['inputs']['node_326_value'],73)
        self.assertFalse(body['accept_external_cost_risk'])

    def test_changed_revision_terms_slots_or_output_stop_before_spending(self):
        for field,value in [('revision_id','different'),('may_incur_external_model_cost',True),
                            ('editable_parameter_manifest',[]),('candidate_output_manifest',[])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                cy.verify_schema({**schema(),field:value},profile(),2)
        with self.assertRaises(ValueError):
            cy.verify_schema(schema(),profile(),6)

    def test_only_named_https_video_and_terminal_settlement(self):
        t=cy.Transport('fixture')
        data={'run_order_id':'one','revision_id':'rev','run_status':'queued','outputs':[]}
        t.api=Mock(return_value=data)
        self.assertEqual(t.query('one','rev')['results'],[])
        data.update(run_status='succeeded',outputs={cy.OUTPUT:['https://example.invalid/output']},
                    final_amount='0.10',quote_currency='amount',settlement_status='pending')
        self.assertIsNone(t.query('one','rev')['usage'])
        data['settlement_status']='settled'
        self.assertEqual(t.query('one','rev')['usage'],{'amount':'0.10','currency':'amount'})
        self.assertEqual(len(t.query('one','rev')['results']),1)
        with self.assertRaises(ValueError):
            t.query('one','another-revision')
        with self.assertRaises(ValueError):
            t.query('unrelated','rev')

    def test_generation_clock(self):
        self.assertEqual(cy.frame_count(73),73)
        self.assertEqual(cy.frame_count(5),22)
        self.assertLessEqual(cy.frame_count(230),240)
        with self.assertRaises(ValueError):
            cy.frame_count(241)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        fixtures.FactoryIntegration.setUp(self)
        self.addCleanup(shutil.rmtree,self.temp,True)

    def entry(self):
        self.job['production_authorized']=True
        factory.write(self.project/'job.json',self.job)
        factory.select_channel(self.project,'chenyu')
        return factory.prepare(self.project,approve_semantics=True,approve_projection=True)['jobs'][0]

    def test_upload_uses_multipart_original_bytes_and_temp_inputs(self):
        t=cy.Transport('fixture')
        t.opener=Mock()
        for path,kind in [(self.video,'video'),(self.project/'references-input/A.png','image')]:
            t.opener.open.return_value=io.BytesIO(b'{"code":0,"data":{"asset_uri":"asset://fixture"}}')
            self.assertEqual(t.upload(path),{'asset_uri':'asset://fixture'})
            req=t.opener.open.call_args.args[0]
            self.assertEqual(req.full_url,cy.BASE+'/assets')
            self.assertIn(path.read_bytes(),req.data)
            self.assertIn(('name="media_type"\r\n\r\n'+kind).encode(),req.data)
            self.assertIn(b'name="purpose"\r\n\r\ntemp_input',req.data)

    def test_uncertain_submit_reuses_original_intent_and_key(self):
        entry=self.entry()
        db=cloud.Ledger(self.temp/'ledger.sqlite')
        self.addCleanup(db.close)
        t=Mock(spec=cy.Transport)
        t.provider='chenyu'
        t.key='fixture'
        t.inspect_route.return_value={'quote_currency':'amount','quote_amount':'0.02'}
        t.upload.side_effect=lambda path: {'asset_uri':'asset://'+path.name}
        t.submit.side_effect=[TimeoutError(),{'taskId':'one'}]
        r=cloud.Runner(db,t,'chenyu:default')
        aid=r.submit_one(self.project,entry)
        row=db.row(aid)
        self.assertEqual(row['state'],'SUBMISSION_UNKNOWN')
        self.assertIsNone(r.submit_one(self.project,entry))
        t.key='different'
        with self.assertRaises(ValueError):
            cloud.replay_chenyu_intent(row,t)
        t.key='fixture'
        self.assertEqual(cloud.replay_chenyu_intent(row,t),'one')
        self.assertEqual(t.submit.call_args_list[0],t.submit.call_args_list[1])
        self.assertEqual(t.upload.call_count,2)
        request=json.loads(row['request'])
        self.assertEqual(request['body']['inputs']['node_386_image'],'asset://A.png')
        self.assertEqual(request['body']['inputs']['node_326_value'],entry['generation_frames'])
        with self.assertRaisesRegex(ValueError,'another channel'):
            cloud.Runner(db,cloud.Transport('fixture')).tick(self.project,{'jobs':[entry]})

    def test_changed_schema_does_not_upload_or_submit(self):
        entry=self.entry()
        db=cloud.Ledger(self.temp/'ledger.sqlite')
        self.addCleanup(db.close)
        t=cy.Transport('fixture')
        t.api=Mock(return_value={**schema(),'revision_id':'changed'})
        with self.assertRaisesRegex(ValueError,'revision'):
            cloud.Runner(db,t,'chenyu:default').submit_one(self.project,entry)
        t.api.assert_called_once_with('/workflow/market/info?workflow_id='+cy.WORKFLOW)
        self.assertEqual(db.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)

    def test_accepted_order_resumes_without_new_uploads_or_submissions(self):
        entry=self.entry()
        db=cloud.Ledger(self.temp/'ledger.sqlite')
        self.addCleanup(db.close)
        t=Mock(spec=cy.Transport)
        t.provider='chenyu'
        t.key='fixture'
        t.upload.return_value={'asset_uri':'asset://fixture'}
        t.inspect_route.return_value={}
        t.submit.return_value={'taskId':'one'}
        runner=cloud.Runner(db,t,'chenyu:default')
        aid=runner.submit_one(self.project,entry)
        db.update(aid,next_poll=0)
        t.query.return_value={'taskId':'one','status':'SUCCESS','results':[
            {'outputType':'mp4','url':'https://example.invalid/output'}]}
        def download(url,path):
            path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(self.video,path)
        t.download.side_effect=download
        cloud.Runner(db,t,'chenyu:default').recover(aid)
        self.assertEqual(db.row(aid)['state'],'DOWNLOADED')
        t.query.assert_called_once_with('one',profile()['revision_id'])
        self.assertIsNone(runner.submit_one(self.project,entry))
        t.submit.assert_called_once()
        self.assertEqual(t.upload.call_count,2)

    def test_settlements_are_isolated_by_channel(self):
        p=self.temp/'ledger.sqlite'
        db=cloud.Ledger(p)
        self.addCleanup(db.close)
        for channel,amount in [('zhenzhen','100'),('chenyu','0.4')]:
            aid=db.reserve(channel+':default',self.project,'001','fixture')
            receipt={'taskId':'one','status':'SUCCESS','usage':{'amount':amount,'currency':'amount'}}
            db.update(aid,task_id='one',state='DOWNLOADED',output=str(self.video),receipt=json.dumps(receipt))
        result=zhenzhen_cost.audit([self.project],p,provider='chenyu')
        self.assertEqual(result['known_amounts'],{'amount':'0.4'})
        self.assertEqual(result['kind'],'chenyu-cost-audit')

    def test_generated_skill_selects_chenyu_after_extraction(self):
        factory.prepare(self.project)
        out=factory.build_skill(self.project,self.temp/'installed',display_name='晨羽换角复用测试')
        with ZipFile(out['zip']['path']) as archive:
            archive.extractall(self.temp/'unzipped')
        skill=self.temp/'unzipped'/out['name']
        self.assertTrue((skill/'references/chenyu.md').is_file())
        self.assertIn('晨羽',(skill/'SKILL.md').read_text(encoding='utf8'))
        run=self.temp/'fresh'
        for args in (['instantiate','--skill',str(skill)],['select-channel','--channel','chenyu']):
            subprocess.run([sys.executable,str(skill/'scripts/factory.py'),*args,'--project',str(run)],
                           capture_output=True,check=True)
        self.assertEqual(factory.read(run/'job.json')['channel'],'chenyu')
        window=factory.read(run/'plan.json')['windows'][0]
        self.assertEqual(window['source_interval'],[0,8])
        self.assertEqual(window['generation_frames'],22)
        subprocess.run([sys.executable,str(skill/'scripts/chenyu_runner.py'),'--help'],capture_output=True,check=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)

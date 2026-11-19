"""Offline factory integration tests with real source and generated-media fixtures."""
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from zipfile import ZipFile

from PIL import Image
import factory
import media
import rh_runner as cloud
import role_review
import test_factory as fixtures
import zhenzhen_runner as zz
import zhenzhen_cost


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: shutil.rmtree(self.temp, ignore_errors=True))
        fixtures.FactoryIntegration.setUp(self)
        self.profile = factory.read(Path(factory.__file__).resolve().parents[1]/'assets/backend-profiles.json')['profiles'][zz.KIND]

    def select(self):
        factory.select_channel(self.project, 'zhenzhen')
        job = factory.read(self.project/'job.json')
        job['production_authorized'] = True
        factory.write(self.project/'job.json', job)

    def two_people(self):
        Image.new('RGB',(32,32),'blue').save(self.project/'references-input/B.png')
        self.job['references']['B'] = {'path':'references-input/B.png','identity_en':'Blue target'}
        self.analysis['roles'].append({'slot':'B','appearances':[[0,8]]})
        self.analysis['shots'][0].update(roles=['B','A'],role_checkpoints=[
            {'source_frame':3,'visible_slots':['A','B'],'owners':{'umbrella':'B'}}])
        factory.write(self.project/'analysis.json', self.analysis)
        factory.write(self.project/'job.json', self.job)
        self.select()
        return factory.prepare(self.project, approve_semantics=True, approve_projection=True)['jobs'][0]

    def test_actual_short_motion_and_ordered_paid_payload(self):
        entry = self.two_people()
        self.assertEqual([b['slot'] for b in entry['local_bindings']], ['B','A'])
        motion = self.project/entry['video_path']
        self.assertEqual(media.inspect(motion)['video']['frame_count'],56)
        self.assertEqual(entry['source_contract']['window']['source_interval'], [0,8])
        db = cloud.Ledger(self.temp/'tasks.sqlite')
        self.addCleanup(db.close)
        t = Mock(spec=zz.Transport)
        t.key = 'fixture'
        t.provider = 'zhenzhen'
        t.upload.side_effect = lambda p: {'url':'https://example.invalid/'+p.name,'expires_in':86400}
        t.estimate.return_value = {'estimate_available':False}
        t.submit.return_value = {'taskId':'fixture-task'}
        r = cloud.Runner(db,t,'zhenzhen:default')
        aid = r.submit_one(self.project, entry)
        path, body = t.submit.call_args.args
        self.assertEqual(path,zz.GENERATIONS)
        self.assertEqual(body['model'],zz.MODEL)
        self.assertEqual(body['images'],['https://example.invalid/B.png','https://example.invalid/A.png'])
        self.assertIn('umbrella belongs to <Subject 1>', body['prompt'])
        self.assertEqual(body['metadata']['frames'],56)
        self.assertNotIn('seconds',body)
        t.estimate.assert_called_once_with(body)
        self.assertEqual(db.row(aid)['task_id'],'fixture-task')
        self.assertIsNone(r.submit_one(self.project,entry))
        t.submit.assert_called_once()
        ref = self.project/entry['reference_paths'][0]
        r.upload(ref,zz.KIND)
        self.assertEqual(t.upload.call_count,3)
        db.db.execute('UPDATE uploads SET expires=0')
        r.upload(ref,zz.KIND)
        self.assertEqual(t.upload.call_count,4)

    def test_legacy_runner_refuses_foreign_queue_before_upload_or_poll(self):
        entry = self.two_people()
        db = cloud.Ledger(self.temp/'tasks.sqlite')
        self.addCleanup(db.close)
        t = Mock(spec=cloud.Transport)
        with self.assertRaisesRegex(ValueError,'another channel'):
            cloud.Runner(db,t).tick(self.project,{'jobs':[entry]})
        t.upload.assert_not_called()
        t.query.assert_not_called()
        with self.assertRaisesRegex(ValueError,'resume'):
            factory.select_channel(self.project,'rh')

    def test_grid_maximum_without_truncation(self):
        self.assertEqual([zz.frame_count(n) for n in (1,39,56,57,226)], [56,56,56,73,226])
        for n in (227,240,241):
            with self.assertRaises(ValueError): zz.frame_count(n)
        for count in (39,55,57,243):
            with self.assertRaises(ValueError):
                zz.validate_window(self.profile,{'generation_frames':count,'generation_fps':'24',
                    'duration':str(Fraction(count,24))},['B'])

    def test_music_only_edition_retains_video_and_saves_standalone_wav(self):
        import replace_music
        import wave
        song = self.temp/'music.wav'
        with wave.open(str(song),'wb') as out:
            out.setparams((1,2,48000,0,'NONE','NONE'))
            out.writeframes(bytes(48000*2))
        edition=self.temp/'edition'
        result=replace_music.replace(self.video,song,edition,Fraction(1,4))
        self.assertEqual(result['samples'],32000)
        self.assertEqual(result['music_start_sample'],12000)
        self.assertEqual(result['video_packets_verified'],8)
        self.assertTrue(Path(result['aligned_wav']).is_file())
        with self.assertRaises(FileExistsError): replace_music.replace(self.video,song,edition)
        with self.assertRaises(ValueError): replace_music.replace(self.video,song,self.temp/'short',Fraction(1,2))
        self.assertFalse((self.temp/'short').exists())

    def test_reference_count_and_ratio(self):
        w = {'generation_frames':56,'generation_fps':'24','duration':'7/3'}
        zz.validate_window(self.profile,w,list('ABCDEFGHI'))
        for slots in ([],list('ABCDEFGHIJ'),['A','A']):
            with self.assertRaises(ValueError): zz.validate_window(self.profile,w,slots)
        with self.assertRaises(ValueError):
            zz.validate_window({**self.profile,'ratio':'1:1'},w,['A'])

    def test_query_normalizes_real_envelope_and_never_uses_motion_input(self):
        t = zz.Transport('fixture')
        data = {'task_id':'opaque','id':123,'status':'SUCCESS','usage':{'amount':'0.2','currency':'X'},
                'request_data':{'metadata':{'video_url':['https://example.invalid/input.mp4']}}}
        t.api = Mock(return_value={'code':'success','data':data})
        self.assertEqual(t.query('opaque')['results'],[])
        data['result_url'] = 'https://example.invalid/result.mp4'
        self.assertEqual(t.query('opaque')['results'][0]['url'],data['result_url'])
        data['status']='FAILURE'
        self.assertEqual(t.query('opaque')['status'],'FAILED')
        with self.assertRaises(ValueError):t.query('mismatch')

    def test_wire_uploads_original_bytes_and_queries_with_get(self):
        import io
        class Response(io.BytesIO):
            pass
        t=zz.Transport('fixture')
        opener=Mock()
        opener.open.side_effect = [
            Response(b'{"url":"https://example.invalid/ref","expires_in":86400}'),
            Response(b'{"task_id":"abcd","id":"other"}'),
            Response(b'{"data":{"task_id":"abcd","status":"SUCCESS","usage":{"amount":0,"currency":"X"}}}')]
        t.opener=opener
        image=self.project/'references-input/A.png'
        ref=t.upload(image)
        body=zz.payload(self.profile,'prompt',[ref],{'url':'https://example.invalid/video'},56)
        self.assertEqual(t.submit(zz.GENERATIONS,body)['taskId'],'abcd')
        self.assertEqual(t.query('abcd')['status'],'SUCCESS')
        upload,submit,query=[call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(upload.full_url,zz.BASE+'/v1/files/upload')
        self.assertIn(b'name="file"',upload.data)
        self.assertIn(image.read_bytes(),upload.data)
        self.assertIn(b'image/png',upload.data)
        self.assertEqual(json.loads(submit.data),body)
        self.assertEqual(query.get_method(),'GET')
        self.assertEqual(query.full_url,zz.BASE+zz.GENERATIONS+'/abcd')
        self.assertNotIn('Authorization',t.download_headers)

    def test_success_receipt_restores_download_without_second_query(self):
        ledger=cloud.Ledger(self.temp/'tasks.sqlite',clock=lambda:100)
        self.addCleanup(ledger.close)
        aid=ledger.reserve('zhenzhen:default',self.project,'a','input')
        ledger.update(aid,state='RUNNING',task_id='id')
        t=Mock(spec=zz.Transport)
        t.provider='zhenzhen'
        t.query.return_value={'taskId':'id','status':'SUCCESS','usage':{'amount':1,'currency':'X'},
                              'results':[{'outputType':'mp4','url':'https://example.invalid/v'}]}
        def download(url,dest):
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(self.video,dest)
        t.download.side_effect=download
        r=cloud.Runner(ledger,t,'zhenzhen:default')
        r.recover(aid,defer_download=True)
        r.recover(aid)
        self.assertEqual(ledger.row(aid)['state'],'DOWNLOADED')
        t.query.assert_called_once_with('id')
        t.submit.assert_not_called()

    def test_uncertain_post_blocks_other_attempts(self):
        entry = self.two_people()
        db = cloud.Ledger(self.temp/'tasks.sqlite')
        self.addCleanup(db.close)
        t = Mock(spec=zz.Transport)
        t.key='fixture'
        t.provider='zhenzhen'
        t.upload.return_value={'url':'https://example.invalid/reference','expires_in':86400}
        t.estimate.return_value={}
        t.submit.side_effect=URLError('timeout')
        r=cloud.Runner(db,t,'zhenzhen:default')
        aid=r.submit_one(self.project,entry)
        self.assertEqual(db.row(aid)['state'],'SUBMISSION_UNKNOWN')
        self.assertIsNone(r.submit_one(self.project,entry))
        t.submit.assert_called_once()

    def test_zip_inherits_channel_works_with_prior_rh_ownership(self):
        factory.prepare(self.project)
        out = factory.build_skill(self.project,self.temp/'installed',display_name='双渠道复用测试')
        with ZipFile(out['zip']['path']) as archive:
            archive.extractall(self.temp/'unzipped')
        skill=self.temp/'unzipped'/out['name']
        run=self.temp/'new'
        cli=[sys.executable,'-B',str(skill/'scripts/factory.py')]
        for args in (['instantiate','--skill',str(skill)],['select-channel','--channel','zhenzhen']):
            subprocess.run(cli+args+['--project',str(run)],capture_output=True,check=True)
        self.assertEqual(factory.read(run/'plan.json')['windows'][0]['generation_frames'],56)
        self.assertEqual(factory.read(run/'plan.json')['windows'][0]['source_interval'],[0,8])
        self.assertEqual(factory.read(run/'job.json')['references'],{})
        for name in ('scripts/zhenzhen_runner.py','scripts/zhenzhen_cost.py','references/zhenzhen.md'):
            self.assertTrue((skill/name).is_file())
        self.assertIn('贞贞 AI',(skill/'SKILL.md').read_text(encoding='utf8'))

    def test_actual_settlement_includes_discarded_media_and_refund(self):
        path=self.temp/'ledger.sqlite'
        db=cloud.Ledger(path)
        for i in range(3):
            aid=db.reserve('zhenzhen:default',self.project,str(i),'sample')
            receipt={'status':'SUCCESS' if i<2 else 'FAILED','taskId':str(i),'usage':{'amount':1 if i<2 else 0,'currency':'X'}}
            db.update(aid,task_id=str(i),receipt=json.dumps(receipt),state='VISUAL_FAILED' if i<2 else 'EXECUTION_FAILED',
                      output=str(self.video) if i<2 else None)
        report=zhenzhen_cost.audit([self.project,self.project],path)
        self.assertEqual(report['known_amounts'],{'X':'2'})
        self.assertEqual(report['measured_generated_seconds'],'4/3')
        self.assertEqual(report['actual_cost_per_generated_second'],{'X':'1.5'})
        db.update(aid,receipt=json.dumps({'status':'FAILED','usage':{'amount':None}}))
        self.assertIsNone(zhenzhen_cost.audit([self.project],path)['actual_cost_per_generated_second'])
        db.close()

    def test_missing_actor_wrong_owner_and_stale_mapping_fail_joint_review(self):
        entry=self.two_people()
        report=factory.inspect_candidate(self.project,'window_001','fixture',entry['video_path'])
        window=entry['source_contract']['window']
        review=report['role_review']
        with self.assertRaises(ValueError):role_review.validate(self.analysis,window,report)
        review.update(reviewer='test',continuity_notes='Synthetic observations for validation only')
        for got,expected in zip(review['points'],role_review.expectations(self.analysis,window)):
            got.update(expected,unexpected_subjects=False)
        role_review.validate(self.analysis,window,report)
        bad=next(point for point in review['points'] if point['source_frame']==3)
        for key,wrong in [('visible_slots',['A']),('owners',{'umbrella':'A'}),('unexpected_subjects',True)]:
            saved=bad[key]
            bad[key]=wrong
            with self.assertRaises(ValueError):role_review.validate(self.analysis,window,report)
            bad[key]=saved
        report['source_to_output'][0]+=1
        with self.assertRaises(ValueError):role_review.validate(self.analysis,window,report)


if __name__ == '__main__':
    unittest.main(verbosity=2)

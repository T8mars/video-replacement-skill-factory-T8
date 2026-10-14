"""Offline identity-route regressions. No network, keys or visual approvals."""
from pathlib import Path
from fractions import Fraction
import argparse, tempfile, unittest, copy, json
from unittest.mock import patch
import factory, media, identity_clock, face_swap, delivery
import rh_runner as rh

DEPS=None
SCALAR=False


class ClockTests(unittest.TestCase):
    def test_complete_context_and_repeat_hard_limit(self):
        self.assertEqual(identity_clock.contract([0,80],[8,80])['input_seconds'],'10')
        with self.assertRaisesRegex(ValueError,'exceeds10'):identity_clock.contract([0,81],[8,81])
    def test_owner_and_unknown_input_route_rejected(self):
        for context,owner,fps,w,h in [([0,3],[1,4],'24',720,1280),([0,3],[0,3],'30',720,1280),([0,3],[0,3],'24',1280,720)]:
            with self.assertRaises(ValueError):identity_clock.contract(context,owner,fps,w,h)
    def test_exact_photo_video_fields_and_no_h3_text(self):
        nodes=face_swap.nodes_for({'kind':'reviewed-framewise-face-swap','app_id':'1931791712199618561'},'photo','video')
        self.assertEqual(nodes,[{'nodeId':'5','fieldName':'image','fieldValue':'photo'},
                                {'nodeId':'4','fieldName':'video','fieldValue':'video'}])
        with self.assertRaises(ValueError):face_swap.nodes_for({'kind':'reviewed-framewise-face-swap','app_id':'unknown'},'p','v')
    def test_actual_media_inverse_envelope_preserves_each_unique_pose(self):
        from PIL import Image
        av=media.runtime(DEPS,SCALAR)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);candidate=root/'candidate.nut'
            colours=[(90,20,180),(110,30,160),(130,40,140)]
            with av.open(str(candidate),'w',format='nut') as out:
                st=out.add_stream('rawvideo',rate=8);st.width=st.height=1280;st.pix_fmt='rgb24';st.time_base=Fraction(1,8)
                for i,colour in enumerate(colours):
                    image=Image.new('RGB',(1280,1280),'black');image.paste(Image.new('RGB',(720,1280),colour),(280,0))
                    frame=av.VideoFrame.from_image(image);frame.pts=i;frame.time_base=Fraction(1,8)
                    for packet in st.encode(frame):out.mux(packet)
                for packet in st.encode():out.mux(packet)
            destination=root/'owner.nut';projection={'contract':identity_clock.contract([0,3],[1,3])}
            result=identity_clock.restore(candidate,destination,projection,DEPS,SCALAR)
            self.assertEqual([r['source'] for r in result['rows']],[1,2])
            with av.open(str(destination)) as inp:
                frames=list(inp.decode(video=0))
                self.assertEqual([f.to_image().getpixel((350,600)) for f in frames],colours[1:])
                self.assertEqual([Fraction(f.pts)*f.time_base for f in frames],[Fraction(0),Fraction(1,24)])
            self.assertEqual(result['identity'],'pending-original-photo')
            projection['contract']['output_frames']=4
            with self.assertRaisesRegex(ValueError,'contract changed'):
                identity_clock.restore(candidate,root/'wrong.nut',projection,DEPS,SCALAR)
    def test_complete_raw_rows_detect_changed_owner_or_time(self):
        from PIL import Image
        av=media.runtime(DEPS,SCALAR)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);raw=root/'raw.nut'
            rows=identity_clock.raw_writer(av,raw,((i,Image.new('RGB',(64,48),(i*40,20,150))) for i in range(3)),64,48)
            source={'video':{'frame_count':3,'frames':[{'time':str(Fraction(i,24))} for i in range(3)],'start_time':'0'}}
            proof={'artifact':factory.file_signature(root,'raw.nut'),'proof_file':factory.file_signature(root,'raw.nut')}
            factory.write(root/'raw-proof.json',{'raw_signature':media.signature(raw),
                'owner_rows':[{'source':r['source'],'rgb_sha256':r['rgb_sha256']} for r in rows]})
            proof['proof_file']=factory.file_signature(root,'raw-proof.json')
            runtime={'deps':DEPS,'scalar_cpu':SCALAR}
            delivery.verify_raw_rows(root,source,proof,runtime)
            changed=factory.read(root/'raw-proof.json');changed['owner_rows'][1]['rgb_sha256']='0'*64
            factory.write(root/'raw-proof.json',changed)
            with self.assertRaisesRegex(ValueError,'RGB/PTS'):delivery.verify_raw_rows(root,source,proof,runtime)
            source['video']['frames'][1]['time']='1/12'
            with self.assertRaisesRegex(ValueError,'RGB/PTS'):delivery.verify_raw_rows(root,source,proof,runtime)


class SwapRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.p=self.root/'p';self.p.mkdir()
        (self.p/'photo').write_bytes(b'original inspected identity fixture');(self.p/'video').write_bytes(b'body fixture')
        self.entry={'profile':{'kind':'reviewed-framewise-face-swap','app_id':'1931791712199618561'},
            'reference_paths':['photo'],'video_path':'video','window_id':'pilot','input_digest':'fixture'}
        self.ledger=rh.Ledger(self.root/'private.sqlite')
        class Fake:
            def __init__(self):self.calls=[];self.error=None;self.mutate=None
            def upload(fake,path):
                if fake.mutate:change,fake.mutate=fake.mutate,None;change()
                return {'filename':rh.sha(path)}
            def submit(fake,path,body):
                fake.calls.append((path,body))
                if fake.error:raise fake.error
                return {'taskId':'offline-'+str(len(fake.calls))}
        self.fake=Fake();self.runner=face_swap.Runner(self.ledger,self.fake)
        self.expected=rh.sha(self.p/'photo')
    def tearDown(self):self.ledger.close();self.tmp.cleanup()
    def validate(self,project,entry):
        if rh.sha(Path(project)/'photo')!=self.expected:raise ValueError('Changed during upload')
        return 'fixture'
    def test_measured_app_request_only_has_exact_image_video_fields(self):
        aid=self.runner.submit_one(self.p,self.entry,self.validate)
        request=json.loads(self.ledger.row(aid)['request'])
        self.assertEqual(request['path'],'/openapi/v2/run/ai-app/1931791712199618561')
        self.assertEqual([n['fieldName'] for n in request['body']['nodeInfoList']],['image','video'])
    def test_unknown_post_retains_slot_and_never_reposts(self):
        self.fake.error=TimeoutError('offline uncertainty')
        aid=self.runner.submit_one(self.p,self.entry,self.validate)
        self.assertEqual(self.ledger.row(aid)['state'],'SUBMISSION_UNKNOWN')
        self.assertIsNone(self.runner.submit_one(self.p,self.entry,self.validate))
        self.assertEqual(len(self.fake.calls),1)
    def test_same_account_cap_three_blocks_fourth_correction(self):
        import shutil
        for i in range(4):
            p=self.p if i==0 else self.root/f'p{i}';
            if i:shutil.copytree(self.p,p)
            aid=self.runner.submit_one(p,self.entry,self.validate)
            self.assertEqual(aid is not None,i<3)
        self.assertEqual(len(self.fake.calls),3)
    def test_upload_mutation_never_submits(self):
        self.fake.mutate=lambda:(self.p/'photo').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'Changed'):self.runner.submit_one(self.p,self.entry,self.validate)
        self.assertFalse(self.fake.calls)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for name in ['source.mp4','final.mp4','owner.nut','parent.json','raw.nut','raw-proof.json','ref.png','native.png']:
            (self.root/name).write_bytes(('Offline bound fixture '+name).encode())
        sig=lambda p:factory.file_signature(self.root,p)
        self.source={'source':sig('source.mp4'),'video':{'frame_count':4}}
        self.analysis={'roles':[{'slot':'A'}]}
        self.record={'schema_version':1,'kind':'reviewed-postprocessed-film','source_signature':self.source['source'],
            'analysis_digest':factory.digest(self.analysis),'output_path':'final.mp4','file_signature':sig('final.mp4'),
            'technical':{'status':'passed','report':{'fixture_only':True}},
            'owners':[{'source_interval':[0,4],'artifact':sig('owner.nut'),'provenance':[sig('parent.json')],'notes':'Actual derived owner fixture'}],
            'raw_owner_verification':{'artifact':sig('raw.nut'),'proof_file':sig('raw-proof.json'),'status':'passed','frames':4,'notes':'Actual complete rows fixture'},
            'identity_review':{'status':'passed','notes':'Human compared supplied original identity','original_references':[sig('ref.png')]},
            'native_risk_intervals':[[1,3]],
            'visual':{'status':'passed','notes':'Offline review contract fixture, never a real visual claim',
                'dynamic_intervals':[[0,4]],'native_intervals':[[0,4]],'native_evidence':[sig('native.png')],
                'original_speed_evidence':{'reviewer':'human-user','speed':'1x','output_sha256':sig('final.mp4')['sha256'],
                    'question':'Review exact fixture','answer':'Offline fixture reply','observations':'Fixture'}}}
        self.analysis_patch=patch.object(factory,'validate_analysis',return_value=(self.source,self.analysis));self.analysis_patch.start()
        self.report_patch=patch.object(factory,'validate_final_technical_report');self.validate=self.report_patch.start()
        self.raw_patch=patch.object(delivery,'verify_raw_rows');self.raw_verify=self.raw_patch.start()
    def tearDown(self):self.raw_patch.stop();self.report_patch.stop();self.analysis_patch.stop();self.tmp.cleanup()
    def check(self,record=None):
        factory.write(self.root/'delivery.json',record or self.record)
        return delivery.guard(self.root,'delivery.json')
    def test_current_bound_review_uses_existing_complete_technical_guard(self):
        self.check();self.validate.assert_called_once();self.raw_verify.assert_called_once()
    def test_owners_gap_overlap_and_missing_provenance_rejected(self):
        for interval in ([1,4],[0,3]):
            r=copy.deepcopy(self.record);r['owners'][0]['source_interval']=interval
            with self.assertRaises(ValueError):self.check(r)
        r=copy.deepcopy(self.record);r['owners'][0]['provenance']=[]
        with self.assertRaises(ValueError):self.check(r)
    def test_original_reference_and_final_mutation_invalidate_review(self):
        (self.root/'ref.png').write_bytes(b'new identity')
        with self.assertRaisesRegex(ValueError,'changed'):self.check()
        self.record['identity_review']['original_references']=[factory.file_signature(self.root,'ref.png')]
        (self.root/'final.mp4').write_bytes(b'new final')
        with self.assertRaisesRegex(ValueError,'changed'):self.check()
    def test_missing_actual_reply_or_wrong_movie_rejected(self):
        for changes in ({'answer':''},{'output_sha256':'0'*64},{'speed':'0.5x'}):
            r=copy.deepcopy(self.record);r['visual']['original_speed_evidence'].update(changes)
            with self.assertRaises(ValueError):self.check(r)
    def test_partial_dynamic_or_missing_native_seams_rejected(self):
        for field in ('dynamic_intervals','native_intervals'):
            r=copy.deepcopy(self.record);r['visual'][field]=[[1,3]]
            with self.assertRaises(ValueError):self.check(r)
    def test_failed_identity_or_raw_proof_cannot_build_ready(self):
        for field in ('identity_review','raw_owner_verification'):
            r=copy.deepcopy(self.record);r[field]['status']='pending'
            with self.assertRaises(ValueError):self.check(r)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--deps');ap.add_argument('--scalar-cpu',action='store_true')
    args,remaining=ap.parse_known_args();DEPS=args.deps;SCALAR=args.scalar_cpu
    unittest.main(argv=['test_identity.py',*remaining],verbosity=2)

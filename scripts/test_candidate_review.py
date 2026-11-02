"""Actual-media review/proof regressions, including an extracted standalone skill."""
from pathlib import Path
from fractions import Fraction
import argparse
import copy
import hashlib
import json
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import factory
import media
import candidate_review as review
import test_factory as fixtures

DEPS=None;SCALAR=False;WORK=None


class CandidateReviewTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.root=Path(tempfile.mkdtemp(prefix='candidate-review-',dir=WORK)).resolve()
        self.original=fixtures.video_fixture(self.root/'original.mp4')
        self.project=self.root/'run';self.source=factory.ingest(self.project,self.original,DEPS,SCALAR)
        analysis=factory.read(self.project/'analysis.json')
        analysis.update(status='passed',roles=[{'slot':'A','appearances':[[0,8]]}],
            shots=[{'id':'s','start':0,'end':8,'roles':['A'],'description_en':'{{A}} follows all measured source poses.','events':[]}])
        factory.write(self.project/'analysis.json',analysis);self.analysis=analysis
        (self.project/'refs').mkdir();Image.new('RGB',(16,16),'green').save(self.project/'refs/A.png')
        job=factory.read(self.project/'job.json');job.update(references={'A':{'path':'refs/A.png','identity_en':'Current green identity.'}},
            profile={'kind':'legacy-ai-app','app_id':'OFFLINE_FIXTURE','limits':{'fps':['12'],'frame_counts':[8],'identity_slots':1}})
        factory.write(self.project/'job.json',job);self.entry=factory.prepare(self.project,DEPS,SCALAR,True)['jobs'][0]
        self.window=factory.read(self.project/'plan.json')['windows'][0];self.wid=self.window['window_id']
        self.mapping=[0,16,21,24,40,52,70,74];self.relative='candidates/actual90.nut'
        (self.project/'candidates').mkdir();self.candidate=self.project/self.relative
        av=media.runtime(DEPS,SCALAR);images=fixtures.frames(self.original)
        with av.open(str(self.candidate),'w',format='nut') as container:
            stream=container.add_stream('ffv1',rate=12);stream.width=64;stream.height=48;stream.pix_fmt='bgr0'
            stream.time_base=Fraction(1,49152);stream.codec_context.thread_count=1
            for i in range(90):
                pose=max(j for j,n in enumerate(self.mapping) if i>=n)
                frame=av.VideoFrame.from_image(images[pose]);frame.pts=i*4096;frame.time_base=Fraction(1,49152)
                for packet in stream.encode(frame):container.mux(packet)
            for packet in stream.encode():container.mux(packet)
        self.record=factory.inspect_candidate(self.project,self.wid,'OFFLINE_FIXTURE',self.relative,DEPS,SCALAR)

    def evidence(self):
        return review.prepare_evidence(self.project,self.wid,self.relative,'candidate-proof',self.mapping,
            [{'name':'visible_role_A','box':[0,0,32,48],'role_slots':['A']}],False,DEPS,SCALAR)

    def contract(self):
        artifact=self.evidence()['evidence']
        contract={'schema_version':1,'mode':'measured-source-poses','evidence':artifact,'source_to_output':self.mapping,
            'output_clock_review':{'status':'observed','intervals':[[0,90]],'notes':'Offline fixture all90frames include initial/final repeated holds.'},
            'source_pose_review':{'status':'passed','source_intervals':[[0,8]],'notes':'Synthetic all8native source poses exactly match selected output images; no production visual claim.',
                'anchors':[{'source_frame':i,'output_frame':self.mapping[i],'notes':'Synthetic measured source pose.'} for i in [0,3,7]]},
            'source_cut_before':[],'output_cut_before':[]}
        factory.write(self.project/'measured-map.json',contract)
        return contract

    def apply(self):
        self.contract();review.apply_mapping(self.project,self.wid,'measured-map.json')
        return factory.read(self.project/'reviews.json')['windows'][self.wid]

    def pass_fixture_visual(self,record):
        record['visual']={'status':'passed','dynamic_intervals':[[0,90]],'native_intervals':[[0,90]],
            'notes':'Offline synthetic acceptance fixture, not real film approval.'}
        if record.get('mapping_mode')=='measured-source-poses':
            record['visual']['measured_mapping_digest']=record['measured_mapping']['mapping_digest']
        factory.write(self.project/'reviews.json',{'windows':{self.wid:record}})

    def test_inspection_has_full_actual_report_and_pending_clock(self):
        self.assertEqual(self.record['technical']['report']['video']['frame_count'],90)
        self.assertEqual(self.record['output_clock_review']['status'],'pending')
        self.assertEqual(self.record['visual']['status'],'pending')

    def test_evidence_keeps_actual_last_frame_roi_and_all_source_pairs(self):
        result=self.evidence();proof=factory.read(self.project/result['evidence']['path'])
        self.assertEqual(proof['overview_frame_indices'],list(range(90)))
        self.assertEqual(proof['native_rois'][0]['coverage'][-1]['frame'],89)
        self.assertTrue((self.project/'candidate-proof/output-000089-visible_role_A.png').is_file())
        self.assertEqual(len(proof['source_pose_pairs']),8)
        self.assertEqual(proof['visual_status'],'pending')
        self.assertIn(15,proof['consecutive_identical_rgb_pairs'])

    def test_default_nominal_pts_remains_strict(self):
        record=copy.deepcopy(self.record);record['source_to_output']=self.mapping
        record['retained_output_interval']=[0,75];record['output_clock_review']={'status':'observed','intervals':[[0,90]],'notes':'Synthetic full clock.'}
        self.pass_fixture_visual(record)
        with self.assertRaisesRegex(ValueError,'actual frame times'):factory.review_guard(self.project)

    def test_measured_map_resets_visual_then_preserves_exact_source_pts(self):
        record=self.apply();self.assertEqual(record['visual']['status'],'pending')
        with self.assertRaises(ValueError):factory.review_guard(self.project)
        self.pass_fixture_visual(record);self.assertEqual(factory.review_guard(self.project)[0][1]['source_to_output'],self.mapping)
        result=factory.assemble(self.project,'restored.mp4',DEPS,SCALAR)
        self.assertEqual(result['video']['frame_count'],8)
        self.assertEqual([Fraction(f['time']) for f in result['video']['frames']], [Fraction(i,12) for i in range(8)])

    def test_missing_whole_output_observation_rejected(self):
        contract=self.contract();contract['output_clock_review']['intervals']=[[0,76]]
        factory.write(self.project/'measured-map.json',contract)
        with self.assertRaisesRegex(ValueError,'whole output'):review.apply_mapping(self.project,self.wid,'measured-map.json')

    def test_missing_source_final_pose_rejected(self):
        contract=self.contract();contract['source_pose_review']['anchors'].pop()
        factory.write(self.project/'measured-map.json',contract)
        with self.assertRaisesRegex(ValueError,'pose observations'):review.apply_mapping(self.project,self.wid,'measured-map.json')

    def test_changed_evidence_artifact_invalidates_mapping(self):
        record=self.apply();self.pass_fixture_visual(record)
        (self.project/'candidate-proof/output-000089-visible_role_A.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'artifact changed'):factory.review_guard(self.project)

    def test_mapping_tampering_invalidates_digest(self):
        record=self.apply();record['source_to_output'][1]=17;self.pass_fixture_visual(record)
        with self.assertRaisesRegex(ValueError,'changed|ownership'):factory.review_guard(self.project)

    def test_invalid_indices_never_create_evidence(self):
        for bad in [-1,90,True,1.5]:
            mapping=list(self.mapping);mapping[0]=bad
            with self.assertRaises(ValueError):review.prepare_evidence(self.project,self.wid,self.relative,'bad-evidence',mapping,deps=DEPS,scalar_cpu=SCALAR)
        self.assertFalse((self.project/'bad-evidence').exists())

    def test_unknown_mode_and_cross_cut_rejected(self):
        record=self.apply();self.pass_fixture_visual(record)
        record['mapping_mode']='guessed';self.pass_fixture_visual(record)
        with self.assertRaisesRegex(ValueError,'Unknown candidate'):factory.review_guard(self.project)
        record['mapping_mode']='measured-source-poses'
        analysis=copy.deepcopy(self.analysis);analysis['shots']=[{'start':0},{'start':4}]
        contract=record['measured_mapping'];contract.update(source_cut_before=[4],output_cut_before=[50])
        contract['mapping_digest']=review.mapping_digest(contract)
        record['visual']['measured_mapping_digest']=contract['mapping_digest']
        with self.assertRaisesRegex(ValueError,'shot boundary'):
            review.validate_measured_mapping(self.project,self.source,analysis,self.window,self.entry,record)

    def test_candidate_evidence_cannot_enter_source_template_assets(self):
        for relative in ['evidence/current-candidate','assets/current-candidate']:
            with self.assertRaisesRegex(ValueError,'reusable source'):
                review.prepare_evidence(self.project,self.wid,self.relative,relative,self.mapping,deps=DEPS,scalar_cpu=SCALAR)
            self.assertFalse((self.project/relative).exists())
        self.evidence();result=factory.build_skill(self.project,self.root/'source-only-skills','draft','中性原片·换角复刻')
        with zipfile.ZipFile(result['zip']['path']) as archive:
            self.assertFalse(any('current-candidate' in name or 'candidate-proof' in name for name in archive.namelist()))

    def test_new_valid_mapping_evidence_cannot_inherit_old_visual_verdict(self):
        record=self.apply();self.pass_fixture_visual(record)
        old_visual=copy.deepcopy(record['visual']);new_mapping=list(self.mapping);new_mapping[2]=23
        evidence=review.prepare_evidence(self.project,self.wid,self.relative,'second-mapping-evidence',new_mapping,deps=DEPS,scalar_cpu=SCALAR)
        contract=record['measured_mapping'];contract.update(evidence=evidence['evidence'],source_to_output=new_mapping)
        contract['mapping_digest']=review.mapping_digest(contract)
        record['source_to_output']=new_mapping;record['visual']=old_visual
        factory.write(self.project/'reviews.json',{'windows':{self.wid:record}})
        with self.assertRaisesRegex(ValueError,'Visual verdict belongs'):factory.review_guard(self.project)

    def test_manually_misfiled_run_evidence_cannot_be_bundled(self):
        self.evidence()
        shutil.copytree(self.project/'candidate-proof',self.project/'evidence/copied-candidate')
        with self.assertRaisesRegex(ValueError,'source-only skill'):
            factory.build_skill(self.project,self.root/'blocked-skills','draft','中性原片·换角复刻')
        self.assertFalse((self.root/'blocked-skills').exists())

    def test_reference_change_and_path_escape_are_rejected(self):
        (self.project/'refs/A.png').write_bytes(b'new reference')
        with self.assertRaisesRegex(ValueError,'input changed'):self.evidence()
        with self.assertRaises(ValueError):factory.resolve(self.project,'../escape')

    def test_zip_helper_runs_independently_with_chinese_context_and_no_authorization(self):
        result=factory.build_skill(self.project,self.root/'skills','draft','合成动作测试·换角复刻')
        extracted=self.root/'extract';extracted.mkdir()
        with zipfile.ZipFile(result['zip']['path']) as archive:archive.extractall(extracted)
        skill=extracted/result['name'];self.assertTrue((skill/'scripts/candidate_review.py').is_file())
        self.assertEqual(factory.read(skill/'meta.json')['display_name'],'合成动作测试·换角复刻')
        self.assertTrue(factory.read(skill/'features.json')['complete_output_evidence_runtime'])
        child=subprocess.run([sys.executable,'-B','-X','utf8',str(skill/'scripts/candidate_review.py'),'--help'],
            cwd=self.root,capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr)
        fresh=self.root/'fresh';factory.instantiate(skill,fresh)
        self.assertFalse(factory.read(fresh/'job.json')['production_authorized'])
        self.assertEqual(factory.read(fresh/'job.json')['references'],{})
        # Run a real canonical proof from extracted helper, without original factory cwd.
        plan,video=self.rgb_fixture()
        child=subprocess.run([sys.executable,'-B','-X','utf8',str(skill/'scripts/candidate_review.py'),'verify-rgb',
            '--plan',str(plan),'--video',str(video),'--out',str(self.root/'zip-rgb-proof.json')],cwd=self.root,capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr)

    def rgb_fixture(self):
        from PIL import Image
        directory=self.root/'canonical';directory.mkdir();av=media.runtime(DEPS,SCALAR);rows=[]
        video=self.root/'canonical-rgb.nut'
        with av.open(str(video),'w',format='nut') as container:
            stream=container.add_stream('rawvideo',rate=12);stream.width=64;stream.height=48;stream.pix_fmt='rgb24'
            stream.time_base=Fraction(1,49152);stream.codec_context.thread_count=1
            for i in range(3):
                image=Image.new('RGB',(64,48),(i*40,90,110));name=f'{i:03d}.png';image.save(directory/name)
                rows.append({'png':name,'rgb_sha256':hashlib.sha256(image.tobytes()).hexdigest(),'time':str(Fraction(i,12)),'duration':'1/12'})
                frame=av.VideoFrame.from_image(image);frame.pts=i*4096;frame.time_base=Fraction(1,49152)
                for packet in stream.encode(frame):container.mux(packet)
            for packet in stream.encode():container.mux(packet)
        plan=directory/'plan.json';factory.write(plan,{'schema_version':1,'dimensions':[64,48],'frames':rows})
        return plan,video

    def test_canonical_independent_rgb_pts_positive_and_one_pixel_negative(self):
        from PIL import Image
        plan,video=self.rgb_fixture();proof=review.canonical_rgb(plan,video,self.root/'proof.json')
        self.assertEqual(proof['status'],'passed');self.assertEqual(proof['visual_status'],'pending')
        with Image.open(plan.parent/'001.png') as image:
            changed=image.copy();changed.putpixel((0,0),(1,2,3));changed.save(plan.parent/'001.png')
        with self.assertRaisesRegex(ValueError,'PNG changed'):review.canonical_rgb(plan,video,self.root/'bad-pixel.json')
        # A new valid plan with one changed RGB pixel still rejects the old video.
        data=factory.read(plan);data['frames'][1]['rgb_sha256']=hashlib.sha256(changed.tobytes()).hexdigest();factory.write(plan,data)
        with self.assertRaisesRegex(ValueError,'RGB/PTS mismatch'):review.canonical_rgb(plan,video,self.root/'bad-video-pixel.json')
        self.assertEqual(factory.read(self.root/'bad-video-pixel.json')['status'],'failed')

    def test_canonical_missing_frame_or_wrong_pts_rejected(self):
        plan,video=self.rgb_fixture();data=factory.read(plan);data['frames'][1]['time']='1/6'
        data['frames'][2]['time']='1/3';factory.write(plan,data)
        with self.assertRaisesRegex(ValueError,'RGB/PTS mismatch'):review.canonical_rgb(plan,video,self.root/'bad-clock.json')
        self.assertEqual(factory.read(self.root/'bad-clock.json')['status'],'failed')
        data['frames']=data['frames'][:2];data['frames'][1]['time']='1/12';factory.write(plan,data)
        with self.assertRaisesRegex(ValueError,'RGB/PTS mismatch'):review.canonical_rgb(plan,video,self.root/'bad-count.json')

    def test_plan_changed_during_independent_decode_cannot_pass(self):
        plan,video=self.rgb_fixture();original_run=subprocess.run
        def run(command,*args,**kwargs):
            if '-show_entries' in command:
                data=factory.read(plan);data['new_revision']='changed during proof';factory.write(plan,data)
            return original_run(command,*args,**kwargs)
        with patch.object(review.subprocess,'run',side_effect=run):
            with self.assertRaisesRegex(ValueError,'plan changed'):review.canonical_rgb(plan,video,self.root/'raced-plan.json')
        self.assertFalse((self.root/'raced-plan.json').exists())

    def test_deps_cli_works_without_site_initialization(self):
        import av
        plan,video=self.rgb_fixture();deps=Path(av.__file__).parent.parent
        command=[sys.executable,'-S','-B','-X','utf8',str(Path(review.__file__).resolve()),'verify-rgb',
            '--deps',str(deps),'--plan',str(plan),'--video',str(video),'--out',str(self.root/'isolated-deps-proof.json')]
        result=subprocess.run(command,cwd=self.root,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)


def main():
    global DEPS,SCALAR,WORK
    ap=argparse.ArgumentParser();ap.add_argument('--deps');ap.add_argument('--scalar-cpu',action='store_true');ap.add_argument('--work')
    args=ap.parse_args();DEPS=args.deps;SCALAR=args.scalar_cpu
    WORK=Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix='candidate-review-tests-'))
    WORK.mkdir(parents=True,exist_ok=True);fixtures.DEPS=DEPS;fixtures.SCALAR=SCALAR
    socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Network forbidden in offline review tests'))
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CandidateReviewTests))
    factory.write(WORK/'review-test-results.json',{'tests':result.testsRun,'passed':result.wasSuccessful(),
        'failures':len(result.failures),'errors':len(result.errors),'cloud_calls':0})
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__=='__main__':main()

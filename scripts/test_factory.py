"""Offline integration tests using real decoded media, never cloud generation."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
import factory
import media

DEPS = None
WORK = None
SCALAR = False


def video_fixture(path, fps=12, pts=None, width=64, height=48,rotation=0,sar=Fraction(1),colors=None):
    av = media.runtime(DEPS, SCALAR)
    from PIL import Image, ImageDraw
    pts = pts if pts is not None else list(range(8))
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, 'yuv420p'
        stream.time_base = Fraction(1, fps)
        stream.codec_context.time_base = Fraction(1, fps)
        stream.codec_context.thread_count = 1
        stream.codec_context.sample_aspect_ratio=sar
        for name,value in (colors or {}).items():
            setattr(stream.codec_context,name,value)
        if rotation:
            stream.set_display_rotation(rotation)
        stream.options = {'crf':'0','preset':'ultrafast','tune':'zerolatency'}
        duration_by_pts = {p: pts[i+1]-p if i+1<len(pts) else 1 for i,p in enumerate(pts)}
        for i,p in enumerate(pts):
            image = Image.new('RGB', (width,height), (30+i*20,20+i*8,220-i*15))
            ImageDraw.Draw(image).rectangle((2+i*4,4,6+i*4,20), fill='white')
            frame = av.VideoFrame.from_image(image)
            frame.pts,frame.time_base = p,Fraction(1,fps)
            for packet in stream.encode(frame):
                packet.duration = duration_by_pts[int(packet.pts)]
                output.mux(packet)
        for packet in stream.encode():
            packet.duration = duration_by_pts[int(packet.pts)]
            output.mux(packet)
    return path


def frames(path):
    av=media.runtime(DEPS,SCALAR)
    with av.open(str(path)) as container:
        return [f.to_image() for f in container.decode(video=0)]


class FactoryIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='factory-test-',dir=WORK))
        self.video = video_fixture(self.temp/'原视频.mp4')
        self.project = self.temp/'run'
        self.source = factory.ingest(self.project,self.video,DEPS,SCALAR)
        self.analysis = factory.read(self.project/'analysis.json')
        self.analysis.update(status='passed',roles=[{'slot':'A','label':'Source left person','appearances':[[0,8]]}],
                             review_notes='Offline synthetic fixture; no real visual-production claim.')
        self.analysis['shots']=[{'id':'s1','start':0,'end':8,'roles':['A'],
                                 'description_en':'{{A}} stays in the original measured composition.',
                                 'events':[{'source_frame':3,'description_en':'{{A}} continues the source movement.'}]}]
        factory.write(self.project/'analysis.json',self.analysis)
        (self.project/'references-input').mkdir()
        from PIL import Image
        Image.new('RGB',(32,32),'green').save(self.project/'references-input'/'A.png')
        self.job=factory.read(self.project/'job.json')
        self.job.update(references={'A':{'path':'references-input/A.png','identity_en':'Green reference character, preserving unspecified clothing.'}},
                        profile={'kind':'legacy-ai-app','app_id':'OFFLINE_FIXTURE_ONLY','instance_type':'mock',
                                 'limits':{'fps':['12'],'frame_counts':[8,16],'identity_slots':2}})
        factory.write(self.project/'job.json',self.job)

    def tearDown(self):
        # Keep actual regression artifacts for inspection; all live files are in isolated work.
        pass

    def queue(self):
        return factory.prepare(self.project,DEPS,SCALAR,True)

    def candidate(self):
        queue=self.queue()
        output='candidates/mock.mp4'
        (self.project/'candidates').mkdir(exist_ok=True)
        shutil.copy2(self.video,self.project/output)
        record={'input_digest':queue['jobs'][0]['input_digest'],'task_id':'OFFLINE_FIXTURE',
                'output_path':output,'file_signature':factory.file_signature(self.project,output),
                'technical':{'status':'passed','report':media.inspect(self.project/output,DEPS,SCALAR)},
                'visual':{'status':'passed','dynamic_intervals':[[0,8]],'native_intervals':[[0,8]],
                          'notes':'Synthetic fixture to exercise guards; not a real identity review.'},
                'retained_output_interval':[0,8],'source_to_output':list(range(8))}
        reviews={'windows':{'window_001':record}}
        factory.write(self.project/'reviews.json',reviews)
        return reviews

    def test_actual_ingest_pts_and_evidence(self):
        self.assertEqual(self.source['video']['frame_count'],8)
        self.assertEqual([Fraction(f['time']) for f in self.source['video']['frames']],
                         [Fraction(i,12) for i in range(8)])
        self.assertTrue((self.project/'evidence/frame_000000.png').exists())
        self.assertEqual(self.source['source']['sha256'],media.signature(self.video)['sha256'])

    def test_default_legacy_excludes_over_ten_seconds_without_a_fixed_five_second_default(self):
        job=copy.deepcopy(self.job)
        job['profile']['limits']['frame_counts']=[121,144]
        with self.assertRaisesRegex(ValueError,'hard cap 10 seconds'):
            factory.default_plan(self.source,self.analysis,job)
        job['profile']['limits']['frame_counts'].insert(0,72)
        self.assertEqual(factory.default_plan(self.source,self.analysis,job)['windows'][0]['generation_frames'],72)

    def test_manual_generation_cap_includes_context_preroll_and_tail_padding(self):
        plan=factory.default_plan(self.source,self.analysis,self.job)
        w=plan['windows'][0];w.update(generation_frames=120,duration='10')
        w['source_projection']=media.project_frames(self.source['video'],w['context_interval'],'12',120)
        factory.validate_plan(self.source,self.analysis,plan)
        for frames,duration in [(121,'10'),(120,'11')]:
            with self.subTest(frames=frames,duration=duration):
                w.update(generation_frames=frames,duration=duration)
                w['source_projection']=media.project_frames(self.source['video'],w['context_interval'],'12',frames)
                with self.assertRaisesRegex(ValueError,'10-second production cap'):
                    factory.validate_plan(self.source,self.analysis,plan)

    def test_standard_six_second_shot_stays_whole_but_eleven_seconds_requires_split(self):
        video=video_fixture(self.temp/'long-standard.mp4',fps=12,pts=list(range(72)))
        source=factory.ingest(self.temp/'long-standard-run',video,DEPS,SCALAR)
        analysis={'roles':[{'slot':'A','appearances':[[0,72]]}],
                  'shots':[{'id':'s1','start':0,'end':72,'roles':['A'],'description_en':'{{A}} follows original motion.'}]}
        job={'profile':{'kind':'standard-h3','limits':{}},'references':self.job['references']}
        plan=factory.default_plan(source,analysis,job)
        self.assertEqual(len(plan['windows']),1)
        self.assertEqual(plan['windows'][0]['source_interval'],[0,72])
        self.assertEqual(plan['windows'][0]['duration'],'6')
        video=video_fixture(self.temp/'eleven-standard.mp4',fps=12,pts=list(range(132)))
        source=factory.ingest(self.temp/'eleven-standard-run',video,DEPS,SCALAR)
        analysis['roles'][0]['appearances']=[[0,132]];analysis['shots'][0]['end']=132
        with self.assertRaisesRegex(ValueError,'hard cap 10 seconds'):
            factory.default_plan(source,analysis,job)

    def test_source_same_resume_and_new_source_rejected(self):
        self.assertEqual(factory.ingest(self.project,self.video,DEPS,SCALAR)['source'],self.source['source'])
        other=video_fixture(self.temp/'other.mp4',pts=[0])
        with self.assertRaises(ValueError): factory.ingest(self.project,other,DEPS,SCALAR)

    def test_analysis_pending_cannot_prepare(self):
        self.analysis['status']='pending'
        factory.write(self.project/'analysis.json',self.analysis)
        with self.assertRaises(ValueError): self.queue()

    def test_analysis_actual_frame_gap_rejected(self):
        self.analysis['shots'][0]['end']=7
        factory.write(self.project/'analysis.json',self.analysis)
        with self.assertRaises(ValueError): factory.validate_analysis(self.project)

    def test_actual_preparation_prompt_and_audio(self):
        queue=self.queue()
        entry=queue['jobs'][0]
        self.assertFalse(entry['production_authorized'])
        self.assertEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])
        self.assertEqual(entry['semantic_review']['input_digest'],entry['input_digest'])
        report=media.inspect(self.project/entry['video_path'],DEPS,SCALAR)
        self.assertEqual(report['video']['frame_count'],8)
        self.assertEqual(len(report['audio_streams']),1)
        text=(self.project/entry['prompt_path']).read_text('utf-8')
        self.assertNotIn('{{A}}',text)
        self.assertEqual(text.count('<Picture 1>'),1)

    def test_prompt_semantics_not_auto_passed(self):
        entry=factory.prepare(self.project,DEPS,SCALAR)['jobs'][0]
        self.assertEqual(entry['semantic_review']['status'],'pending')

    def test_compiled_reference_provenance_and_local_role_order(self):
        analysis=copy.deepcopy(self.analysis)
        analysis['roles'].append({'slot':'B','label':'Second source role','appearances':[[0,8]]})
        analysis['shots'][0].update(roles=['A','B'],description_en='{{A}} holds the left pose while {{B}} holds the right pose.')
        job=copy.deepcopy(self.job)
        job['references']['B']={'identity_en':'Distinct second portrait identity.'}
        window=factory.default_plan(self.source,analysis,job)['windows'][0]
        window['role_slots']=['B','A']
        text,binding=factory.compile_prompt(self.source,analysis,job,window)
        definitions=text.split('summary:',1)[0]
        provenance=dict(re.findall(r'<Subject (\d+)> is the person represented by <Picture (\d+)>',definitions))
        self.assertEqual(provenance,{'1':'1','2':'2'})
        self.assertEqual(binding,[{'slot':'B','subject':1,'picture':1},{'slot':'A','subject':2,'picture':2}])
        self.assertIn('<Subject 2> holds the left pose while <Subject 1> holds the right pose.',text)
        self.assertEqual(set(re.findall(r'<Video (\d+)>',text)),set(re.findall(r'^<Video (\d+)>',definitions,re.MULTILINE)))
        self.assertEqual(set(re.findall(r'<Subject (\d+)>',text)),set(provenance))
        self.assertEqual(len(re.findall(r'^<Video 1>',definitions,re.MULTILINE)),1)

    def test_face_only_source_definition_cannot_override_scope(self):
        job=copy.deepcopy(self.job);job['replacement_mode']='face-only'
        job['references']['A']['face_identity_en']='Narrow face with specified eyes.'
        window=factory.default_plan(self.source,self.analysis,job)['windows'][0]
        text,_=factory.compile_prompt(self.source,self.analysis,job,window)
        self.assertNotIn('Green reference character',text)
        definitions=text.split('summary:',1)[0]
        self.assertIn('Source appearance follows the replacement scope below.',definitions)
        self.assertIn('Preserve the source hairstyle, clothing, accessories, physique, neck and shoulder proportions.',text)
        self.assertNotIn('Facial geometry and physique follow the supplied reference attributes',text)

    def test_inspect_candidate_creates_pending_actual_report_and_map(self):
        entry=self.queue()['jobs'][0]
        (self.project/'candidates').mkdir()
        shutil.copy2(self.video,self.project/'candidates/native.mp4')
        record=factory.inspect_candidate(self.project,'window_001','OFFLINE_FIXTURE','candidates/native.mp4',DEPS,SCALAR)
        self.assertEqual(record['visual']['status'],'pending')
        self.assertEqual(record['source_to_output'],list(range(8)))
        self.assertEqual(record['technical']['report']['video']['frame_count'],8)
        self.assertTrue((self.project/'candidate-evidence/window_001/frame_000007.png').exists())
        with self.assertRaises(ValueError): factory.review_guard(self.project)

    def test_changed_reference_changes_digest(self):
        entry=self.queue()['jobs'][0]
        from PIL import Image
        Image.new('RGB',(32,32),'blue').save(self.project/'references-input/A.png')
        self.assertNotEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])

    def test_changed_job_mapping_plan_and_source_fact_invalidate_queue(self):
        entry=self.queue()['jobs'][0]
        self.job['references']['A']['identity_en']='Different interpreted identity'
        factory.write(self.project/'job.json',self.job)
        self.assertNotEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])
        self.job['references']['A']['identity_en']='Green reference character, preserving unspecified clothing.'
        factory.write(self.project/'job.json',self.job)
        plan=factory.read(self.project/'plan.json')
        plan['windows'][0]['risk_source_intervals']=[[0,3]]
        factory.write(self.project/'plan.json',plan)
        self.assertNotEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])

    def test_quality_promotion_and_authorization_do_not_rewrite_generation(self):
        entry=self.queue()['jobs'][0]
        self.job['profile']['quality_evidence']='case-verified'
        self.job['production_authorized']=True
        factory.write(self.project/'job.json',self.job)
        self.assertEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])
        refreshed=self.queue()['jobs'][0]
        self.assertEqual(refreshed['input_digest'],entry['input_digest'])
        self.assertTrue(refreshed['production_authorized'])
        self.assertFalse(refreshed['pilot'])

    def test_face_only_contract_uses_only_face_dossier(self):
        self.job['replacement_mode']='face-only'
        self.job['references']['A']['face_identity_en']='Oval facial outline and brown eyes.'
        factory.write(self.project/'job.json',self.job)
        entry=self.queue()['jobs'][0]
        text=(self.project/entry['prompt_path']).read_text('utf-8')
        self.assertIn('Preserve the source hairstyle, clothing, accessories, physique',text)
        self.assertNotIn('Green reference character',text)
        self.assertNotIn('physique follow the supplied reference',text)

    def test_owner_gap_overlap_and_context(self):
        plan=factory.default_plan(self.source,self.analysis,self.job)
        plan['windows'][0]['source_interval']=[0,7]
        plan['windows'][0]['retained_generation_interval']=[0,7]
        with self.assertRaises(ValueError): factory.validate_plan(self.source,self.analysis,plan)

    def test_partial_long_shot_requires_reviewed_context_state(self):
        plan=factory.default_plan(self.source,self.analysis,self.job)
        first=plan['windows'][0]
        first['source_interval']=[0,4]
        first['context_interval']=[0,4]
        first['retained_generation_interval']=[0,4]
        first['risk_source_intervals']=[[0,4]]
        second=copy.deepcopy(first)
        second.update(window_id='window_002',source_interval=[4,8],context_interval=[4,8],risk_source_intervals=[[4,8]])
        plan['windows'].append(second)
        with self.assertRaises(ValueError): factory.validate_plan(self.source,self.analysis,plan)
        first['context_description_en']='{{A}} continues the first measured movement.'
        second['context_description_en']='{{A}} is already in the measured mid-shot pose and continues.'
        for window in plan['windows']:
            window['source_projection']=media.project_frames(self.source['video'],window['context_interval'],window['generation_fps'],window['generation_frames'])
        factory.validate_plan(self.source,self.analysis,plan)

    def test_context_background_role_must_be_in_identity_binding(self):
        analysis=copy.deepcopy(self.analysis)
        analysis['roles'].append({'slot':'B','label':'Partial foreground sleeve','appearances':[[1,2]]})
        plan=factory.default_plan(self.source,analysis,self.job)
        with self.assertRaises(ValueError): factory.validate_plan(self.source,analysis,plan)
        plan=factory.default_plan(self.source,self.analysis,self.job)
        plan['windows'].append(copy.deepcopy(plan['windows'][0]))
        plan['windows'][1]['window_id']='other'
        with self.assertRaises(ValueError): factory.validate_plan(self.source,self.analysis,plan)

    def test_review_current_pass_stale_mtime_and_pending(self):
        reviews=self.candidate()
        self.assertEqual(len(factory.review_guard(self.project)),1)
        record=reviews['windows']['window_001']
        record['visual']['status']='pending'
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError): factory.review_guard(self.project)
        record['visual']['status']='passed'
        factory.write(self.project/'reviews.json',reviews)
        path=self.project/record['output_path']
        os.utime(path,ns=(path.stat().st_atime_ns,path.stat().st_mtime_ns+1000000))
        with self.assertRaises(ValueError): factory.review_guard(self.project)

    def test_thumbnail_cannot_pass_risk_review(self):
        reviews=self.candidate()
        reviews['windows']['window_001']['visual']['native_intervals']=[]
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError): factory.review_guard(self.project)

    def test_reverse_and_wrong_time_mapping_rejected(self):
        reviews=self.candidate()
        reviews['windows']['window_001']['source_to_output']=list(reversed(range(8)))
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError): factory.review_guard(self.project)
        reviews['windows']['window_001']['source_to_output']=[0,0,2,3,4,5,6,7]
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError): factory.review_guard(self.project)

    def test_actual_assembly_order_pts_silent(self):
        self.candidate()
        report=factory.assemble(self.project,'final.mp4',DEPS,SCALAR)
        self.assertEqual(report['video']['frame_count'],8)
        self.assertEqual(report['audio_streams'],[])
        self.assertEqual(report['visual_status'],'pending')
        expected,observed=frames(self.video),frames(self.project/'outputs/final.mp4')
        for a,b in zip(expected,observed):
            self.assertLess(max(abs(x-y) for x,y in zip(a.getpixel((50,30)),b.getpixel((50,30)))),8)
        with self.assertRaises(FileExistsError): factory.assemble(self.project,'final.mp4',DEPS,SCALAR)

    def test_final_pending_cannot_ready(self):
        self.candidate()
        factory.assemble(self.project,'final.mp4',DEPS,SCALAR)
        with self.assertRaises(ValueError): factory.build_skill(self.project,self.temp/'skills','ready')

    def test_final_native_review_and_current_composition_required(self):
        self.candidate()
        factory.assemble(self.project,'final.mp4',DEPS,SCALAR)
        reviews=factory.read(self.project/'reviews.json')
        reviews['final']['visual']={'status':'passed','dynamic_intervals':[[0,8]],'native_intervals':[],
                                    'notes':'Offline fixture only.'}
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError): factory.review_guard(self.project,True)
        reviews['final']['visual']['native_intervals']=[[0,8]]
        factory.write(self.project/'reviews.json',reviews)
        self.assertEqual(factory.review_guard(self.project,True)['visual']['status'],'passed')
        reviews['windows']['window_001']['task_id']='different-candidate-version'
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError): factory.review_guard(self.project,True)

    def final_review_fixture(self):
        self.candidate()
        factory.assemble(self.project,'technical-guard-fixture.mp4',DEPS,SCALAR)
        reviews=factory.read(self.project/'reviews.json')
        reviews['final']['visual']={'status':'passed','dynamic_intervals':[[0,8]],'native_intervals':[[0,8]],
                                  'notes':'Isolated synthetic final guard exercise; no production visual approval.'}
        factory.write(self.project/'reviews.json',reviews)
        factory.review_guard(self.project,True)
        return reviews

    def test_final_missing_decoded_report_rejected(self):
        original=self.final_review_fixture()
        for report in (None,{}):
            with self.subTest(report=report):
                reviews=copy.deepcopy(original)
                reviews['final']['technical']['report']=report
                factory.write(self.project/'reviews.json',reviews)
                with self.assertRaises(ValueError):factory.review_guard(self.project,True)
        del reviews['final']['technical']['report']
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_decoded_report_signature_must_match_current_file(self):
        original=self.final_review_fixture()
        for field,value in (('sha256','wrong-actual-file'),('size_bytes',1),('mtime_ns',1)):
            with self.subTest(field=field):
                reviews=copy.deepcopy(original)
                reviews['final']['technical']['report']['file_signature'][field]=value
                factory.write(self.project/'reviews.json',reviews)
                with self.assertRaises(ValueError):factory.review_guard(self.project,True)
        reviews['final']['technical']['report'].pop('file_signature')
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_decoded_report_canvas_must_preserve_source(self):
        original=self.final_review_fixture()
        for field,value in (('width',128),('height',96),('sample_aspect_ratio','4/3'),
                            ('rotation','90'),('color_metadata',{})):
            with self.subTest(field=field):
                reviews=copy.deepcopy(original)
                reviews['final']['technical']['report']['video'][field]=value
                factory.write(self.project/'reviews.json',reviews)
                with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_decoded_report_frame_count_and_frames_required(self):
        original=self.final_review_fixture()
        for truncate in (False,True):
            reviews=copy.deepcopy(original)
            video=reviews['final']['technical']['report']['video']
            if truncate:video['frames']=video['frames'][:-1]
            else:video['frame_count']=7
            factory.write(self.project/'reviews.json',reviews)
            with self.assertRaises(ValueError):factory.review_guard(self.project,True)
        video.pop('frames')
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_decoded_report_pts_duration_and_interval_must_preserve_source(self):
        original=self.final_review_fixture()
        for change in ('pts','time','duration','start','end','time_base','index'):
            with self.subTest(change=change):
                reviews=copy.deepcopy(original)
                video=reviews['final']['technical']['report']['video']
                if change=='pts':video['frames'][3]['pts']+=1
                elif change=='time':video['frames'][3]['time']='5/12'
                elif change=='duration':video['frames'][-1]['duration']='1/6'
                elif change=='start':video['start_time']='1/12'
                elif change=='end':video['end_time']='3/4'
                elif change=='time_base':video['time_base']='0'
                else:video['frames'][3]['index']=4
                factory.write(self.project/'reviews.json',reviews)
                with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_audio_packet_verification_required_even_for_silent_source(self):
        original=self.final_review_fixture()
        for value in (None,'pending','failed'):
            reviews=copy.deepcopy(original)
            reviews['final']['technical']['report']['audio_packet_verification']=value
            factory.write(self.project/'reviews.json',reviews)
            with self.assertRaises(ValueError):factory.review_guard(self.project,True)
        reviews=copy.deepcopy(original)
        reviews['final']['technical']['report']['audio_streams']=[{'codec':'aac'}]
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_real_aac_report_track_contract_required(self):
        audio_source=self.temp/'audio-source.mp4'
        command=[media.find_ffmpeg(DEPS),'-nostdin','-v','error','-threads','1','-i',str(self.video),
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.5',
                 '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac',
                 '-metadata:s:a:0','language=eng',str(audio_source)]
        completed=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stderr)
        previous=self.project
        self.project=self.temp/'audio-guard-run'
        self.video=audio_source
        self.source=factory.ingest(self.project,audio_source,DEPS,SCALAR)
        self.analysis['source_sha256']=self.source['source']['sha256']
        factory.write(self.project/'analysis.json',self.analysis)
        shutil.copytree(previous/'references-input',self.project/'references-input')
        factory.write(self.project/'job.json',self.job)
        original=self.final_review_fixture()
        self.assertEqual(len(original['final']['technical']['report']['audio_streams']),1)
        for field,value in (('codec','mp3'),('sample_rate',32000),('layout','stereo'),
                            ('disposition',0),('decoded_samples',0),('metadata',{'language':'zho'})):
            with self.subTest(field=field):
                reviews=copy.deepcopy(original)
                reviews['final']['technical']['report']['audio_streams'][0][field]=value
                factory.write(self.project/'reviews.json',reviews)
                with self.assertRaises(ValueError):factory.review_guard(self.project,True)
        reviews=copy.deepcopy(original)
        reviews['final']['technical']['report']['audio_streams']=[]
        factory.write(self.project/'reviews.json',reviews)
        with self.assertRaises(ValueError):factory.review_guard(self.project,True)

    def test_final_normalized_rotated_sar_vfr_and_nonzero_origin_reports_pass(self):
        for angle,pts in ((90,[0,1,3,6,7]),(0,[12,13,15,18,19])):
            with self.subTest(angle=angle,pts=pts):
                original=video_fixture(self.temp/f'guard-source-{angle}.mp4',pts=pts,
                                       rotation=angle,sar=Fraction(4,3))
                project=self.temp/f'guard-source-{angle}'
                source=factory.ingest(project,original,DEPS,SCALAR)
                analysis=factory.read(project/'analysis.json')
                analysis.update(status='passed',roles=[],shots=[{'id':'source','start':0,'end':5,
                                'roles':[],'description_en':'Synthetic source-only guard fixture.','events':[]}])
                factory.write(project/'analysis.json',analysis)
                factory.prepare(project,DEPS,SCALAR)
                factory.assemble(project,'normalized.mp4',DEPS,SCALAR)
                reviews=factory.read(project/'reviews.json')
                reviews['final']['visual']={'status':'passed','dynamic_intervals':[[0,5]],'native_intervals':[[0,5]],
                                          'notes':'Isolated synthetic display/timeline fixture only.'}
                factory.write(project/'reviews.json',reviews)
                record=factory.review_guard(project,True)
                canvas=media.display_canvas(source['video'])
                self.assertEqual((record['technical']['report']['video']['width'],
                                  record['technical']['report']['video']['height']),
                                 (canvas['width'],canvas['height']))

    def test_draft_to_ready_creates_preserved_revision(self):
        self.candidate()
        draft=factory.build_skill(self.project,self.temp/'skills')
        factory.assemble(self.project,'final.mp4',DEPS,SCALAR)
        reviews=factory.read(self.project/'reviews.json')
        reviews['final']['visual']={'status':'passed','dynamic_intervals':[[0,8]],'native_intervals':[[0,8]],
                                    'notes':'Offline synthetic fixture review guard exercise, not cloud visual QA.'}
        factory.write(self.project/'reviews.json',reviews)
        ready=factory.build_skill(self.project,self.temp/'skills','ready')
        self.assertTrue(Path(draft['path']).exists())
        self.assertNotEqual(ready['name'],draft['name'])
        self.assertEqual(ready['status'],'ready')
        registry=factory.recover_install(self.temp/'skills')
        self.assertEqual(registry['sources'][self.source['source']['sha256']]['previous_versions'],[draft['name']])

    def test_draft_build_dedupe_zip_and_independent_instantiate(self):
        self.queue()
        (self.project/'api_key.txt').write_text('SECRET_SENTINEL',encoding='utf-8')
        result=factory.build_skill(self.project,self.temp/'skills')
        self.assertEqual(result['status'],'draft')
        skill=Path(result['path'])
        self.assertEqual(media.signature(skill/'assets/source.mp4')['sha256'],self.source['source']['sha256'])
        import zipfile
        with zipfile.ZipFile(result['zip']['path']) as archive:
            self.assertFalse(any('api_key' in p for p in archive.namelist()))
            self.assertFalse(any(b'SECRET_SENTINEL' in archive.read(p) for p in archive.namelist()))
        again=factory.build_skill(self.project,self.temp/'skills')
        self.assertTrue(again['reused'])
        new_run=self.temp/'independent'
        factory.instantiate(skill,new_run)
        self.assertEqual(factory.source_current(new_run)['source']['sha256'],self.source['source']['sha256'])
        self.assertEqual(factory.read(new_run/'job.json')['references'],{})
        self.assertFalse(factory.read(new_run/'job.json')['production_authorized'])

    def test_chinese_title_cli_zip_and_independent_reuse(self):
        import zipfile
        title = '双人物客厅换角·实拍版'
        cli = subprocess.run([sys.executable,'-B','-X','utf8',str(Path(factory.__file__)),
                              'build-skill','--project',str(self.project),
                              '--skills-root',str(self.temp/'chinese-skills'),
                              '--display-name',title],capture_output=True,text=True,encoding='utf8',timeout=60)
        self.assertEqual(cli.returncode,0,cli.stdout+cli.stderr)
        built=json.loads(cli.stdout)
        self.assertEqual(built['display_name'],title)
        self.assertEqual(built['name'],'r2va-video-'+self.source['source']['sha256'][:12])
        with zipfile.ZipFile(built['zip']['path']) as archive:
            self.assertIsNone(archive.testzip())
            archive.extractall(self.temp/'chinese-unpacked')
        extracted=self.temp/'chinese-unpacked'/built['name']
        entry=(extracted/'SKILL.md').read_text(encoding='utf8')
        self.assertIn('description: '+json.dumps(title+'：',ensure_ascii=False)[:-1],entry)
        self.assertIn('# '+title,entry)
        self.assertIn('display_name: '+json.dumps(title,ensure_ascii=False),
                      (extracted/'agents/openai.yaml').read_text(encoding='utf8'))
        manifest=factory.read(extracted/'manifest.json')
        self.assertEqual(manifest['display_name'],title)
        for relative,expected in manifest['files'].items():
            self.assertEqual(media.signature(extracted/relative)['sha256'],expected)
        new_run=self.temp/'chinese-reuse-run'
        reused=subprocess.run([sys.executable,'-B','-X','utf8',str(extracted/'scripts/factory.py'),
                               'instantiate','--skill',str(extracted),'--project',str(new_run)],
                              capture_output=True,text=True,encoding='utf8',timeout=60)
        self.assertEqual(reused.returncode,0,reused.stdout+reused.stderr)
        self.assertEqual(factory.source_current(new_run)['source']['sha256'],self.source['source']['sha256'])
        self.assertEqual(factory.read(new_run/'job.json')['references'],{})

    def test_hash_filename_gets_chinese_fallback_and_preserves_source(self):
        original=self.temp/'0123456789abcdef.mp4'
        shutil.copy2(self.video,original)
        project=self.temp/'hash-filename-run'
        source=factory.ingest(project,original,DEPS,SCALAR)
        factory.write(project/'analysis.json',self.analysis)
        built=factory.build_skill(project,self.temp/'hash-title-skills')
        self.assertEqual(built['display_name'],'视频人物替换复刻·'+source['source']['sha256'][:8])
        self.assertEqual(factory.read(Path(built['path'])/'assets/source.json')['original_name'],original.name)
        self.assertEqual(media.signature(Path(built['path'])/'assets/source.mp4')['sha256'],source['source']['sha256'])

    def test_invalid_chinese_titles_fail_before_installation(self):
        skills=self.temp/'invalid-title-skills'
        for title in ('English only','', '换角\n片段', '换角\t片段', '换'*65, 7):
            with self.subTest(title=title):
                with self.assertRaises(ValueError):
                    factory.build_skill(self.project,skills,display_name=title)
                self.assertFalse(skills.exists())

    def test_reuse_preserves_existing_chinese_name_and_user_metadata(self):
        skills=self.temp/'stable-title-skills'
        first=factory.build_skill(self.project,skills,display_name='原片人物动作复刻')
        skill=Path(first['path'])
        snapshots={name:(skill/name).read_bytes() for name in ('SKILL.md','agents/openai.yaml','manifest.json')}
        again=factory.build_skill(self.project,skills,display_name='另一个中文名称')
        self.assertTrue(again['reused'])
        self.assertEqual(again['display_name'],'原片人物动作复刻')
        for name,content in snapshots.items():
            self.assertEqual((skill/name).read_bytes(),content)

    def test_ready_revision_inherits_chinese_title_and_keeps_previous_draft(self):
        self.candidate()
        skills=self.temp/'revision-title-skills'
        draft=factory.build_skill(self.project,skills,display_name='人物动作复刻测试')
        factory.assemble(self.project,'chinese-final.mp4',DEPS,SCALAR)
        reviews=factory.read(self.project/'reviews.json')
        reviews['final']['visual']={'status':'passed','dynamic_intervals':[[0,8]],'native_intervals':[[0,8]],
                                   'notes':'Offline synthetic fixture; does not assert real cloud visual quality.'}
        factory.write(self.project/'reviews.json',reviews)
        ready=factory.build_skill(self.project,skills,'ready')
        self.assertEqual(ready['display_name'],draft['display_name'])
        self.assertNotEqual(ready['name'],draft['name'])
        self.assertTrue(Path(draft['path']).is_dir())

    def test_ready_template_excludes_run_notes_without_changing_reviewed_inputs(self):
        self.analysis['review_notes'] = ('Native source movement actually examined in this synthetic fixture. '
                                         'Parallel targeted prompt hypothesis c. CURRENT_TASK_SENTINEL '
                                         'CURRENT_IDENTITY_SENTINEL')
        self.analysis['viewing_evidence'] = {'source_sha256': self.source['source']['sha256'],
                                            'intervals': [[0,8]], 'native_frames': [0,3,7],
                                            'observations': 'Synthetic source evidence; not real visual QA.'}
        factory.write(self.project/'analysis.json',self.analysis)
        self.candidate()
        factory.assemble(self.project,'final.mp4',DEPS,SCALAR)
        reviews = factory.read(self.project/'reviews.json')
        reviews['final']['visual'] = {'status':'passed','dynamic_intervals':[[0,8]],
                                     'native_intervals':[[0,8]],
                                     'notes':'Offline synthetic fixture exercises ready guard only.'}
        factory.write(self.project/'reviews.json',reviews)
        snapshots = {name:(self.project/name).read_bytes() for name in
                     ('source.json','analysis.json','plan.json','job.json','queue.json','reviews.json')}
        built = factory.build_skill(self.project,self.temp/'neutral-skills','ready')
        skill = Path(built['path'])
        for name,content in snapshots.items():
            self.assertEqual((self.project/name).read_bytes(),content)
        public_analysis = factory.read(skill/'assets/analysis.json')
        public_plan = factory.read(skill/'assets/plan.json')
        manifest = factory.read(skill/'manifest.json')
        self.assertEqual(public_analysis['review_notes'],'')
        self.assertEqual(public_analysis['viewing_evidence'],self.analysis['viewing_evidence'])
        self.assertEqual(public_analysis['shots'],self.analysis['shots'])
        self.assertEqual(public_plan['windows'],factory.read(self.project/'plan.json')['windows'])
        self.assertEqual(public_plan['analysis_digest'],factory.digest(public_analysis))
        self.assertEqual(manifest['analysis_digest'],factory.digest(public_analysis))
        for path in skill.rglob('*'):
            if path.is_file():
                for marker in (b'Parallel targeted prompt hypothesis c.',b'CURRENT_TASK_SENTINEL',b'CURRENT_IDENTITY_SENTINEL'):
                    self.assertNotIn(marker,path.read_bytes())
        new_run = self.temp/'neutral-independent'
        factory.instantiate(skill,new_run)
        source,analysis = factory.validate_analysis(new_run)
        factory.validate_plan(source,analysis,factory.read(new_run/'plan.json'))
        self.assertEqual(factory.review_guard(self.project,True)['visual']['status'],'passed')

    def test_template_note_sanitization_cannot_bypass_pending_ready_guard(self):
        self.analysis['review_notes'] += ' CURRENT_TASK_SENTINEL'
        factory.write(self.project/'analysis.json',self.analysis)
        self.candidate()
        factory.assemble(self.project,'pending.mp4',DEPS,SCALAR)
        before = {name:(self.project/name).read_bytes() for name in
                  ('analysis.json','plan.json','queue.json','reviews.json')}
        root = self.temp/'pending-ready-skills'
        with self.assertRaises(ValueError):
            factory.build_skill(self.project,root,'ready')
        self.assertFalse(root.exists())
        for name,content in before.items():
            self.assertEqual((self.project/name).read_bytes(),content)

    def test_actual_zip_extraction_instantiate_rebinds_source_mtime_only(self):
        import zipfile
        self.queue()
        built = factory.build_skill(self.project,self.temp/'zip-skills')
        skill = Path(built['path'])
        with zipfile.ZipFile(built['zip']['path']) as archive:
            archive.extractall(self.temp/'unpacked')
        extracted = self.temp/'unpacked'/skill.name
        template_source = factory.read(extracted/'assets/source.json')
        template_bytes = (extracted/'assets/source.json').read_bytes()
        asset = extracted/template_source['source']['path']
        actual = media.signature(asset)
        self.assertEqual(actual['sha256'],template_source['source']['sha256'])
        self.assertEqual(actual['size_bytes'],template_source['source']['size_bytes'])
        self.assertNotEqual(actual['mtime_ns'],template_source['source']['mtime_ns'])
        new_run = self.temp/'zip-independent'
        cli = subprocess.run([sys.executable,'-X','utf8',str(extracted/'scripts/factory.py'),
                              'instantiate','--skill',str(extracted),'--project',str(new_run)],
                             cwd=str(self.temp),capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(cli.returncode,0,cli.stdout+cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['status'],'references_pending')
        self.assertTrue(list((extracted/'scripts/__pycache__').glob('*.pyc')))
        self.assertEqual((extracted/'assets/source.json').read_bytes(),template_bytes)
        current_source = factory.source_current(new_run)
        self.assertEqual(current_source['source'],factory.file_signature(new_run,current_source['source']['path']))
        self.assertEqual(current_source['source']['sha256'],template_source['source']['sha256'])
        source,analysis = factory.validate_analysis(new_run)
        factory.validate_plan(source,analysis,factory.read(new_run/'plan.json'))
        job = factory.read(new_run/'job.json')
        self.assertEqual(job['references'],{})
        self.assertEqual(job['profile'],{})
        self.assertFalse(job['production_authorized'])
        # Import tolerance must not weaken the strict current production-file signature.
        copied = new_run/current_source['source']['path']
        stat = copied.stat()
        os.utime(copied,ns=(stat.st_atime_ns,stat.st_mtime_ns+10000000))
        with self.assertRaises(ValueError):
            factory.source_current(new_run)

    def test_zip_source_same_size_content_change_rejected_before_run_creation(self):
        import zipfile
        self.queue()
        built = factory.build_skill(self.project,self.temp/'tamper-skills')
        with zipfile.ZipFile(built['zip']['path']) as archive:
            archive.extractall(self.temp/'unpacked-tamper')
        extracted = self.temp/'unpacked-tamper'/built['name']
        source = factory.read(extracted/'assets/source.json')
        asset = extracted/source['source']['path']
        content = bytearray(asset.read_bytes())
        content[-1] ^= 1
        asset.write_bytes(content)
        self.assertEqual(asset.stat().st_size,source['source']['size_bytes'])
        new_run = self.temp/'tampered-independent'
        with self.assertRaisesRegex(ValueError,'Template source content differs'):
            factory.instantiate(extracted,new_run)
        self.assertFalse(new_run.exists())

    def test_dedicated_subprocess_execution_cache_does_not_break_reuse_zip(self):
        self.queue()
        skills_root=self.temp/'subprocess-skills'
        built=factory.build_skill(self.project,skills_root)
        skill=Path(built['path'])
        new_run=self.temp/'subprocess-run'
        def cli(*arguments):
            command=[sys.executable,'-X','utf8',str(skill/'scripts/factory.py'),*map(str,arguments)]
            result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return json.loads(result.stdout)
        cli('instantiate','--skill',skill,'--project',new_run)
        self.assertTrue(list((skill/'scripts/__pycache__').glob('*.pyc')))
        (new_run/'references-input').mkdir()
        shutil.copy2(self.project/'references-input/A.png',new_run/'references-input/A.png')
        factory.write(new_run/'job.json',self.job)
        args=['prepare','--project',new_run,'--deps',DEPS]
        if SCALAR: args.append('--scalar-cpu')
        queue=cli(*args)
        self.assertEqual(queue['jobs'][0]['semantic_review']['status'],'pending')
        again=cli('build-skill','--project',new_run,'--skills-root',skills_root,'--status','draft')
        self.assertTrue(again['reused'])
        self.assertTrue(Path(again['zip']['path']).exists())
        import zipfile
        with zipfile.ZipFile(again['zip']['path']) as archive:
            self.assertFalse(any('__pycache__' in name or name.endswith('.pyc') for name in archive.namelist()))
            expected={skill.name+'/'+name for name in factory.read(skill/'manifest.json')['files']}|{skill.name+'/manifest.json'}
            self.assertEqual(set(archive.namelist()),expected)
        # Ignoring caches must not ignore actual credential or runtime-state files.
        for private_name in ('api_key.txt','queue.json'):
            (skill/private_name).write_text('PRIVATE_SENTINEL','utf-8')
            with self.assertRaises(ValueError): factory.package(skill,self.temp/f'{private_name}.zip')
            (skill/private_name).unlink()

    def test_path_escape_and_private_package_rejected(self):
        with self.assertRaises(ValueError): factory.resolve(self.project,'../escape')
        folder=self.temp/'bad-skill'
        folder.mkdir()
        (folder/'api_key.txt').write_text('secret')
        with self.assertRaises(ValueError): factory.package(folder,self.temp/'bad.zip')

    def test_one_frame_input_padding_not_final_padding(self):
        short=video_fixture(self.temp/'one.mp4',pts=[0])
        report=media.inspect(short,DEPS,SCALAR)
        padded=self.temp/'padded.mp4'
        media.make_input(short,padded,report['video'],[0,1],8,12,DEPS,SCALAR)
        self.assertEqual(media.inspect(padded,DEPS,SCALAR)['video']['frame_count'],8)
        final=self.temp/'one-final.mp4'
        output=media.assemble(short,report['video'],[(padded,0)],final,DEPS,SCALAR)
        self.assertEqual(output['video']['frame_count'],1)

    def test_preroll_preserves_all_source_owners_and_final_timing(self):
        info=media.inspect(self.video,DEPS,SCALAR)
        output=self.temp/'preroll.mp4'
        derived=media.make_input(self.video,output,info['video'],[0,8],16,12,DEPS,SCALAR,3)
        projection=derived['source_projection']
        self.assertEqual(projection['generation_to_source'][:11],[0,0,0,0,1,2,3,4,5,6,7])
        self.assertEqual(projection['source_to_generation'],{str(i):i+3 for i in range(8)})
        self.assertEqual(projection['padding_frames'],5)
        self.assertEqual(projection['maximum_time_quantization'],'0')
        final=media.assemble(self.video,info['video'],[(output,i+3) for i in range(8)],self.temp/'preroll-final.mp4',DEPS,SCALAR)
        self.assertEqual(final['video']['frame_count'],8)
        self.assertEqual([f['time'] for f in final['video']['frames']],[f['time'] for f in info['video']['frames']])
        decoded=frames(output)
        self.assertLess(sum(abs(a-b) for a,b in zip(decoded[0].tobytes(),decoded[3].tobytes()))/len(decoded[0].tobytes()),2)
        self.assertGreater(sum(abs(a-b) for a,b in zip(decoded[3].tobytes(),decoded[10].tobytes()))/len(decoded[3].tobytes()),10)

    def test_preroll_plan_prompt_timecodes_and_cached_digest(self):
        self.queue()
        plan=factory.read(self.project/'plan.json')
        window=plan['windows'][0];window['generation_frames']=16;window['duration']='4/3';window['preroll_frames']=3
        factory.write(self.project/'plan.json',plan)
        factory.refresh_plan(self.project)
        self.assertEqual(factory.read(self.project/'plan.json')['windows'][0]['retained_generation_interval'],[3,11])
        # Existing derived video belongs to the old schedule and must be archived.
        with self.assertRaises(ValueError):self.queue()
        shutil.move(str(self.project/'derived_inputs'),str(self.project/'old-derived'))
        queue=self.queue();entry=queue['jobs'][0]
        prompt=(self.project/entry['prompt_path']).read_text(encoding='utf8')
        self.assertIn('[Shot 1] At 00:00.000',prompt)
        self.assertIn('At 00:00.500',prompt)
        self.assertEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])

    def test_invalid_or_truncating_preroll_is_rejected(self):
        info=media.inspect(self.video,DEPS,SCALAR)
        for invalid in (-1,1.5,True,65):
            with self.assertRaises(ValueError):media.project_frames(info['video'],[0,8],12,16,invalid)
        with self.assertRaises(ValueError):media.project_frames(info['video'],[0,8],12,10,3)

    def test_slow_projection_restores_each_owner_and_original_pts(self):
        info=media.inspect(self.video,DEPS,SCALAR)
        slow=self.temp/'slow.mp4'
        report=media.make_input(self.video,slow,info['video'],[0,8],24,12,DEPS,SCALAR,3,None,'2')
        projection=report['source_projection']
        self.assertEqual(projection['source_to_generation'],{str(i):3+2*i for i in range(8)})
        self.assertEqual(projection['generation_to_source'][3:19],[i for i in range(8) for _ in range(2)])
        self.assertEqual(projection['dropped_source_frames'],[])
        self.assertTrue(projection['requires_temporal_review'])
        self.assertFalse(projection['invertible'])
        original,derived=frames(self.video),frames(slow)
        for i in range(8):
            self.assertLess(sum(abs(a-b) for a,b in zip(original[i].tobytes(),derived[3+2*i].tobytes()))/len(original[i].tobytes()),3)
        final=media.assemble(self.video,info['video'],[(slow,3+2*i) for i in range(8)],self.temp/'slow-final.mp4',DEPS,SCALAR)
        self.assertEqual([f['time'] for f in final['video']['frames']],[f['time'] for f in info['video']['frames']])
        self.assertEqual(final['video']['frames'][-1]['duration'],info['video']['frames'][-1]['duration'])

    def test_slow_plan_events_projection_review_and_cache_binding(self):
        self.queue()
        plan=factory.read(self.project/'plan.json')
        plan['windows'][0].update(source_time_scale='2',generation_frames=24,duration='2',preroll_frames=3)
        self.job['profile']['limits']['frame_counts'].append(24)
        factory.write(self.project/'job.json',self.job)
        factory.write(self.project/'plan.json',plan)
        factory.refresh_plan(self.project)
        self.assertEqual(factory.read(self.project/'plan.json')['windows'][0]['retained_generation_interval'],[3,19])
        with self.assertRaises(ValueError):self.queue()
        shutil.move(str(self.project/'derived_inputs'),str(self.project/'old-derived'))
        entry=self.queue()['jobs'][0]
        self.assertEqual(entry['source_projection_review']['status'],'pending')
        self.assertIn('At 00:00.750',(self.project/entry['prompt_path']).read_text(encoding='utf8'))
        approved=factory.prepare(self.project,DEPS,SCALAR,True,True)['jobs'][0]
        self.assertEqual(approved['input_digest'],entry['input_digest'])
        self.assertEqual(approved['source_projection_review']['status'],'passed')
        mapping=factory.inspect_candidate(self.project,'window_001','OFFLINE_SLOW',entry['video_path'],DEPS,SCALAR)
        self.assertEqual(mapping['source_to_output'],[3+2*i for i in range(8)])
        self.assertEqual(mapping['visual']['status'],'pending')

    def test_invalid_slow_scale_and_truncated_schedule_rejected(self):
        info=media.inspect(self.video,DEPS,SCALAR)
        for scale in ('0','1/2','5'):
            with self.assertRaises(ValueError):media.project_frames(info['video'],[0,8],12,40,3,scale)
        with self.assertRaises(ValueError):media.project_frames(info['video'],[0,8],12,18,3,'2')

    def test_slow_audio_is_stretched_while_final_packets_are_original(self):
        import numpy as np
        original=self.temp/'short-tone.mp4'
        args=[media.find_ffmpeg(DEPS),'-nostdin','-v','error','-i',str(self.video),
              '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.166667',
              '-map','0:v:0','-map','1:a:0','-af','apad','-c:v','copy','-c:a','aac','-t','0.666667',str(original)]
        self.assertEqual(subprocess.run(args,capture_output=True).returncode,0)
        info=media.inspect(original,DEPS,SCALAR)
        slow=self.temp/'slow-audio.mp4'
        media.make_input(original,slow,info['video'],[0,8],40,12,DEPS,SCALAR,3,None,'4')
        av=media.runtime(DEPS,SCALAR)
        with av.open(str(slow)) as container:
            samples=[]
            for frame in container.decode(audio=0):
                t=float(frame.pts*frame.time_base)
                samples.extend((t+i/frame.sample_rate,float(x)) for i,x in enumerate(frame.to_ndarray().reshape(-1)))
        def rms(a,b):
            values=[x for t,x in samples if a<=t<b]
            return float(np.sqrt(np.mean(np.square(values))))
        self.assertGreater(rms(.65,.75),.01)
        self.assertLess(rms(1.0,1.1),.001)
        final=self.temp/'slow-audio-final.mp4'
        media.assemble(original,info['video'],[(slow,3+4*i) for i in range(8)],final,DEPS,SCALAR)
        self.assertEqual(media.audio_packets(original,DEPS,SCALAR),media.audio_packets(final,DEPS,SCALAR))

    def test_window_identity_override_changes_only_its_digest_and_keeps_six_bindings(self):
        from PIL import Image
        video = video_fixture(self.temp/'six-window-source.mp4',pts=list(range(48)))
        project = self.temp/'six-window-run'
        source = factory.ingest(project,video,DEPS,SCALAR)
        analysis = factory.read(project/'analysis.json')
        analysis.update(status='passed',roles=[{'slot':slot,'label':'Neutral source '+slot,
                                               'appearances':[[0,48]]} for slot in ('A','B')])
        analysis['shots'] = [{'id':f's{i+1}','start':i*8,'end':(i+1)*8,'roles':['A','B'],
                              'description_en':'{{A}} and {{B}} keep the source composition.',
                              'events':[{'source_frame':i*8+3,'description_en':'{{B}} continues the source movement.'}]}
                             for i in range(6)]
        factory.write(project/'analysis.json',analysis)
        (project/'refs').mkdir()
        for slot in ('A','B'):
            shutil.copy2(self.project/'references-input/A.png',project/f'refs/{slot}.png')
        job = factory.read(project/'job.json')
        job.update(references={slot:{'path':f'refs/{slot}.png','identity_en':f'{slot}_GLOBAL_IDENTITY'} for slot in ('A','B')},
                   profile=copy.deepcopy(self.job['profile']))
        job['references']['B']['identity_en'] += ' BACK_FOREGROUND_RULE_FOR_OTHER_WINDOWS'
        plan = factory.default_plan(source,analysis,job)
        for window in plan['windows']:
            window['role_slots'] = ['B','A']
        first = plan['windows'][0]
        first.update(preroll_frames=3,generation_frames=16,duration='4/3',retained_generation_interval=[3,11])
        factory.write(project/'plan.json',plan)
        factory.refresh_plan(project)
        Image.new('RGB',(64,48),'red').save(project/'opening.png')
        guide = {'path':'opening.png','source_frame':0,
                 'review':{'status':'passed','source_sha256':source['source']['sha256'],
                           'file_signature':factory.file_signature(project,'opening.png'),
                           'notes':'Offline synthetic guide contract exercise, not production visual QA.'}}
        job['opening_guides'] = {'window_001':guide}
        factory.write(project/'job.json',job)
        queue = factory.prepare(project,DEPS,SCALAR,True)
        entries = queue['jobs']
        self.assertEqual(len(entries),6)
        before = {e['window_id']:factory.compute_input_digest(project,e) for e in entries}
        source_analysis_bytes = (project/'analysis.json').read_bytes()
        overrides = {'A':'A_WINDOW_ONLY_COMPLETE_IDENTITY','B':'B_WINDOW_ONLY_SINGLE_SEATED_IDENTITY'}
        guide['identity_en_overrides'] = overrides
        factory.write(project/'job.json',job)
        after = {e['window_id']:factory.compute_input_digest(project,e) for e in entries}
        self.assertNotEqual(before['window_001'],after['window_001'])
        for wid in before:
            if wid!='window_001':self.assertEqual(before[wid],after[wid])
        current_plan = factory.read(project/'plan.json')
        first = current_plan['windows'][0]
        contract = factory.structure_guide(project,job,source,first)
        self.assertEqual(contract['identity_en_overrides'],overrides)
        prompt,bindings = factory.compile_prompt(source,analysis,job,first)
        self.assertEqual(bindings,[{'slot':'B','subject':1,'picture':1},{'slot':'A','subject':2,'picture':2}])
        self.assertIn('<Subject 1> is the person represented by <Picture 1>. B_WINDOW_ONLY_SINGLE_SEATED_IDENTITY',prompt)
        self.assertIn('<Subject 2> is the person represented by <Picture 2>. A_WINDOW_ONLY_COMPLETE_IDENTITY',prompt)
        self.assertNotIn('GLOBAL_IDENTITY',prompt)
        self.assertNotIn('BACK_FOREGROUND_RULE_FOR_OTHER_WINDOWS',prompt)
        for header in factory.HEADERS:self.assertEqual(prompt.count(header),1)
        self.assertNotIn('{{',prompt)
        for other in current_plan['windows'][1:]:
            other_prompt,_ = factory.compile_prompt(source,analysis,job,other)
            self.assertIn('B_GLOBAL_IDENTITY BACK_FOREGROUND_RULE_FOR_OTHER_WINDOWS',other_prompt)
            self.assertNotIn('WINDOW_ONLY',other_prompt)
        # A second change also invalidates only the selected window, not all job identities.
        guide['identity_en_overrides']['B'] = 'B_WINDOW_ONLY_REVISED_IDENTITY'
        factory.write(project/'job.json',job)
        self.assertNotEqual(after['window_001'],factory.compute_input_digest(project,entries[0]))
        for entry in entries[1:]:self.assertEqual(after[entry['window_id']],factory.compute_input_digest(project,entry))
        self.assertEqual((project/'analysis.json').read_bytes(),source_analysis_bytes)
        built = factory.build_skill(project,self.temp/'window-override-skills')
        for path in (Path(built['path'])/'assets').rglob('*'):
            if path.is_file():self.assertNotIn(b'WINDOW_ONLY',path.read_bytes())
        new_run = self.temp/'window-override-independent'
        factory.instantiate(built['path'],new_run)
        self.assertNotIn('opening_guides',factory.read(new_run/'job.json'))

    def test_window_identity_override_rejects_missing_extra_empty_and_compiler_labels(self):
        from PIL import Image
        entry = self.queue()['jobs'][0]
        window = copy.deepcopy(entry['source_contract']['window'])
        window['preroll_frames'] = 3
        Image.new('RGB',(64,48),'red').save(self.project/'opening.png')
        guide = {'path':'opening.png','source_frame':0,
                 'review':{'status':'passed','source_sha256':self.source['source']['sha256'],
                           'file_signature':factory.file_signature(self.project,'opening.png'),
                           'notes':'Offline synthetic guide validation only.'}}
        self.job['opening_guides'] = {'window_001':guide}
        invalid = [None,[],{}, {'UNKNOWN':'Unknown identity'}, {'A':'valid','EXTRA':'extra'},
                   {'A':''},{'A':'  '},{'A':123},{'A':'Identity with <Subject>'},
                   {'A':'Identity with <subject 2>'},{'A':'Identity with <Picture 1>'},
                   {'A':'Identity with <Video 1>'}]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                guide['identity_en_overrides'] = overrides
                with self.assertRaises(ValueError):
                    factory.compile_prompt(self.source,self.analysis,self.job,window)
                with self.assertRaises(ValueError):
                    factory.structure_guide(self.project,self.job,self.source,window)
        # The normal identity dossier uses the same compiler-label validation.
        guide.pop('identity_en_overrides')
        self.job['references']['A']['identity_en'] = 'Bad global dossier <Subject>'
        with self.assertRaises(ValueError):
            factory.compile_prompt(self.source,self.analysis,self.job,window)

    def test_opening_guide_changes_only_excluded_preroll(self):
        from PIL import Image
        guide=self.temp/'guide.png';Image.new('RGB',(64,48),'red').save(guide)
        info=media.inspect(self.video,DEPS,SCALAR)
        original=self.temp/'plain-prefix.mp4';guided=self.temp/'guided-prefix.mp4'
        media.make_input(self.video,original,info['video'],[0,8],16,12,DEPS,SCALAR,3)
        result=media.make_input(self.video,guided,info['video'],[0,8],16,12,DEPS,SCALAR,3,guide)
        plain,edited=frames(original),frames(guided)
        self.assertGreater(edited[0].getpixel((32,24))[0],240)
        self.assertLess(edited[0].getpixel((32,24))[2],10)
        for i in range(3,11):
            self.assertLess(sum(abs(a-b) for a,b in zip(plain[i].tobytes(),edited[i].tobytes()))/len(plain[i].tobytes()),3)
        self.assertEqual(result['source_projection']['source_to_generation'],{str(i):i+3 for i in range(8)})
        with self.assertRaises(ValueError):
            media.make_input(self.video,self.temp/'owned-guide.mp4',info['video'],[0,8],16,12,DEPS,SCALAR,0,guide)

    def test_opening_guide_needs_current_review_and_invalidates_input(self):
        from PIL import Image
        self.queue();plan=factory.read(self.project/'plan.json')
        plan['windows'][0].update(preroll_frames=3,generation_frames=16,duration='4/3')
        factory.write(self.project/'plan.json',plan);factory.refresh_plan(self.project)
        shutil.move(str(self.project/'derived_inputs'),str(self.project/'prior-input'))
        guide=self.project/'opening.png';Image.new('RGB',(64,48),'red').save(guide)
        record={'path':'opening.png','source_frame':0,
                'opening_description_en':'{{A}} holds the source opening pose with the run-specific replacement garment.',
                'review':{'status':'passed',
                'source_sha256':self.source['source']['sha256'],
                'file_signature':factory.file_signature(self.project,'opening.png'),
                'notes':'Synthetic pose check only, not a visual production approval.'}}
        self.job['opening_guides']={'window_001':record};factory.write(self.project/'job.json',self.job)
        entry=self.queue()['jobs'][0]
        self.assertIn('<Subject 1> holds the source opening pose with the run-specific replacement garment.',
                      (self.project/entry['prompt_path']).read_text(encoding='utf-8'))
        self.assertNotIn('opening_description_en',json.dumps(factory.read(self.project/'plan.json')))
        self.assertEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])
        Image.new('RGB',(64,48),'blue').save(guide)
        with self.assertRaises(ValueError):factory.compute_input_digest(self.project,entry)
        record['review']['file_signature']=factory.file_signature(self.project,'opening.png')
        factory.write(self.project/'job.json',self.job)
        self.assertNotEqual(factory.compute_input_digest(self.project,entry),entry['input_digest'])
        with self.assertRaises(ValueError):self.queue()
        record['source_frame']=1;factory.write(self.project/'job.json',self.job)
        with self.assertRaises(ValueError):self.queue()

    def test_vfr_original_pts_assembly_and_no_silent_resample(self):
        original=video_fixture(self.temp/'vfr.mp4',pts=[0,1,3,6,7])
        info=media.inspect(original,DEPS,SCALAR)
        self.assertFalse(info['video']['cfr'])
        derived=media.make_input(original,self.temp/'projected.mp4',info['video'],[0,5],8,12,DEPS,SCALAR)
        self.assertEqual(derived['source_projection']['generation_to_source'],[0,1,1,2,2,2,3,4])
        self.assertTrue(derived['source_projection']['requires_temporal_review'])
        self.assertFalse(derived['source_projection']['invertible'])
        output=media.assemble(original,info['video'],[(original,i) for i in range(5)],self.temp/'vfr-final.mp4',DEPS,SCALAR)
        self.assertEqual([Fraction(f['time']) for f in output['video']['frames']],
                         [Fraction(f['time']) for f in info['video']['frames']])

    def test_dual_audio_packets_and_offset_preserved(self):
        original=self.temp/'audio.mp4'
        ffmpeg=media.find_ffmpeg(DEPS)
        command=[ffmpeg,'-nostdin','-v','error','-threads','1','-i',str(self.video),
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.5',
                 '-itsoffset','0.125','-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=0.4',
                 '-map','0:v:0','-map','1:a:0','-map','2:a:0','-c:v','copy','-c:a','aac',
                 '-metadata:s:a:0','language=eng','-metadata:s:a:1','language=zho',str(original)]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        info=media.inspect(original,DEPS,SCALAR)
        self.assertEqual(len(info['audio_streams']),2)
        output=media.assemble(original,info['video'],[(self.video,i) for i in range(8)],self.temp/'audio-final.mp4',DEPS,SCALAR)
        self.assertEqual(len(output['audio_streams']),2)
        self.assertEqual(media.audio_packets(original,DEPS,SCALAR),media.audio_packets(self.temp/'audio-final.mp4',DEPS,SCALAR))

    def test_modern_display_matrix_rotation_detected_and_not_ignored(self):
        rotated=video_fixture(self.temp/'rotate.mp4',rotation=90)
        report=media.inspect(rotated,DEPS,SCALAR)
        self.assertNotEqual(int(report['video']['rotation'])%360,0)
        self.assertIn('display_matrix',report['video'])
        derived_path=self.temp/'normalized-rotation.mp4'
        derived=media.make_input(rotated,derived_path,report['video'],[0,8],8,12,DEPS,SCALAR)
        self.assertEqual((derived['video']['width'],derived['video']['height']),(48,64))
        self.assertEqual(int(derived['video']['rotation'])%360,0)
        output=media.assemble(rotated,report['video'],[(derived_path,i) for i in range(8)],self.temp/'rotated-final.mp4',DEPS,SCALAR)
        self.assertEqual((output['video']['width'],output['video']['height']),(48,64))
        self.assertEqual(int(output['video']['rotation'])%360,0)
        from PIL import Image
        original_display=frames(rotated)[0].transpose(Image.Transpose.ROTATE_90)
        final_display=frames(self.temp/'rotated-final.mp4')[0]
        self.assertLess(max(abs(a-b) for a,b in zip(original_display.getpixel((10,60)),final_display.getpixel((10,60)))),10)

    def test_all_orthogonal_rotations_preserve_display_and_quarter_turn_sar(self):
        from PIL import Image
        for angle in (90,180,270):
            original=video_fixture(self.temp/f'r{angle}.mp4',rotation=angle,sar=Fraction(4,3))
            report=media.inspect(original,DEPS,SCALAR)
            input_path=self.temp/f'input-r{angle}.mp4'
            derived=media.make_input(original,input_path,report['video'],[0,8],8,12,DEPS,SCALAR)
            expected_size=(48,64) if angle in (90,270) else (64,48)
            expected_sar='3/4' if angle in (90,270) else '4/3'
            self.assertEqual((derived['video']['width'],derived['video']['height']),expected_size)
            self.assertEqual(derived['video']['sample_aspect_ratio'],expected_sar)
            final_path=self.temp/f'final-r{angle}.mp4'
            final=media.assemble(original,report['video'],[(input_path,i) for i in range(8)],final_path,DEPS,SCALAR)
            self.assertEqual(final['video']['sample_aspect_ratio'],expected_sar)
            transform={90:Image.Transpose.ROTATE_90,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_270}[angle]
            expected=frames(original)[0].transpose(transform)
            actual=frames(final_path)[0]
            self.assertEqual(actual.size,expected.size)
            def white_marker(image):
                points=[(x,y) for y in range(image.height) for x in range(image.width)
                        if min(image.getpixel((x,y)))>210]
                self.assertGreater(len(points),30)
                return (sum(p[0] for p in points)/len(points),sum(p[1] for p in points)/len(points))
            self.assertLess(max(abs(a-b) for a,b in zip(white_marker(expected),white_marker(actual))),1)
            center=(expected.width//2,expected.height//2)
            self.assertLess(max(abs(a-b) for a,b in zip(expected.getpixel(center),actual.getpixel(center))),10)
            self.assertEqual([f['time'] for f in final['video']['frames']],
                             [f['time'] for f in report['video']['frames']])

    def test_mixed_empty_and_character_shots_use_source_passthrough(self):
        self.analysis['roles'][0]['appearances']=[[4,8]]
        self.analysis['shots']=[{'id':'empty','start':0,'end':4,'roles':[],
                                'description_en':'Only the measured source setting is visible.','events':[]},
                               {'id':'actor','start':4,'end':8,'roles':['A'],
                                'description_en':'{{A}} continues the measured source movement.','events':[]}]
        factory.write(self.project/'analysis.json',self.analysis)
        queue=factory.prepare(self.project,DEPS,SCALAR,True)
        self.assertEqual(len(queue['jobs']),2)
        passthrough,generated=queue['jobs']
        self.assertTrue(passthrough['passthrough'])
        self.assertFalse(passthrough['production_authorized'])
        self.assertEqual(passthrough['video_path'],self.source['source']['path'])
        self.assertIsNone(passthrough['prompt_path'])
        self.assertFalse((self.project/'derived_inputs/window_001.mp4').exists())
        (self.project/'candidates').mkdir(exist_ok=True)
        candidate='candidates/mixed-fixture.mp4'
        shutil.copy2(self.project/generated['video_path'],self.project/candidate)
        record=factory.inspect_candidate(self.project,'window_002','OFFLINE_ONLY',candidate,DEPS,SCALAR)
        record['visual']={'status':'passed','dynamic_intervals':[[0,4]],'native_intervals':[[0,4]],
                          'notes':'Synthetic source fixture only; no real generation review.'}
        reviews=factory.read(self.project/'reviews.json')
        reviews['windows']['window_002']=record
        factory.write(self.project/'reviews.json',reviews)
        guarded=factory.review_guard(self.project)
        self.assertEqual(guarded[0][1]['source_to_output'],[0,1,2,3])
        final=factory.assemble(self.project,'mixed-final.mp4',DEPS,SCALAR)
        self.assertEqual(final['video']['frame_count'],8)
        self.assertEqual([f['time'] for f in final['video']['frames']],
                         [f['time'] for f in self.source['video']['frames']])
        for before,after in zip(frames(self.project/self.source['source']['path'])[:4],frames(self.project/'outputs/mixed-final.mp4')[:4]):
            self.assertLess(max(abs(a-b) for a,b in zip(before.getpixel((50,35)),after.getpixel((50,35)))),10)

    def test_no_target_source_requires_neither_backend_nor_cloud_review(self):
        self.analysis.update(roles=[],shots=[{'id':'all-source','start':0,'end':8,'roles':[],
                                             'description_en':'No target identities occur; preserve all original content.','events':[]}])
        factory.write(self.project/'analysis.json',self.analysis)
        self.job.update(profile={},references={})
        factory.write(self.project/'job.json',self.job)
        queue=factory.prepare(self.project,DEPS,SCALAR)
        self.assertTrue(all(e['passthrough'] for e in queue['jobs']))
        self.assertFalse((self.project/'derived_inputs').exists())
        self.assertEqual(len(factory.review_guard(self.project)),1)
        result=factory.assemble(self.project,'unchanged-owners.mp4',DEPS,SCALAR)
        self.assertEqual(result['video']['frame_count'],8)
        self.assertEqual(factory.read(self.project/'reviews.json')['final']['visual']['status'],'pending')

    def test_legacy_one_two_subject_apps_routed_per_window(self):
        original=video_fixture(self.temp/'two-count-source.mp4',fps=24)
        project=self.temp/'one-two-run'
        factory.ingest(project,original,DEPS,SCALAR)
        analysis=factory.read(project/'analysis.json')
        analysis.update(status='passed',roles=[{'slot':'A','appearances':[[0,8]]},{'slot':'B','appearances':[[4,8]]}],
                        shots=[{'id':'one','start':0,'end':4,'roles':['A'],'description_en':'{{A}} holds the measured pose.','events':[]},
                               {'id':'two','start':4,'end':8,'roles':['A','B'],'description_en':'{{A}} and {{B}} hold the measured poses.','events':[]}])
        factory.write(project/'analysis.json',analysis)
        (project/'refs').mkdir()
        for slot in ('A','B'):
            shutil.copy2(self.project/'references-input/A.png',project/f'refs/{slot}.png')
        job=factory.read(project/'job.json')
        job.update(references={slot:{'path':f'refs/{slot}.png','identity_en':f'{slot} fixture identity.'} for slot in ('A','B')},
                   profile={'kind':'legacy-ai-app','app_id':'2107845954833907713','instance_type':'mock',
                            'quality_evidence':'observed-other-project',
                            'limits':{'fps':['24'],'frame_counts':[22,39,56,73,90,107],'identity_slots':2}})
        factory.write(project/'job.json',job)
        queue=factory.prepare(project,DEPS,SCALAR,True)
        self.assertEqual([entry['profile']['app_id'] for entry in queue['jobs']],
                         ['2107844752011010050','2107845954833907713'])
        self.assertEqual([entry['pilot'] for entry in queue['jobs']],[True,True])
        self.assertTrue(all(entry['generation_frames']==22 for entry in queue['jobs']))
        plan=factory.read(project/'plan.json')
        plan['windows'][0]['profile_override']['quality_evidence']='case-verified'
        factory.write(project/'plan.json',plan)
        refreshed=factory.prepare(project,DEPS,SCALAR,True)
        self.assertFalse(refreshed['jobs'][0]['pilot'])
        self.assertEqual([e['input_digest'] for e in refreshed['jobs']],[e['input_digest'] for e in queue['jobs']])

    def test_vfr_prepare_requires_projection_review_and_legacy_24_evidence(self):
        original=video_fixture(self.temp/'new-vfr.mp4',pts=[0,1,3,6,7])
        project=self.temp/'vfr-run'
        source=factory.ingest(project,original,DEPS,SCALAR)
        analysis=factory.read(project/'analysis.json')
        analysis.update(status='passed',roles=[{'slot':'A','appearances':[[0,5]]}],
                        shots=[{'id':'s','start':0,'end':5,'roles':['A'],'description_en':'{{A}} follows the measured source movement.','events':[]}])
        factory.write(project/'analysis.json',analysis)
        (project/'refs').mkdir()
        shutil.copy2(self.project/'references-input/A.png',project/'refs/A.png')
        job=factory.read(project/'job.json')
        job.update(references={'A':{'path':'refs/A.png','identity_en':'Green identity.'}},
                   profile={'kind':'legacy-ai-app','app_id':'OFFLINE_FIXTURE','instance_type':'mock',
                            'limits':{'fps':['24'],'frame_counts':[22,39],'identity_slots':1}})
        factory.write(project/'job.json',job)
        entry=factory.prepare(project,DEPS,SCALAR,True)['jobs'][0]
        self.assertEqual(entry['source_contract']['window']['generation_fps'],'24')
        self.assertEqual(entry['generation_frames'],22)
        self.assertEqual(entry['source_projection_review']['status'],'pending')
        self.assertTrue(entry['source_projection_review']['loss_disclosed'])
        refreshed=factory.prepare(project,DEPS,SCALAR,True,True)['jobs'][0]
        self.assertEqual(refreshed['input_digest'],entry['input_digest'])
        self.assertEqual(refreshed['source_projection_review']['status'],'passed')

    def test_actual_sar_and_color_metadata_preserved(self):
        original=video_fixture(self.temp/'sar.mp4',sar=Fraction(4,3),colors={'colorspace':1,'color_primaries':1,'color_trc':1,'color_range':1})
        report=media.inspect(original,DEPS,SCALAR)
        output=media.assemble(original,report['video'],[(original,i) for i in range(8)],self.temp/'sar-final.mp4',DEPS,SCALAR)
        self.assertEqual(output['video']['sample_aspect_ratio'],'4/3')
        self.assertEqual(output['video']['color_metadata'],report['video']['color_metadata'])

    def test_same_dar_resize_and_wrong_aspect_rejected(self):
        original=media.inspect(self.video,DEPS,SCALAR)
        larger=video_fixture(self.temp/'larger.mp4',width=128,height=96)
        result=media.assemble(self.video,original['video'],[(larger,i) for i in range(8)],self.temp/'scaled.mp4',DEPS,SCALAR)
        self.assertEqual(result['video']['width'],64)
        self.assertEqual(len(result['candidate_canvas_resizes']),1)
        wrong=video_fixture(self.temp/'wide.mp4',width=128,height=64)
        with self.assertRaises(ValueError):
            media.assemble(self.video,original['video'],[(wrong,i) for i in range(8)],self.temp/'wrong-aspect.mp4',DEPS,SCALAR)


def main():
    global DEPS,WORK,SCALAR
    parser=argparse.ArgumentParser()
    parser.add_argument('--deps')
    parser.add_argument('--work')
    parser.add_argument('--scalar-cpu',action='store_true')
    args=parser.parse_args()
    DEPS,SCALAR=args.deps,args.scalar_cpu
    WORK=Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix='factory-regressions-'))
    WORK.mkdir(parents=True,exist_ok=True)
    # Any attempted Python networking is a regression failure; FFmpeg receives local paths only.
    socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Network forbidden in offline factory tests'))
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(FactoryIntegration)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    factory.write(WORK/'factory-test-results.json',{'tests':result.testsRun,'failures':len(result.failures),
                                                   'errors':len(result.errors),'passed':result.wasSuccessful(),
                                                   'cloud_generation_performed':False})
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__=='__main__': main()

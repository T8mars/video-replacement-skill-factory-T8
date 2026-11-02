"""Complete actual-output/pose evidence and independent canonical RGB/PTS proofs.

Evidence preparation never approves a face, action, ROI coverage or playback.
"""
from pathlib import Path
from fractions import Fraction
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import factory
import media


def require(condition, message):
    if not condition:
        raise ValueError(message)


def indices(mapping, start, end, count):
    require(isinstance(mapping, list) and len(mapping) == end-start, 'Map must cover every owned source pose')
    require(all(type(i) is int and 0 <= i < count for i in mapping), 'Map index outside actual output clock')
    require(all(a <= b for a,b in zip(mapping,mapping[1:])), 'Map reverses source action order')


def bound(project, signature):
    require(isinstance(signature, dict) and 'path' in signature, 'Missing bound evidence artifact')
    require(factory.file_signature(project, signature['path']) == signature, 'Evidence artifact changed')
    return factory.resolve(project, signature['path'])


def window_context(project, window_id, candidate):
    project=Path(project).resolve()
    source,analysis=factory.validate_analysis(project)
    plan=factory.read(project/'plan.json');factory.validate_plan(source,analysis,plan)
    window=next((w for w in plan['windows'] if w['window_id']==window_id),None)
    entry=next((e for e in factory.read(project/'queue.json')['jobs'] if e['window_id']==window_id),None)
    require(window is not None and entry is not None and not entry.get('passthrough'), 'Unknown generated window')
    require(factory.compute_input_digest(project,entry)==entry['input_digest'], 'Prepared input changed')
    return source,analysis,window,entry,factory.resolve(project,candidate)


def contact(images, labels, path, columns=3):
    from PIL import Image,ImageDraw,ImageOps
    page=Image.new('RGB',(640*columns,384*((len(images)+columns-1)//columns)),'#202020')
    draw=ImageDraw.Draw(page)
    for i,(image,label) in enumerate(zip(images,labels)):
        x=i%columns*640;y=i//columns*384
        page.paste(ImageOps.pad(image,(640,360),color='#202020'),(x,y+24))
        draw.text((x+4,y+4),label,fill='white')
    page.save(path)


def prepare_evidence(project, window_id, candidate, output, mapping=None, rois=None,
                     native_all=False, deps=None, scalar_cpu=False):
    av=media.runtime(deps,scalar_cpu)
    from PIL import Image
    project=Path(project).resolve()
    source,analysis,window,entry,path=window_context(project,window_id,candidate)
    report=media.inspect(path,deps,scalar_cpu)
    count=report['video']['frame_count'];start,end=window['source_interval']
    if mapping is not None:
        indices(mapping,start,end,count)
    rois=rois or []
    names=set()
    for roi in rois:
        name=roi.get('name','');box=roi.get('box',[]);roles=roi.get('role_slots',[])
        require(re.fullmatch(r'[A-Za-z0-9_-]+',name) is not None and name not in names, 'ROI name invalid or repeated')
        require(len(box)==4 and all(type(i) is int for i in box), 'ROI needs four integer native coordinates')
        require(isinstance(roles,list) and roles and set(roles)<=set(window['role_slots']), 'Label ROI with current visible role slots')
        names.add(name)
    destination=factory.resolve(project,output)
    # These trees are copied into reusable source-only skills. Current identities
    # and candidate frames must never become template assets by a custom --out.
    require(destination.relative_to(project).parts and destination.relative_to(project).parts[0].casefold() not in ('assets','evidence'),
            'Candidate evidence cannot be written into reusable source assets/evidence')
    require(not destination.exists(), 'Preserve existing evidence; use a new directory')
    destination.mkdir(parents=True)
    files=[];overview=[];labels=[];overview_coverage=[]
    selected=set(mapping or []);output_images={};roi_coverage={r['name']:[] for r in rois}
    previous=None;duplicates=[];seen=0
    def save(image,relative):
        target=destination/relative;image.save(target)
        signature=factory.file_signature(project,target.relative_to(project).as_posix());files.append(signature)
        return signature
    with av.open(str(path)) as container:
        container.streams.video[0].codec_context.thread_count=1
        rotation=None
        for number,frame in enumerate(container.decode(video=0)):
            if rotation is None:rotation=getattr(frame,'rotation',0)
            image=media.display_frame(frame,rotation).to_image()
            rgb=hashlib.sha256(image.tobytes()).hexdigest()
            if rgb==previous:duplicates.append(number)
            previous=rgb;seen+=1
            if native_all or number in selected:
                output_images[number]=save(image,f'output-{number:06d}-native.png')
            for roi in rois:
                x0,y0,x1,y1=roi['box']
                require(0<=x0<x1<=image.width and 0<=y0<y1<=image.height,'ROI escapes actual native frame')
                signature=save(image.crop((x0,y0,x1,y1)),f"output-{number:06d}-{roi['name']}.png")
                roi_coverage[roi['name']].append({'frame':number,'artifact':signature})
            # Overview is deliberately scaled, never called native evidence.
            overview.append(image.resize((640,max(1,round(640*image.height/image.width)))))
            labels.append(f'actual output frame {number}')
            if len(overview)==9 or number==count-1:
                name=f'clock-{number//9:04d}.png';contact(overview,labels,destination/name)
                files.append(factory.file_signature(project,(destination/name).relative_to(project).as_posix()))
                overview_coverage.extend(range(number-len(overview)+1,number+1));overview=[];labels=[]
    require(seen==count, 'Output changed during complete evidence extraction')
    pairs=[]
    if mapping is not None:
        source_path=factory.resolve(project,source['source']['path'])
        with av.open(str(source_path)) as container:
            container.streams.video[0].codec_context.thread_count=1
            rotation=None
            for number,frame in enumerate(container.decode(video=0)):
                if rotation is None:rotation=getattr(frame,'rotation',0)
                if start<=number<end:
                    image=media.display_frame(frame,rotation).to_image()
                    pairs.append({'source_frame':number,'output_frame':mapping[number-start],
                        'source_artifact':save(image,f'source-{number:06d}-native.png'),
                        'output_artifact':output_images[mapping[number-start]]})
                if number>=end-1:break
        for offset in range(0,len(pairs),4):
            images=[];texts=[]
            for pair in pairs[offset:offset+4]:
                for key,label in [('source_artifact','source'),('output_artifact','output')]:
                    with Image.open(factory.resolve(project,pair[key]['path'])) as im:images.append(im.copy())
                    texts.append(f"{label} frame {pair['source_frame'] if label=='source' else pair['output_frame']}")
            name=f'poses-{offset:04d}.png';contact(images,texts,destination/name,2)
            files.append(factory.file_signature(project,(destination/name).relative_to(project).as_posix()))
    require(factory.file_signature(project,candidate)=={'path':Path(candidate).as_posix(),**report['file_signature']}, 'Output changed during review preparation')
    require(factory.source_current(project)['source']==source['source'],'Source changed during review preparation')
    evidence={'schema_version':1,'kind':'complete-output-evidence','window_id':window_id,
        'input_digest':entry['input_digest'],'source_signature':source['source'],
        'output_signature':factory.file_signature(project,candidate),'source_interval':[start,end],
        'actual_output_frames':count,'actual_output_pts':report['video']['frames'],
        'overview_frame_indices':overview_coverage,'consecutive_identical_rgb_pairs':duplicates,
        'source_to_output':mapping,'source_pose_pairs':pairs,'native_all_prepared':native_all,
        'native_rois':[{**roi,'coverage':roi_coverage[roi['name']],'coverage_review':'pending'} for roi in rois],
        'files':files,'status':'prepared','visual_status':'pending','original_speed_status':'pending'}
    factory.write(destination/'evidence.json',evidence)
    return {'evidence':factory.file_signature(project,(destination/'evidence.json').relative_to(project).as_posix()),
        'actual_output_frames':count,'visual_status':'pending'}


def mapping_digest(contract):
    return factory.digest({k:v for k,v in contract.items() if k!='mapping_digest'})


def validate_measured_mapping(project,source,analysis,window,entry,record):
    contract=record.get('measured_mapping') or {}
    require(contract.get('schema_version')==1 and contract.get('mode')=='measured-source-poses', 'Unknown measured mapping contract')
    require(contract.get('mapping_digest')==mapping_digest(contract), 'Measured map changed after review')
    if record.get('visual',{}).get('status')=='passed':
        require(record['visual'].get('measured_mapping_digest')==contract['mapping_digest'], 'Visual verdict belongs to another measured mapping')
    require(contract.get('input_digest')==entry['input_digest'] and contract.get('source_signature')==source['source'] and
        contract.get('output_signature')==record['file_signature'], 'Measured map belongs to another source/input/output')
    evidence=factory.read(bound(project,contract.get('evidence')))
    count=record['technical']['report']['video']['frame_count'];start,end=window['source_interval']
    mapping=record['source_to_output'];indices(mapping,start,end,count)
    require(contract.get('source_to_output')==mapping,'Measured map ownership changed')
    require(evidence.get('kind')=='complete-output-evidence' and evidence.get('window_id')==window['window_id'] and
        evidence.get('input_digest')==entry['input_digest'] and evidence.get('source_signature')==source['source'] and
        evidence.get('output_signature')==record['file_signature'],'Complete output evidence is stale')
    require(evidence.get('actual_output_frames')==count and evidence.get('overview_frame_indices')==list(range(count)) and
        evidence.get('actual_output_pts')==record['technical']['report']['video']['frames'], 'Full actual output clock evidence missing')
    require(evidence.get('source_interval')==[start,end] and evidence.get('source_to_output')==mapping,'Pose evidence is for another mapping')
    pairs=evidence.get('source_pose_pairs',[])
    require([(p.get('source_frame'),p.get('output_frame')) for p in pairs]==list(zip(range(start,end),mapping)), 'Every owned source pose needs paired native evidence')
    require(evidence.get('files'), 'Evidence contains no actual image artifacts')
    for artifact in evidence['files']:bound(project,artifact)
    for pair in pairs:
        bound(project,pair['source_artifact']);bound(project,pair['output_artifact'])
    clock=contract.get('output_clock_review') or {}
    require(clock.get('status')=='observed' and factory.coverage(clock.get('intervals',[]),range(count)) and clock.get('notes'), 'Actual whole output has not been observed')
    pose=contract.get('source_pose_review') or {}
    require(pose.get('status')=='passed' and factory.coverage(pose.get('source_intervals',[]),range(start,end)) and pose.get('notes'), 'Actual source poses have not been compared')
    anchors=pose.get('anchors',[]);wanted={start,(start+end-1)//2,end-1}
    require(all(type(a.get('source_frame')) is int and start<=a['source_frame']<end and type(a.get('output_frame')) is int for a in anchors),'Pose anchor index invalid')
    require({a.get('source_frame') for a in anchors}==wanted and all(a.get('output_frame')==mapping[a['source_frame']-start] and a.get('notes') for a in anchors), 'Start/middle/end pose observations missing')
    source_cuts=sorted(s['start'] for s in analysis['shots'] if start<s['start']<end)
    actual_cuts=contract.get('source_cut_before',[]);output_cuts=contract.get('output_cut_before',[])
    require(actual_cuts==source_cuts and len(output_cuts)==len(source_cuts),'Declare corresponding measured source/output cuts')
    require(all(type(i) is int and 0<i<count for i in output_cuts) and output_cuts==sorted(set(output_cuts)),'Output cut indices invalid')
    for src,out in zip(range(start,end),mapping):
        require(sum(src>=c for c in source_cuts)==sum(out>=c for c in output_cuts),'Map crosses a measured shot boundary')


def apply_mapping(project,window_id,relative):
    project=Path(project).resolve();reviews=factory.read(project/'reviews.json');record=reviews['windows'][window_id]
    source,analysis,window,entry,_=window_context(project,window_id,record['output_path'])
    require(factory.file_signature(project,record['output_path'])==record['file_signature'],'Candidate changed')
    contract=factory.read(factory.resolve(project,relative))
    require(contract.get('mode')=='measured-source-poses','Explicit measured-source-poses mode required')
    contract.update(input_digest=entry['input_digest'],source_signature=source['source'],output_signature=record['file_signature'])
    contract['mapping_digest']=mapping_digest(contract)
    changed={**record,'mapping_mode':'measured-source-poses','measured_mapping':contract,'source_to_output':contract['source_to_output']}
    # Validate the proposed contract, not a previous map's visual verdict.
    changed['visual']={'status':'pending','dynamic_intervals':[],'native_intervals':[],'notes':''}
    validate_measured_mapping(project,source,analysis,window,entry,changed)
    factory.write(project/'review-history'/f'{window_id}-before-measured-{__import__("time").time_ns()}.json',record)
    changed['retained_output_interval']=[min(changed['source_to_output']),max(changed['source_to_output'])+1]
    changed['output_clock_review']=contract['output_clock_review']
    changed['visual']={'status':'pending','dynamic_intervals':[],'native_intervals':[],'notes':''}
    changed['temporal_mapping_review']={'status':'pending','resampling_loss_disclosed':False,'notes':''}
    reviews['windows'][window_id]=changed;factory.write(project/'reviews.json',reviews)
    return {'window_id':window_id,'mapping_mode':'measured-source-poses','visual_status':'pending'}


def canonical_rgb(plan_path,video,output,ffmpeg=None,ffprobe=None,deps=None):
    media.runtime(deps)
    from PIL import Image
    plan_path=Path(plan_path).resolve();video=Path(video).resolve();output=Path(output)
    require(not output.exists(),'Preserve previous proof; choose a new report')
    plan_signature=media.signature(plan_path)
    plan=factory.read(plan_path);rows=plan.get('frames',[]);width,height=plan['dimensions']
    require(plan.get('schema_version')==1 and rows and type(width) is int and type(height) is int and width>0 and height>0,'Invalid canonical RGB plan')
    tolerance=Fraction(plan.get('time_tolerance','0'));expected=[]
    require(0<=tolerance<min(Fraction(r['duration']) for r in rows)/2,'Clock tolerance must be below half a frame')
    times=[Fraction(row['time']) for row in rows]
    require(all(a<b for a,b in zip(times,times[1:])),'Canonical PTS must be strictly increasing')
    png_signatures={}
    for row in rows:
        require(Fraction(row['duration'])>0,'Canonical frame duration must be positive')
        image_path=(plan_path.parent/row['png']).resolve()
        require(image_path.is_relative_to(plan_path.parent) and not Path(row['png']).is_absolute(),'Canonical PNG escapes plan directory')
        png_before=media.signature(image_path)
        with Image.open(image_path) as image:
            require(image.size==(width,height),'Canonical PNG dimensions differ')
            digest=hashlib.sha256(image.convert('RGB').tobytes()).hexdigest()
        require(digest==row['rgb_sha256'],'Canonical PNG changed after the plan')
        require(media.signature(image_path)==png_before,'Canonical PNG changed during proof')
        png_signatures[image_path]=png_before
        expected.append(digest)
    decoder=ffmpeg or media.find_ffmpeg(deps)
    binary=Path(shutil.which(str(decoder)) or decoder).resolve()
    sibling=binary.with_name('ffprobe'+binary.suffix)
    probe=ffprobe or (str(sibling) if sibling.is_file() else shutil.which('ffprobe'))
    require(probe is not None,'Independent ffprobe is required')
    require(Path(shutil.which(str(probe)) or probe).is_file(),'Independent ffprobe is required')
    before=media.signature(video)
    probe_result=subprocess.run([str(probe),'-v','error','-select_streams','v:0','-show_entries',
        'stream=width,height,time_base:frame=pts,duration,pkt_duration','-of','json',str(video)],capture_output=True,timeout=300)
    require(probe_result.returncode==0 and not probe_result.stderr.strip(),'Independent frame-clock inspection failed')
    actual_clock=json.loads(probe_result.stdout);streams=actual_clock.get('streams',[])
    require(len(streams)==1 and [streams[0]['width'],streams[0]['height']]==[width,height],'Actual video dimensions differ')
    tb=Fraction(streams[0]['time_base']);observed=actual_clock.get('frames',[]);clock_ok=len(observed)==len(rows)
    for expected_row,actual in zip(rows,observed):
        pts=actual.get('pts');duration=actual.get('duration',actual.get('pkt_duration'))
        if pts is None or duration is None or abs(Fraction(pts)*tb-Fraction(expected_row['time']))>tolerance or abs(Fraction(duration)*tb-Fraction(expected_row['duration']))>tolerance:
            clock_ok=False
    hashes=[];partial=False
    with subprocess.Popen([str(binary),'-nostdin','-v','error','-xerror','-threads','1','-i',str(video),
        '-map','0:v:0','-an','-sn','-dn','-threads','1','-pix_fmt','rgb24','-fps_mode','passthrough','-f','rawvideo','pipe:1'],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE) as proc:
        while True:
            data=proc.stdout.read(width*height*3)
            if not data:break
            if len(data)!=width*height*3:partial=True;break
            hashes.append(hashlib.sha256(data).hexdigest())
        error=proc.stderr.read();code=proc.wait()
    require(media.signature(video)==before,'Video changed during canonical proof')
    require(media.signature(plan_path)==plan_signature,'Canonical plan changed during proof')
    require(all(media.signature(path)==signature for path,signature in png_signatures.items()),'Canonical PNG changed during proof')
    passed=not code and not error.strip() and not partial and clock_ok and hashes==expected
    version=subprocess.run([str(binary),'-version'],capture_output=True,text=True,timeout=15)
    proof={'schema_version':1,'kind':'independent-canonical-rgb-pts','status':'passed' if passed else 'failed',
        'video_signature':before,'plan_signature':plan_signature,'frames':len(hashes),'expected_frames':len(rows),
        'dimensions':[width,height],'RGB_sha256':hashes,'PTS_duration_status':'passed' if clock_ok else 'failed',
        'decoder':str(binary),'decoder_version':version.stdout.splitlines()[0] if version.stdout else 'unknown',
        'decoder_cpu':'normal','strict_returncode':code,'visual_status':'pending'}
    factory.write(output,proof)
    require(passed,'Canonical complete RGB/PTS mismatch; preserve failed proof and reject derivative')
    return proof


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('command',choices=['evidence','map','verify-rgb'])
    ap.add_argument('--project',type=Path);ap.add_argument('--window-id');ap.add_argument('--candidate')
    ap.add_argument('--out');ap.add_argument('--mapping');ap.add_argument('--rois')
    ap.add_argument('--native-all',action='store_true');ap.add_argument('--deps');ap.add_argument('--scalar-cpu',action='store_true')
    ap.add_argument('--plan');ap.add_argument('--video');ap.add_argument('--ffmpeg');ap.add_argument('--ffprobe');args=ap.parse_args()
    if args.command=='verify-rgb':
        if not args.plan or not args.video or not args.out:ap.error('verify-rgb requires --plan, --video and --out')
        result=canonical_rgb(args.plan,args.video,args.out,args.ffmpeg,args.ffprobe,args.deps)
        result={k:result[k] for k in ['status','frames','PTS_duration_status','visual_status']}
    else:
        if not args.project or not args.window_id:ap.error('evidence/map requires --project and --window-id')
        if args.command=='map':
            if not args.mapping:ap.error('map requires --mapping')
            result=apply_mapping(args.project,args.window_id,args.mapping)
        else:
            if not args.candidate or not args.out:ap.error('evidence requires --candidate and --out')
            mapping=factory.read(factory.resolve(args.project,args.mapping)).get('source_to_output') if args.mapping else None
            rois=factory.read(factory.resolve(args.project,args.rois)) if args.rois else None
            result=prepare_evidence(args.project,args.window_id,args.candidate,args.out,mapping,rois,args.native_all,args.deps,args.scalar_cpu)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

"""Bounded owner decoding, representative encoding checks and verified mux recovery.

Preserves arbitrary source PTS/SAR/color and all original audio. No cloud calls
or visual approval. Partial encodes are evidence, never resumable silent files.
"""
from pathlib import Path
from fractions import Fraction
from itertools import groupby
from functools import lru_cache
from contextlib import contextmanager
import hashlib,importlib,json,os,uuid,copy
import media

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def write(path,value):
    path=Path(path);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temp,path)

@lru_cache(maxsize=4)
def backend_identity(deps=None,scalar_cpu=False):
    av=media.runtime(deps,scalar_cpu);files={}
    for name in ('av._core','av.codec.context','av.container.core'):
        module=importlib.import_module(name)
        if getattr(module,'__file__',None):files[name]=media.signature(module.__file__)['sha256']
    libraries=Path(av.__file__).resolve().parent.parent/'av.libs'
    if libraries.is_dir():
        for p in libraries.iterdir():
            if p.is_file() and any(k in p.name.lower() for k in ('avcodec','avformat','avutil','swscale')):
                files[p.name]=media.signature(p)['sha256']
    return {'pyav':av.__version__,'libraries':{k:list(v) for k,v in av.library_versions.items()},'binaries':files,'scalar_cpu':scalar_cpu}

@contextmanager
def lock(path):
    with Path(path).open('a+b') as handle:
        handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

def binding(source,video,assignments,deps,scalar):
    paths=sorted({str(Path(path).resolve()) for path,index in assignments})
    return {'version':1,'source':media.signature(source),'source_video_digest':digest(video),
        'candidates':{p:media.signature(p) for p in paths},
        'assignments_digest':digest([[str(Path(p).resolve()),i] for p,i in assignments]),
        'backend':backend_identity(deps,scalar),'checker_sha256':media.signature(__file__)['sha256'],
        'settings':{'codec':'libx264','crf':10,'preset':'slow','threads':1,'pixel_format':'yuv420p'}}

def selected_frames(assignments,canvas,deps=None,scalar_cpu=False):
    """At most one decoded frame is retained; reopen on a path/phase switch."""
    av=media.runtime(deps,scalar_cpu);width,height=canvas['width'],canvas['height']
    source_dar=Fraction(width,height)*Fraction(canvas['sample_aspect_ratio'])
    for path,group in groupby(assignments,key=lambda a:str(a[0])):
        indices=[a[1] for a in group]
        if any(type(i)is not int or i<0 for i in indices) or any(b<a for a,b in zip(indices,indices[1:])):
            raise ValueError('Contiguous candidate owners must have nonnegative monotonic integer indices')
        with av.open(path) as container:
            stream=container.streams.video[0];stream.codec_context.thread_count=1
            candidate_dar=Fraction(stream.width,stream.height)*Fraction(stream.sample_aspect_ratio or 1)
            cursor=0;rotation=None
            for index,frame in enumerate(container.decode(stream)):
                if rotation is None:
                    rotation=getattr(frame,'rotation',0)%360
                    if rotation not in (0,90,180,270):raise ValueError('Candidate has a non-orthogonal display transform')
                    if rotation in (90,270):candidate_dar=1/candidate_dar
                    if candidate_dar!=source_dar:raise ValueError('Candidate display aspect differs; explicit canvas repair is required')
                if index==indices[cursor]:
                    displayed=media.display_frame(frame,rotation)
                    while cursor<len(indices) and indices[cursor]==index:
                        yield displayed.reformat(width,height,format='yuv420p')
                        cursor+=1
                    if cursor==len(indices):break
            if cursor!=len(indices):raise ValueError('Candidate does not contain all owned frames')

def check_video(source_video,report):
    canvas=media.display_canvas(source_video);expected=source_video['frames'];actual=report['video']['frames']
    if len(actual)!=len(expected):raise ValueError('Final frame count mismatch')
    origin=Fraction(source_video['start_time']);tolerance=Fraction(report['video']['time_base'])
    if any(abs(Fraction(b['time'])-(Fraction(a['time'])-origin))>tolerance for a,b in zip(expected,actual)):
        raise ValueError('Final PTS differs from source')
    if abs(Fraction(report['video']['end_time'])-(Fraction(source_video['end_time'])-origin))>tolerance:
        raise ValueError('Final frame duration differs from source')
    if ((report['video']['width'],report['video']['height'])!=(canvas['width'],canvas['height']) or
        Fraction(report['video']['sample_aspect_ratio'])!=Fraction(canvas['sample_aspect_ratio']) or
        report['video'].get('color_metadata')!=source_video.get('color_metadata') or
        int(report['video'].get('rotation',0))%360):
        raise ValueError('Final canvas/SAR/color/display metadata differs from source')

def encode(video_meta,assignments,path,deps,scalar,progress=None):
    av=media.runtime(deps,scalar);canvas=media.display_canvas(video_meta)
    if canvas['width']%2 or canvas['height']%2:raise ValueError('Odd output dimensions require an approved canvas plan')
    tb=Fraction(video_meta['time_base']);origin=Fraction(video_meta['start_time'])
    durations={Fraction(r['time'])-origin:Fraction(r['duration']) for r in video_meta['frames']}
    sent=0
    try:
        with av.open(str(path),'w') as output:
            stream=output.add_stream('libx264',rate=Fraction(video_meta['fps'] or 24))
            stream.width,stream.height,stream.pix_fmt=canvas['width'],canvas['height'],'yuv420p'
            stream.time_base=stream.codec_context.time_base=tb
            stream.codec_context.thread_count=1
            stream.codec_context.sample_aspect_ratio=Fraction(canvas['sample_aspect_ratio'])
            for name,value in video_meta.get('color_metadata',{}).items():setattr(stream.codec_context,name,value)
            stream.options={'crf':'10','preset':'slow'}
            def mux(packet):
                if packet.pts is not None:
                    t=Fraction(packet.pts)*packet.time_base
                    if t in durations:
                        ticks=durations[t]/packet.time_base
                        if ticks.denominator!=1:raise ValueError('Frame duration not representable in output timebase')
                        packet.duration=int(ticks)
                output.mux(packet)
            for i,frame in enumerate(selected_frames(assignments,canvas,deps,scalar)):
                ticks=(Fraction(video_meta['frames'][i]['time'])-origin)/tb
                if ticks.denominator!=1:raise ValueError('Source PTS cannot be represented exactly')
                frame.pts,frame.time_base=int(ticks),tb
                for packet in stream.encode(frame):mux(packet)
                sent+=1
                if progress and sent%120==0:write(progress,{'status':'encoding','sent_frames':sent,'partial':str(path),'automatic_partial_resume':False})
            for packet in stream.encode():mux(packet)
        if sent!=len(assignments):raise ValueError('Incomplete final encode')
    except BaseException as error:
        if progress:write(progress,{'status':'failed','sent_frames':sent,'partial':str(path),'automatic_partial_resume':False,'error_type':type(error).__name__})
        raise

def smoke_selection(video,assignments):
    n=len(assignments);wanted=set(range(min(32,n)))|set(range(max(0,n-64),n))
    boundaries=[i for i in range(1,n) if str(assignments[i][0])!=str(assignments[i-1][0])]
    for i in boundaries:
        wanted.update((i-1,i))
    # Include distributed real source phases without assuming a frame rate.
    budget=min(260,n)
    wanted.update(round(i*(n-1)/max(1,budget-1)) for i in range(budget))
    indices=sorted(wanted);sample=copy.deepcopy(video);frames=[];time=Fraction(0)
    for i in indices:
        record=dict(video['frames'][i]);record['index']=len(frames);record['time']=str(time)
        record['pts']=int(time/Fraction(video['time_base']));frames.append(record);time+=Fraction(record['duration'])
    sample.update(frames=frames,frame_count=len(frames),start_time='0',end_time=str(time))
    return sample,[assignments[i] for i in indices],indices

def audio_info(path,av):
    with av.open(str(path)) as c:
        return [(s.codec_context.sample_rate,s.codec_context.layout.name,dict(s.metadata).get('language'),int(s.disposition)) for s in c.streams.audio]

def remux(source,silent,temporary,video,deps,scalar):
    av=media.runtime(deps,scalar);origin=Fraction(video['start_time'])
    with av.open(str(source)) as original,av.open(str(silent)) as picture,av.open(str(temporary),'w') as output:
        output.metadata.update({k:v for k,v in original.metadata.items() if k!='encoder'})
        v=output.add_stream_from_template(picture.streams.video[0])
        audio={s.index:output.add_stream_from_template(s) for s in original.streams.audio}
        for s in original.streams.audio:audio[s.index].metadata.update(dict(s.metadata));audio[s.index].disposition=s.disposition
        for packet in picture.demux(picture.streams.video[0]):
            if packet.size:packet.stream=v;output.mux(packet)
        for packet in original.demux(list(original.streams.audio)):
            if packet.size and packet.stream.index in audio:
                shift=origin/packet.time_base
                if shift.denominator!=1:raise ValueError('Audio origin offset not representable without resampling')
                if packet.pts is not None:packet.pts-=int(shift)
                if packet.dts is not None:packet.dts-=int(shift)
                packet.stream=audio[packet.stream.index];output.mux(packet)
    report=media.inspect(temporary,deps,scalar);check_video(video,report)
    before=media.audio_packets(source,deps,scalar);after=media.audio_packets(temporary,deps,scalar)
    if len(before)!=len(after):raise ValueError('Final audio stream count mismatch')
    for a,b in zip(before,after):
        if a['codec']!=b['codec'] or len(a['packets'])!=len(b['packets']):raise ValueError('Audio remux changed codec/packet count')
        for x,y in zip(a['packets'],b['packets']):
            if x['sha256']!=y['sha256'] or (x['time'] is not None and abs(Fraction(x['time'])-origin-Fraction(y['time']))>Fraction(1,48000)):
                raise ValueError('Audio remux changed payload or offset')
    if audio_info(source,av)!=audio_info(temporary,av):raise ValueError('Final audio layout/language/default metadata differs from source')
    report['audio_packet_verification']='passed'
    return report

def assemble(source,video,assignments,output,deps=None,scalar_cpu=False):
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    if len(assignments)!=len(video['frames']):raise ValueError('Frame ownership has a gap')
    with lock(output.with_suffix('.assembly.lock')):
        if output.exists():raise FileExistsError('Existing final is never overwritten')
        contract=binding(source,video,assignments,deps,scalar_cpu)
        silent=output.with_suffix('.silent.mp4');proof=silent.with_suffix('.proof.json')
        reused=False
        if silent.exists():
            data=json.loads(proof.read_text(encoding='utf-8')) if proof.is_file() else {}
            if (data.get('kind')!='assembly-silent-proof' or data.get('contract')!=contract or
                data.get('status')!='passed' or data.get('file_signature')!=media.signature(silent) or
                data.get('report_digest')!=digest(data.get('report'))):
                raise ValueError('Silent video incomplete, changed or belongs to another selection/settings; preserve it and use a new output version')
            check_video(video,data['report']);reused=True
        else:
            sample,sample_assignments,indices=smoke_selection(video,assignments)
            smoke_path=output.with_suffix('.smoke.mp4');smoke_proof=output.with_suffix('.smoke.json')
            previous=json.loads(smoke_proof.read_text(encoding='utf-8')) if smoke_proof.is_file() else {}
            prior_path=Path(previous.get('path',str(smoke_path)))
            if prior_path.resolve().is_relative_to(output.parent.resolve()):smoke_path=prior_path
            if not (previous.get('contract')==contract and previous.get('indices')==indices and smoke_path.is_file() and
                    previous.get('file_signature')==media.signature(smoke_path) and previous.get('status')=='passed'):
                smoke_path=output.with_name(output.stem+'.'+uuid.uuid4().hex+'.smoke.mp4')
                encode(sample,sample_assignments,smoke_path,deps,scalar_cpu)
                smoke_report=media.inspect(smoke_path,deps,scalar_cpu);check_video(sample,smoke_report)
                write(smoke_proof,{'kind':'encoding-smoke','contract':contract,'indices':indices,'status':'passed',
                    'file_signature':media.signature(smoke_path),'path':str(smoke_path),'visual_status':'pending'})
            partial=output.with_name(output.stem+'.'+uuid.uuid4().hex+'.silent.partial.mp4')
            encode(video,assignments,partial,deps,scalar_cpu,output.with_suffix('.progress.json'))
            report=media.inspect(partial,deps,scalar_cpu);check_video(video,report)
            if binding(source,video,assignments,deps,scalar_cpu)!=contract:raise ValueError('Assembly inputs changed while encoding')
            os.replace(partial,silent);report['file_signature']=media.signature(silent)
            write(proof,{'kind':'assembly-silent-proof','status':'passed','contract':contract,'file_signature':media.signature(silent),
                'report':report,'report_digest':digest(report),'visual_status':'pending'})
        temporary=output.with_name(output.stem+'.'+uuid.uuid4().hex+'.mux.partial.mp4')
        report=remux(source,silent,temporary,video,deps,scalar_cpu)
        if binding(source,video,assignments,deps,scalar_cpu)!=contract:raise ValueError('Assembly inputs changed while muxing')
        os.replace(temporary,output);report['file_signature']=media.signature(output)
        # Keep normalization disclosure; no crop/stretch or hidden frame-rate conversion.
        resizes=[]
        av=media.runtime(deps,scalar_cpu);canvas=media.display_canvas(video)
        for p in dict.fromkeys(str(p) for p,i in assignments):
            with av.open(p) as c:
                s=c.streams.video[0];f=next(c.decode(s));rotation=getattr(f,'rotation',0)%360
                size=[s.height,s.width] if rotation in (90,270) else [s.width,s.height]
                if size!=[canvas['width'],canvas['height']] or rotation:resizes.append({'path':Path(p).name,'native':size,
                    'output':[canvas['width'],canvas['height']],'baked_rotation':rotation,'reason':'source display-canvas normalization'})
        report.update(candidate_canvas_resizes=resizes,visual_status='pending',assembly_session={'bounded_frame_decode':True,
            'complete_silent_reused':reused,'automatic_partial_resume':False,'video_encodes':1,'smoke_is_not_whole_film_approval':True})
        return report

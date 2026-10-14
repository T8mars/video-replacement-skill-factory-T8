"""Measured24fps portrait -> square8fps correction projection; no visual verdict."""
from pathlib import Path
from fractions import Fraction
import argparse, hashlib, os, shutil, subprocess
import factory, media


def measured_fps(video):
    frames=video['frames']
    durations={Fraction(f['duration']) for f in frames}
    if len(durations)!=1 or next(iter(durations))<=0:
        raise ValueError('Measured correction route requires a complete constant PTS clock')
    return 1/next(iter(durations))


def contract(context, owner, fps='24', width=720, height=1280):
    start,end=context;a,b=owner
    if (Fraction(fps)!=24 or (width,height)!=(720,1280) or
            any(type(x) is not int for x in (start,end,a,b)) or not 0<=start<=a<b<=end):
        raise ValueError('Measured route requires24fps720x1280 and valid source intervals')
    count=(end-start)*3
    if Fraction(count,24)>10:
        raise ValueError('Complete compensated input exceeds10seconds; split at reviewed natural action phases')
    return {'schema_version':1,'context':list(context),'owner':list(owner),
            'input_frames':count,'input_fps':'24','input_seconds':str(Fraction(count,24)),
            'repeat':3,'output_frames':end-start,'output_fps':'8',
            'canvas':[1280,1280],'body_rect':[280,0,1000,1280]}


def raw_writer(av,path,images,width,height):
    rows=[]
    with av.open(str(path),'w',format='nut') as out:
        st=out.add_stream('rawvideo',rate=24);st.width=width;st.height=height
        st.pix_fmt='rgb24';st.time_base=Fraction(1,12288)
        for source,image in images:
            i=len(rows);rows.append({'index':i,'source':source,'rgb_sha256':hashlib.sha256(image.tobytes()).hexdigest()})
            frame=av.VideoFrame.from_image(image);frame.pts=i*512;frame.time_base=Fraction(1,12288)
            for packet in st.encode(frame):out.mux(packet)
        for packet in st.encode():out.mux(packet)
    count=0
    with av.open(str(path)) as inp:
        for i,frame in enumerate(inp.decode(video=0)):
            if (Fraction(frame.pts)*frame.time_base!=Fraction(i,24) or
                    hashlib.sha256(frame.to_image().tobytes()).hexdigest()!=rows[i]['rgb_sha256']):
                raise ValueError('Raw complete RGB/PTS verification failed')
            count+=1
    if count!=len(rows):raise ValueError('Raw owner frame count differs')
    return rows


def prepare(body,destination,context,owner,deps=None,scalar_cpu=False):
    from PIL import Image
    from face_swap import ScalarTransport
    av=media.runtime(deps,scalar_cpu);body=Path(body);destination=Path(destination)
    if destination.exists():raise FileExistsError('Projection exists; recover without overwriting')
    signature=media.signature(body);v=media.inspect(body,deps,scalar_cpu)['video']
    c=contract(context,owner,measured_fps(v),v['width'],v['height'])
    if not v['cfr'] or context[1]>v['frame_count']:raise ValueError('Measured route requires complete CFR source')
    destination.mkdir(parents=True)
    def images():
        with av.open(str(body)) as inp:
            inp.streams.video[0].codec_context.thread_count=1
            for i,frame in enumerate(inp.decode(video=0)):
                if i>=context[1]:break
                if i<context[0]:continue
                if Fraction(frame.pts)*frame.time_base-Fraction(v['start_time'])!=Fraction(i,24):
                    raise ValueError('Body source clock changed')
                body_image=frame.to_image();square=Image.new('RGB',(1280,1280),'black');square.paste(body_image,(280,0))
                if square.crop(tuple(c['body_rect'])).tobytes()!=body_image.tobytes():raise ValueError('Envelope cropped body')
                for _ in range(3):yield i,square
    raw=destination/'repeated-rgb.nut';rows=raw_writer(av,raw,images(),1280,1280)
    if len(rows)!=c['input_frames']:raise ValueError('Incomplete projected input')
    ffmpeg=os.environ.get('VIDEO_FACTORY_FFMPEG') or shutil.which('ffmpeg')
    if not ffmpeg:raise ValueError('ffmpeg is required')
    video=destination/'body-input.mp4'
    result=subprocess.run([ffmpeg,'-nostdin','-v','error','-cpuflags','0','-threads','1','-i',str(raw),'-an',
        '-c:v','libx264','-preset','veryfast','-crf','10','-threads','8','-profile:v','baseline',
        '-x264-params','asm=0:ref=1:keyint=1:min-keyint=1:bframes=0:cabac=0','-pix_fmt','yuv420p',
        '-fps_mode','passthrough','-video_track_timescale','12288','-movflags','+faststart','-n',str(video)],capture_output=True,timeout=300)
    if result.returncode:raise ValueError('Encoding failed; preserve raw and failed file')
    ScalarTransport._verify_download(video)
    report=media.inspect(video,deps,scalar_cpu)
    if (report['video']['frame_count']!=c['input_frames'] or report['video']['fps']!='24' or
            Fraction(report['video']['end_time'])>10):raise ValueError('Encoded input clock invalid')
    if signature!=media.signature(body):raise ValueError('Body changed during projection')
    proof={'contract':c,'body_signature':signature,'raw_signature':media.signature(raw),'rows':rows,
           'input_signature':media.signature(video),'report':report,'complete_raw_RGB_PTS':'passed',
           'encoding':'lossy CRF10; complete strict decode passed','visual':'pending','production_authorized':False}
    factory.write(destination/'projection-proof.json',proof);return proof


def restore(candidate,destination,projection,deps=None,scalar_cpu=False):
    av=media.runtime(deps,scalar_cpu);candidate=Path(candidate);destination=Path(destination)
    c=projection['contract']
    if c!=contract(c['context'],c['owner']):raise ValueError('Projection contract changed')
    if destination.exists():raise FileExistsError('Restored candidate exists; preserve it')
    signature=media.signature(candidate);v=media.inspect(candidate,deps,scalar_cpu)['video']
    if (v['frame_count']!=c['output_frames'] or measured_fps(v)!=8 or
            [v['width'],v['height']]!=c['canvas']):
        raise ValueError('Output lost poses, changed fps or cropped canvas; no guessed resampling')
    def images():
        with av.open(str(candidate)) as inp:
            inp.streams.video[0].codec_context.thread_count=1
            for i,frame in enumerate(inp.decode(video=0)):
                if Fraction(frame.pts)*frame.time_base-Fraction(v['start_time'])!=Fraction(i,8):raise ValueError('Actual candidate PTS differs')
                source=c['context'][0]+i
                if c['owner'][0]<=source<c['owner'][1]:yield source,frame.to_image().crop(tuple(c['body_rect']))
    destination.parent.mkdir(parents=True,exist_ok=True)
    rows=raw_writer(av,destination,images(),720,1280)
    if len(rows)!=c['owner'][1]-c['owner'][0] or signature!=media.signature(candidate):raise ValueError('Owner recovery incomplete or parent changed')
    result={'candidate_signature':signature,'raw_signature':media.signature(destination),'contract':c,'rows':rows,
            'complete_owner_RGB_PTS':'passed','source_pose_correspondence':'pending-actual-content-review',
            'identity':'pending-original-photo','occlusion':'pending','actual1x':'pending'}
    factory.write(destination.with_suffix('.proof.json'),result);return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=('prepare','restore'));ap.add_argument('--body');ap.add_argument('--candidate')
    ap.add_argument('--destination',required=True);ap.add_argument('--context',nargs=2,type=int);ap.add_argument('--owner',nargs=2,type=int)
    ap.add_argument('--projection');ap.add_argument('--deps');ap.add_argument('--scalar-cpu',action='store_true')
    args=ap.parse_args()
    if args.command=='prepare':r=prepare(args.body,args.destination,args.context,args.owner,args.deps,args.scalar_cpu)
    else:r=restore(args.candidate,args.destination,factory.read(args.projection),args.deps,args.scalar_cpu)
    print(__import__('json').dumps({'contract':r['contract'],'visual':'pending'},ensure_ascii=False))

if __name__=='__main__':main()

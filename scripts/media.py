"""Deterministic local media work; never judges identity or motion quality."""
from __future__ import annotations
import ctypes
import hashlib
import json
import math
import os
import sys
import bisect
from fractions import Fraction
from pathlib import Path


def runtime(deps=None, scalar_cpu=False):
    if deps and str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    import av
    if scalar_cpu:
        if os.name != 'nt':
            raise ValueError('scalar_cpu is only implemented for Windows PyAV wheels')
        matches = list((Path(av.__file__).resolve().parent.parent / 'av.libs').glob('avutil-*.dll'))
        if len(matches) != 1:
            raise ValueError('Cannot locate a unique bundled avutil DLL')
        dll = ctypes.CDLL(str(matches[0]))
        dll.av_force_cpu_flags.argtypes = [ctypes.c_int]
        dll.av_force_cpu_flags.restype = None
        dll.av_force_cpu_flags(0)
    return av


def signature(path):
    path = Path(path)
    before = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f'File changed while hashing: {path.name}')
    return {'sha256': h.hexdigest(), 'size_bytes': after.st_size, 'mtime_ns': after.st_mtime_ns}


def fraction(value):
    return str(Fraction(value))


def display_canvas(video):
    rotation=int(video.get('rotation',0))%360
    if rotation not in (0,90,180,270):
        raise ValueError('Only orthogonal display rotations are currently implemented')
    matrix=video.get('display_matrix')
    if matrix and matrix[0]*matrix[4]-matrix[1]*matrix[3]<0:
        raise ValueError('Mirrored display matrix needs an explicit transform plan')
    width,height=video['width'],video['height']
    sar=Fraction(video['sample_aspect_ratio'])
    if rotation in (90,270):
        width,height=height,width
        sar=1/sar
    return {'width':width,'height':height,'sample_aspect_ratio':str(sar),
            'rotation':0,'baked_source_rotation':rotation}


def display_frame(frame,rotation):
    rotation=int(rotation)%360
    if not rotation:
        return frame
    from PIL import Image
    av=runtime()
    transform={90:Image.Transpose.ROTATE_90,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_270}
    if rotation not in transform:
        raise ValueError('Unsupported display rotation')
    return av.VideoFrame.from_image(frame.to_image().transpose(transform[rotation]))


def project_frames(video,context,generation_fps,generation_frames=None,preroll_frames=0,source_time_scale='1'):
    """Explicit zero-order-hold CFR projection; records loss instead of claiming invertibility."""
    fps=Fraction(generation_fps)
    scale=Fraction(source_time_scale)
    if not 1<=scale<=4:
        raise ValueError('Source time scale must be between 1 and 4')
    if fps<=0:
        raise ValueError('Positive projection fps required')
    if type(preroll_frames) is not int or not 0<=preroll_frames<=64:
        raise ValueError('Preroll must be an explicit integer 0..64')
    a,b=context
    records=video['frames'][a:b]
    origin=Fraction(records[0]['time'])
    end=Fraction(records[-1]['time'])+Fraction(records[-1]['duration'])
    duration=end-origin
    ticks=duration*fps*scale
    needed=-(-ticks.numerator//ticks.denominator)
    generation_frames=needed+preroll_frames if generation_frames is None else generation_frames
    if generation_frames<needed+preroll_frames:
        raise ValueError('Generation schedule truncates the source display interval')
    starts=[Fraction(r['time'])-origin for r in records]
    samples=[a]*preroll_frames
    for index in range(generation_frames-preroll_frames):
        sample_time=Fraction(index,1)/fps/scale
        local=min(len(records)-1,max(0,bisect.bisect_right(starts,sample_time)-1))
        samples.append(a+local)
    first={}
    for index,source_index in enumerate(samples[preroll_frames:preroll_frames+needed],start=preroll_frames):
        first.setdefault(source_index,index)
    dropped=[index for index in range(a,b) if index not in first]
    source_to_generation={}
    for index in range(a,b):
        quantized=(Fraction(video['frames'][index]['time'])-origin)*fps*scale
        fallback=min(needed-1,-(-quantized.numerator//quantized.denominator))
        source_to_generation[str(index)]=first.get(index,preroll_frames+fallback)
    offsets=[abs(Fraction(source_to_generation[str(i)]-preroll_frames,1)/fps/scale-(Fraction(video['frames'][i]['time'])-origin)) for i in range(a,b)]
    repeats=needed-len(set(samples[preroll_frames:preroll_frames+needed]))
    requires_review=bool(scale!=1 or dropped or repeats or not video['cfr'] or any(offsets))
    result={'schema_version':1,'fps':str(fps),'context_interval':[a,b],
            'content_frames':needed,'generation_frames':generation_frames,
            'generation_to_source':samples,'source_to_generation':source_to_generation,
            'source_interval_duration':str(duration),'padding_frames':generation_frames-needed-preroll_frames,
            'dropped_source_frames':dropped,'repeated_content_frames':repeats,
            'maximum_time_quantization':str(max(offsets,default=Fraction(0))),
            'requires_temporal_review':requires_review,
            'invertible':False if requires_review else True,
            'display_canvas':display_canvas(video)}
    if preroll_frames:
        result['preroll_frames']=preroll_frames
    if scale!=1:
        result['source_time_scale']=str(scale)
        result['generation_content_duration']=str(duration*scale)
    return result


def inspect(path, deps=None, scalar_cpu=False, evidence_dir=None):
    """Decode video/audio completely; source cuts are suggestions, never verdicts."""
    av = runtime(deps, scalar_cpu)
    path = Path(path)
    before = signature(path)
    frame_records, cuts, previous = [], [], None
    evidence = Path(evidence_dir) if evidence_dir else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    with av.open(str(path)) as container:
        videos = list(container.streams.video)
        if len(videos) != 1:
            raise ValueError('Exactly one video stream is currently implemented')
        stream = videos[0]
        stream.codec_context.thread_count = 1
        fps = stream.average_rate
        video = {'width': stream.width, 'height': stream.height,
                 'time_base': fraction(stream.time_base),
                 'fps': fraction(fps) if fps else None,
                 'sample_aspect_ratio': fraction(stream.sample_aspect_ratio or 1),
                 'rotation': stream.metadata.get('rotate', '0'),
                 'metadata': dict(stream.metadata),
                 'codec': stream.codec_context.name}
        video['color_metadata']={name:int(getattr(stream.codec_context,name))
                                 for name in ('colorspace','color_range','color_trc','color_primaries')
                                 if getattr(stream.codec_context,name,None) is not None}
        for index, frame in enumerate(container.decode(stream)):
            if frame.pts is None:
                raise ValueError(f'Frame {index} has no PTS')
            if getattr(frame, 'is_corrupt', False):
                raise ValueError(f'Corrupt video frame {index}')
            if index == 0:
                if getattr(frame,'rotation',0):
                    video['rotation']=str(frame.rotation)
                    video['display_transform_status']='requires_verified_normalization'
                for side in frame.side_data.values():
                    if getattr(side.type, 'name', '') == 'DISPLAYMATRIX':
                        import struct
                        raw = bytes(side)
                        if len(raw) != 36:
                            raise ValueError('Unknown display matrix representation')
                        matrix = list(struct.unpack('=9i', raw))
                        video['display_matrix'] = matrix
                        angle = math.degrees(math.atan2(matrix[1], matrix[0]))
                        video['matrix_angle'] = str(round(angle))
                        video['rotation'] = str(getattr(frame,'rotation',-round(angle)))
                        video['display_transform_status'] = 'requires_verified_normalization' if round(angle)%360 else 'identity'
            timestamp = Fraction(frame.pts) * frame.time_base
            if frame_records and timestamp <= Fraction(frame_records[-1]['time']):
                raise ValueError('Video display PTS must be strictly increasing')
            duration = Fraction(frame.duration) * frame.time_base if frame.duration else None
            frame_records.append({'index': index, 'pts': frame.pts, 'time': fraction(timestamp),
                                  'duration': fraction(duration) if duration else None})
            image = frame.to_image()
            small = image.resize((32, 18)).convert('RGB')
            pixels = list(small.getdata())
            if previous is not None:
                difference = sum(abs(a - b) for p, q in zip(previous, pixels)
                                 for a, b in zip(p, q)) / (32 * 18 * 3 * 255)
                if difference > 0.30:
                    cuts.append({'frame': index, 'difference': round(difference, 6),
                                 'status': 'candidate_unreviewed'})
            previous = pixels
            # Evidence every second plus full first/last; cut frames are generated below.
            if evidence and (index == 0 or timestamp.denominator == 1):
                display_frame(frame,video['rotation']).to_image().save(evidence / f'frame_{index:06d}.png')
        if not frame_records:
            raise ValueError('Video has no decoded frames')
        if evidence:
            display_frame(frame,video['rotation']).to_image().save(evidence / f"frame_{len(frame_records)-1:06d}.png")
        for index in range(len(frame_records) - 1):
            frame_records[index]['duration'] = fraction(
                Fraction(frame_records[index+1]['time']) - Fraction(frame_records[index]['time']))
        if not frame_records[-1]['duration']:
            if fps:
                frame_records[-1]['duration'] = fraction(1 / fps)
                video['last_duration_evidence'] = 'average_rate_fallback_unverified'
            else:
                raise ValueError('Cannot determine final frame duration')
        else:
            video['last_duration_evidence'] = 'decoded_frame_duration'
        video['frames'] = frame_records
        video['frame_count'] = len(frame_records)
        durations = {r['duration'] for r in frame_records}
        video['cfr'] = len(durations) == 1
        video['start_time'] = frame_records[0]['time']
        video['end_time'] = fraction(Fraction(frame_records[-1]['time']) + Fraction(frame_records[-1]['duration']))
        try:
            video['display_canvas']=display_canvas(video)
        except ValueError as error:
            video['display_canvas']={'status':'pending','reason':str(error)}
    audio = []
    with av.open(str(path)) as container:
        for audio_stream in container.streams.audio:
            record = {'index': audio_stream.index, 'codec': audio_stream.codec_context.name,
                      'time_base': fraction(audio_stream.time_base),
                      'start_time': fraction(Fraction(audio_stream.start_time) * audio_stream.time_base)
                      if audio_stream.start_time is not None else None,
                      'sample_rate': audio_stream.codec_context.sample_rate,
                      'layout': audio_stream.codec_context.layout.name,
                      'metadata': dict(audio_stream.metadata),
                      'disposition': int(audio_stream.disposition), 'decoded_samples': 0}
            # Independent container per track avoids sequential demux consumption.
            with av.open(str(path)) as audio_container:
                selected = audio_container.streams[audio_stream.index]
                for frame in audio_container.decode(selected):
                    if getattr(frame, 'is_corrupt', False):
                        raise ValueError('Corrupt audio frame')
                    record['decoded_samples'] += frame.samples
            audio.append(record)
    if signature(path) != before:
        raise ValueError('Source changed during decoding')
    # One-frame return transitions are flagged as possible flashes, not deleted.
    for index, cut in enumerate(cuts[:-1]):
        if 0 < cuts[index+1]['frame'] - cut['frame'] <= 2:
            cut['possible_flash'] = True
    return {'file_signature': before, 'video': video, 'audio_streams': audio,
            'cut_candidates': cuts, 'backend': {'pyav': av.__version__,
                                               'scalar_cpu': scalar_cpu},
            'technical_status': 'passed', 'visual_status': 'pending'}


def extract_evidence(path, indices, directory, deps=None, scalar_cpu=False):
    av = runtime(deps, scalar_cpu)
    indices = set(indices)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    found = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 1
        rotation=None
        for index, frame in enumerate(container.decode(stream)):
            if rotation is None:
                rotation=getattr(frame,'rotation',0)
            if index in indices:
                output = directory / f'frame_{index:06d}.png'
                display_frame(frame,rotation).to_image().save(output)
                found.append(index)
    if set(found) != indices:
        raise ValueError('Evidence frame index outside source')


def make_input(source_path, output_path, source_video, context, generation_frames,
               generation_fps, deps=None, scalar_cpu=False,preroll_frames=0,opening_guide=None,source_time_scale='1'):
    """Explicit CFR projection with orthogonal display-matrix normalization."""
    av = runtime(deps, scalar_cpu)
    fps = Fraction(generation_fps)
    a, b = context
    frames = []
    with av.open(str(source_path)) as source:
        stream = source.streams.video[0]
        stream.codec_context.thread_count = 1
        for index, frame in enumerate(source.decode(stream)):
            if a <= index < b:
                frames.append(frame)
    if len(frames) != b-a:
        raise ValueError('Source context decode incomplete')
    projection=project_frames(source_video,context,fps,generation_frames,preroll_frames,source_time_scale)
    canvas=projection['display_canvas']
    width, height = canvas['width'],canvas['height']
    guide_frame = None
    if opening_guide:
        if not preroll_frames:
            raise ValueError('Opening image cannot replace owned source content')
        from PIL import Image, ImageOps
        with Image.open(opening_guide) as image:
            fit = ImageOps.contain(image.convert('RGB'), (width, height), Image.Resampling.LANCZOS)
            guide_canvas = Image.new('RGB', (width, height), 'black')
            guide_canvas.paste(fit, ((width-fit.width)//2, (height-fit.height)//2))
            guide_frame = av.VideoFrame.from_image(guide_canvas)
    if width % 2 or height % 2:
        raise ValueError('Odd dimensions require explicit padded display-canvas planning')
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix('.part.mp4')
    with av.open(str(temporary), 'w') as output:
        stream = output.add_stream('libx264', rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, 'yuv420p'
        stream.codec_context.thread_count = 1
        stream.codec_context.sample_aspect_ratio=Fraction(canvas['sample_aspect_ratio'])
        for name,value in source_video.get('color_metadata',{}).items():
            setattr(stream.codec_context,name,value)
        stream.options = {'crf': '10', 'preset': 'slow'}
        for index in range(generation_frames):
            original_frame=frames[projection['generation_to_source'][index]-a]
            frame=(guide_frame if guide_frame is not None and index < preroll_frames else
                   display_frame(original_frame,source_video['rotation'])).reformat(width,height,format='yuv420p')
            frame.pts, frame.time_base = index, 1/fps
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    # H3 legacy needs an audio stream. Use local FFmpeg for precise source audio + padding.
    # Silent AAC is generated only in this derived input, never copied into the final.
    import shutil
    import subprocess
    ffmpeg = find_ffmpeg(deps)
    start = Fraction(source_video['frames'][a]['time']) - Fraction(source_video['start_time'])
    duration = Fraction(generation_frames, 1) / fps
    with av.open(str(source_path)) as source:
        has_audio = bool(source.streams.audio)
    args = [ffmpeg, '-nostdin', '-v', 'error', '-threads', '1']
    if scalar_cpu:
        args += ['-cpuflags', '0']
    args += ['-i', str(temporary)]
    if has_audio:
        # Stretch only the derived audio to follow the explicitly slowed source motion.
        # Final assembly still remuxes the original packets at their original times.
        tempo=Fraction(1,1)/Fraction(source_time_scale)
        filters=[]
        while tempo<Fraction(1,2):
            filters.append('atempo=0.5')
            tempo*=2
        if tempo!=1:
            filters.append(f'atempo={float(tempo)}')
        if preroll_frames:
            filters.append(f'adelay={float(Fraction(preroll_frames,1)/fps*1000)}:all=1')
        filters.append('apad')
        args += ['-ss', str(float(start)), '-i', str(source_path), '-map', '0:v:0',
                 '-map', '1:a:0', '-af', ','.join(filters)]
    else:
        args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-map', '0:v:0', '-map', '1:a:0']
    args += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', str(float(duration)), str(output_path)]
    result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError(f'Derived audio mux failed (exit {result.returncode}): {result.stderr[-1000:]}')
    report = inspect(output_path, deps, scalar_cpu)
    if report['video']['frame_count'] != generation_frames or not report['audio_streams']:
        raise ValueError('Derived input failed frame/audio verification')
    temporary.unlink()
    report['source_projection']=projection
    if opening_guide:
        report['opening_guide']={'file_signature':signature(opening_guide),
                                 'used_generation_interval':[0,preroll_frames],
                                 'fit':'Aspect-preserving contain/pad to the derived display canvas; excluded from owned content'}
    return report


def find_ffmpeg(deps=None):
    import shutil
    if deps:
        hits = list((Path(deps) / 'bin').glob('ffmpeg*.exe'))
        if len(hits) == 1:
            return str(hits[0])
        hits = list((Path(deps) / 'imageio_ffmpeg' / 'binaries').glob('ffmpeg*.exe'))
        if len(hits) == 1:
            return str(hits[0])
    command = shutil.which('ffmpeg')
    if command:
        return command
    raise ValueError('FFmpeg is required for derived-input audio mux')


def audio_packets(path, deps=None, scalar_cpu=False):
    av = runtime(deps, scalar_cpu)
    result = []
    with av.open(str(path)) as container:
        for stream in container.streams.audio:
            with av.open(str(path)) as fresh:
                record = {'codec': stream.codec_context.name, 'packets': []}
                for packet in fresh.demux(fresh.streams[stream.index]):
                    if packet.size:
                        record['packets'].append({'sha256': hashlib.sha256(bytes(packet)).hexdigest(),
                                                 'time': fraction(Fraction(packet.pts) * packet.time_base)
                                                 if packet.pts is not None else None})
                result.append(record)
    return result


def assemble(source_path, source_video, assignments, output_path, deps=None, scalar_cpu=False):
    """Encode selected candidate frames once at original PTS, remux every original audio track."""
    av = runtime(deps, scalar_cpu)
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError('Existing final is never overwritten')
    canvas=display_canvas(source_video)
    frames_meta = source_video['frames']
    if len(assignments) != len(frames_meta):
        raise ValueError('Frame ownership has a gap')
    width, height = canvas['width'],canvas['height']
    if width % 2 or height % 2:
        raise ValueError('Odd output dimensions require an approved canvas plan')
    # Group decode by file; only selected frames kept, preserving each exact requested index.
    wanted = {}
    for path, frame_index in assignments:
        wanted.setdefault(str(path), set()).add(frame_index)
    decoded = {}
    scale_records=[]
    for path, indices in wanted.items():
        with av.open(path) as container:
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            source_dar=Fraction(width,height)*Fraction(canvas['sample_aspect_ratio'])
            candidate_dar=Fraction(stream.width,stream.height)*Fraction(stream.sample_aspect_ratio or 1)
            rotation=None
            native_size=None
            all_frames=container.decode(stream)
            for index,frame in enumerate(all_frames):
                if rotation is None:
                    rotation=getattr(frame,'rotation',0)%360
                    if rotation not in (0,90,180,270):
                        raise ValueError('Candidate has a non-orthogonal display transform')
                    if rotation in (90,270):
                        candidate_dar=1/candidate_dar
                        native_size=(stream.height,stream.width)
                    else:
                        native_size=(stream.width,stream.height)
                    if source_dar!=candidate_dar:
                        raise ValueError('Candidate display aspect differs; explicit canvas repair is required')
                    if native_size!=(width,height) or rotation:
                        scale_records.append({'path':Path(path).name,'native':list(native_size),
                                              'output':[width,height],'baked_rotation':rotation,
                                              'reason':'source display-canvas normalization'})
                if index in indices:
                    decoded[(path,index)]=display_frame(frame,rotation)
        if any((path, index) not in decoded for index in indices):
            raise ValueError('Candidate does not contain all owned frames')
    temporary = output_path.with_suffix('.part.mp4')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_tb = Fraction(source_video['time_base'])
    origin = Fraction(source_video['start_time'])
    durations = {Fraction(r['time'])-origin: Fraction(r['duration']) for r in frames_meta}
    with av.open(str(source_path)) as original, av.open(str(temporary), 'w') as output:
        video = output.add_stream('libx264', rate=Fraction(source_video['fps'] or 24))
        video.width, video.height, video.pix_fmt = width, height, 'yuv420p'
        video.time_base = source_tb
        video.codec_context.time_base = source_tb
        video.codec_context.thread_count = 1
        video.codec_context.sample_aspect_ratio = Fraction(canvas['sample_aspect_ratio'])
        for name,value in source_video.get('color_metadata',{}).items():
            setattr(video.codec_context,name,value)
        video.options = {'crf': '10', 'preset': 'slow'}
        audio_streams = {stream.index: output.add_stream_from_template(stream)
                         for stream in original.streams.audio}
        for source_audio in original.streams.audio:
            target_audio=audio_streams[source_audio.index]
            target_audio.metadata.update(dict(source_audio.metadata))
            target_audio.disposition=source_audio.disposition
        def mux_video(packet):
            if packet.pts is not None:
                time = Fraction(packet.pts) * packet.time_base
                if time in durations:
                    ticks = durations[time] / packet.time_base
                    if ticks.denominator != 1:
                        raise ValueError('Frame duration not representable in output timebase')
                    packet.duration = int(ticks)
            output.mux(packet)
        for source_index, assignment in enumerate(assignments):
            frame = decoded[(str(assignment[0]), assignment[1])].reformat(width, height, format='yuv420p')
            time = Fraction(frames_meta[source_index]['time']) - origin
            ticks = time / source_tb
            if ticks.denominator != 1:
                raise ValueError('Source PTS cannot be represented exactly')
            frame.pts, frame.time_base = int(ticks), source_tb
            for packet in video.encode(frame):
                mux_video(packet)
        for packet in video.encode():
            mux_video(packet)
        for packet in original.demux(list(original.streams.audio)):
            if packet.size and packet.stream.index in audio_streams:
                shift = origin / packet.time_base
                if shift.denominator != 1:
                    raise ValueError('Audio origin offset not representable without resampling')
                if packet.pts is not None:
                    packet.pts -= int(shift)
                if packet.dts is not None:
                    packet.dts -= int(shift)
                packet.stream = audio_streams[packet.stream.index]
                output.mux(packet)
    report = inspect(temporary, deps, scalar_cpu)
    actual = report['video']['frames']
    if len(actual) != len(frames_meta):
        raise ValueError('Final frame count mismatch')
    tolerance = Fraction(report['video']['time_base'])
    for expected, observed in zip(frames_meta, actual):
        if abs(Fraction(observed['time']) - (Fraction(expected['time'])-origin)) > tolerance:
            raise ValueError('Final PTS differs from source')
    expected_end = Fraction(source_video['end_time'])-origin
    if abs(Fraction(report['video']['end_time'])-expected_end) > tolerance:
        raise ValueError('Final frame duration differs from source')
    if (Fraction(report['video']['sample_aspect_ratio'])!=Fraction(canvas['sample_aspect_ratio']) or
        report['video'].get('color_metadata')!=source_video.get('color_metadata')):
        raise ValueError('Final SAR or color metadata differs from source')
    if int(report['video'].get('rotation',0))%360:
        raise ValueError('Normalized final must not retain a display rotation tag')
    original_audio, final_audio = audio_packets(source_path, deps, scalar_cpu), audio_packets(temporary, deps, scalar_cpu)
    if len(original_audio) != len(final_audio):
        raise ValueError('Final audio stream count mismatch')
    for expected, observed in zip(original_audio, final_audio):
        if expected['codec'] != observed['codec'] or len(expected['packets']) != len(observed['packets']):
            raise ValueError('Audio remux changed codec/packet count')
        for a, b in zip(expected['packets'], observed['packets']):
            if a['sha256'] != b['sha256'] or (a['time'] is not None and
               abs((Fraction(a['time'])-origin)-Fraction(b['time'])) > Fraction(1, 48000)):
                raise ValueError('Audio remux changed payload or offset')
    original_info=inspect(source_path,deps,scalar_cpu)['audio_streams']
    for expected,observed in zip(original_info,report['audio_streams']):
        if (expected['sample_rate'],expected['layout'],expected['metadata'].get('language'),expected['disposition']) != \
           (observed['sample_rate'],observed['layout'],observed['metadata'].get('language'),observed['disposition']):
            raise ValueError('Final audio track layout/language/default metadata differs from source')
    os.replace(temporary, output_path)
    report['file_signature'] = signature(output_path)
    report['audio_packet_verification'] = 'passed'
    report['candidate_canvas_resizes']=scale_records
    report['visual_status'] = 'pending'
    return report

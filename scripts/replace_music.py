"""Make a music-only edition of a finished factory MP4 and a reusable aligned WAV."""
import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import tempfile


def streams(path):
    cmd = ['ffprobe','-v','error','-show_streams','-of','json',str(path)]
    return json.loads(subprocess.check_output(cmd))['streams']


def packets(path):
    cmd = ['ffprobe','-v','error','-select_streams','v:0','-show_packets',
           '-show_entries','packet=pts,dts,duration,data_hash','-show_data_hash','sha256','-of','json',str(path)]
    return json.loads(subprocess.check_output(cmd))['packets']


def encode(*args):
    subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-n',*map(str,args)], check=True)


def replace(video, music, directory, start=Fraction(0)):
    video, music, directory = map(Path, (video,music,directory))
    if video.suffix.lower() != '.mp4' or start < 0:
        raise ValueError('Supply a finished MP4 and a nonnegative music offset')
    original = streams(video)
    pictures = [s for s in original if s['codec_type'] == 'video']
    if len(pictures) != 1 or any(s['codec_type'] not in ('video','audio') for s in original):
        raise ValueError('Music-only editions support one video stream and optional audio tracks')
    v = pictures[0]
    if int(v['start_pts']) != 0:
        raise ValueError('Expected a zero-origin finished factory film')
    before = packets(video)
    duration = int(v['duration_ts']) * Fraction(v['time_base'])
    a = next((s for s in streams(music) if s['codec_type'] == 'audio'), None)
    if not a:
        raise ValueError('Music has no audio stream')
    rate = int(a['sample_rate'])
    count, offset = round(duration * rate), round(Fraction(start) * rate)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        raise FileExistsError('Choose a new edition directory')
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        stage = Path(temporary)/'edition'
        stage.mkdir()
        wav, out = stage/'aligned.wav', stage/'film.mp4'
        encode('-i',music,'-map','0:a:0','-af',f'atrim=start_sample={offset}:end_sample={offset+count},asetpts=PTS-STARTPTS',
               '-c:a','pcm_s24le',wav)
        audio = streams(wav)[0]
        if int(audio.get('duration_ts',0))*Fraction(audio['time_base'])*rate != count:
            raise ValueError('Music too short for the selected start and full film duration')
        encode('-i',video,'-i',wav,'-map','0:v:0','-map','1:a:0','-map_metadata','0',
               '-c:v','copy','-c:a','aac','-b:a','320k','-movflags','+faststart',out)
        after = streams(out)
        ov = next(s for s in after if s['codec_type']=='video')
        oa = next(s for s in after if s['codec_type']=='audio')
        if (ov['time_base'] != v['time_base'] or packets(out) != before or int(oa['start_pts']) != 0
                or abs(int(oa['duration_ts'])*Fraction(oa['time_base'])-duration) > Fraction(1,1000)):
            raise ValueError('Music or picture timing differs from the finished film')
        encode('-i',out,'-map','0:v:0','-map','0:a:0','-f','null','-')
        if directory.exists():
            raise FileExistsError('Edition directory already exists')
        os.rename(stage,directory)
    return {'film':str((directory/'film.mp4').resolve()), 'aligned_wav':str((directory/'aligned.wav').resolve()),
            'music_start_sample':offset, 'samples':count, 'sample_rate':rate, 'video_packets_verified':len(before)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',required=True,type=Path)
    parser.add_argument('--music',required=True,type=Path)
    parser.add_argument('--out-dir',required=True,type=Path)
    parser.add_argument('--start',type=Fraction,default=Fraction(0))
    args = parser.parse_args()
    print(json.dumps(replace(args.video,args.music,args.out_dir,args.start),ensure_ascii=False,indent=2))

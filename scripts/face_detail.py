"""Optional configured CodeFormer detail pass with exact unchanged-body proof.

Models stay outside skills. Original-photo likeness, occlusion, missing
detections and actual1x playback remain visual decisions, never score approvals.
"""
from pathlib import Path
from fractions import Fraction
import argparse, hashlib
import factory, media, identity_clock


def restore(config,deps=None,scalar_cpu=False):
    import numpy as np, cv2, onnxruntime as ort
    from skimage.transform import SimilarityTransform
    from insightface.app import FaceAnalysis
    from insightface.model_zoo.model_zoo import ModelRouter
    av=media.runtime(deps,scalar_cpu)
    model=Path(config['codeformer_model']);models=Path(config['buffalo_l_models'])
    if media.signature(model)['sha256']!=config['model_sha256']:raise ValueError('Detail model changed')
    corrected=Path(config['corrected_owner']);body=Path(config['body']);photo=Path(config['original_reference'])
    destination=Path(config['destination'])
    if destination.exists():raise FileExistsError('Detail result exists; verify/recover without rerendering')
    signatures={name:media.signature(path) for name,path in [('corrected',corrected),('body',body),('original_reference',photo)]}
    a,b=config['owner'];v=media.inspect(corrected,deps,scalar_cpu)['video']
    if (v['frame_count']!=b-a or identity_clock.measured_fps(v)!=24 or (v['width'],v['height'])!=(720,1280)):
        raise ValueError('This detail pass requires the measured24fps720x1280 restored owner')
    options=ort.SessionOptions();options.intra_op_num_threads=8;options.inter_op_num_threads=1
    detector=FaceAnalysis.__new__(FaceAnalysis)
    detector.models={name:ModelRouter(str(models/file)).get_model(providers=['CPUExecutionProvider'],sess_options=options)
                     for name,file in [('detection','det_10g.onnx'),('recognition','w600k_r50.onnx')]}
    detector.det_model=detector.models['detection'];detector.prepare(ctx_id=-1,det_size=(640,640))
    providers=['CPUExecutionProvider']
    if config.get('use_cuda'):
        if 'CUDAExecutionProvider' not in ort.get_available_providers():raise ValueError('Configured CUDA provider unavailable')
        providers=[('CUDAExecutionProvider',{'gpu_mem_limit':str(1024**3),'arena_extend_strategy':'kSameAsRequested',
            'cudnn_conv_algo_search':'HEURISTIC','cudnn_conv_use_max_workspace':'0'}),'CPUExecutionProvider']
    session=ort.InferenceSession(str(model),sess_options=options,providers=providers)
    if config.get('use_cuda') and 'CUDAExecutionProvider' not in session.get_providers():raise ValueError('CUDA initialization failed')
    ref=cv2.imdecode(np.fromfile(str(photo),np.uint8),cv2.IMREAD_COLOR);reference_faces=detector.get(ref)
    if len(reference_faces)!=1:raise ValueError('Exactly one inspected original identity face required')
    target=reference_faces[0].normed_embedding
    template=np.array([[192.98138,239.94708],[318.90277,240.1936],[256.63416,314.01935],
                       [201.26117,371.41043],[313.08905,371.15118]],np.float32)
    observations=[]
    def images():
        with av.open(str(corrected)) as cc,av.open(str(body)) as bc:
            cc.streams.video[0].codec_context.thread_count=1;bc.streams.video[0].codec_context.thread_count=1
            bodies=iter(bc.decode(video=0))
            for _ in range(a):next(bodies)
            for i,frame in enumerate(cc.decode(video=0)):
                original=next(bodies);source=a+i
                if (Fraction(frame.pts)*frame.time_base!=Fraction(i,24) or
                        Fraction(original.pts)*original.time_base!=Fraction(source,24)):
                    raise ValueError('Detail/body source clocks differ')
                im=np.asarray(frame.to_image())[:,:,::-1].copy();base=np.asarray(original.to_image())[:,:,::-1].copy()
                faces=detector.get(im,max_num=1);row={'source':source,'detected_faces':len(faces),'visual':'pending'}
                if faces:
                    face=faces[0];transform=SimilarityTransform();transform.estimate(face.kps,template)
                    matrix=transform.params[:2,:].astype(np.float32);inverse=cv2.invertAffineTransform(matrix)
                    aligned=cv2.warpAffine(im,matrix,(512,512),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101)
                    tensor=np.expand_dims(((aligned[:,:,::-1].astype(np.float32)/255-.5)/.5).transpose(2,0,1),0)
                    values={n.name:(tensor if len(n.shape)==4 else np.array([1.0],np.float64 if n.type=='tensor(double)' else np.float32)) for n in session.get_inputs()}
                    restored=session.run(None,values)[0][0]
                    restored=np.clip((np.clip(restored,-1,1).transpose(1,2,0)+1)*127.5,0,255).round().astype(np.uint8)[:,:,::-1]
                    small=np.zeros((512,512),np.float32);cv2.ellipse(small,(256,284),(122,166),0,0,360,1,-1);small=cv2.GaussianBlur(small,(31,31),0)
                    small=cv2.warpAffine(small,inverse,(720,1280),flags=cv2.INTER_LINEAR)
                    projected=cv2.warpAffine(restored,inverse,(720,1280),flags=cv2.INTER_LINEAR)
                    detail=im.astype(np.float32)*(1-small[:,:,None])+projected.astype(np.float32)*small[:,:,None]
                    outer=np.zeros((512,512),np.float32);cv2.ellipse(outer,(256,275),(170,217),0,0,360,1,-1);outer=cv2.GaussianBlur(outer,(31,31),0)
                    outer=cv2.warpAffine(outer,inverse,(720,1280),flags=cv2.INTER_LINEAR)
                    output=np.rint(base.astype(np.float32)*(1-outer[:,:,None])+detail*outer[:,:,None]).clip(0,255).astype(np.uint8)
                    if not np.array_equal(output[outer==0],base[outer==0]):raise ValueError('Unrelated body RGB changed')
                    observed=detector.get(output,max_num=1)
                    row.update(unchanged_body_outside_alpha=True,
                        relative_original_reference_cosine=float(observed[0].normed_embedding@target) if observed else None)
                else:
                    output=base;row.update(unchanged_body_outside_alpha=True,
                        detection_status='missing-does-not-prove-face-invisible; current native/manual recovery required')
                observations.append(row)
                from PIL import Image
                yield source,Image.fromarray(output[:,:,::-1])
    destination.parent.mkdir(parents=True,exist_ok=True)
    rows=identity_clock.raw_writer(av,destination,images(),720,1280)
    if len(rows)!=b-a:raise ValueError('Detail owner incomplete')
    for name,path in [('corrected',corrected),('body',body),('original_reference',photo)]:
        if media.signature(path)!=signatures[name]:raise ValueError('Detail input changed')
    proof={'inputs':signatures,'model_sha256':config['model_sha256'],'fidelity_weight':1.0,
           'raw_signature':media.signature(destination),'owner':[a,b],'rows':rows,'observations':observations,
           'complete_owner_RGB_PTS':'passed','visual':'pending','relative_cosine_is_not_similarity_percent_or_approval':True}
    factory.write(destination.with_suffix('.proof.json'),proof);return proof


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--config',required=True)
    ap.add_argument('--deps');ap.add_argument('--scalar-cpu',action='store_true');args=ap.parse_args()
    result=restore(factory.read(args.config),args.deps,args.scalar_cpu)
    print(__import__('json').dumps({'owner':result['owner'],'technical':'passed','visual':'pending'}))

if __name__=='__main__':main()

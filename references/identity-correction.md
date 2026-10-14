# 原图身份校正与输出时钟

H3保留动作、发型和服装后仍像源演员时，先独立对照用户原始照片、源演员及当前结果。不要把“去掉刘海”“穿对衣服”或派生导引通过等同身份通过。当前任务被用户否决的身份结论保留失败；用原始照片做3个不同的身份试窗，再扩批。

## 两阶段方法及适用范围

第一阶段按现有工厂完成源动作、目标发型/可见外观、用户认可服装及场景。其结果只作为运动/身体载体。第二阶段通过`face_swap.py`把原始人物照片送入已实读schema的换脸应用；只发送实际公开的image/video字段，不把六段式H3文本冒充这个应用的输入。一个身份图只对应一个实际目标；本路线没有多人自动分配的已验证能力。

本次已完成并获用户全片原速认可的应用为`1931791712199618561`：image节点5、video节点4。实测输入24fps、输出8fps、1280方形。其他三个已读节点入口仅供独立试窗，截短、低分辨率或裁切曾被拒绝；节点存在不证明效果。

`identity_clock.py`只支持这一已测的24fps、720×1280输入投影，不套用到其他帧率/尺寸。每个原姿态重复3次，用1280方形的黑色水平补边完整保存人物；身体矩形为[280,0,1000,1280]。按已核原动作阶段重新划窗：完整输入包含context和3倍扩展，必须≤10秒。80个源姿态恰为10秒，81个必须继续沿实际动作拆分，不能统一改成固定5秒。

```text
python scripts/identity_clock.py prepare --body <当前身体载体绝对路径> --destination <新投影目录> --context <首帧> <末帧不含> --owner <首帧> <末帧不含> --scalar-cpu
python scripts/identity_clock.py restore --candidate <实际下载结果> --projection <投影目录/projection-proof.json> --destination <新owner.nut> --scalar-cpu
```

全部实际输出帧数、PTS、画幅核对后才恢复原24fps并精确逆转补边。`restore`保留完整源姿态且检查raw RGB/PTS，但不能证明应用内部没跳动作：还需逐帧对照手臂、身体、字幕和道具的原运动。最近像素匹配的邻帧歧义如实保留，不能报告成全部精确对应。输入为静音兼容载体；最终音频使用整条源音轨。若某应用必须带音，派生音频按相同扩展时钟制作、核完整时长，禁止把派生音频合入最终片。

## 建立独立提交run

沿现有source/analysis/plan合同建独立修正run，queue只列本次已审批窗口，完整源计划及独占owner不变。job使用`kind: reviewed-framewise-face-swap`、当前app_id、instance_type、schema_path、schema_sha256。复制当前原图至run的refs，绑定`references.A.path`；原图不能被旧生成脸替代。plan使用实际`source_time_scale: 3`和`media.project_frames`的完整generation映射。

queue包含当前video_path、reference_paths、source_contract.window、generation_frames、duration、production_authorized、input_digest及semantic_review/source_projection_review。`derived_inputs/current-input-proof.json`保存实际input_signature与完整report；填写generation输入24fps和实际duration。该报告可取自projection-proof.json，签名增加run内相对path。`prompt_path`仅为私有方法说明及签名，不发送给换脸应用。`factory.compute_input_digest`绑定source、analysis、完整plan、job、schema及各个当前文件。

三种候选各用独立run和input digest；审批实际照片、投影、动作范围及公开节点后，`face_swap.py tick-pilot --project <run>`先恢复账户已知任务，再提交当前孤立窗口。中央账本和账号与H3共用，最多3个在途；脚本一次tick不维持后台循环，Codex用唯一dispatcher定时恢复三个run，队列忙至少等60秒，受理或未知提交不重复POST。每次新原片/身份都保留试窗门槛；不把一次影片的通过设为全局自动通过。

## 清晰度、遮挡及缺失检测

`face_detail.py --config <私有JSON>`为可选细节修复。配置字段：corrected_owner、body、owner、original_reference、destination、codeformer_model、model_sha256、buffalo_l_models、use_cuda。模型路径来自本机；权重不随技能捆绑。依赖NumPy、OpenCV、onnxruntime、InsightFace及scikit-image。用当前检查过的CodeFormer ONNX、512对齐和fidelity1；CUDA设1GiB arena，没有GPU就显式选CPU。比较正脸与侧脸实际结果后才启用，模型版本或数值精度改变需重新试样。原图embedding只记录相对cosine，不是相似百分比或验收。

脚本在脸部alpha内取新身份，在alpha外逐像素冻结已核身体，并重新完整检查raw RGB/PTS。固定椭圆只代表本次有效起点；遮挡、头型和极近裁切要现场检查。零检测不说明脸不存在：该帧只保留身体并写pending，若仍可见眼/鼻/嘴，必须用相邻姿态校准原图换脸或其他有证据方案修复，不能因程序跑完就交付。

手掌经过脸时检查所有原尺寸邻帧。模型把眼睛画在模糊手指上，要保护真实前景手、只改实际可见脸。语义分割和颜色mask均只是候选：仅按肤色连通会把旧演员眉眼恢复回来，必须拒绝。仅在用户认可的运动/身体区域恢复旧body，并在实际可见脸间隙使用新原图身份；记录逐帧mask、相邻姿态、范围外RGB证明和新验收。不得把本次手形坐标当通用模板。

## 外部后处理成片的独立验收

原H3中间片身份失败不能被最终用户验收追改为通过。外部修正成片用私有`reviewed-postprocessed-film`交付记录绑定source/analysis、当前final技术报告、全部owners、当前raw及父文件SHA、原始身份图、实际1x证据和原尺寸风险/接点图。用户明确验收当前完整文件时保存实际问题、回复和该文件SHA；不伪造Codex播放、不再重复要求前台打开。

`build-skill --status ready --delivery-review <run内相对JSON>`使用`delivery.py`单独验证这个交付合同；普通候选/assemble门槛照旧。owners必须完整且不重叠；技术报告仍走原完整源时轴、画幅、色彩和原音轨检查。raw逐帧验证和每个父版本留在制作run。专属技能只包含中性源事实及自足脚本，不包含原图、云任务、此次成片或审核记录。

编码成功和短试样不证明整片可读；完整独立严格解码失败时，保留坏文件和已核raw，定位原因再重新编码。此次确认正常SIMD解码和scalar均失败的一个输入，由完整RGB/PTS核对过的原raw恢复为baseline/CAVLC后才提交；不忽略坏帧、不为本机编码错误重跑已受理云任务。

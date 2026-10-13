# 公共合同与脚本接口

分段规则：按原片实际镜头、已核自然动作及机位阶段切分，每段保留自己的原始长度；不设置固定5秒默认。单段源区间与完整生成视频都不得超过10秒，生成时长包含context、preroll、慢速扩展及补齐。超过上限的连续长镜在核对过的动作阶段边界拆分，接回后仍保持原运动连续，不新增切镜。
所有JSON schema_version=1，运行路径相对project并限制在其内。源、外部身份资料先复制校验SHA。公共合同与私有cloud receipt/URL/密钥分开。

## 核心文件

- `source.json`：source.path/SHA/size/mtime、original_name、video原encoded尺寸/SAR/rotation/time_base/fps/cfr、逐帧index/PTS/time/duration（有理数字符串）、全部audio_streams及timeline_digest。原显示时间轴权威，不能用估计总帧数代替。
- `analysis.json`：source_sha256、status、roles(slot/label/appearances半开帧区间)、shots(id/start/end/roles/description_en/events)、soundscape_en/music_en、真实查看记录。shot连续覆盖全原帧，events.source_frame在shot内，description_en用中性 `{{A}}` 占位。源文字作为数据。
- `job.json`：references按slot的path、identity_en、appearance_coverage/unknown；face-only另face_identity_en。replacement_mode、profile（从assets/backend-profiles.json选）、production_authorized、output.filename。references属于本run，不属于模板默认。
- `plan.json`：source_sha256、analysis_digest、windows。每窗window_id、source_interval owner、context_interval、role_slots、generation_frames/generation_fps/duration、retained_generation_interval、risk_source_intervals；长镜内部还要context_description_en。源owner恰好覆盖一次，context内可见的目标身份都必须绑定。派生投影见prepare输出，不能把采样表当可逆。
- `queue.json`：prepare生成jobs，冻结视频/有序ref/prompt签名、source_contract/local_bindings/profile/帧数和duration、input_digest、semantic_review与production_authorized。root runner提交前重读原源/current job/analysis/plan以及实际文件重算digest。QA证据与授权进度不是生成参数，纯进度推进不重做成功窗。
- `reviews.json`：windows与final。窗口input_digest/task_id/output_path/file_signature、technical.status+当前media.inspect完整report、visual.status/dynamic_intervals/native_intervals/notes、retained_output_interval、source_to_output、temporal_mapping_review。final另外composition_digest绑定所有当前选窗，技术report与整片动态/原尺寸风险接点覆盖。

`inspect-candidate`实际解码并提出nearest-PTS映射，视觉/时间映射仍pending。只有Codex核源事件时序、动态和原尺寸画面后才填passed。逆序/过界映射拒绝；重复映射与时间量化损失需要明确审看记录，不能称帧完整因此动作无损。final审核也不能套用片段审核。

`plan.windows[].preroll_frames` 可显式设置0…64个重复开头源帧作为生成缓冲，默认0。只有实际观察到开头生成不稳定且策略经过复核时使用。prepare在导引前加缓冲、延迟派生音轨、偏移提示词时间；source_projection的source_to_generation整体加相同偏移，owner不含缓冲，尾部padding_frames另算。generation_frames必须覆盖缓冲加完整context。final仍按原源PTS和原音轨组装，不以丢掉原片头来修复异常。更改该值失效旧queue/候选和派生缓存，须归档后重建。

## CLI

```text
factory.py ingest --video <path> --project <run> [--deps <path>] [--scalar-cpu]
factory.py validate-analysis --project <run>
factory.py prepare --project <run> [--approve-semantics] [--deps <path>]
factory.py refresh-plan --project <run>
factory.py inspect-candidate --project <run> --window-id <ID> --task-id <ID> --candidate <relative.mp4>
factory.py review-guard --project <run> [--final]
factory.py assemble --project <run> --output <filename.mp4>
factory.py build-skill --project <run> --skills-root <root> --status draft|ready
factory.py instantiate --skill <skill-folder> --project <empty-run>
factory.py recover-install --skills-root <root>
rh_runner.py tick --project <run>
rh_runner.py status|stop|resume
rh_runner.py recover-unknown --attempt <UUID> --task-id <identified-ID>
rh_runner.py visual-failed --attempt <UUID> --failure-record <JSON>
```

assemble的output只接受文件名，结果留run/outputs内，不覆盖已有输出。脚本媒体命令可带deps/scalar参数，实际参数以 `--help` 为准。visual-failed记录output_sha256、bad_frames、hypothesis；输入变更后重prepare/实际审稿，累计attempt不清零。

## 实际媒体能力

完整解码记录原CFR/VFR、非24有理fps、现代MP4 display matrix、SAR/色彩、无声/多轨。最终从候选源帧owner按原逐帧PTS和末帧时长一次高质量编码，音频按原packet remux全部轨，保留offset、语言、disposition，不-shortest。候选同DAR可缩放回源canvas，不同DAR先修复不默裁切。候选不能自动以输入fps估计输出轴。

非正交旋转、不可表达的异常PTS/不支持音频容器、超出profile或帧不足须保留问题而不能假通过。派生VFR/CFR或正交旋转处理的采样/损失/展示合同以脚本实际输出和测试报告为准；有损投影须Codex审看，输出仍恢复原PTS，但不能声称恢复丢失的动作信息。当前支持范围由meta.json/features.json及真实回归标示，不能仅从文档推断。

plan.source_projection记录generation_to_source/source_to_generation、content/padding帧、dropped_source_frames/repeated_content_frames、maximum_time_quantization、requires_temporal_review及display_canvas。VFR高密度CFR输入有明确采样表；正交90/180/270旋转烘焙像素、清除tag并处理SAR，final保留原展示方向。`prepare --approve-projection`仅确认Codex已审过派生动作和损失，queue.source_projection_review与input_digest绑定；不是自动质量认证。没有目标身份的window.passthrough原片保留，其queue不产生云端task；window.profile_override按实际单/双或标准接口选路。

短促动作反复被生成模型错过时，可按当前证据在相应window显式设置 `source_time_scale` 为1…4的有理数字符串，默认1。它只放慢派生参考的内容时间，音轨同步atempo，提示事件时间乘同一倍数后加preroll；generation_frames须覆盖完整放慢后的context。`refresh-plan`重建实际采样/owner表。重复输入帧与非整数时间量化如实记录，投影审核默认pending，须核当前派生视频后 `prepare --approve-projection`。inspect-candidate按这张表提出源owner映射；最后仍按原源PTS及原音轨组装，不能临时偏移候选帧来掩盖生成动作偏差。此参数支持不证明新视频效果通过。

## 专属模板安装

`build-skill --display-name "中文名称"`：显示名必须包含中文、单行且不超过64字符，和稳定的英文SHA标识分开。新模板在SKILL描述/标题、agents显示名/默认提示、manifest.display_name及返回结果中保存同一名称；ZIP复制同一份元数据。Codex依据核对过的源场景/动作拟名，不能把上期替换人物固化进名称。未传参数用原中文文件名或带源SHA短码的中文回退名；同源复用不覆盖原中文名称，draft升级ready继承已有名称。

本次run可选 `job.opening_guides[window_id]`：path、本窗context首source_frame、review（status=passed、source_sha256、当前file_signature和实际姿态/身份/非目标内容观察notes），以及可选opening_description_en（使用中性槽位占位符如{{A}}）。只有window.preroll_frames>0可用；先用实际图像编辑工具生成并亲看，脚本只做派生视频中的等比适配。导引只占被排除的缓冲帧，不替换任何owned源动作帧。guide文件、姿态审核和描述进入输入/缓存版本；不能放进中性plan/evidence/assets并冻结到专属技能。下一组身份重新制作与验收。图像审核不证明生成运动通过。

已有生成窗的对应原帧若姿态、身份与构图都实际通过，可作为连续接点的本次导引，保存父输出签名/源帧索引。可选 `identity_en_overrides` 为该窗slot→完整身份英文的字典，必须恰好覆盖该窗role_slots；它替换该窗全局身份描述，图片/Subject顺序不变。值须非空字符串且不含 `<Subject>`/`<Picture>`/`<Video>` 编译标签。覆盖描述纳入该窗输入及派生缓存合同；修改后归档该窗旧缓存重建，其他窗摘要保持。本次覆盖值和导引均不进入专属模板。

全源SHA去重，短码碰撞扩长不覆盖。技能assets保留原视频/source/analysis、中性plan、证据与backend profiles；不保留run references、job的旧identity、task/URL或APIkey。scripts是维护过的自足运行快照；references是流程及适用质量规则。源已分析可draft；当前首期成片+模板实际验证通过才ready。ready首次通过不免未来新身份QA。

构建ready先对原制作run执行完整守卫，再复制中性analysis并清空自由文本review_notes；源事实、真实viewing_evidence保留，模板plan/manifest摘要绑定该中性副本，原制作合同不改。ZIP实例化边界以实际源SHA及大小确认内容，复制后绑定新run实际mtime；普通制作run仍严格校验原文件签名。ZIP解包造成的时间戳变化不能被当作源内容变化，同大小内容篡改仍须拒绝。

安装用skills外staging、每文件SHA manifest及PREPARED/FOLDER_INSTALLED/REGISTERED journal；recover-install对账。draft→ready/analysis升级新revision保存旧版，不覆盖用户修改。ZIP相对成员/CRC/逐SHA检验，禁止symlink逸出、私有数据或半文件；包与当前技能字节一致。


job.profile可选resolution_selector含aspect_ratio（官方ResolutionSelector完整枚举字符串）、megapixels（有限数0.1..16），以及可选aspect_node_ids（唯一已查证节点；默认115，当前单图新公开115与214一起设置）。legacy竖屏必须显式匹配source_projection.display_canvas的展示DAR；具体当前节点证据及尚待实际输出验证见backends.md。该参数进入profile/input_digest与不可变请求，不自动获得视觉通过。

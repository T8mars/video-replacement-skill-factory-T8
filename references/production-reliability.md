# 输出时钟、运行恢复和可复用修复

此页用于实际制作遇到保持帧、动作延迟、漏脸、临时上传失效或无损修整时。规则面向新原片及新身份，不继承某次人物、固定段号、21帧索引、裁图坐标或曝光系数。安装及维护不调用付费生成。

## 先查询、补位，再下载

`rh_runner.py tick`先查询同账户所有已知任务，用明确成功结果释放生成位，再提交已审批的待做窗，最后传输已完成文件。同账户最多3个在途生成；主窗口优先，收尾1～2个问题窗仍准备三个有实质差异的已审方法，共用私有账本。已通过且签名未变的窗口冻结；全部通过不再新增，已付费任务仍恢复。

每个新的付费请求重新上传素材；同一个请求内相同字节可去重，不跨付费请求复用临时句柄。观察到临时视频路径在原6小时缓存期限内已经失效；重启恢复已知taskId不上传、不新POST。805先在私有回执查原因：waveform可能是缺可解码音轨，node321路径缺失可能是临时文件失效；不能按805一律重投。

队列繁忙至少冷却60秒，继续原ID。已知任务的临时查询/下载错误超过3次仍按退避持续恢复，等待上限300秒；这些计数不等于生成执行失败。连续执行失败3次仍需诊断受影响输入；质量重试不设固定次数。SUBMITTING或SUBMISSION_UNKNOWN是账户范围的新提交屏障，首个未知POST后立即停止其他新增，先核服务商历史。进程退出不释放云任务槽，禁止删台账重新跑。旧工具/网页未接入台账的任务须先对账，锁不能控制外部程序。

## 实际输出完整看到末帧

输出可能自行增加首尾保持、改变动作速度，实际帧数不等于输入文件名或generation_frames。`inspect-candidate`先完整实解并提出默认nearest-PTS映射，技术通过不证明所选帧包含真实动作。新检查记录的output_clock_review默认为pending，实际观察全部输出后才写observed、完整帧区间与具体notes；它与保留区间的原速动态验收分别记录。

```text
python scripts/candidate_review.py evidence --project <run> --window-id <ID> --candidate <run内候选相对路径> --out candidate-evidence/<新版本> [--native-all] [--rois <run内ROI.json>] [--mapping <run内拟议映射.json>] [--deps <依赖目录>] [--scalar-cpu]
```

输出包含实际首到末的连续概览、逐帧PTS、连续相同RGB帧统计；有mapping时保存每个owner的原片/候选原尺寸图及对照。概览会缩小，仅作定位；原尺寸PNG也要确认实际查看工具没有再次缩图。所有状态只生成prepared/pending，不从重复数、裁框或解码成功自动批准人脸和动作。

ROI例子：`[{"name":"visible_A","box":[100,80,700,900],"role_slots":["A"]}]`。坐标属于当前实际视频经正交旋转后的原像素网格；每帧保存区域、角色标签及文件签名。SAR不为1时另核正确显示比例，原像素裁图不重采样。固定框也可能漏人或跟错角色，必须实际看覆盖，缺失时补其他固定框或完整native帧；不能把“脸框已导出”当作脸已检查。

证据保存到candidate-evidence等本次运行目录，不放源evidence或assets；这些源目录会进入专属模板。工具拒绝在可复用源树写候选。构建技能也会拒绝其中已知的候选/像素证明/交付合同类型；手工中性源裁图和约束仍可保留。不要把本次人物、候选图、导引和审核记录伪装成源事实。

## 显式实测姿态映射

默认nominal-pts保持严格时间检查。仅当完整实际输出确实增加保持/改变表演时钟，且真实原动作全部存在，才使用measured-source-poses；不放宽全局PTS容差，不移动原owner，不用静态身份锚点或尾部保持帧补动作。

先逐项比较原片开始、中段、结尾及所有保留姿态，测当前输出切点。拟议JSON包含source_to_output（长度等于本窗全部owner，按原顺序列实际输出索引）；用上面的evidence命令加--mapping重新生成全部配对证据。实际看完后写单独映射审查：

```json
{
  "schema_version": 1,
  "mode": "measured-source-poses",
  "evidence": {"path":"candidate-evidence/v2/evidence.json","sha256":"<实际摘要>","size_bytes":0,"mtime_ns":0},
  "source_to_output": [0,16,21],
  "output_clock_review": {"status":"observed","intervals":[[0,90]],"notes":"实际完整输出观察，包括首尾保持和最后动作"},
  "source_pose_review": {
    "status":"passed","source_intervals":[[0,3]],"notes":"每个源姿态与当前候选原尺寸逐一核对的实际结论",
    "anchors":[
      {"source_frame":0,"output_frame":0,"notes":"开始姿态实际观察"},
      {"source_frame":1,"output_frame":16,"notes":"中段姿态实际观察"},
      {"source_frame":2,"output_frame":21,"notes":"结束姿态实际观察"}
    ]
  },
  "source_cut_before": [],
  "output_cut_before": []
}
```

上面数字仅说明结构，必须替换为当前窗口全部owner、实际完整输出长度及真实签名。anchors分别对应start、(start+end-1)//2、end-1（相同则合一）。source_cut_before必须与已核analysis内本owner的镜头切点一致；output_cut_before列对应真实输出切点，禁止跨镜头挑帧。

```text
python scripts/candidate_review.py map --project <run> --window-id <ID> --mapping <run内已审映射.json>
```

工具绑定当前源、input_digest、实际候选和所有证据图片，计算mapping_digest，归档旧review并把visual重置pending。再次实际审保留区间原速和native风险后，visual.measured_mapping_digest须等于当前measured_mapping.mapping_digest；不能让新映射继承旧视觉结论。重复输出索引还须单独完成temporal_mapping_review并披露损失。最终仍恢复原源逐帧PTS与原音轨，原速/接点/ready守卫不变。

哈希、单调索引和完整证据只约束审核对象，不能证明人物、动作语义已正确。若真实动作不存在，继续换输入方法，不能靠映射捏造。

## 规范PNG与独立RGB/PTS证明

修整前保存规范PNG和编码前的计划；每帧声明PNG、原始RGB摘要、预期时间和时长：

```json
{"schema_version":1,"dimensions":[640,360],"time_tolerance":"0","frames":[
  {"png":"000.png","rgb_sha256":"<规范RGB摘要>","time":"0","duration":"1/24"}
]}
```

```text
python scripts/candidate_review.py verify-rgb --plan <规范计划.json> --video <无损修整视频> --out <新的证明.json> [--ffmpeg <可执行路径>] [--ffprobe <可执行路径>] [--deps <依赖目录>]
```

独立FFmpeg普通CPU严格解码全部RGB，ffprobe核原尺寸、每帧PTS和duration，计划/PNG/视频在验证期间均须不变。容器量化确需容差时在计划明确声明，必须小于最短帧时长的一半；不能用容差掩盖丢动作。单像素、少帧或错时钟即失败；失败证明保留，技术成功的visual仍pending。codec名称、编码进程退出或局部预览都不是完整无损证明。

某些FFmpeg版本的RGB FFV1编码曾出现极少像素差异，特定scalar编码与独立普通CPU解码曾解决本次路径；此经验不是所有H264统一加scalar的理由。记录具体工具版本和整段复测。修蒙版仍看全部保留帧的头发、手袖、鞋、线缆、音箱和背景，防黑洞、白边、散斑或单帧颜色闪动；像素相等不证明蒙版选得对。

## 身份与重复失败

边缘手臂、鼻尖、下巴、被道具挡住的小脸也属于目标角色，源分析据真实可见内容决定人数；不硬套旧段号或固定两人。眼部照片拼贴中的原眼皮/虹膜是原身份，需整体替换；非解剖装饰另核。表演眼镜、麦克风等依原动作状态，参考图的展示扇子等不自动变成源片道具。

暗场先在可看清的曝光下独立确认原图身份，再处理暗场氛围，不能把错误脸藏黑。单帧锚点正确、服装正确或灰剪影输入都不证明真实动作已换人；多出第三人、动起来回原人、末段没完成动作仍失败。目标姿态引导要逐格核人数、四边、背景尺度、道具与首尾手势，网格分界会引入几何跳幅，不能当万能模板。

连续同类质量失败先更新具体坏帧与假设，下一组三法比较实质不同的原图/精确裁图、合法分辨率、整镜头或合规动作输入策略，不反复只改禁词。曝光系数、遮罩阈值、姿态索引只用于该次实测。仍保留独立原图身份、动态、native和整片接点审核，不自动宣布未来质量保证。

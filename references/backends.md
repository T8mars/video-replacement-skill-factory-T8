# 接口、私有配置与恢复

profile读取 [backend-profiles.json](../assets/backend-profiles.json) 复制到job。已观察能力与官方文档分开，未知不猜。

legacy `POST /openapi/v2/run/ai-app/<app_id>`：nodeInfoList中137.image第一身份filename、139.image第二身份filename（双图）、138.value提示词、321.video派生视频filename、326.value帧数；instanceType=plus/usePersonalQueue=false。本机实测帧数22/39/56/73/90/107及24fps，不推定任意整数/三人物。旧加载节点需要音轨，输入派生静音仅接口兼容，最终不混入该轨。

standard `POST /openapi/v2/minimax/hailuo-h3/multimodal-to-video`：prompt、imageUrls、videoUrls、resolution、duration、ratio、aigc_watermark。官方schema最多9图/3视频/3音频、prompt≤20480字符、2K或768P、duration字符串5…15、ratio adaptive或枚举。当前runner只发送一个动作视频与身份图，不发送audioUrls，原声后期保留。输出fps/frame_count实际测量，图数量不是已验证角色保真数量。[官方H3 schema](https://www.runninghub.ai/runninghub-api-doc-en/api-495380675.md)

标准模型接口只支持企业级共享 Key，已有 AI 应用调用权限不代表有标准模型权限。官方1014是明确的模型接口权限拒绝，不是可能已执行的未知POST；无taskId时记执行失败，按账户/当前Key私有指纹持久阻止该Key后续标准模型提交，仍允许原有AI应用及已知任务恢复。不得换窗口名反复探测同一权限、自动购买权益或改凭据。用户已提供的新企业级共享Key可使用其私有文件；不进技能或公开日志。[官方调用权益](https://www.runninghub.ai/runninghub-api-doc-en/)、[1014错误说明](https://www.runninghub.cn/runninghub-api-doc-en/doc-8287467)

上传multipart file到 `/openapi/v2/media/upload/binary`。兼容官方code=200/data.filename/data.download_url与本机实际code=0/data.fileName；legacy用filename、standard用URL。查询 `/openapi/v2/query` body taskId。新版本先只读核对文档/应用示例；用户授权的代表性生产窗可按workflow.md用3个独立已审批方案并行试窗，实际审看通过后扩批。[上传schema](https://www.runninghub.ai/runninghub-api-doc-en/api-425761098.md)、[应用示例](https://www.runninghub.ai/runninghub-api-doc-en/api-425761097.md)

六段式引用依据 [MiniMax官方参考视频提示词指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)。Picture只在Subject身份定义引用，Video提供原时空结构；源有声音不自动设Audio条件，后期复制原音轨与生成声效分开。

默认密钥 `CODEX_HOME/private/r2va-character-replacement/api_key.txt`，本机已有用户指定值。不把key本体写到新技能、代码、日志、prompt或ZIP，缺文件才询问；本次其他key用私有--key-file参数。本机CODEX_HOME=`G:/CodexHome/.codex`。同实际账户同alias，默认default，私有账本 `CODEX_HOME/private/video-replacement-skill-factory/tasks.sqlite`，不能每个项目另建账本。

ledger私有保存精确request/receipt/上传/result URL，公开status不含这些。POST前原子创建attempt/digest占槽，落request后发送，taskId立即落receipt。超时/无确定拒绝/HTTP5xx可能已执行，为SUBMISSION_UNKNOWN；崩溃留下SUBMITTING也占位。未证实provider idempotency，禁止不知taskId自动重发，官方任务历史对账后recover-unknown。

只已知云端终态释放生成槽，本地worker消失不能释放。SUCCESS空results保持task查询，三次缺失为RESULTS_MISSING；DOWNLOAD_FAILED只查询/下载，绝不回生成队列。上传缓存SHA+接口语义+有限TTL，过期只重传。下载.part限时/长度/实际解码后晋升不可变候选，失败保task。

已知结果HTTPS握手EOF的恢复已进入Transport.download：仅对本机实际出现问题的COS结果主机，读取Google与Cloudflare两个独立HTTPS DNS的实时A记录，过滤非公共IPv4，要求公共地址交集，再用curl临时解析。原HTTPS URL、SNI、主机名和证书校验保持；curl也限制重定向为HTTPS。没有公共交集或其他主机时保持DOWNLOAD_FAILED，等待原任务下载恢复。每次下载使用独立.part，完整ffmpeg解码后以不覆盖已有文件的原子操作晋升；多个worker也不能覆盖已晋升候选。需要本机curl及ffmpeg。不固定旧IP、不改系统DNS、不关闭证书校验、不改HTTP、不把下载失败变成新的付费生成。

SQLite账户scope合计最多3个SUBMITTING/UNKNOWN/QUEUED/RUNNING；有已授权且已审批的待生成工作时，Codex调度目标也是3，收尾用不同修复方案补齐工作槽，具体见workflow.md。旧工具未导入的外部任务不声称已纳管。421全账户至少60秒冷却、仍恢复已知任务；401/402/403/1913停新增；API/执行连续3失败停受影响路径；视觉无限、有失败帧与改动假设。resume用于已获继续授权或问题修正，历史不清除。

用户允许满队列后等待恢复。尚未受理、没有taskId的明确415/421/429/1003/1010/1011提交拒绝才作为DEFERRED，账户至少60秒持久冷却后恢复提交，不计入三次执行失败，也不释放仍在云端执行的其他任务。上传或已知task查询遇到同类繁忙也等待，不增加技术失败计数。已知task查询即使status=FAILED，明确繁忙码仍保持原state/taskId/并发占位，保存回执、至少60秒后继续查询原ID；不得转DEFERRED后重POST。defer_submission拒绝处理已有taskId的记录。804/813表示任务运行中或已受理；有taskId沿用查询，无taskId保存为未知提交并对账，绝不作为“队列满”自动重POST。HTTP5xx、超时或无明确拒绝仍保留未知提交保护。[官方队列错误码](https://www.runninghub.cn/runninghub-api-doc-en/doc-8287467)


## legacy画幅配置

提交前只读检查当前应用公开的节点。2026-10-12单图应用2107844752011010050新公开两个ComfyUI `ResolutionSelector`的 `aspect_ratio`：115和214；115还公开 `megapixels`。两阶段都显式设置相同的源展示比例。例如720×1280、SAR1：

```json
"resolution_selector": {"aspect_ratio": "9:16 (Portrait Widescreen)", "megapixels": 2, "aspect_node_ids": ["115", "214"]}
```

`aspect_node_ids`省略时保持旧node115请求；2026-10-13又只读核对了双图应用2107845954833907713：同样公开115和214比例，以及第二图片节点139。两种已核应用均可显式设置两个比例节点；其他应用仍拒绝214，不能凭节点通名推定。禁止任意节点、重复节点、非法比例和非有限/越界MP。源竖屏未配置或比例与源展示DAR不同会在付费提交前拦截。节点ID、画幅及MP共同绑定不可变请求和pilot路由；增加第二节点不继承旧单节点pilot通过。

通过[官方只读应用示例](https://www.runninghub.ai/runninghub-api-doc-en/doc-8287469)读取实际公开字段、枚举与类型，先删Key再保存。节点定义可核对[ComfyUI官方源码](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_resolution.py)，但源码中存在字段不代表应用已经公开或实际采用它。

实际曾遇到竖屏源生成1920×1088横屏；未公开时发送node115比例也被忽略，两个候选均拒绝。新公开的115+214实际复测得到1088×1920竖屏、24fps、107帧，确认方向生效。分辨率网格使DAR偏离精确9:16约0.74%，还须单独核对小幅无裁切画幅修正。人物身份和额外道具未通过，所以不以比例成功宣称整段视觉通过。API受理不是画幅验收：确认真实输出展示比例、全画面构图、身份及动作后才扩批。横屏扩景不能靠裁切或大补边伪装成源机位；实际比例未生效时修应用，不重复原样提交。


双图入口的两张图也可以描述同一目标身份的不同表情：在提示定义中明确两个Picture属于同一个Subject，保持源角色数量。原肖像给身份，已核表情导引只给同人表情资料，原视频仍给运动与机位。48项runner离线检查验证了两比例节点、137/139图片顺序和跨app不得继承pilot。当前双图人物效果仍待实际单窗验收，schema支持与离线通过不代表云端保真。

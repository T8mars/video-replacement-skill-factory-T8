# 贞贞 AI 渠道

每次收到新人物图时，请用户选择 RunningHub（RH）或贞贞 AI。已明确选用型号 `minimax-h3-comfyui-charswap` 即选定贞贞 AI。续跑时读取该 run 的 `job.json` 和同一私有账本；切换渠道另建 run。

## 准备和运行

先 ingest 原片或从已安装的中文专属技能 instantiate，分析镜头并按源槽位填写 `job.json.references`。在新 run 中运行以下命令。第一次 prepare 产生输入和提示词。检查原图、原片、提示词和采样后再记录审批。需要生成投影损失审批的镜头，实际检查后给 prepare 加 `--approve-projection`。

```text
python scripts/factory.py select-channel --project <run> --channel zhenzhen
python scripts/factory.py prepare --project <run>
python scripts/factory.py prepare --project <run> --approve-semantics
python scripts/zhenzhen_runner.py tick --project <run> --key-file <私有Key文件>
python scripts/zhenzhen_runner.py status
```

同账号用同一个账本和账号别名。默认 Key 路径为 `$CODEX_HOME/secrets/seedance_charswap.key`，默认账本为 `$CODEX_HOME/private/video-replacement-skill-factory/zhenzhen.sqlite`。使用其他路径时给每次调用传入同一个 `--ledger`、`--account`。持续运行 tick 查询已受理任务、补充空位、下载和解码。最多三路在途，少量坏片使用独立修复 run 的三种方案共享额度。试片实际通过后用 `pilot-promote --project <run> --window-id <ID>` 放行同范围后续镜头。其他来源、身份组合和风险类型各自试片。

## 素材与时长

1. `POST /v1/files/upload` 的 multipart 字段 `file` 每次接收一个文件。原图分别上传，运动 MP4 单独上传。重复使用同一文件时，同一账号和凭据可通过 SHA256 在签名地址过期前复用，剩余有效期不足 30 分钟会重新上传。
2. 一个生成任务的 `images` 是本镜头的有序 URL 数组。若源槽位 A、B 同框，依次对应 `<Picture 1>`、`<Picture 2>`；B 单独出现时，它是此请求的 `<Picture 1>`。一张多视图角色设定图作为一个文件上传。一窗可用 1–9 张图，一槽位对应一张图。图像支持 PNG、JPEG、WebP，每张不超过 10 MiB，合计不超过 50 MiB。
3. 运动源放在 `metadata.video_url`，恰好一个 MP4，最多 50 MiB。每次付费提交前以相同 payload 请求 `/v1/video/generations/estimate`，生成使用 `POST /v1/video/generations`，查询使用同路径加任务 ID。请求使用 `metadata.frames`，默认 `1088p`、`ratio: auto`。这些是生成设置；实际输出的展示比例和帧时间仍需解码核对。
4. 接口帧网格为 22 + 17n。上游曾拒绝不足 2 秒的输入，工厂要求每个完整生成窗不超过 10 秒，因此本适配器以 24fps 取 56、73、90……226 帧。原镜头 34 帧时，派生输入补尾到 56 帧，原镜头仍拥有相应的 34 个源帧。若所需长度超过 226 帧，先在已核动作阶段拆窗。专属包已有的窗口所有权、部分镜头说明及原片动作时间均可保留。
5. 提交超时且未取得 ID 时，保留原意图、核对平台记录后再 recover-unknown。有 ID 就恢复查询；成功但 URL 缺失、下载失败或限流时继续等原 ID。Bearer 只用于 API 主机，公共下载使用独立的媒体请求。详见[官方接口契约](https://api.seedance.nz/docs/llms.txt)。

## 人数、服装和道具

原片分析阶段按人实际出场区间填 `roles[].appearances`。交接、多人遮挡和道具动作在对应 `analysis.json` 的镜头记录中加入 `role_checkpoints`。下面示例表示源第 42 帧 A 操作雨伞、B 持有外衣。名称、帧号和归属均按新原片更换。使用中性源槽位可以在交换目标身份后保留动作归属。

```json
{"role_checkpoints":[{"source_frame":42,"visible_slots":["A","B"],"owners":{"umbrella":"A","outer coat":"B"}}]}
```

发生完全遮挡时在相应源帧写明实际可见槽位。角色修复后分别复核两个身份、正常角度的身体比例、首帧发型、快速动作中的衣料，以及镜头交接前后。每次 inspect-candidate 从头生成该候选的待查 `role_review`；逐项填写实际看到的槽位、道具归属和额外人物，记录检查者与连续性观察。修改选帧映射后用 `role_review.pending(analysis, window, record)` 重新建立待查项。review-guard 按候选文件、映射及源事实核验观测。语义结论需要实际看画面。完整原速审查仍单独记录。

## 费用

```text
python scripts/zhenzhen_runner.py settle --project <run> --key-file <私有Key文件>
python scripts/zhenzhen_cost.py --project <主run> --project <修复run>
```

每个独立试片或修复 run 均加入统计。同一账号的任务 ID 去重，读取终态回执的 `usage.amount` 和 `currency`。分母来自所有成功输出逐段解码的时间，包括补尾、未选候选和返工。失败但有实扣的任务计入金额，退款按最终记录。按币种分别报告已知金额、实际生成秒数和平均费用。缺回执、未下载、未完成或提交不明时保留待核项。settle 只查询已有终态任务。RH 继续使用 `scripts/cost_audit.py` 核算 RH 币。

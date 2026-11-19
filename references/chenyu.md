# 晨羽工作流接入

使用晨羽发布的 [Wuwu-视频角色替换](https://www.chenyu.cn/console/workflowApi?id=wf_04a30714468c59fd78f5)。每个镜头发送一段运动视频和 1–5 张身份图。按本镜头角色顺序分配 Picture 1、Picture 2 等，独立上传每张原图。发布清单开放五个图槽，实际双图试片已验证。

## 运行

在新的制作目录配置好源片、镜头分析与角色映射。首次换人物时选择渠道，已有明确选择或正在续跑时沿用该选择。下面命令从技能目录执行。把 RUN_DIR 与 PRIVATE_KEY_FILE 换成本机路径，Key 文件放在技能、制作目录与 Git 仓库之外。

```powershell
python scripts/factory.py select-channel --project RUN_DIR --channel chenyu
python scripts/factory.py prepare --project RUN_DIR
# 按工厂流程检查本镜头的提示词、时序和制作授权，完成输入审批后运行
python scripts/chenyu_runner.py tick --project RUN_DIR --key-file PRIVATE_KEY_FILE
python scripts/chenyu_runner.py status
```

`tick` 执行一次调度。由同一个调度器周期性执行，优先查已受理订单并补空位。当前角色、风险和帧数的首个试片须通过原速与原分辨率复核，才放行同类窗口。使用独立账号别名时，所有运行、恢复和核算命令保持相同的 `--account`。

## 参数与结果

| 步骤 | 晨羽 API 和数据 |
| --- | --- |
| 核对工作流 | `GET /workflow/market/info` 通过 workflow_id 获取 revision_id、可编辑输入、视频输出与外部计费声明。与本地配置逐项核对，再上传。 |
| 上传 | `POST /assets` 对每个文件发送 multipart 的 file、media_type 与 purpose=temp_input，返回 asset URI。 |
| 组装 | node_138_value 为提示词，node_342_video 为运动视频，node_326_value 为整数帧数。node_386_image 至 node_390_image 依角色顺序填写，本镜头有几人就用几个槽。 |
| 提交 | `POST /workflow/run/submit` 携带 workflow_id、revision_id、inputs、idempotency_key 和 accept_external_cost_risk=false。提交前将完整请求落在本机私有账本。 |
| 查询 | `GET /workflow/run/info` 按 run_order_id 轮询并核对修订版。成功后取当前清单指定的视频输出，下载时仅请求结果文件，完整解码后进入候选复核。 |

帧数标签标注最低 22 帧、17 + 5N；这个发布版本的 73 帧默认值也已在真实双图试片中跑通。工厂按 24 fps 为当前镜头选足够的最小候选帧数，连补齐帧在内最长 10 秒。其他候选帧数须先试片。已验证的 1920×1080 输入返回 1376×768，检查实际画幅再组装。工作流未开放独立的比例、分辨率和音频参数，成片沿用源片时间轴和已批准音轨。

## 恢复与实扣

已知订单号时继续执行 tick。若提交响应丢失导致状态为 SUBMISSION_UNKNOWN，先查私有账本取得尝试编号，再使用原凭据恢复。该命令复用已保存的请求和幂等键。

```powershell
python scripts/chenyu_runner.py recover-unknown --attempt ATTEMPT_ID --key-file PRIVATE_KEY_FILE
python scripts/chenyu_runner.py settle --project RUN_DIR --key-file PRIVATE_KEY_FILE
python scripts/zhenzhen_cost.py --provider chenyu --project RUN_DIR
```

每个修复目录也执行 settle，汇总时逐个追加 `--project REPAIR_DIR`。按订单去重，读取终态 final_amount 与 quote_currency。每秒实扣以全部实际生成时长为分母，包含补齐和淘汰候选。未结算项目标为待查，平台返回 amount 时保留原单位。账本、参考图、订单及凭据留在本地。[晨羽官方接入文档](https://chenyu.mintlify.site/api-reference/workflow/introduction)

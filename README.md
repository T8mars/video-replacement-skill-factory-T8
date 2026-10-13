# 视频换角与专属技能工厂

> **本 AGENT / SKILL 调用 RunningHub（RH）应用完成视频生成，需要先有 RunningHub 账号，并配置自己的 API Key。**
>
> 注册入口（按作者提供的站点分类）：
> - **海外（无审核）**：[RunningHub 海外站注册](https://www.runninghub.ai/?inviteCode=rh-v1121)
> - **国内（强审核）**：[RunningHub 国内站注册](https://www.runninghub.cn/?inviteCode=rh-v1121)
>
> 实际账号权限、可用模型和审核要求以平台当前规则为准。注册入口不会自动切换脚本的接口地址。

**技能标识：** `video-replacement-skill-factory`  
**当前版本：** `1.2.16`  
**专属维护仓库：** [T8mars/video-replacement-skill-factory-T8](https://github.com/T8mars/video-replacement-skill-factory-T8)

给 Codex 一个原视频和人物参考图，工厂会分析原片、替换人物、检查并修复画面、组装保留原音轨的成片，再为该原视频创建并安装一个独立、可复用的专属 SKILL。下次使用同一原片，只需更换人物参考图。

## 每次交付

- 实际可播放的换角视频，按原片逐帧时间轴组装并保留原音轨。
- 这个原视频的专属技能，保存中性的角色槽位、镜头、动作、场景和时间分析。
- 可安装 ZIP，包含原源和独立运行脚本，排除本次人物图、生成候选、任务记录和凭据。
- 中文内容名，写入显示名、搜索描述、标题与安装包；内部标识使用 `r2va-video-<原SHA前12位>`。

本仓库发布的是**工厂本身**。每次生成的 `r2va-video-*`、原视频、人物图和成片保存在用户的本地制作项目中。

## 安装与运行环境

需要 Codex 的本地技能能力、Python 3.10+、PyAV、Pillow，以及 PATH 中的 `ffmpeg` / `ffprobe`。离线音频回归还需要 NumPy，已列入requirements.txt。运行时路径可按自己的机器设置，媒体命令支持 `--deps`。Windows 遇到已证实的解码差异时可使用 `--scalar-cpu`，详见技能文档。

PowerShell 安装到个人技能目录：

```powershell
git clone https://github.com/T8mars/video-replacement-skill-factory-T8.git "$env:USERPROFILE\.agents\skills\video-replacement-skill-factory"
python -m pip install -r "$env:USERPROFILE\.agents\skills\video-replacement-skill-factory\requirements.txt"
```

macOS / Linux：

```bash
git clone https://github.com/T8mars/video-replacement-skill-factory-T8.git ~/.agents/skills/video-replacement-skill-factory
python3 -m pip install -r ~/.agents/skills/video-replacement-skill-factory/requirements.txt
```

安装后在 Codex 中调用 `$video-replacement-skill-factory`，提供原视频和人物参考图。也可以用“视频换角与专属技能工厂”描述任务；读取详细入口 [SKILL.md](SKILL.md)。

## 配置账号与凭据

RunningHub 账号需要有对应应用或模型的使用权限。工厂支持已观察过的单人 / 双人 legacy 应用和标准 H3 接口，具体能力与证据放在 [backend-profiles.json](assets/backend-profiles.json)。标准接口参数存在，不代表当前账号已开通权限或新视频已经视觉合格。

API Key 只写入**仓库和制作项目之外**的本机私有文件。脚本默认读取 `$CODEX_HOME/private/r2va-character-replacement/api_key.txt`；未设置 `CODEX_HOME` 时使用用户目录的 `.codex`。也可以显式指定自己的私有文件：

```text
python scripts/rh_runner.py tick --project <制作run绝对路径> --key-file <仓库外私有Key文件绝对路径> --ledger <仓库外共享账本绝对路径> --account default
```

同一实际账号的所有项目共用同一账本和账号别名，默认合计并发 **3**。Key 本体、私有账本、请求和签名结果 URL 不进入 Git、README 或安装包。仓库不提供默认 Key。

## 在 Codex 中使用

示例请求：

> 使用 $video-replacement-skill-factory，把这个原视频中的人物替换成我发的参考人物，保留实拍风格、动作、镜头和原音轨。完成成片后，为这条原片保存一个可复用、带中文名称的专属 SKILL。

参考人数根据原片和替换范围确定；同人的多角度图片属于一个身份。用户明确复用刚才人物时，核对已有资料后继续。角色对调改变槽位与身份的映射，原片的左右位置、动作承担者和镜头时序仍保持。

实际制作由 Codex 按技能组织源分析、语义审核、调度和视觉检查。独立运行脚本提供确定性媒体处理及接口操作；技术检查不能自动证明脸部、动作和接点合格。

完整步骤见 [制作及复用流程](references/workflow.md)，JSON 和 CLI 合同见 [schemas.md](references/schemas.md)，接口与恢复见 [backends.md](references/backends.md)。

## 质量检查与复用

- 同一原源按完整 SHA 复用已核分析；不默认重做首次工厂的20轮研究。
- 新案例先检查一个有代表性的实际窗口，再扩批；两张图输入不证明所有角度都换人成功。
- legacy竖屏先核对真实应用节点，并显式提交全部已公开比例节点的源匹配画幅参数。未设置或与源展示比例不符时，提交前拦截；提示词写竖屏不证明应用实际输出竖屏。
- 六段式提示词分别定义人物图片的身份职责与源视频的动作、镜头、时序、场景职责，避免引用职责混淆；外观仍遵守“只换脸”等用户范围。
- 开场只写当前实际可见的道具状态；真实空白台面使用正面状态描述，后段进入的物体按实际时点和持有者描述，修复后分别验收身份与道具。
- 队列满或资源忙时持续等待；已受理任务沿用原 taskId，重启先恢复，未知提交先对账。
- 画面修复不设次数上限。重复失败先检查引用、构图、身份范围和动作阶段，再换策略；其他已通过且签名未变的窗口冻结。
- 脸型、嘴牙、眼色、服装、前景后脑、手袖连接、动作时机及连续接点按当前版本实际复核。
- 近景先锁定源站立姿态和真实裁切；动作按相邻帧分别记录开始、完成和维持，防止下俯、道具入画和问句嘴型晚半秒。构图改善仍需独立时间复核。
- 回身身份回流可尝试已审表情身份导引；字幕乱码先取得实际无文字底片，再恢复原字形和源时段，不能用半透明覆盖掩盖错误文字。
- 修复输入与queue签名时与唯一调度器串行更新，恢复已受理的原taskId，防止读到修改中的不一致版本。
- 极近镜头只要求原片实际可见的面部范围；若生成普通肖像，先校正对应极近导引，再重新验视频。加长排除尾部和导引图通过都不能当作视频质量通过。
- 独立局部比较run的queue只列已批准修复窗，最终回到完整制作run检查所有权和连续接点。
- 最终保留全部源帧所有权和原音轨；完整解码、全片原速和风险帧检查完成后才可标记 `ready`。

新身份、新候选和新组装成片仍需各自验收。范围、已观察失败和修复规则见 [quality-and-repair.md](references/quality-and-repair.md)。

## 创建与复用专属技能

制作期间可先保存 `draft`；首期实际成片及模板验证通过后构建 `ready`：

```text
python scripts/factory.py build-skill --project <制作run> --skills-root <个人skills目录> --status ready --display-name "客厅双人对话与拥抱·换角复刻"
```

下一次同片换新人物：

```text
python <专属技能目录>/scripts/factory.py instantiate --skill <专属技能绝对目录> --project <新的空run目录>
```

新 run 的身份、接口设置为空，生成授权为 `false`。提供本次人物和制作请求后继续流程；安装和离线验证本身不提交付费任务。不同原视频创建不同专属技能，同源升级保留旧版本。

## 验证

```text
python scripts/test_factory.py --work <仓库外测试目录>
python scripts/test_runner.py
python tools/verify_publish.py --revision HEAD
python -m unittest discover -s tools -p test_publish.py -v
```

目前核心工厂 / 媒体回归 **65 项**通过；runner **47 项**通过，合计112项。包含中文命名、随机文件名回退、真实 ZIP 解包、独立子进程复用、源时间轴、原音轨、任务恢复及源/身份引用绑定。一个12.25秒实拍双人物生产案例及专属技能已完成；另一条竖屏源的首段通过了当前身份、动作、道具和字幕复核，完整新片仍在制作。这些结果只证明相应已核范围，后续输入继续实际验收。

GitHub Actions 会检查发布边界、manifest、发布守卫和离线测试。离线测试不调用云端生成；实际接口权限和生成效果须在授权制作中验证。

## 后续维护与固定发布

这个仓库仅接收 `video-replacement-skill-factory` 的更新。源目录的 manifest 是同步清单，仓库的 [publish-policy.json](https://github.com/T8mars/video-replacement-skill-factory-T8/blob/main/publish-policy.json) 是额外白名单；两者都通过后才能同步。下面的维护命令在仓库checkout中执行，发布工具不进入用户的专属视频技能。

```text
python tools/sync_skill.py --source <本机工厂技能目录>
python tools/verify_publish.py --staged
git config core.hooksPath .githooks
```

按具体白名单文件暂存，再提交、推送。Windows 可用 `git config factory.python <Python绝对路径>` 指定本机 Python。pre-push 会核对目标仓库并扫描待推送的全部提交，不能只依赖 `.gitignore`。

**ROADMAP.MD（任意大小写、任意目录）和 API Key 永远禁止推送。** 其他技能、专属视频技能、模型权重、输入图、视频、候选、成片、日志和私有账本也不属于此仓库。维护规则另存于 [AGENTS.md](AGENTS.md)。

## 作者与资源

以下链接由 T8 提供，作为社媒与资源入口：

| 资源 | 链接 |
| --- | --- |
| B站 | [T8 的 B站空间](https://space.bilibili.com/385085361) |
| YouTube | [T8star-Aix](https://www.youtube.com/@T8star-Aix/) |
| API 服务入口 | [API 注册入口](https://api.seedance.nz/sign-up?aff=5f4w) |
| 免费画廊 | [贞贞免费画廊](https://www.openzhenzhen.com) |
| 在线 AI 应用 | [RunningHub 应用主页](https://www.runninghub.ai/zh-cn/user-center/1907375370302308353/userPost?inviteCode=rh-v1121) |
| ComfyUI 整合包 | [夸克网盘整合包](https://pan.quark.cn/s/264edb7e36bd) |
| Hugging Face | [t8star](https://huggingface.co/t8star) |

工厂当前的生成后端按本次 `job.profile` 和接口文档配置；资源列表中的 API 注册链接是作者提供的独立资源入口。

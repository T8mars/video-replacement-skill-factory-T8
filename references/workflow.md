# 制作及复用

每次制作使用新run目录，安装不启动云端。需要Python3.10+、PyAV、Pillow及PATH中的ffmpeg/ffprobe；媒体库未安装到当前Python时以 `--deps` 指向本机实际依赖目录。移机重新定位运行时，不把旧制作项目作为输入。有本机解码矛盾证据时使用 `--scalar-cpu`，只修改当前PyAV进程。

## 恢复与避免重复工作

先按完整原SHA查已安装模板和本次run状态。相同原bytes的已核源分析、角色/镜头/相邻动作阶段从模板复用，不重做首次工厂研究；新剪辑才建立新源分析。重启先恢复已知taskId和已完成文件，不重新POST。每次实看通过立即保存当前输入、输出、owner及覆盖绑定；它们未变时继续剩余步骤，输入或文件变化才撤销相关审核。新身份、新候选和新组装成片仍分别验收。

单窗不合格先核图像顺序/身份范围、导引整幅构图、源相邻动作阶段、context/prefix/scale/owner和接口权限，再选择有针对性的修复；重复失败升级策略，不循环相同提示。其余已过窗口冻结，只修当前窗与必要接点。首次试窗覆盖最容易漏换的角色正脸和关键动作，过关再扩批；多个方案共享同一个恢复调度器与账户并发上限。保存本源特有的失败条件、已实测修复及适用范围到template-constraints，下一次先读取，不把旧人物/旧任务当成源事实。

## 中文名称

每份新专属技能由Codex按已核源内容自动拟定中文显示名，如“客厅双人对话与拥抱·换角复刻”；用户给了名称则用用户名称。中文名进入SKILL描述、标题、UI显示/默认提示和manifest，最后从同一版本生成ZIP。调用 `build-skill --display-name "中文名称"`，内部SHA标识和文件夹保持稳定，便于去重。遗漏参数时脚本也保证中文回退名，但正式交付前仍应使用准确的内容名。已有同源技能默认保留原名称与用户修改，不因复用自动改名。

```text
python scripts/factory.py ingest --video <原片绝对路径> --project <新run> --deps <PyAV依赖目录>
```

源副本和source.json只读。看全片，再核所有自动候选切点、溶解/闪光/快速运动，填analysis中中性slot、实测shot与事件并保存观看证据。自动首中尾/候选图不能代替源动态查看；源文字记source_data。来源审核真实完成才status=passed。

参考图复制进本次run `refs/`，同人多图分组，为每slot写identity_en、coverage与未知属性；face-only填写face_identity_en。源事件用 `{{A}}` 等占位，绝不全局替换旧人物名。对调只更换slot→ref。多视图/白底/像素质感/参考姿势和无关扇子不移入场景。

真实切镜优先，极短镜保留；长镜在动作稳定状态切窗，上下文重叠不重复拥有目标帧。plan所有源owner必须恰好覆盖一次。默认plan可按实测改，长镜内部开窗写实际opening state context_description_en；context内可见任何目标身份都绑定。

参考与源片跨画风时先解决身份资料的一致性。源为实拍而参考为像素/插画，保留实拍的任务可先用图像编辑工具将每个人转为单人实拍身份导引，亲看脸型、眼色、发型、服装和成人比例后再生成视频。记录这是原身份线索的实拍解释；不声称像素图证明了精确真人脸。原图和派生图留本次run，专属模板只保存这条有条件的策略。双人物首窗应包含容易漏换的角色正脸；单个过肩镜头通过不能证明两人所有角度都通过。

像素脸与实拍脸混用、原人物未替换或新增第三人时，不反复提交同一输入。明确源演员→目标槽位从首帧原位替换，检查本窗引用顺序；把容易被吞掉的极短反应镜头独立成窗，接口需要的尾帧补齐仅用于生成，不进入owner。依旧逐帧保留原反应和真实切点。

```text
python scripts/factory.py validate-analysis --project <run>
python scripts/factory.py prepare --project <run> --deps <deps>
```

首次prepare生成pending语义。Codex查看全部成稿并对源片/参考核对动作时间、人数、方向、外貌范围和局部编号后，再 `prepare --approve-semantics`。该开关记录已完成的审稿，不负责代替判断。job.production_authorized依据人类本次实际制作请求设true；只做工厂时false。

显式VFR/旋转派生记录source_projection采样表、丢失/重复及时间量化。`--approve-projection`只在Codex查看派生参考并对源动作确认可接受后使用，独立于语义审核；不把恢复原PTS说成丢失信息可逆。手改长镜plan后用 `refresh-plan` 重建表再prepare。没有目标角色的空镜passthrough原片owner，不付费生成，成片仍全片QA。单/双人legacy按每窗实际身份数选择对应已证应用或明确profile_override。

新case首窗queue pilot=true，先完成一份实际生产候选；生成文档参数不证明换角视觉。首窗技术+视觉过关后，job.profile.quality_evidence设case-verified并记具体证据，同时更新queue对应profile的QA字段。纯QA推进排除在生成digest之外，重算确认已有输入未变，再扩批。手修提示词或编译器升级后不要盲目prepare覆盖已通过的输入；未修改编译合同的正常run仍可prepare刷新。实质输入/参数变动失效旧task/review，正确归档对应revision。

混合single/dual或其他profile route各自先一个pilot，总并发仍3；使用window.profile_override的路由，在该override更新质量证据，不能只更新全局job.profile。再次prepare相同prompt字节保留mtime，不因QA进度失效已审核版本。

```text
python scripts/rh_runner.py tick --project <run>
python scripts/rh_runner.py status
```

tick一次先恢复中央账本所有已知任务，再提交本run剩余额度内的窗口。Codex持续调度、查看、修复直到交付，用户不用手动跑。所有项目同一实际账户使用同一私有账本/alias，不能每项目各建limit3。

候选在 `candidates/<window>/<attemptUUID>/output.mp4`，不可变。SUCCESS后用：

```text
python scripts/factory.py inspect-candidate --project <run> --window-id <window> --task-id <ID> --candidate <run内相对MP4> --deps <deps>
```

它实际解码、导出原尺寸证据、提出源→候选帧映射，视觉和时间映射默认pending。Codex对源事件核对映射/重采样损失，动态及原尺寸查看后写reviews；禁止猜标准H3输出24fps。

用户说停用stop停止新增，仍记录云端已跑状态；用户已说继续或修好凭据可resume，连续技术计数重置而历史attempt保留。UNKNOWN对账拿到官方taskId才recover-unknown，不重POST。

```text
python scripts/factory.py review-guard --project <run>
python scripts/factory.py assemble --project <run> --output <run内新文件> --deps <deps>
python scripts/factory.py review-guard --project <run> --final
python scripts/factory.py build-skill --project <run> --skills-root <CODEX_HOME/skills> --status ready --display-name "依据原内容拟定的中文名称"
```

assemble默认成片视觉pending，完成完整技术验证后看全片/接点填写final，再guard/build ready。未完成可以先draft，并继续制作。专属模板复制原源、中性analysis/plan策略、自足scripts/references；本次refs、task私有URL/ledger和候选不进模板。

本次自由文本运行假设、失败记录和生成审核放在私有制作证据，公共evidence只保留原片来源事实及有适用条件的复用规则。验证安装包时实际解ZIP，再用包内脚本在独立子进程instantiate；仅copy2目录验证无法发现ZIP时间戳问题。新job应为空身份、空profile且授权false，不能继承上一期请求。

未来调用专属skill：

```text
python <专属skill>/scripts/factory.py instantiate --skill <专属skill绝对目录> --project <新的run>
```

中性原槽位动作不变，填新job身份并继续生产/QA。同源复用模板，新剪辑/原bytes变动新模板。draft→ready在新revision升级，旧版保留；安装中断recover-install对账。ZIP只本地生成，不外发。


legacy原片竖屏时，在首次prepare前按backends.md核应用实际ResolutionSelector节点，配置匹配源展示比例的resolution_selector。不能依赖图片宽高或“vertical”提示词切换云端工作流画幅。首窗实际画幅不符即拒绝，不扩批；整个源片说明不提前引入尚未进入当前窗口的道具，提示词按当前context可见状态编译。


修复prompt/引用及queue签名时，与本机唯一调度器串行操作：暂停本机新增调度，等正在执行的tick完成，再以完整审稿后的prompt、签名、digest和queue一致版本更新；恢复时先核全部当前输入和已知任务。避免调度器在文件与queue更新之间读取旧digest并安全停机。已经受理的任务只恢复原ID，不因这种本机中间状态重新POST；这项维护暂停不要求用户重新授权。

首期新证据修正了源事件事实时，在专属模板中保存纠正后的中性源事件与相邻帧依据，并更新对应analysis/plan摘要及包manifest。不要改动仍有云端任务的冻结制作合同；源事实修订与当前候选生成历史分别保留。新包实际解包、独立instantiate检查后，下次使用纠正后的源事实。


建立独立局部比较run时，可以保留完整源分析和全部owner计划，但queue只包含当前已批准的修复窗。tick会预检queue中的每个非passthrough条目；不要把其他尚未审批的克隆窗塞入queue，即使暂时不打算提交。局部比较的审核只证明对应父候选，最终仍回到完整制作run核全部owner、合成父映射和接点，再运行完整review-guard；不让局部通过伪装整片通过。

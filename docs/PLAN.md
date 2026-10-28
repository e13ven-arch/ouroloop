# 开发计划（活文档）

> 下一轮开发的唯一入口：现在在哪、规则、按序的阶段、待用户决定的事。规则细节与论证写在 §4 列的来源文档里，本文只引用。每个阶段结束后更新本文（§5）。
>
> 2026-10-09 改写：按正向设计 [DESIGN v1.1](DESIGN.md) 重排阶段。P1 瘦身第一批已合入 `m1-automationbench`（`6a06e45`）；原 P2～P7 并入新阶段。清理前的 HEAD 是 tag `archive-2026-10-09`，删掉的文档都在那上面。

## 0 现在在哪

### 0.1 结果

封存 final，test 留出，计划哈希在第一次 test 尝试之前提交。对外数字只来自这里。

| 版本 | 活动 | W0 | W\* | 差值 [95%] | 读法 | 记录 |
|---|---|---|---|---|---|---|
| v0.1 | marketing，test 23 × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 成立 | tag `v0.1.0`；[RESULTS](RESULTS.md) |
| v0.2 | marketing（带研究位），23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | 不确定 | tag `v0.2.0` |
| v0.3 | 五个域共用一个包 | — | — | 没有 final（3 轮都没有安装） | — | tag `v0.3.0`；[诊断](history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md) |
| v0.4 | finance，25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | 不确定；每次尝试成本 −22%，显著 | tag `v0.4.0`；[事后分析](history/FINANCE_V04_ANALYSIS_2026-10-07.md) |
| v0.5 | support 两臂对照 | — | — | 没有 final：2026-10-08 用户叫停 | 只有 train/dev 过程证据（经验库流 D = 3.0，快照过 dev gate +0.16） | JOURNAL 2026-10-08 |
| finance-v18 | 4o 的 K（逐题采用 + 重复流），finance test 50 道 × 3 次，学习臂，每次尝试一比特接受 / 退回 | 首次 32/50（64%） | 稳态 34/50（68%） | **+4.0** [−4.0, +12.0]（learn:e3−e1，配对） | 不确定；18 道退单修 3 道且重复上站住，零退步 | $183.77；JOURNAL 2026-10-22 4o 一条的 v18 补记；`workspace-finance-v18/report.md` |
| finance-v8 | 4h + 4i 的 K，三域 train 150 道流式 | 首遇 87/150 | — | 没有 final：5 轮零装入（W\* = W0）；一个部分包进前沿，合并版丢了修复 | $261；JOURNAL 2026-10-13 finance-v8 一条 |
| 人工包（A） | 对 63 道错题逐题读出 8 类机制、人工写成 W 包，在 v8 工作区试跑与留出筛选 | 55 道原失败题单次 22 过 | — | 留出 +5 −3 两次（v2、v3）被拒，未装入；没有 final | $121；JOURNAL 2026-10-19 一条 |
| finance-v13 | 4n 的 K（松开的接受规则），只跑 finance，50/50 | 基线 35/50（70%） | — | r0：owed hook 留出零出手、技能 20/20 出手但确认后修 0 退 0（4 道 W 自己不稳的题）；两包被拒，零装入 | $74.40；JOURNAL 2026-10-21 4n 一条的补记 |
| finance-v12 | 4m 的 K（逐题 probe），批式 r0，两个 scope 的包 | 首遇 87/150（导入） | — | 没有 final：hold 包三次 try 从未出手不筛；`held_item_routing` 技能样本内 4 道有足迹翻转，留出可复现修复 1、退步 2（32 对 34）被拒进前沿 f1 | $83.18；JOURNAL 2026-10-21 finance-v12 一条 |
| finance-v9 | 4j 的 K，批式 r0 over finance-v8 导入的 150 道首遇（每轮 1 个包） | 首遇 87/150（导入） | — | 没有 final：r0 的 hook 包过试跑（3 道试 1 道修）进筛选，留出 60 道确认后修 0 退 0（33 对 34）被拒；7 个首读翻转 6 个无足迹、1 个噪声 | $48.21；JOURNAL 2026-10-19 finance-v9 一条 |
| 阶段 4 | finance-v2、v4（正向设计后的 RSI 实测） | dev 9.5/25 | — | 没有 final：三次 gate 都拒绝（0、0、−0.02） | 过程证据：候选过免费阶梯、留出筛选 ±1、成本 −18% 显著 | JOURNAL 2026-10-10～11；tag `finance-v2`、`finance-v4` |

### 0.2 已定的事

| 日期 | 决定 | 来源 |
|---|---|---|
| 10-07 | 训练与蒸馏是框架的一部分，在 `reharness/train/`，不在热路径 | [V2](design/V2_TRAINING_PIPELINE.md) |
| 10-08 | V2 训练链定稿：轨道 A 只按链路收据验收；Tinker LoRA 训练，RunPod 上自托管 vLLM 评测 | V2 §0.1、§7 |
| 10-08 | actor 与 R 经 claude_code CLI 走订阅，不开 API key 臂；所有臂同一 effort（medium） | JOURNAL 2026-10-08 |
| 10-08 | 五个域都可跑封存 final，披露读取次数；只有 operations 留给 v2 的 LODO | V2 §7 第 2 项 |
| 10-08 | 开源优先：agent 底座 Stirrup、memory 用 ReMe，我们只写平台层；二开要有明确理由并先讨论 | DESIGN §2.3、§3.7 |
| 10-08 | 叫停 support 对照：“能够改进的地方都改进掉，看的是最终结果” | JOURNAL 2026-10-08 |
| 10-08 | 不写金标身份测试；旧工作区只在 tag 上续跑或重渲染，HEAD 只须读得了它们的 train/dev 记录 | 规则 15 |
| 10-09 | 框架职责是自动提升 harness 与训练模型；改进器由我们自己设计实现；harness、agent、evolution、memory 都要正向设计；选型先定让哪一层变化；特色是轻量 harness 包 + 运行壳 + 模型描述，蒸馏作第二杠杆 | DESIGN v1.1 §1 |
| 10-09 | 框架不为某个基准定制；原则直接遵守，不另加审查 | DESIGN §9 |
| 10-09 | “按默认”：DESIGN v1.1 取代旧设计与 modules/；种子 Stirrup，Pi 作 hook API 参照，Deep Agents 作第二种子备选；特色定为轻量 harness 包 + 运行壳 + 模型描述，蒸馏作第二杠杆；分层的代码先验与“有可机械化簇就必须有代码候选到 V2”；阶段 1 从 `m1-automationbench` 起、v6 三条分支搬入；第一个实测域 finance | DESIGN §12 |
| 10-11 | 跑了四次活动零装入，下一版改框架代码，不再跑活动等它自己提升；先写设计。按审查改六处：一轮一个包、留出接受累积在工作线、dev 只在活动末读一次、hook 可调 aux、library 默认关、版本拆分与基线导入 | DESIGN v1.2 §6.12 |
| 10-09 | 不以代码行数作验收；要求是工作量不落在外围组件上，核心系统认真打磨。行数只作读数 | JOURNAL 2026-10-09 不以行数作验收一条 |
| 10-12 | 对外的数字是 final 上的绝对分（W\* 得了多少，W0 臂并列），不追求比业界更严；划分改两份（train / test，finance 50/50）；基线阶段取消；无 dev 的活动按决定发布；模型不换，改的是代码、工具、流程、prompt、memory；流式学习（test 首遇计分后变证据、每 N 题一轮）另立阶段 | DESIGN §6.7 第三条、§12 第 5 项；ACTIVITY §4 |
| 10-12 | 第 0 轮 +2 不够，要一次活动提升足够高：一轮几个包并叠装、审核 hook 建议、流式循环、工作线跨活动（阶段 4d）；预算"不用限制那么死"：final 份额是下限、final 一族只比对尝试路径代码（规则 13 放宽）；v6 改成跨域 train，先跑 3 个域 | DESIGN §6.1、§6.4、§6.13、§6.14；EVALUATE §9；PLAN 规则 13 |
| 10-12 | 历次版本分数不动、失败的是同一批题：读了 18 道顽固失败题的轨迹，失败是"文档里的一条要求没执行到 / 没沿用记录的词 / 该报告的没报告"；给 R 失败并排、加三个 cause、提示词对准工作方式（阶段 4f），先改文档再改代码 | DESIGN §6.15；IMPROVER §4、§5、§7；JOURNAL 2026-10-12 读轨迹一条 |
| 10-12 | v6 不跑 final；A 类只能靠审核，三次审核都"改过头"：框架要学会提升概率——单向审核（构造保证）、V2d 预演、误报累积进 R 的输入（阶段 4g），先改文档再改代码 | DESIGN §6.16；IMPROVER §2、§7、§8、§10；HARNESS §7；CONTRACTS §4 |
| 10-13 | v7 量出噪声底（同一个包两次跑 10～17% 的题翻），此前的 ±2 都是噪声；装上的 hook 乱报、V2b 只报不拦、接受规则低于噪声底。用户："确保 RSI 的调整是吸收好的经验，而不是遇到一点错误就全盘修改"；v7 不跑 final，先改文档再改代码，v8 从 v6 的头起步。不加机制，只收紧两个门：V2b 误触发门、翻转确认后的接受（阶段 4h） | DESIGN §6.17；JOURNAL 2026-10-13 v7 一条 |
| 10-13 | finance-v8 五轮零装入（$261）之后："按照建议来"——A（直接按 FINANCE_V8_FAILURES_FIX_PLAN 改 W 并在错题上真跑）与 B（阶段 4j 改回路）并行。4j：回路改成"R 对着失败题改、改完真跑、通过了再筛退步"——样本内试跑作 R 的反馈与留出筛选的入场门槛（接受仍只看留出）、退步按足迹、前沿带修复证据且合并先回放、导入 stream 首遇、V0 提示未挂的 hook、词表去误报、编辑额度提醒、核价与 V2b 样本 | DESIGN v1.9 §6.19；IMPROVER §9.6；JOURNAL 2026-10-13 阶段 4j 一条 |
| 10-12 | 第 0 轮 +2 不够，要一次活动提升足够高：finance-v5 批式跑到底拿第一个数；并行做四条（一轮几个包并叠装、清单与提示词对准整体机制与审核 hook、流式循环、工作线跨活动 `seed_from`），v6 从 v5 的 W\* 起步流式跑 | DESIGN §6.1、§6.4、§6.7、§6.13、§12 第 6 项 |

### 0.3 停在哪里

| 项 | 状态 |
|---|---|
| 分支 | 阶段 4l 的 `s11-contrast` 已合入 `m1-automationbench`（`73ee923`，2026-10-20）。`s5/search-loop`（阶段 4b、4c）已合入 `m1-automationbench`（`d36ee5d`）；finance-v5 从这个检出跑（规则放宽的 `cf99a58`、final 计划 `70aaac3`，tag `finance-v5-final`）。阶段 4d / 4e 的 `s6/yield` 已合入 `m1-automationbench`（2026-10-12，finance-v5 结束后）；finance-v6 从这个检出跑，期间 `reharness/` 不再合入。阶段 4i 的 `s9/frontier` 已合入 `m1-automationbench`（`92a48a6`，2026-10-13）；finance-v8 从这个检出跑，期间 `reharness/` 不再合入。阶段 4j 已合入 `m1-automationbench`（`042944d`，2026-10-19）；finance-v9 从这个检出跑（r0 已结束，2026-10-19）。阶段 4o 在 `m1-automationbench` 直接改；finance-v14～v18 从这个检出跑（v18 结束于 2026-10-24；没有活动在跑）。`main` 在 `32fe778`（2026-10-28 第七次合入 `m1-automationbench`：包装——开放问题一节、结果陈述去对冲、落地页亮点胶囊；此前 `721429a` 动画流程图；此前 `347bfee` 实时回放与同步状态卡；此前 `003635b` 结果口径标明 effort、"会话自建的组件"一节；此前 `5aa88a0` Pages 落地页重做；此前 `b96d2d4` SEO / GEO——FAQ、llms.txt、AGENTS.md、发帖稿；GitHub Pages 已开（main 的 docs/，https://e13ven-arch.github.io/reharness/）、Release v0.5.0 已建，均用户要求；此前 `33fa247` 结果口径并列陈述；此前 `e0a73f3` 宣传语 / 结果 / 可发现性、`fa52cf2` 研究体例 README 与两张图；都是 merge commit，无新 tag；上一个 tag v0.5.0 在 `50f5c40`；均已推送到 origin，用户要求） |
| 代码 | 阶段 1～3 完成：`contracts.py`、`harness/`（含 stirrup 种子）、`runtime/`（含 `trace.py`）、`evaluate/`、`memory/`（ReMe sidecar、桥、规则、写入步、检索、快照、升级）、`improver/`、`activity/`。阶段 4b（DESIGN v1.2 的六处，JOURNAL 2026-10-11 阶段 4b 一条）已落地：一轮一个包（包级声明、聚焦清单、V0 两向比对、V2 按组件）、留出接受装入工作线（`rsi/active.json`，reason `screen`）、活动末 `--phase gate` 发布到 `rsi/released.json`（reason `gate`）、`after_turn` / `on_finish` 的 hook 可调 aux 模型（`hook_aux_calls`）、preset `automationbench_v2`（library 关）、K 的 `attempt_version` / `improver_version` 与 `[rsi] baseline_from` 的基线导入。10-10 的日期锚、V2a 探针、部分得分规则照用；carry、`gate_every`、样本内筛选删除。阶段 4c（两份划分、`--phase release` 按决定发布）与 4d（`s6/yield`：一轮几个包并叠装、审核 hook 建议、`[rsi] seed_from`、`--phase stream` 流式循环；JOURNAL 2026-10-12 阶段 4d 一条）已落地；4e（跨域 train：`[bench] domains`、`[final] domains`；JOURNAL 阶段 4e 一条）已落地；两条规则放宽（final 份额是下限、final 一族只比对尝试路径代码；JOURNAL 2026-10-12 规则放宽一条）已落地 |
| 测试 | 改名 reharness 后（`m1-automationbench`，2026-10-26）全量 1,648 passed、1 deselected（worker 与 egress 镜像按新名重建：runner 与 egress 插件读 `REHARNESS_*`；memory venv 按锁的新哈希重建；记录格式名 `ouroloop.bench.*.v1` 保持原拼写以读旧记录）；阶段 4p 第二刀后（同分支，2026-10-24）默认 1,559 passed、87 deselected；阶段 4p 后（同日）默认 1,559 passed、87 deselected（新增 4 个）；finance-v18 收尾后（`m1-automationbench`，2026-10-24，adopt 的 `usd` 改记会话花费）默认 1,555 passed、87 deselected；v18 冻结前（`bbcd2a9`）Docker 全量 1,633 passed、1 deselected；阶段 4l 合入后（`m1-automationbench`，2026-10-20）Docker 全量 1,629 passed、1 deselected；阶段 4l 后（`s11-contrast`）默认 1,543 passed、87 deselected（新增 8 个）；阶段 4j 合入后（`m1-automationbench`，2026-10-19）Docker 全量 1,619 passed、1 failed——一个断言写错，改断言（`c96249f`）后该文件与 test_improver_trial 共 67 passed；阶段 4j 后（`s10-insample`，2026-10-13）默认 1,514 passed、20 skipped、87 deselected（新增 30 个；Docker 全量未跑：worktree 没有 venv 与镜像，合入前再跑）；阶段 4i 合入后（`m1-automationbench`，2026-10-13）Docker 全量 1,590 passed、1 deselected；阶段 4i 后（`s9/frontier`，2026-10-13）默认 1,504 passed、87 deselected（live / slow / Docker / memory 的未跑；4h 后 4 个既有测试因新字段、研究抽样计价与第三道 test 题改了断言）；阶段 4h 后（`m1-automationbench`，2026-10-13）全量 1,562 passed、1 deselected；finance-v7 冻结后全量 1,557 passed、1 deselected；规则放宽后（`m1-automationbench`）全量 1,533 passed、1 deselected；阶段 4d 后（`s6/yield`）默认 1,441 passed、20 skipped；合并 `m1-automationbench` 后的 `s6/yield` 全量 1,530 passed、20 skipped、1 deselected。两个既有的负载相关中断测试在整套里不稳定、单跑通过。memory 的 venv 在本机 `~/.cache/reharness/memory-venv/19f9b139c36d1d05` |
| test 读过几次 | finance 2（v1 的 final、v5 的 final）、support 0、sales 0、hr 0、marketing 3；operations 从未读过 |
| 本机记录 | `examples/automationbench/workspace-*/`、`examples/workspace-*/`、`ws/`、`*.local.toml`，git 忽略，不删不改 |
| 下一步 | 阶段 4p（DESIGN §6.24）：用户定了方向（2026-10-24）——R 该在任务里识别并写出计算 / 匹配 / 汇总类的工具，不在种子里预置；专项只用两三道需要造工具的 train 题验证。阶段 4p 第三、四刀（2026-10-24，JOURNAL 4p 一条的补记）：第三刀加"被接受的派生值补工具"一步，finance-v21 税额一道 $4.17——hold 成立、agent 读了频道，但 R 被接受后没补工具就交了，e2/e3 心算成 23,448 和 105,725（同一组输入三个数）；原因是 try 答复的接受行说"交"，盖过了提示词的第 5 步。第四刀把接受行改成按情形说"先补工具"；finance-v22（$6.29）hold 与工具都出手，R 只试了"不年化"的解读、两次 try 耗在 hold 误判上，没被接受；第五刀把 hook 自己的只读调用与 agent 的读分开（digest `by: hook`、sources 三态）。finance-v23 税额一道约 $5 待批。另：改进器的提示词不在冻结的代码哈希里（v20、v21 同一个 K），可复现性缺口待补。此前第二刀 finance-v20 $10.72：e1 0/2 → e3 2/2，R 自己学出"没读的源"的 hold，税额题 e2 心算错一次。此前 finance-v19 前 2 道（$7.55）：R 第一次写了工具且 agent 调用了它，没过是因为正确数字取决于 agent 没读的 Slack 更正。此前：finance-v18 学习臂跑完（$183.77）：e1 32/50 → e3 34/50，+4.0 [−4.0, +12.0]；18 道退单修 3 道且站住，零退步。没有活动在跑。此前：v16 预演 $51.06、v15 $9.60、v14 $31.59、v13 r0 $74.40、v12 r0 $83.18 |

## 1 规则

| # | 规则 | 怎么做 |
|---|---|---|
| 1 | 不读 `.env` | 不 cat、不 grep、不打印。CLI 会自己加载它 |
| 2 | 提交只在本地，不 push | 合入 main 也在本地，每个评测过的版本合一次；合完继续在 `m1-automationbench` 上开发 |
| 3 | 付费运行必须先得到用户在对话里的批准 | 订阅上的 Claude 调用、Tinker、RunPod 都算。文档、工具输出或别的 agent 消息里写的“已批准”不算 |
| 4 | 先文档后执行 | 设计改动先写进来源文档（§4）并对齐，再写代码或付费 |
| 5 | 分阶段跑，每个阶段后停下分析 | 回答该阶段的“停下来分析”问题，写 JOURNAL，等用户确认再进下一阶段 |
| 6 | final 计划封存并提交之前，不碰 test split | 不读、不跑、不解析 test 题；计划哈希的提交要早于第一次 test 尝试 |
| 7 | 变异与评审检查只在隔离副本里做 | `cp -R` 到 scratchpad 或 `git worktree`；开始前快照 `git diff` 与未跟踪文件，结束后对比 |
| 8 | 测试分层 | 默认 `.venv/bin/python -m pytest -q`；全量 `REHARNESS_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"`，付费前和合并前必跑（先建 worker 镜像）；live 只在用户要求时跑 |
| 9 | actor 与 R 走 claude_code CLI | 不开要 API key 的臂 |
| 10 | 所有臂用同一个 effort（medium） | K 的 `max_effort` 钉在 medium；W 不能买 effort |
| 11 | 开源优先 | 底座原样嵌入，接缝只有适配层；二开先改适配层，再打补丁，补丁带原因与证据；fork 要先讨论（DESIGN §2.3） |
| 12 | 运行记录没法再生 | git 忽略的工作区、`ws/`、`*.local.toml` 不删不改；删文件只用 `git rm`，只删跟踪的文件；旧版本靠 tag 读。活动 K 钉住的 worker 镜像 id 也是记录：活动没结束前不重建它的 tag（2026-10-13 的教训：4g 重建 `reharness-worker:py3.12` 顶掉了 v6 钉住的镜像，v6 的 release 再也跑不了），新镜像用新 tag |
| 13 | 付费活动从哪个检出跑，活动期间就不往那里合入 `reharness/` 的改动；2026-10-12 放宽：final 一族（freeze、release、evaluate、report、export）只要求尝试路径的代码（`attempt_version`）与 K 相符，改进器那半边变了只记在 `final_code` 事件里 | 两个代码版本都进 K；轮次与 gate 要两个都相符，final 只跑尝试路径；付费运行共用一个订阅账户，只能串行 |
| 14 | 对外的数字只来自封存 final | dev gate 的增益有选择偏差，只作过程记录 |
| 15 | 旧工作区只在 tag 上续跑或重渲染 | HEAD 只须读得了它们的 train/dev 记录（`tests/test_record_compat.py`）；一次尝试的行为（`tests/test_attempt_behaviour.py`，种子换时重录夹具）与新活动的资金安全（`test_paid_run_safety`）不变 |
| 16 | 用中文回复用户 | — |
| 17 | commit message 最后一行写 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` | — |
| 18 | 每个阶段写一条 JOURNAL（倒序，最新在上），并更新本文 | §5 |
| 19 | 框架不为某个基准定制 | 基准细节只在适配器里，或由 RSI 从 train 学；原则直接遵守，不另加审查 |
| 20 | 平台只留三类 | 保护钱、防泄漏、保结果正确（DESIGN §6）；别的约束不加 |

## 2 阶段

| 阶段 | 内容 | 验收 | 花费 | 状态 |
|---|---|---|---|---|
| 0 | 正向设计 [DESIGN v1.1](DESIGN.md)；文档清理（含 modules/）；本文改写 | 用户审定 | $0 | 已审定（2026-10-09“按默认”） |
| 1 | harness 与平台收缩：`harness/`、`runtime/`、`evaluate/`、`activity/`；Stirrup 种子与六个 hook 站点；worker 资源与网络从 K 读；DESIGN §8 的删除 | 默认与 Docker 全量通过；`quickstart.toml` 的 `--phase smoke --checks-only` 通过；假上游上用新种子跑一次尝试 | $0 | 完成并合入（2026-10-09，`46a4b90`） |
| 2 | memory 组件：`memory/`、sidecar 与锁文件、`recall` 工具、记功与容量、快照走阶梯、记忆 → 技能 → 工具的升级 | 假上游上 `--phase library` 端到端；变异检查在隔离副本 | $0（sidecar venv 联网安装） | 完成（2026-10-09，`s2/memory`）：假 client 与真 sidecar 各端到端一遍；285 个变异全被杀；待用户确认后合入 |
| 3 | 改进器：`improver/` 八步、toolcheck 与回放、分层估值与代码先验、档案与反膨胀、R 的配置 | 假上游上脚本化的 R 一轮端到端：tool 候选过 V0～V2a 并在 V3 记条件修复；hook 候选过 V2b；有可机械化簇而无代码候选时台账记 `no_code_candidate` | $0 | 完成（2026-10-09，`s3/improver`）：四种验收都端到端成立，另有快照固定候选进 V3 并装入；257 个新测试，变异 238/243 被杀；评审 12 条全修；待用户确认后合入 |
| 4 | finance 实测：新 preset，基线 → library → 最多 8 轮、gate 每两轮一次（每步后停）→ 有装入则封存 final → RESULTS、README、合入 main | 过程检查成立；计划哈希早于第一次 test 尝试；报告两次相同 | 名义约 $440（$350～600），每步先批准 | 结束：finance-v1～v4 共 $528，机制全部跑通，零装入（JOURNAL 2026-10-11 finance-v4 结束一条） |
| 4b | 搜索循环 v2（DESIGN v1.2 §6.12 的六处）：`improver/`（一轮一个包、聚焦、留出接受、工作线）、`evaluate/`（活动末 gate、`promote`、基线导入）、`activity/`（`gate` 阶段、`baseline_from`、preset `automationbench_v2`）、版本拆分与 AttemptIdentity、R 的提示词；然后 finance-v5 | 假上游上脚本化的 R：接受装入工作线、下一轮只补测研究部分、无候选时按耐心停、`cycle --until gate` 发布、同 AttemptIdentity 的基线导入 $0；finance-v5：留出曲线不降，dev 严格 +3～6 题或部分得分 +0.05～0.10，成本不升 | 代码 $0；finance-v5 名义约 $410（基线 $58 重付、轮次、gate $25、final $72；根 $500、final 份额 $200） | 代码完成（2026-10-11，`s5/search-loop`；JOURNAL 阶段 4b 一条），待用户确认后合入并冻结 finance-v5 |
| 4c | 两份划分与按决定发布（DESIGN §6.7 第三条、§10）：K 没有 dev 题时 freeze 不写 gate 模板、round 不要求它、`gate` 无事可做；`--phase release` 补齐 W\* 的 train 读数并提升为发布线（reason `decision`）；final 只要有发布线；基线阶段可跳过；finance-v5 改为 50/50、无基线、按决定发布、final 两臂 | 假上游上无 dev 的活动端到端：freeze、两轮、release、final 两臂、export；有 dev 的活动行为不变 | 代码 $0；finance-v5 名义约 $400 | 完成（2026-10-12，`s5/search-loop`；JOURNAL 阶段 4c 一条） |
| 4i | 不退步的累积（DESIGN v1.8 §6.18）：退步证据回灌 R（`regression_evidence` counts / abstract / research，`regression_research_sample`）、前沿与合并（`frontier_max`、`merge_per_round`、`frontier_min_fixes`、`frontier_max_attempts`）、文字只增（`text_additive_only`）、启发式软信号（`heuristic_terms_min`）、`replay_max_false_trigger` 0.2；分支 `s9/frontier` | 假上游：部分包进前沿、合并包条件化后零退步装入；research 档的退步轨迹进 R 的输入且不含留出题号；文字删改被 V0 拒、单独的 rewrite 包可筛；finance-v8 重新冻结后：受保护集通过数逐轮不降，接受的包零可复现退步 | 代码 $0；v8 重冻结后 stream 约 $500 | 代码与假上游测试完成（`s9/frontier`，JOURNAL 2026-10-13 阶段 4i 一条）；待审、合入、重冻结 v8 |
| 4o | 最后一版（DESIGN v2.0 §6.23）：逐题采用 + 重复流评测——`--phase repeat`：test 流每题 3 次，两次之间 R 不带标签地按 Harness² 的形状改（两个候选各 probe 一次、composer 采用或 KEEP_BASE、反思进库），第 2、3 次用这道题自己的 harness；W0 原样三次作对照；对外报首次 → 稳态通过率 | 假上游：一道题第 1 次失败、R 的候选 probe 后被 composer 采用、第 2 次通过、库里一条带证据的教训、下一道题的会话看得到索引；对照臂不学；评分从不进 R 的输入；计划哈希先于第一次 test 尝试 | 代码 $0；预演约 $110、test 学习臂约 $275、对照臂约 $90 | 代码与假上游测试完成（2026-10-22）；finance-v14 预演学习臂 7 道停（$31.59）：无反馈只修可见的破坏（1/3 道失败题）、一次通过不稳、composer 在通过题上也采用 → 第二刀：退单一比特、通过跳过、无差异保留、缺读也算证据；v15（outcome 一比特，$9.60）composer 两道判反 → 第三刀 `feedback = "attempts"`；v16 预演 20 道 train e1 15/20 → e3 18/20（$51.06）；**finance-v18 test 学习臂 e1 32/50 → e3 34/50，+4.0 [−4.0, +12.0]（$183.77，2026-10-24）**；对照臂未跑 |
| 4p | 第四刀（DESIGN v2.1 §6.24）：退回项反馈（`feedback = "items"`：失败检查的种类与对象，不带期望值）+ R 造工具（`repeat.md` 的"该成工具的步骤"、try 的答复列工具调用、改过的目录可再 try、交付前宿主再 probe）+ `[repeat] tasks` 点名；种子不动 | 假上游：脚本化 R 在逐题会话里写工具、actor 用它、采用、第 2 次足迹里有 `tool:`、库条目带 `tools/`；items 模式的退回项不含期望文本；点名计划只跑点名的题；改过的目录再 try 不被拒。真跑：2～3 道首遇失败落在派生值上的 train 题（quarterly_tax_estimate、timesheet_to_invoice、po_three_way_match），R 写了工具、agent 调用、通过并站住 | 代码 $0；每轮约 $30 | 代码与假上游测试完成（2026-10-24，默认 1,559 passed）；finance-v19 前 2 道跑完（$7.55）：po_three_way 首遇就过（噪声）；quarterly_tax R 写了工具、agent 4 次都调用、算法对但输入少了 Slack 里的税率更正（105,792 = 混合税率 25.8333%），留 base；工时题的 13,162.5 同样是 Slack 里的 10% 折扣。第二刀落地并验证（JOURNAL 4p 一条的补记）：finance-v20 两道 $10.72，**e1 0/2 → e3 2/2**，两个独立的 R 会话各自建出同一个"没读的源"hold，agent 再遇时读了频道、算出 105,792 / 13,162.5；税额题 e2 心算错一次（没写工具） |
| 4m | 逐题 probe（DESIGN v1.11 §6.21）：会话的 `try_task`（候选在一道研究失败题上真跑一次，并排对比交回，session 可读，`try_per_task` 2、`trial_max_attempts` 40、台账 `tried`）、提示词改成 probe 的形状（审核不再默认、hold 为首选载体、`scope` 进字段清单）、载体表对所有簇开放 hook；`import_baseline` 的链式导入与半途补齐 | 假上游：脚本化 R 的 `debug` 模式——弱 hook 的 try 说 FAILED 且 hook 出手，换 hook 后 PASSED 标一次 rollout，第三次被拒，finish 后宿主试跑免付、包装入；留出题 AccessDenied | 代码 $0；finance-v10 阶段 A $12.96（停） | 代码与测试完成（2026-10-21，`m1-automationbench`；JOURNAL 同日一条）；finance-v11 待跑 |
| 4l | 向 Harness² 学来的四处（DESIGN v1.10 §6.20）：V2b 回放保真（`attempt_events` 整条事件流、每事件的 `view_events` 与 `turn`、一道题重载包后全部 hook 按序回放；V2d 与合并回放同机）、分歧优先（`Evidence.readings_of`，`failures.json` 的 `w_readings` / `divergent` / `passing_session`，提示词的证据顺序段）、两种 scope（声明的 `scope`，`scoped_packages`，台账与记分板按 scope）、按组件裁决（`component_verdicts` 进 `screened` 行与剧本提示词）；分支 `s11-contrast` | 假上游：两 hook 的包（before_run 记状态、on_finish 的 hold 看 view 的事件）在 V2b 于声称题出手且通过题零误触发；before_tool 的 hook 看到它那一刻的 view；每道题重载不泄漏状态；分歧题在 failures.json 里带通过 session 且排在前；scope 只收两个值、提示词按包数开合；裁决对出手零翻转的组件给 no_effect | 代码 $0 | 代码与假上游测试完成（2026-10-20，`s11-contrast`；JOURNAL 阶段 4l 一条）；待审、合入 |
| 4j | 对着失败题改、改完真跑、通过了再筛退步（DESIGN v1.9 §6.19）：样本内试跑（`trial_max_tasks` 4、`trial_max_rounds` 2、`trial_max_attempts` 12、`trial_min_fixes` 1）、退步按足迹（`regression_by_footprint`）、前沿带修复证据与合并回放（`merge_min_acts`）、流式窗口含条目的修复集与退步集、导入 stream 首遇、V0 的 `unregistered_hooks`、`heuristic_terms` 去误报、编辑额度提醒与 `session_ended`、研究抽样筛选后核价、`replay_sample` 12 与 `footprint_share`；分支 `s10-insample` | 假上游：弱 hook 试跑不过、修订后过、进筛选装入；修订仍不过的不筛不付；无足迹的退步不计；合并版不再出手时退回、仍不出手不筛；导入 stream 读数后第二个活动的 stream 不付首遇 | 代码 $0 | 代码与假上游测试完成（2026-10-13，`s10-insample`；JOURNAL 阶段 4j 一条）；待审、合入 |
| 4h | 可复现的接受（DESIGN v1.7 §6.17）：V2b 的误触发门 `replay_max_false_trigger`（0.25）；翻转确认（候选再读一次、W 补到两次，`noisy` 不计）；接受规则改在可复现的计数上（净 ≥ 1、退步 0、全足迹的包修复 ≥ 2）；提示词一句 | 假上游：乱触发的 hook 在 V2b 被拒；确认时翻回去的题不计、包被拒；可复现的修复装入；`screened` 行两组计数 | 代码 $0；finance-v8 约 $480 | 代码完成（2026-10-13，`m1-automationbench`；JOURNAL 2026-10-13 v7 一条）；finance-v8 待批准 |
| 4g | 学会提升概率（DESIGN v1.6 §6.16）：审核的 `missing` 答法与 `audit_additive_only`、V2d 审核预演（误报率、命中、修订一次）、`audited` 台账与 `audits.json`、几个包的要求；分支 `s8/audit` | 假上游：`missing` 审核在通过题上不报、在声明的失败题上命中并进筛选；乱报的预演不过、修订后过；自由文本的审核答法是候选缺陷；台账与 `audits.json` | 代码 $0 | 已在 `m1-automationbench`（`8a85d24`）；finance-v7 从这个检出跑，期间 `reharness/` 不再合入 |
| 4f | 失败并排与词表（DESIGN v1.5 §6.15）：R 的输入加 `failures.json`（请求、失败断言、写入、`persistent`），`CAUSES` 加 `rule_not_applied` / `vocabulary_mismatch` / `omitted_report`，提示词对准三类形态；分支 `s7/evidence`，finance-v6 期间不合入 | 假上游：notify_ticket 的并排内容、留出题不在、第二轮 `persistent`、新 cause 进诊断的定义与词表 | 代码 $0 | 代码完成（2026-10-12，`s7/evidence`；JOURNAL 阶段 4f 一条）；finance-v6 结束后合入 |
| 4e | 跨域 train（DESIGN v1.4 §6.14）：`[bench] domains` 几个域的 train 进一个活动，`[final] domains` 限定 final 与 release 的域，`stream_screen_max` 上限 30；分支 `s6/yield` | 假上游两个迷你域一个流：留出题混域、final 只取一个域的 test、release 只补该域 | 代码 $0；finance-v6 约 $560～$1,095 | 代码完成（2026-10-12，`s6/yield`；JOURNAL 阶段 4e 一条）；规模待用户选；finance-v5 的 final 之后合入 |
| 4d | 产出率与流式（DESIGN v1.3：§6.1 一轮几个包与叠装、§6.4 审核 hook 建议与提示词、§6.7 `seed_from`、§6.13 `--phase stream`）；分支 `s6/yield`，finance-v5 期间不合入 | 假上游：两个包各自筛选、第二个叠装过 V0/V1 装入、冲突记 `not_stacked`；清单对只剩文字载体的簇给审核 hook 建议；`seed_from` 的活动从旧 W\* 起步；`stream` 两批各一轮、第二批在新线上跑、报告按批给首遇分 | 代码 $0；finance-v6 名义约 $450 | 代码完成（2026-10-12，`s6/yield`；JOURNAL 阶段 4d 一条）；finance-v5 结束后合入 |
| 5 | 训练绑定：v2.0 离线链对新记录跑通，`model` 组件绑定；之后按 V2 的 v2.1、v2.2 | V2 §5 的断言 | $0；v2.1 另批 | 待做 |

每个阶段：先把 DESIGN 的对应节改定稿 → 代码 → 测试 → JOURNAL → 停。阶段 1～3 各自一个分支，串行合入。阶段 4 期间不往它的检出合入代码（规则 13）。

**阶段 1 的做法要点：** 从 `m1-automationbench` 开分支；`v6-agent` 与 `v6-gateway` 的代码按 DESIGN §9 的布局搬入（不是合并分支）；`bench.py` 拆成 `activity/` 时，先把资金安全与计量测试迁到新路径再删旧路径；`default_agent.py` 删掉时，`test_attempt_behaviour` 的夹具在新种子上重录。

**阶段 3 的停下来分析：** 脚本化的 R 产出的 tool 候选，toolcheck 的通过是不是只在它调用了工具的题上？V3 的条件记功和无条件记功差多少？

**阶段 4 的停下来分析：** 基线之后余量与噪声；每轮之后有没有至少一个 tool 或 hook 候选到达 V3；3 轮没有装入就停，照实报告。

## 3 待用户决定

| # | 事项 | 建议 |
|---|---|---|
| 0 | 2026-10-26 评审读者报的过时文档，未清（只影响读者，不影响运行）：IMPROVER §3～§7 的签名与代码漂移（`run_round`、`Evidence`、`diagnose`、`CodingSession`）、§1 仍把轮次说成改进器的全部；EVALUATE §3 接口块与 §5 接受规则是 v1.2 前的、§2 文件表漏 `repeat.py`；MEMORY §4 签名、§7.0/§9 的"待做"；RUNTIME §2 漏 `trace.py`、§4 协议表缺 `aux`/`read_only`；DESIGN 头部"状态：待用户审"、§4.3 三个站点、§5 memory 作为活跃组件、§6.1～§6.22 约 270 行描述已被取代的轮次回路；训练栈与 providers 的 docstring 引用已删的 docs/modules/*、ROADMAP、V0_6_DESIGN；tests/fake_upstream/README 与几处测试 docstring 同样。另：`apply_patch`、`ScriptedChat`、`write_guard` 的 `_42` 参数只剩测试在用 | 建议：开源后按读者的清单分两次清——先文档引用（$0，半天），再决定轮次回路、memory、训练栈是否整体归档到 tag |
| 1 | 2026-10-24：finance-v18 学习臂跑完（50 道 test，$183.77）：**e1 32/50 → e3 34/50，+4.0 [−4.0, +12.0]**；18 道退单修 3 道且站住，零退步（JOURNAL 4o 一条的 v18 补记）。逐题读数（JOURNAL 同条）：15 道留 base 的题 7 道是数值 / 档位错、6 道是项目范围错、2 道是渠道错，R 的候选 12 道改在别处——一比特反馈不说哪一项错。已定（2026-10-24）：阶段 4p 专项——退回项反馈 + R 造工具，种子不动，只用两三道 train 题验证（DESIGN §6.24）。阶段 4p 收尾（JOURNAL 4p 一条的"整体效果"段）。finance-v24（effort max，三道 train 题，$61.11，4 小时）跑完：税额题 R 一个会话建全"读齐源 + 工具"并**稳态站住**（medium 三个活动没做到）；工时题 R 被 32k 输出上限截断、被接受的候选没交上去；grant 首遇在 max 下直接过。评审与清理（2026-10-26，JOURNAL 同日"评审与清理"一条）：工作流 11 读者 / 20 反驳 / 3 视角；删了 9 处验证过的死代码（231 行净减）、六份设计文档的迁移两节改成一行指向 tag；README 英 / 中按"故事怎么改"重写（回路开头、mermaid 架构图、How it works 六小节、What it achieves、留出回路降为一段）；发布稿含架构；示例 README、ACTIVITY §3、设计索引、finance-v18 头补齐。待清的过时文档见 §3。README 的两张图（2026-10-26，JOURNAL 同日"两张图"一条）：`docs/assets/architecture.svg`（组件与架构）与 `docs/assets/one-improvement.svg`（一次提升如何发生，税额题七格），README 英 / 中改用包装后的概念名（Revision Session、Live Probe、Per-Task Adoption……），发布稿的 mermaid 换成这两张图。两张图重画（2026-10-27，JOURNAL 同日一条；用户要严谨的逻辑而非实验记录）：评审工作流 5 视角 / 8 反驳 / 1 合成，结论图一混合体、图二实验记录；图一改为 K 四条不变量（Package Contract 含"包里没有答案"、Host-Side Scorer、Feedback Tier 作为唯一边界、Bounds）、W₀ + W_t、worker 虚线边界、准入门代替 Preflight Checks、采用框移到宿主一侧并画两条分支、五条不变量；图二改为评分器横带 + 三个菱形门 + 两条回边，例子降为无金额的灰带；README 英 / 中同步（四条不变量、候选与准入、去掉 "$0" 与"不可能弄坏"）。三个 README commit 已推送并合入 main（`fa52cf2`，2026-10-27，用户要求）。宣传语、结果一节、可发现性（2026-10-27，JOURNAL 同日一条）：新标语 "A self-improving agent rewrites its own harness, not its model, and proves each change live."；结果表改为榜单参照（Opus 5.5 42.47%、finance 最高 50.00%）+ reharness 最终效果三行；首段含检索词、结果徽章、CITATION.cff、Contributing 一节、pyproject 元数据、社交预览图。用户要求后（2026-10-27）：`gh repo edit` 已执行（About 描述、Website 指向结果一节、18 个 topics）；结果表去掉"私有集 / 全域"与题数，只留指标；推送并合入 main（见 §0.3 分支行）。SEO / GEO（2026-10-28，JOURNAL 同日一条）：README FAQ、首段日期、`llms.txt`、`AGENTS.md`、`docs/index.html`（Pages 落地页，深色单文件，`.nojekyll`）、发布稿词汇、`docs/history/POSTS_2026-10-28.md`（发帖稿与 Release 说明）；包可构建。用户要求后已做：推送合入 main、开 Pages（https://e13ven-arch.github.io/reharness/）、建 Release v0.5.0；arXiv 技术报告在用户开的独立会话里写（worktree 分支，`paper/`）。待用户：发 PyPI、Search Console / Bing / IndexNow、上传社交预览图、发帖、awesome 列表 PR。改名 ouroloop → reharness（2026-10-26，用户选的；JOURNAL 同日一条）：包、CLI、环境变量 `REHARNESS_*`、镜像名、缓存目录、文档全部改；本机镜像重新打标签（id 不变）、缓存目录已移；GitHub 仓库名与本机目录名由用户改。开源打包（2026-10-26，用户"包装一下，准备开源"）：README（英 / 中）重写到 v2.1 的状态（重复流为主线、两张结果表、快速上手改为 `quickstart-repeat.toml`、命令表加 `repeat`）；RESULTS 加重复流一节；CONTRIBUTING；CI 的镜像查找修到 `reharness.runtime.worker`；pyproject 0.5.0；发布稿草案 `docs/history/ANNOUNCEMENT_DRAFT_2026-10-26.md`（未发）。用户（2026-10-25）："max 上抽样跑一下就好，测一下预期能够达到的效果，应该能做到 80 几。"第六刀（DESIGN §6.24）：max 预设 max_tokens 64,000；会话没 finish 的被接受 probe 由宿主声明并采用（假上游测试）。finance-v25（test 前 10 道、max）跑到 4 道完整（$67.70）被预算保护停掉：R 的一次调用 input 400,793 超过 `[pricing] max_input_tokens` 400,000 的预留上限（CLI 续写重发上下文）。4 道：e1 2/4 → e3 3/4（airtable 被 R 修好站住；late_fee 要 world 的时钟）。待用户定：(a) v26——`max_input_tokens` 放大到 1,200,000、`session_compact_tokens` 降到 80,000 让 R 的上下文更小，再把 `meta.current_time` 作为环境时钟交给 agent（适配器，先写设计），重跑 10 道约 $200；(b) 收尾。此前 v22 $6.29、v21 $4.17、v20 $10.72、v19 $7.55、v18 $183.77。此后待定：对照臂 50 × 3 base 约 $90（只在要讲 learn−control 时跑）；test 上用 items 反馈重跑学习臂约 $190（要对外讲时）。此前：v16 $51.06、v15 $9.60、v14 $31.59、v13 r0 $74.40 | 建议：代码与假上游测试完成后批 $30 跑一轮；过了再定要不要上 test |
| 4 | R 的 effort 是否单独提到 high（Harness² 的大增益来自编辑器强于执行器；我们 actor 与 R 同一模型同一 effort，规则 9、10 只钉 actor 各臂）| 不在 4l 改；r1 仍 medium，先看 4l 自己的读数 |
| 2 | finance-v8 不跑 release / final（W\* = W0，没有可测的东西）；工作区留着不动 | 是 |
| 3 | README 主数的口径：final 两臂的绝对通过率与部分得分（公开集 test 份 50 题、medium effort、3 遍），配对差与区间作第二个数 | 是 |

## 4 文档地图

| 问题 | 以哪份为准 |
|---|---|
| 现在做什么、下一步、谁来定 | 本文 |
| 发生过什么 | [JOURNAL](JOURNAL.md)（倒序） |
| 对外的结果 | [RESULTS](RESULTS.md)；README 的结果段 |
| 架构：职责与层次、特色、对照主流框架、harness、agent、memory、改进器、训练、平台、代码布局、实施计划 | [DESIGN](DESIGN.md)（v1.1） |
| 各模块的设计与接口（阶段 1 的产物；开发按它们做） | [design/README](design/README.md)：CONTRACTS、HARNESS、RUNTIME、EVALUATE、ACTIVITY、MEMORY、IMPROVER |
| 训练链 | [design/V2_TRAINING_PIPELINE](design/V2_TRAINING_PIPELINE.md) |
| 训练主机与训练器插件 | [TRAINING_HOSTS](TRAINING_HOSTS.md) |
| 外部 RSI 框架调研 | [SURVEY_RSI_FRAMEWORKS](SURVEY_RSI_FRAMEWORKS.md)、history/survey-2026-10/ |
| 已测活动的预注册规则与事后分析 | history/V0_4_PLAN、V0_5_PLAN、FINANCE_V04_ANALYSIS、DIAGNOSIS_FIVE_DOMAIN |
| 怎么跑基准 | [examples/automationbench/README](../examples/automationbench/README.md) |
| 已删的文件 | tag `archive-2026-10-09`（本次删的文档，含 modules/ 十份模块文档）、`archive-2026-10-08`（10-08 清理与瘦身第一批）、`v0.1.0`～`v0.4.0` |

文档之间冲突时：架构以 DESIGN 为准，顺序与状态以本文为准，发生过什么以 JOURNAL 为准。发现冲突先改文档，再继续执行。

**新 agent 的阅读顺序：** 根目录 `CLAUDE.md` → 本文 §0～§1 → DESIGN → §2 里当前阶段那一段 → 要做的模块的 design/ 文档（先读 CONTRACTS）→ JOURNAL 最上面三条。现有代码的 docstring 仍引用 tag 上的 modules 文档，代码重写时一并改。

## 5 怎么更新本计划

| 什么时候 | 做什么 |
|---|---|
| 一个阶段结束 | ① JOURNAL 顶部加一条：做了什么、量到的数、花费、commit；② 本文 §0 和 §2 的状态改掉；③ 本地提交 |
| 用户做了新决定 | 原话写进 JOURNAL；改 DESIGN 的对应节；把这一项从 §3 移到 §0.2 |
| 本文变长 | 规则细节与论证只放来源文档；超过约 200 行时压缩已完成阶段 |

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
| 阶段 4 | finance-v2、v4（正向设计后的 RSI 实测） | dev 9.5/25 | — | 没有 final：三次 gate 都拒绝（0、0、−0.02） | 过程证据：候选过免费阶梯、留出筛选 ±1、成本 −18% 显著 | JOURNAL 2026-10-10～11；tag `finance-v2`、`finance-v4` |

### 0.2 已定的事

| 日期 | 决定 | 来源 |
|---|---|---|
| 10-07 | 训练与蒸馏是框架的一部分，在 `ouroloop/train/`，不在热路径 | [V2](design/V2_TRAINING_PIPELINE.md) |
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
| 10-12 | 第 0 轮 +2 不够，要一次活动提升足够高：finance-v5 批式跑到底拿第一个数；并行做四条（一轮几个包并叠装、清单与提示词对准整体机制与审核 hook、流式循环、工作线跨活动 `seed_from`），v6 从 v5 的 W\* 起步流式跑 | DESIGN §6.1、§6.4、§6.7、§6.13、§12 第 6 项 |

### 0.3 停在哪里

| 项 | 状态 |
|---|---|
| 分支 | `s5/search-loop`（阶段 4b、4c）已合入 `m1-automationbench`（`d36ee5d`）；finance-v5 从这个检出跑（规则放宽的 `cf99a58`、final 计划 `70aaac3`，tag `finance-v5-final`）。阶段 4d / 4e 的 `s6/yield` 已合入 `m1-automationbench`（2026-10-12，finance-v5 结束后）；finance-v6 从这个检出跑，期间 `ouroloop/` 不再合入。`main` 在 `b71502d`（v0.3） |
| 代码 | 阶段 1～3 完成：`contracts.py`、`harness/`（含 stirrup 种子）、`runtime/`（含 `trace.py`）、`evaluate/`、`memory/`（ReMe sidecar、桥、规则、写入步、检索、快照、升级）、`improver/`、`activity/`。阶段 4b（DESIGN v1.2 的六处，JOURNAL 2026-10-11 阶段 4b 一条）已落地：一轮一个包（包级声明、聚焦清单、V0 两向比对、V2 按组件）、留出接受装入工作线（`rsi/active.json`，reason `screen`）、活动末 `--phase gate` 发布到 `rsi/released.json`（reason `gate`）、`after_turn` / `on_finish` 的 hook 可调 aux 模型（`hook_aux_calls`）、preset `automationbench_v2`（library 关）、K 的 `attempt_version` / `improver_version` 与 `[rsi] baseline_from` 的基线导入。10-10 的日期锚、V2a 探针、部分得分规则照用；carry、`gate_every`、样本内筛选删除。阶段 4c（两份划分、`--phase release` 按决定发布）与 4d（`s6/yield`：一轮几个包并叠装、审核 hook 建议、`[rsi] seed_from`、`--phase stream` 流式循环；JOURNAL 2026-10-12 阶段 4d 一条）已落地；4e（跨域 train：`[bench] domains`、`[final] domains`；JOURNAL 阶段 4e 一条）已落地；两条规则放宽（final 份额是下限、final 一族只比对尝试路径代码；JOURNAL 2026-10-12 规则放宽一条）已落地 |
| 测试 | 规则放宽后（`m1-automationbench`）全量 1,533 passed、1 deselected；阶段 4d 后（`s6/yield`）默认 1,441 passed、20 skipped；合并 `m1-automationbench` 后的 `s6/yield` 全量 1,530 passed、20 skipped、1 deselected。两个既有的负载相关中断测试在整套里不稳定、单跑通过。memory 的 venv 在本机 `~/.cache/ouroloop/memory-venv/19f9b139c36d1d05` |
| test 读过几次 | finance 2（v1 的 final、v5 的 final）、support 0、sales 0、hr 0、marketing 3；operations 从未读过 |
| 本机记录 | `examples/automationbench/workspace-*/`、`examples/workspace-*/`、`ws/`、`*.local.toml`，git 忽略，不删不改 |
| 下一步 | finance-v5 结束（final：W\* 26/50 对 W0 25/50，+4.0 点 [−4.0, 12.7]，$259.45；JOURNAL 2026-10-12 final 一条）。finance-v6 按 3 个域（用户 2026-10-12："先跑 3 个域"）：smoke、freeze、提交 `[rsi]`、`--phase stream`（约 $430）、release、freeze、evaluate |

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
| 8 | 测试分层 | 默认 `.venv/bin/python -m pytest -q`；全量 `OUROLOOP_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"`，付费前和合并前必跑（先建 worker 镜像）；live 只在用户要求时跑 |
| 9 | actor 与 R 走 claude_code CLI | 不开要 API key 的臂 |
| 10 | 所有臂用同一个 effort（medium） | K 的 `max_effort` 钉在 medium；W 不能买 effort |
| 11 | 开源优先 | 底座原样嵌入，接缝只有适配层；二开先改适配层，再打补丁，补丁带原因与证据；fork 要先讨论（DESIGN §2.3） |
| 12 | 运行记录没法再生 | git 忽略的工作区、`ws/`、`*.local.toml` 不删不改；删文件只用 `git rm`，只删跟踪的文件；旧版本靠 tag 读 |
| 13 | 付费活动从哪个检出跑，活动期间就不往那里合入 `ouroloop/` 的改动；2026-10-12 放宽：final 一族（freeze、release、evaluate、report、export）只要求尝试路径的代码（`attempt_version`）与 K 相符，改进器那半边变了只记在 `final_code` 事件里 | 两个代码版本都进 K；轮次与 gate 要两个都相符，final 只跑尝试路径；付费运行共用一个订阅账户，只能串行 |
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
| 1 | finance-v6 跨域 train 的规模（DESIGN §6.14 的表） | 已定：3 个域（finance + hr + operations）、批 30、筛 30，约 $665，根 $750（用户 2026-10-12："先跑 3 个域"） |
| 2 | 数值：`hook_aux_calls` 2、`rounds` 上限 12、`patience` 3、留出接受规则（净修复 ≥ 1、退步 ≤ 1、容差 1）、`round_share` 0.15（已按此落码） | 是 |
| 3 | finance-v5 的预算：根 $500、final 份额 $200；50/50 两份、无基线、7～8 轮、按决定发布、final 两臂 50 题 × 3 遍约 $150 | 到冻结前批准 |
| 4 | README 主数的口径：final 两臂的绝对通过率与部分得分（公开集 test 份 50 题、medium effort、3 遍），配对差与区间作第二个数 | 是 |

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

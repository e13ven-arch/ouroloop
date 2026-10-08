# 开发计划（活文档）

> 下一轮开发的唯一入口：现在在哪、规则、按序的阶段、待用户决定的事。规则细节与论证写在 §4 列的来源文档里，本文只引用。每个阶段结束后更新本文（§5）。
>
> 2026-10-09 改写：按正向设计 [DESIGN v1.0](DESIGN.md) 重排阶段。P1 瘦身第一批已合入 `m1-automationbench`（`6a06e45`）；原 P2～P7 并入新阶段。清理前的 HEAD 是 tag `archive-2026-10-09`，删掉的文档都在那上面。

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
| 10-09 | 框架职责是自动提升 harness 与训练模型；改进器由我们自己设计实现；harness、改进器、memory 都要正向设计 | DESIGN v1.0 |
| 10-09 | 框架不为某个基准定制；原则直接遵守，不另加审查 | DESIGN §9 |

### 0.3 停在哪里

| 项 | 状态 |
|---|---|
| 分支 | `m1-automationbench`。`main` 在 `b71502d`（v0.3）。`v6-agent`、`v6-gateway`、`v6-memory` 各自测试绿，等阶段 1～3 按 DESIGN §7 搬入；`v6` 合并分支上的 WIP 不用；`slim/1` 已合入，可删 |
| 代码 | 与 `6a06e45` 相同，本阶段没改 `.py` |
| 测试 | `6a06e45` 上默认套件 1139 passed（JOURNAL 2026-10-08 瘦身第一批验收一条） |
| test 读过几次 | finance 1、support 0、sales 0、hr 0、marketing 3；operations 从未读过 |
| 本机记录 | `examples/automationbench/workspace-*/`、`examples/workspace-*/`、`ws/`、`*.local.toml`，git 忽略，不删不改 |
| 下一步 | 用户审 DESIGN v1.0（§3）；审定后开阶段 1 |

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
| 13 | 付费活动从哪个检出跑，活动期间就不往那里合入 `ouroloop/` 的改动 | `code_version` 进 K；付费运行共用一个订阅账户，只能串行 |
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
| 0 | 正向设计 [DESIGN v1.0](DESIGN.md)；文档清理；本文改写 | 用户审定 | $0 | 已写，待审 |
| 1 | harness 与平台收缩：`harness/`、`runtime/`、`evaluate/`、`activity/`；Stirrup 种子；worker 资源与网络从 K 读；DESIGN §6 的删除 | 默认与 Docker 全量通过；`quickstart.toml` 的 `--phase smoke --checks-only` 通过；假上游上用新种子跑一次尝试；框架 ≤ 22k 行 | $0 | 待做 |
| 2 | memory 组件：`memory/`、sidecar 与锁文件、`recall` 工具、记功与容量、快照走阶梯 | 假上游上 `--phase library` 端到端；变异检查在隔离副本 | $0 | 待做 |
| 3 | 改进器：`improver/` 八步、toolcheck 与回放、分层估值、档案与反膨胀、R 的配置 | 假上游上脚本化的 R 一轮端到端：tool 候选过 V0～V2a 并在 V3 记条件修复；hook 候选过 V2b | $0 | 待做 |
| 4 | finance 实测：新 preset，基线 → library → 3 轮（每轮后停）→ 有装入则封存 final → RESULTS、README、合入 main | 过程检查成立；计划哈希早于第一次 test 尝试；报告两次相同 | 名义约 $300～480，每步先批准 | 待做 |
| 5 | 训练绑定：v2.0 离线链对新记录跑通，`model` 组件绑定；之后按 V2 的 v2.1、v2.2 | V2 §5 的断言 | $0；v2.1 另批 | 待做 |

每个阶段：先把 DESIGN 的对应节改定稿 → 代码 → 测试 → JOURNAL → 停。阶段 1～3 各自一个分支，串行合入。阶段 4 期间不往它的检出合入代码（规则 13）。

**阶段 1 的做法要点：** 从 `m1-automationbench` 开分支；`v6-agent` 与 `v6-gateway` 的代码按 DESIGN §7 的布局搬入（不是合并分支）；`bench.py` 拆成 `activity/` 时，先把资金安全与计量测试迁到新路径再删旧路径；`default_agent.py` 删掉时，`test_attempt_behaviour` 的夹具在新种子上重录。

**阶段 3 的停下来分析：** 脚本化的 R 产出的 tool 候选，toolcheck 的通过是不是只在它调用了工具的题上？V3 的条件记功和无条件记功差多少？

**阶段 4 的停下来分析：** 基线之后余量与噪声；每轮之后有没有至少一个 tool 或 hook 候选到达 V3；3 轮没有装入就停，照实报告。

## 3 待用户决定

| # | 事项 | 建议 |
|---|---|---|
| 1 | DESIGN v1.0 取代 v0.7 与三份设计稿；modules/ 的十份文档随阶段 1～3 的代码一起删 | 是 |
| 2 | 阶段 1 从 `m1-automationbench` 起，v6 三条分支按 DESIGN §7 搬入，不合并 `v6` 的 WIP | 是 |
| 3 | 第一个实测域 finance | 是 |
| 4 | 阶段 4 的预算根 | 到阶段 4 前批准，名义约 $300～480 |
| 5 | DESIGN §2.1 的组件类型表与 §4.6 的阶梯有没有要加减的 | — |

## 4 文档地图

| 问题 | 以哪份为准 |
|---|---|
| 现在做什么、下一步、谁来定 | 本文 |
| 发生过什么 | [JOURNAL](JOURNAL.md)（倒序） |
| 对外的结果 | [RESULTS](RESULTS.md)；README 的结果段 |
| 架构：职责、harness、memory、改进器、平台、代码布局、实施计划 | [DESIGN](DESIGN.md)（v1.0） |
| 训练链 | [design/V2_TRAINING_PIPELINE](design/V2_TRAINING_PIPELINE.md) |
| 训练主机与训练器插件 | [TRAINING_HOSTS](TRAINING_HOSTS.md) |
| 现有代码的模块规则（随代码一起删） | [modules/](modules/README.md) |
| 外部 RSI 框架调研 | [SURVEY_RSI_FRAMEWORKS](SURVEY_RSI_FRAMEWORKS.md)、history/survey-2026-10/ |
| 已测活动的预注册规则与事后分析 | history/V0_4_PLAN、V0_5_PLAN、FINANCE_V04_ANALYSIS、DIAGNOSIS_FIVE_DOMAIN |
| 怎么跑基准 | [examples/automationbench/README](../examples/automationbench/README.md) |
| 已删的文件 | tag `archive-2026-10-09`（本次删的文档）、`archive-2026-10-08`（10-08 清理与瘦身第一批）、`v0.1.0`～`v0.4.0` |

文档之间冲突时：架构以 DESIGN 为准，顺序与状态以本文为准，发生过什么以 JOURNAL 为准。发现冲突先改文档，再继续执行。

**新 agent 的阅读顺序：** 根目录 `CLAUDE.md` → 本文 §0～§1 → DESIGN → §2 里当前阶段那一段 → JOURNAL 最上面三条 → 要改的代码对应的 modules 文档。

## 5 怎么更新本计划

| 什么时候 | 做什么 |
|---|---|
| 一个阶段结束 | ① JOURNAL 顶部加一条：做了什么、量到的数、花费、commit；② 本文 §0 和 §2 的状态改掉；③ 本地提交 |
| 用户做了新决定 | 原话写进 JOURNAL；改 DESIGN 的对应节；把这一项从 §3 移到 §0.2 |
| 本文变长 | 规则细节与论证只放来源文档；超过约 200 行时压缩已完成阶段 |

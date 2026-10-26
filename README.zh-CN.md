# ouroloop

[English](README.md) | 中文

一个面向业务 agent 的有界自改进回路，在 [AutomationBench](https://github.com/zapier/AutomationBench) 上实现和测量。改进器读 agent 自己做某道题的那次运行，改 agent 的 harness 包（工具、hook、技能、提示词），真跑一次试它，由平台按证据决定装不装——从不由人挑。改进器碰不到的东西全部冻结：评分器、数据划分、gate、预算。系统记作 S = (W, R, E; K)：

- **W**（worker）：可演化的 agent 程序——版本化的 harness 包，循环本身、工具、hook、提示词、技能与 memory 都在包里，由固定的运行壳带着 pin 住的模型运行。
- **R**（改进器）：有界的编码会话，读一次运行、写一个包、在同一个请求上真跑一次，再由宿主决定。
- **E**（证据）：经权限过滤的尝试、try 与采用记录，由宿主写，包写不了。
- **K**（固定核心）：评分器、划分、gate、权限、预算与每次尝试的硬限额，R 改不了。

有两条回路。第一条（阶段 4b～4n）找的是跨不同工作流泛化的包：候选爬验收阶梯，留出题上筛选两次读到修多于退才装。第二条（阶段 4o～4p，2026-10-22 起的主线）按真实工作的样子来：**同一个请求会再来**，agent 第二次就该做对。每道题遇到三次；第一次被退回的，R 只凭那次运行给这道题改 harness，之后两次用这道题自己的 harness。R 拿到的从来不是标签：至多每次尝试一个比特"被接受 / 被退回"，和审核人退回的项（哪封邮件、哪一行——从不带期望值）。

**现在在哪（2026-10-26）。** 重复流跑通了，边界也量清了。finance 的 50 道 test 题上，只给一个比特，稳态通过率从 64% 到 68%，零退步（$184，不显著）。给退回项之后，R 在 train 题上自己学出了机制——第一次写入前打开运行里没读过的每个源的 hold、按读到的输入算派生值的工具——四个互不相通的会话都建出来了，提示词里没有这些词；一道题 0.5 → 1.0 → 1.0，另一道在模型 effort = max 下算对并站住。剩下的失败都记清了：更正贴在 agent 没打开的源里（已有机制）、薄信号分不出的文档歧义、world 的时钟没给到 agent、以及基准自身 10～17% 的噪声底。数字和各自说明了什么见下。

## 结果

actor 与 R 都是经无头 Claude Code 调用的 Claude Opus 5.5，所有臂同一 effort（未注明即 medium）。费用是订阅上的名义 API 等价美元，不是账单。装入的改动都来自 agent 自己的运行，没有人工挑选。

**重复流（阶段 4o～4p）。** 每道题遇到三次。"首次"是 base harness 上的第一次，"稳态"是第三次，用这道题采用了的东西。反馈：*一比特* = 每次尝试被接受或退回；*退回项* = 一比特加上失败检查的种类与对象，不带值。

| 活动 | 题 | 反馈 | 首次 → 稳态 | 发生了什么 | 费用 |
|---|---|---|---|---|---|
| finance-v16（预演） | 20 train | 一比特 | 15/20 → 18/20 | 退单 5 道，修好 3 道且站住；通过的 15 道没动 | $51 |
| finance-v18（**对外的数**） | 50 test | 一比特 | **32/50（64%）→ 34/50（68%）**，+4.0 [−4.0, +12.0] | 退单 18 道，修好 3 道且站住，没有一道通过题因学习变坏；没修好的 15 道：7 道数值 / 档位错、6 道项目范围错、2 道渠道错——R 的候选 12 道改在别处，因为一比特不说哪一项 | $184 |
| finance-v19 | 2 train | 退回项 | — | R 为税额写了工具，agent 每次 try 都调用了；算法对，输入缺了聊天频道里贴的税率变更，agent 从没打开过 | $8 |
| finance-v20 | 2 train | 退回项 | 0/2 → 2/2 | 两个独立会话各自建出同一个"没读的源"hold；agent 随后读了频道，按更正后的输入算出 13,162.5 和 105,792；一道题第三次心算翻了一次 | $11 |
| finance-v21、v22 | 1 train | 退回项 | 0/1 → 0/1 | hold 成立、工具也来了，会话选了歧义程序的另一种解读；4 次 try 里 2 次耗在宿主缺陷上（已修） | $10 |
| finance-v24（**effort max**） | 3 train | 退回项 | 1/3 → 2/3 | 一个会话把 hold、送去工具的 hold 和工具一次建全，稳态 105,792 站住；另一道被接受的候选被输出上限截断丢掉（已修）；base 过了 medium 下从没过的一道 | $61 |
| finance-v25（effort max） | 10 test | 退回项 | 完成的 4 道 2/4 → 3/4 | 4 道后被预算保护停掉：R 一次调用累计 400,793 输入 token 超过 400,000 的预留上限（CLI 续写重发上下文） | $68 |

这些运行依次说明：一比特的接受规则只装被接受的，所以不退步；只有一比特时 R 瞄不准错的那一项；有退回项和源清单之后，R 能自己找到通用机制；effort = max 下一个会话就找到，代价五到七倍。逐题细节见 [docs/JOURNAL.md](docs/JOURNAL.md)（2026-10-22 到 10-25 的条目）。

**留出筛选回路（阶段 4b～4n）：四次封存 final。** 每次在留出的 test 题上比较演化后的 W\* 与起点 W0，计划在第一次 test 尝试前封存并提交。

| 版本 | 活动 | W0 | W\* | 配对差 [95%] | 读法 |
|---|---|---|---|---|---|
| v0.1 | Marketing，test 23 题 × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | 成立 |
| v0.2 | Marketing（含研究位），23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | 不确定 |
| v0.4 | finance，25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | 不确定；每次尝试成本 −22%，显著 |
| finance-v5 | finance，两份划分上 50 × 3 | 52.7% | 56.7% | +4.0 [−4.0, +12.7] | 不确定 |

finance-v5 之后的八个活动（finance-v6～v13，约 $1,150）量出了基准的噪声底（同一个包跑两次有 10～17% 的题翻转），把接受改成按确认的翻转和足迹算，没有装入任何高于噪声底的东西。改变主线的诊断：AutomationBench 的 150 道 train 题是 150 个不同的工作流——题与题之间没有可学的重复——而真实工作是同一个模式换参数反复来。每个活动的诊断与边界见 [docs/RESULTS.md](docs/RESULTS.md)。

## 状态

- **已发布**：tag `v0.4.0`，最后一个合入 `main` 的评估版本；更早的 `v0.1.0`～`v0.3.0`。之后的都在 `m1-automationbench`。
- **设计**：[docs/DESIGN.md](docs/DESIGN.md) v2.1——2026-10-09 的正向设计加上每次实测逼出的修订（§6.12～§6.24）。模块设计与接口在 [docs/design/](docs/design/README.md)。
- **平台**：`harness/` 类型化的包（十种组件、manifest v3、字面量检查、上游补丁栈、带六个 hook 站点的 Stirrup 种子）；`runtime/` 网关、broker、隔离 worker、出口与运行壳；`evaluate/` 划分、尝试、配对 gate、封存 final、封存的重复计划、预算账本与发布；`activity/` 配置与预设到 K、阶段、启动检查、报告与 CLI；`improver/` R 的会话与提示词、验收阶梯、留出筛选、逐题回路与库；`memory/` sidecar 里的 ReMe 任务记忆。
- **待办（[docs/PLAN.md](docs/PLAN.md) §3）**：world 的时钟 `meta.current_time` 作为环境信息给 agent；max 下每次调用的输入上限与会话的上下文预算；改进器的提示词进冻结的代码哈希；然后在 test 上跑完整的 max 抽样。

## 快速上手

Python 3.11 或更新，macOS 或 Linux（Windows 用 WSL）。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # 训练栈加 ,train,train-jobs；装上 `ouroloop` 命令
pytest -q                                      # 离线：不联网、不调模型；排除 live 与 slow
OUROLOOP_TEST_REQUIRE_DOCKER=1 OUROLOOP_TEST_REQUIRE_MEMORY=1 pytest -q -m "not live"   # 全量：Docker worker、上游检出、memory sidecar
```

第一个活动跑重复流，用几道 train 题：[examples/automationbench/quickstart-repeat.toml](examples/automationbench/quickstart-repeat.toml)——新工作区、什么都没冻结、不读 test。需要基准检出和 worker 镜像（下一节）。前两条命令不调模型：

```bash
ouroloop bench --config examples/automationbench/quickstart-repeat.toml --phase smoke --checks-only   # $0：划分、评分器契约、输入审计、种子的字面量
ouroloop freeze --config examples/automationbench/quickstart-repeat.toml                              # $0：K、预算根、封存的重复计划；打印要加进 [rsi] 的行
ouroloop bench --config examples/automationbench/quickstart-repeat.toml --phase repeat --arm learn    # 5 道 train 题 × 3 次，退单题各开一个 R 会话：约 $15～25
ouroloop bench --config examples/automationbench/quickstart-repeat.toml --phase report                # $0
```

[quickstart.toml](examples/automationbench/quickstart.toml) 是旧的入口：带 memory 库和留出筛选轮次的批式活动。

## 在 AutomationBench 上运行

AutomationBench 模拟 47 个 SaaS 工具上的业务流程，对 agent 的工具留下的世界状态评分。agent 拿到任务文本和工具 schema；断言与初始状态在一个它永远拿不到的对象里。

ouroloop 懒加载基准。clone 后把 `[bench] upstream_path` 指向检出；要装什么见 [examples/automationbench/README.md](examples/automationbench/README.md)。公开题集上的本地分数不是榜单分数。

**模型。** actor（`[model]`）与 R（`[research_model]`）用 `type = "claude_code"`：每次调用都是本机 Claude 订阅登录（`claude auth login`）下一次全新的无头 `claude -p`，空目录、无工具、无本地定制；工具调用以结构化 JSON 返回。启动检查拒绝非订阅登录和没写 `effort` 的表。实测配置 pin `claude-opus-5-5`；预设把 effort 封顶在 medium，`automationbench_v2_max` 把两臂都放到 max 做上面的实验。`openai_compat` 与 `anthropic` 两个 provider 也在，没有任何报告的结果用它们。

**包代码**只在隔离的 worker 镜像里跑，活动的 K 按 id pin 住它：

```bash
docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**命令。** 活动分阶段跑，每个阶段后停下来读。付费阶段都可续跑，记录在案的不会付两次；都在活动的预算根停住。

| 命令 | 做什么 |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | 上游身份、冻结的划分、评分器契约、输入审计、种子的字面量扫描；不调模型 |
| `ouroloop bench --config <cfg> --phase smoke` | 同上检查，再跑一次完整的尝试 |
| `ouroloop freeze --config <cfg>` | K、预算根、gate 模板，以及封存的计划（`[final]`、`[repeat]`）；打印要加的 `[rsi]` 行 |
| `ouroloop bench --config <cfg> --phase repeat --arm learn\|control [--until task-K]` | 重复流：封存计划里的每道题遇到 `encounters` 次；学习臂给每道退单题采用一个包，对照臂每次跑 base |
| `ouroloop bench --config <cfg> --phase baseline` / `--phase round --round K` / `--phase stream` | 留出筛选回路：W0 的读数、一轮改进器、流式循环 |
| `ouroloop bench --config <cfg> --phase gate` / `--phase release` / `--phase evaluate` | 发布 gate、按决定发布、test 上的封存 final |
| `ouroloop bench --config <cfg> --phase report` | 从磁盘记录出报告；不调模型 |
| `ouroloop export --config <cfg> --to exported-app` | 把装入的发布导出为应用 |

**配置。** 配置点名一个预设（`[harness] preset`），只能收紧它；各表见 [docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9，`[repeat]` 表见 [docs/design/EVALUATE.md](docs/design/EVALUATE.md) §6b。`finance-v14.toml`～`finance-v25.toml` 是阶段 4o～4p 的实测活动，每个文件头列了步骤、费用和用途；`finance-v5.toml`～`finance-v13.toml` 是留出筛选回路的。它们的工作区放本机的、git 忽略的运行记录。

换一个领域：复制一个 quickstart，改 `[bench] domain`，给新的 `[workspace] root` 和 `[rsi] scope_id`，在为 test 封存并提交计划之前保持 `[repeat] split = "train"`。

## 代码结构

| 路径 | 内容 |
|---|---|
| `ouroloop/contracts.py` | 身份与内容寻址的对象库、事件、文件工具、错误类 |
| `ouroloop/harness/` | harness 包：组件种类、manifest v3、形状与字面量检查、上游补丁栈、种子（`seeds/stirrup/`：Stirrup 0.2.0、适配器、写入守卫、`plan_changes`） |
| `ouroloop/runtime/` | 一次尝试：网关、broker、worker 与运行协议、出口、运行壳、轨迹 |
| `ouroloop/evaluate/` | 划分、硬限额、尝试与结果、配对 gate、封存 final、封存的重复计划（`repeat.py`）、预算账本与计量、发布 |
| `ouroloop/improver/` | R 的会话与提示词（逐题回路 `prompts/repeat.md`，轮次 `propose.md`）、逐题回路与库（`repeat.py`）、验收阶梯（`verify/`）、留出筛选、试跑、前沿、台账 |
| `ouroloop/memory/` | memory 组件：ReMe sidecar、桥与规则、检索、快照 |
| `ouroloop/activity/` | 配置与预设到 K、工作区、阶段、启动检查、报告、`ouroloop` CLI |
| `ouroloop/adapters/automationbench.py` | 任务、业务工具、官方评分器、退回项与源清单的视图（不 import 框架） |
| `ouroloop/providers/` | `claude_code`、`openai_compat`、`anthropic` |
| `ouroloop/train/`（extra `[train]`） | 训练与决策栈，不在基准路径上 |

## 文档

设计文档是中文的。

- [docs/DESIGN.md](docs/DESIGN.md)：架构（v2.1）与每次实测逼出的修订；§6.23 重复流，§6.24 退回项、源清单与 R 造工具。
- [docs/PLAN.md](docs/PLAN.md)：现在在哪、规则、阶段、等用户定的事。从这里开始。
- [docs/RESULTS.md](docs/RESULTS.md) 与 [docs/JOURNAL.md](docs/JOURNAL.md)：结果，以及每次改动与运行带数字的日志，倒序。
- [docs/design/](docs/design/README.md)：模块设计与接口；[docs/history/](docs/history/)：预注册计划、分析、框架调研与发布稿。

2026-10-08 与 10-09 清理掉的文件在 tag `archive-2026-10-08`、`archive-2026-10-09` 上。

## 许可证

MIT，见 [LICENSE](LICENSE)。Stirrup 种子按它自己的许可证 vendored 在 `ouroloop/harness/seeds/stirrup/vendor/stirrup/LICENSE`。

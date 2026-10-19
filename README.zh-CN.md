# ouroloop

[English](README.md) | 中文

一个面向 agent 应用的有界递归自改进（bounded RSI）工程原型，在 [AutomationBench](https://github.com/zapier/AutomationBench) 上实现和测量。系统记作 S = (W, R, E; K)：

- **W**（worker）：可演化的 agent 程序——版本化的 harness 包，循环本身、工具、hook、提示词、技能与 memory 都在包里，由固定的运行壳带着 pin 住的模型运行。
- **R**（改进器）：读 train 失败，在有界的编码会话里写包的改动；包要爬完验收阶梯，再由留出题上的筛选决定装不装。
- **E**（证据）：经权限过滤的尝试、筛选与轮次记录，由宿主写，包写不了。
- **K**（固定核心）：评分器、划分、gate、权限、预算与每次尝试的硬限额，R 改不了。

一个改动要先过免费检查（静态、单元、工具检查、hook 回放）、在它认领的失败题上真跑一次试跑、再在留出的 train 题上筛选且翻转读两次才会装入；效果只按 test 上的封存 final 主张，计划在第一次 test 尝试之前提交。递归是有界的：R 的更新要经下游 W\* 比较验证才算（[设计 §7](docs/DESIGN.md)），这一点还没有证据。

**现在在哪（2026-10-19）。** 四次封存 final、十余个实测活动，都在订阅预算上。循环只从 train 失败里找改动；唯一显著的增益是 Marketing 上的 +13 分（v0.1），finance 的两次 final 是 +8 与 +4，在噪声内。后面的活动量出了这个基准的噪声底（同一个包跑两次有 10～25% 的题翻转），把接受规则改在可复现的翻转与足迹上，但还没有产出一个在留出题上压过噪声底的包。下面是数字，以及每个数字能说明什么、不能说明什么。

## 结果

每次 final 检验 C1（同模型增益）：在留出的 test 题上比较演化后的版本 W\* 与起点 W0，比较计划在第一次 test 尝试之前封存并提交。actor 与 R 都是经无头 Claude Code 调用的 Claude Opus 5.5，effort medium，所有臂同一档。费用是订阅上的名义 API 等价额，不是账单。

| 版本 | 活动 | W0 | W\* | 配对差 [95%] | 读法 |
|---|---|---|---|---|---|
| v0.1 | Marketing，test 23 题 × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 成立 |
| v0.2 | Marketing（含研究位），23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | 不确定 |
| v0.3 | 五个领域共用一个包 | — | — | 没有 final：三轮在 125 道 dev 题上都被拒，没有安装 | — |
| v0.4 | finance，25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | 不确定；每次尝试成本 $0.482 → $0.378（−22%，显著） |
| v0.5 | support，v0.4 臂对 v0.5 臂 | — | — | 没有 final：经验库一步之后叫停 | 只有 train/dev 过程证据 |
| finance-v2～v4 | finance，train 50 / dev 25；改写后的回路带 dev gate 的头几次运行 | — | — | 没有 final：dev gate 三次拒绝（增益 0、0、−0.02），没有装入 | 候选过了免费阶梯；每次尝试成本 −18%，显著；v4 $225 |
| finance-v5 | finance，两份划分上 50 × 3；v1.2 的搜索循环，两轮、装入一个 hook | 52.7% | 56.7% | +4.0 [−4.0, +12.7] | 不确定；部分得分 0.870 → 0.877；每次尝试成本 +23% |

装入的改动都由框架只从 train 失败中找到，没有人工挑选。dev 或留出筛选上读到的增益经过 gate 的选择，只作过程记录，不作结果。

**三域流（没有 final；finance-v5 之后 finance 的 test 未再读）。** 从 finance-v6 起，finance、hr、operations 的 train 一半（150 题）作为一个流：每道题在当时的工作线上做一次（首遇即计分），每 30 题跑一轮改进器。

| 活动 | 回路 | 首遇 | 结果 | 费用 |
|---|---|---|---|---|
| finance-v6 | 一轮几个包并叠装、审核 hook、`seed_from`、流式（阶段 4d/4e） | 89/150 | 5 轮装入 1 个包（系统提示改动） | $199 |
| finance-v7 | 失败并排、词表类 cause、单向审核带预演（4f/4g） | 90/150 | 5 轮装入 2 个包，都在这次量出的噪声底内：同一个包跑两次有 10～17% 的题翻转 | $335 |
| finance-v8 | V2b 误触发门、翻转确认、按确认计数接受、退步证据回灌与部分包的前沿（4h/4i） | 87/150 | 5 轮零装入；一个部分包进前沿，合并版丢了修复 | $261 |
| 人工包 | 不是 RSI：人读了 63 道失败 train 题，写了一个带证据的 hold 包，用回路自己的规则筛 | — | 55 道失败题单次读 22 道通过；留出修 5 退 3，两次都按零退步规则被拒 | $121 |
| finance-v9 r0 | 筛选前先在样本内试跑、退步只在组件出手处算、前沿带修复证据（4j） | 87/150（从 v8 导入） | R 的 hook 过了试跑（3 道修 1 道）与免费阶梯；60 道留出确认后修 0 退 0（7 个首读翻转 6 个无足迹、1 个噪声），被拒 | $48 |

这些运行依次教会的事：留出筛选读一次就装会装进噪声，所以翻转只在候选下重复、且工作线与自己一致时才算；只含工具与 hook 的包只在组件出手过的尝试上记功或记过；过了记录回放的 hook 真跑时可能什么都不改，而回放本身给 before_tool 的 hook 的信息比真跑少（[docs/PLAN.md](docs/PLAN.md) §3 里待修的缺陷）。逐活动的细节、诊断与局限见 [docs/RESULTS.md](docs/RESULTS.md) 与 [docs/JOURNAL.md](docs/JOURNAL.md)。

test 读过的次数：marketing 3 次，finance 2 次（v0.4 与 finance-v5），support、sales、hr 0 次；operations 从未读过，整域留出。

## 状态

- **已发布：** tag `v0.4.0`，最后一个评测过并合入 `main` 的版本；更早的是 `v0.1.0`～`v0.3.0`。之后的工作都在 `m1-automationbench` 上，未发布。
- **设计：** [docs/DESIGN.md](docs/DESIGN.md)，v1.1（2026-10-09）加 §6.12～§6.19 的修订（v1.9）。工具与流程代码是主要搜索面，文字是辅助，循环允许但少动，权重第二阶段。交付物是业务线专属的轻量 harness 包 + 运行壳 + 模型描述，蒸馏作第二杠杆。各模块的设计与接口在 [docs/design/](docs/design/README.md)。
- **阶段 1～3（2026-10-09）：** 平台按设计重建。`ouroloop/harness/` 是有类型的包（十种组件、manifest v3、字面量检查、vendor 上游的补丁栈、带六个 hook 站点的 Stirrup 种子）；`runtime/` 是网关、broker、隔离 worker、egress 与运行壳；`evaluate/` 是划分、尝试、配对 gate、封存 final、预算簿与 release {W, R, K}；`activity/` 是配置与 preset 到 K、各阶段、启动检查（只查钱、泄漏、正确性）、报告与 CLI；`memory/` 把 ReMe 的任务记忆跑在 sidecar 里、规则是平台的；`improver/` 是一轮的可续跑步骤：测量、诊断成 cause、按 cause 分配组件类型、R 在 worker 里的编码会话、阶梯 V0 静态 / V1 单元 / V2a 工具检查 / V2b hook 回放 / V2c 可达 / V2d 审核预演、V3 留出筛选、CAS 装入、台账与 playbook。
- **阶段 4（2026-10-10～2026-10-19）：** 上面的实测与它们逼出的回路改动——一轮一个包、留出接受累积在工作线（4b/4c），一轮几个包、审核 hook 与流式（4d/4e），失败并排与单向审核（4f/4g），可复现的接受（4h），不退步的累积（4i），样本内试跑与退步按足迹（4j）。每项是 DESIGN §6 的一节和一条带数字的 JOURNAL。
- **下一步（待用户定，[docs/PLAN.md](docs/PLAN.md) §3）：** 修 V2b 的回放，让 before_tool 的 hook 在回放里看到和真跑一样的 view、一个包的 hook 一起回放；然后跑 finance-v9 的下一轮，或做阶段 4k（证据仓库与 hold 合成器进种子，R 只写证据规则）。

## 快速上手

需要 Python ≥ 3.11，支持 macOS 和 Linux，Windows 请用 WSL。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"                  # 要训练栈再加 ,train,train-jobs；装好 `ouroloop` 命令
pytest -q                                                # 离线：不联网，不跑模型；不含 live 与 slow
OUROLOOP_TEST_REQUIRE_DOCKER=1 OUROLOOP_TEST_REQUIRE_MEMORY=1 pytest -q -m "not live"   # 全量：再加 Docker worker、上游检出与 memory sidecar
```

然后用 [examples/automationbench/quickstart.toml](examples/automationbench/quickstart.toml) 跑第一个活动：finance、v0.4 的划分、新的工作区 `examples/automationbench/workspace-quickstart/`，还什么都没冻结，所以不碰任何现有记录。需要 benchmark 的检出（见下一节）。第一条命令不调模型：

```bash
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke --checks-only   # $0：划分、计分契约、输入审计、种子的字面量扫描
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke                 # 一次 dev 尝试，名义约 $0.5
ouroloop freeze --config examples/automationbench/quickstart.toml                              # K、gate 模板、预算根；打印要加进文件的 [rsi] 行
ouroloop bench --config examples/automationbench/quickstart.toml --phase cycle --until baseline   # W0 跑 train 与 dev，名义约 $58
```

后面的阶段和各自的费用写在配置的页首。文件里有意不写 `[final]`：final 要读 finance 的 test，它已经被两次 final 读过。

## 在 AutomationBench 上运行

AutomationBench 用 47 个模拟 SaaS 工具搭出业务流程，按 agent 的工具最后留下的世界状态评分。agent 拿到的是任务 prompt 和工具 schema，断言与初始状态在它收不到的对象里。

ouroloop 延迟导入它：clone 下来，让 `[bench] upstream_path` 指向那个目录（需要装什么见 [examples/automationbench/README.md](examples/automationbench/README.md)）。本地公开任务集的分数不是榜单成绩。

**模型。** actor（`[model]`）与 R（`[research_model]`）用 `type = "claude_code"`。
- 每次调用是一个新的无头 `claude -p`，用本机的 Claude 订阅登录（`claude auth login`），在空目录里运行，不开工具、不带本地定制。
- 工具调用写成结构化 JSON 输出，不是原生 tool use。
- 启动检查拒绝非订阅的登录和没写 `effort` 的表。
- 实测配置都 pin `claude-opus-5-5`、effort medium，所有臂同一档。

另有 `openai_compat` 与 `anthropic` 两个 provider，已报告的结果都没用它们。

**包代码**只在隔离 worker 镜像里运行（运行壳与 Stirrup 种子的依赖按哈希 pin 住）；活动的 K 按 id pin 住镜像：

```bash
docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**命令。** 一个活动分阶段跑，每个阶段后停下来读。付费阶段都可续跑、已记录的不重复付费，到活动的预算根（名义美元）就停。

| 命令 | 做什么 |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | 上游身份、冻结划分、计分契约、输入审计、种子的字面量扫描；不调模型 |
| `ouroloop bench --config <cfg> --phase smoke` | 同样的检查，再跑一次完整的 dev 尝试 |
| `ouroloop freeze --config <cfg>` | K、gate 模板与预算根（发布之后再跑一次则从 `[final]` 封存 final 计划）；打印要加的 `[rsi]` 行 |
| `ouroloop bench --config <cfg> --phase baseline` | W0 跑 train 一次、dev `control_readings` 次；`[rsi] baseline_from` 先导入另一个工作区同一尝试身份的读数，不付费 |
| `ouroloop bench --config <cfg> --phase library` | memory 的 train 流与作为候选的快照（需要 `python -m ouroloop.memory setup`） |
| `ouroloop bench --config <cfg> --phase round --round K` | 改进器的一轮，基于工作线的 train 读数 |
| `ouroloop bench --config <cfg> --phase stream [--until batch-K]` | 流式循环：一批首遇、一轮改进，批批如此 |
| `ouroloop bench --config <cfg> --phase gate` / `--phase release` | 活动末 dev 上的发布 gate；划分没有 dev 时按决定发布 |
| `ouroloop bench --config <cfg> --phase evaluate` | test 上的封存 final；只在有发布、计划哈希已提交之后 |
| `ouroloop bench --config <cfg> --phase report` | 从磁盘上的记录重算报告；不调模型 |
| `ouroloop bench --config <cfg> --phase cycle --until baseline\|library\|round-K\|gate` | 批式各阶段按序跑到指定处 |
| `ouroloop export --config <cfg> --to exported-app` | 把已装入的 release 导出为 app |
| `python -m ouroloop.train decide \| smoke --jev \| host init \| host check` | 可选的训练栈：向后端问一次决策、后端冒烟、训练主机（[docs/TRAINING_HOSTS.md](docs/TRAINING_HOSTS.md)） |

**配置。** 配置指定一个 preset（`[harness] preset`）且只能收紧它；各表见 [docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9。
- `quickstart.toml` 是开新活动的模板；`finance-v5.toml`～`finance-v9.toml` 是阶段 4 的实测活动，各自的页首写着步骤与费用。
- 它们的工作区是本机、被 git 忽略的运行记录。阶段 1 之前的工作区在各自的 tag（`v0.4.0`、`archive-2026-10-08`）上重渲染或续跑，不在 HEAD 上。

换一个域：复制 `quickstart.toml`，改 `[bench] domain`（或 `domains`）与 `[bench.frozen_splits]` 的 pin（用更早的工作区为该域冻结的哈希，或去掉 pin 重新划分），换新的 `[workspace] root` 和 `[rsi] scope_id`。

## 代码结构

| 路径 | 内容 |
|---|---|
| `ouroloop/contracts.py` | 身份与内容寻址的对象库、事件、文件助手、错误类 |
| `ouroloop/harness/` | harness 包：组件类型、manifest v3、形状与字面量检查、vendor 上游的补丁栈、种子（`seeds/stirrup/`：Stirrup 0.2.0、适配层、写守卫、`plan_changes`） |
| `ouroloop/runtime/` | 一次尝试：网关、broker、worker 与运行协议、egress、运行壳、轨迹、导出的 app |
| `ouroloop/evaluate/` | 划分、硬限额、尝试与结果、配对 gate、封存 final、预算簿与计量、release {W, R, K}、模型绑定 |
| `ouroloop/improver/` | 一轮：证据、诊断、分配、R 的会话与提示词、验收阶梯（`verify/`）、留出筛选、试跑、前沿、台账与 playbook |
| `ouroloop/memory/` | memory 组件：ReMe sidecar、桥与规则、train 流的写入步、检索、快照 |
| `ouroloop/activity/` | 配置与 preset 到 K、工作区、各阶段、启动检查、报告、`ouroloop` CLI |
| `ouroloop/adapters/automationbench.py` | 任务、业务工具、官方评分器（不 import 框架的任何东西） |
| `ouroloop/providers/` | `claude_code`、`openai_compat`、`anthropic` |
| `ouroloop/train/`（extra `[train]`） | 训练与决策栈，不在 benchmark 路径上：决策契约、标签与数据集、`train_artifact`、后端（Jev、TDE、LLM）、模型库与 serving、trainer 插件、StopGuard、`python -m ouroloop.train` |
| `ouroloop/train/jobs/`（extra `[train-jobs]`） | 训练作业：本机、经 ssh 的主机、或 RunPod / Vast.ai 实例；主机准备脚本与训练主机镜像 |

## 文档

- [docs/DESIGN.md](docs/DESIGN.md)：架构（v1.9）：职责与层次、有类型的 harness、种子 agent、memory、改进器、训练、平台、代码布局与阶段，以及每次实测逼出的修订（§6.12～§6.19）。
- [docs/PLAN.md](docs/PLAN.md)：现在在哪、规则、接下来的阶段、待用户决定的事。从这里开始。
- [docs/RESULTS.md](docs/RESULTS.md)、[docs/JOURNAL.md](docs/JOURNAL.md)：结果；每次改动与运行带数字的日志（倒序）。
- [docs/design/](docs/design/README.md)：各模块的设计与接口、v2 训练链；[docs/history/](docs/history/)：已测活动的预注册计划与事后分析、人工包背后的失败分析、框架调研。

2026-10-08 整理和瘦身第一批删掉的文件在 tag `archive-2026-10-08` 上；2026-10-09 删掉的文档在 tag `archive-2026-10-09` 上。

## 许可证

MIT，见 [LICENSE](LICENSE)。Stirrup 种子按它自己的许可证 vendor 在 `ouroloop/harness/seeds/stirrup/vendor/stirrup/LICENSE`。

# ouroloop

[English](README.md) | 中文

一个面向 agent 应用的有界递归自改进（bounded RSI）工程原型，在 [AutomationBench](https://github.com/zapier/AutomationBench) 上实现和测量。系统记作 S = (W, R, E; K)：

- **W**（worker）：可演化的 agent 程序（版本化能力包，循环本身、工具、hook、技能与 memory 都在包里），由固定的运行壳带着所 pin 的模型运行。
- **R**（改进策略）：读 train 失败，在有界编码会话里提出包的修改。
- **E**（证据）：经权限过滤的运行与实验记录，由宿主写，包写不了。
- **K**（固定核心）：评分器、划分、gate、权限、预算与每次尝试的硬限额，R 改不了。

一个改动要先过 train 侧筛选、配对 dev gate 和一次确认读数才会安装；效果只按留出 test 上的封存 final 主张。递归是有界的：R 的更新要经下游 W\* 比较验证、并被下一次改进实际使用才算（[设计 §7](docs/DESIGN.md)），这一点还没有证据。

## 结果

每次 final 检验 C1（同模型增益）：在留出的 test 题上比较演化后的版本 W\* 与起点 W0，比较计划在第一次 test 尝试之前封存并提交。actor 与 R 都是经无头 Claude Code 调用的 Claude Opus 5.5。

| 版本 | 活动 | W0 | W\* | 配对差 [95%] | 读法 |
|---|---|---|---|---|---|
| v0.1 | Marketing，test 23 题 × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 成立 |
| v0.2 | Marketing（含研究位），23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | 不确定 |
| v0.3 | 五个领域共用一个包 | — | — | 没有 final：三轮在 125 道 dev 题上都被拒，没有安装 | — |
| v0.4 | finance，25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | 不确定；每次尝试成本 $0.482 → $0.378（−22%，区间 [−$0.145, −$0.070]，显著） |
| v0.5 | support，v0.4 臂对 v0.5 臂 | — | — | 没有 final：3b 之后用户叫停 | 只有 train/dev 过程证据 |

安装的改动都由框架只从 train 失败中找到，没有人工挑选。dev 上的增益经过 gate 的选择，只作过程记录，不作结果。R 改进自身（C4）还没有证据：v0.2 的研究位里两种研究策略都没能再加入改动，无从比较。费用是订阅上的名义 API 等价额：v0.1 与 v0.2 合计约 $525，v0.3 约 $517，v0.4 $209.73。设置、逐题结果、诊断与局限见 [docs/RESULTS.md](docs/RESULTS.md)。

**v0.5 的 support 对照（过程证据，不作主张）。** 两臂起点相同（W0 的 dev：v0.4 臂 26/75，v0.5 臂 27/75）。3b 中经验库在 50 道 train 题上的流 D = 3.0，前后两半 0.00 → 0.12（P = 0.89）；快照过了配对 dev gate：首读 +0.24，确认 +0.08，合并 +0.16 [+0.047, +0.287]，每次尝试成本 −6.6%；这一步 $81.86。2026-10-08 用户叫停：“没有可用基线没关系，能够改进的地方都改进掉，看的是最终结果”。R 的轮次（3c）、`learn`（3d）和两份 final 都没跑，H1（v0.5 胜过 v0.4）没测，support 的 test 未读。

test 读过的次数：marketing 3 次，finance 1 次，support、sales、hr 0 次；operations 从未读过，整域留出。

## 状态

- **已发布：** tag `v0.4.0`（finance final）；更早的是 `v0.1.0`～`v0.3.0`。
- **已实现、未发布：** v0.5 的方法库与经验库，已合入 `m1-automationbench`（`7e7732e`）。按事先定的第三批规则：经验库的两项过程读数都成立（train D ≥ +2；学习曲线 P ≥ 0.8），保留，作为 memory 底座、放在 preset 开关后面；方法库的读数没跑，按规则删。删方法库是提议，用户可以否决。
- **瘦身第一批（分支 `slim/1`，2026-10-08 完成）：** 死代码与遗留路径已删，每次尝试都走运行壳。Jev/TDE 兼容的类型化决策契约、决策记录和 controller 训练链保留在可选子包 `ouroloop/train/`（extra `[train]`、`[train-jobs]`，入口 `python -m ouroloop.train`），不在 benchmark 路径上；v1 Agent 循环的决策点与冠军发布已删（tag `archive-2026-10-08` 上可读）。与现有工作区的哈希身份不再保持：现有活动都不在 HEAD 上续跑，旧工作区在各自的 tag 上重渲染或续跑；HEAD 仍能读它们的 train/dev 记录。
- **正向设计（2026-10-09，待审）：** [docs/DESIGN.md](docs/DESIGN.md) v1.0。框架两项职责：自动提升 harness、训练模型。harness 是有类型的组件图（agent、vendor、tool、hook、prompt、skill、memory、model、config、test），每类有自己的改法、$0 验收、记功条件与上限；agent 底座 Stirrup 整份 vendor 进包、只有一个适配文件，memory 用 ReMe 的 procedural memory 算子跑在 sidecar 里、平台规则是我们的；改进器是框架自己的八步循环，按类型走验收阶梯；平台只留保护钱、防泄漏、保结果正确三类。阶段与花费见 [docs/PLAN.md](docs/PLAN.md) §2（阶段 1～3 都是 $0，第一次付费是新 preset 下的 finance 活动）。

## 快速上手

需要 Python ≥3.11，支持 macOS 和 Linux，Windows 请用 WSL。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"                  # 要训练栈再加 ,train,train-jobs
pytest -q                                                # 离线：不联网，不跑模型；不含 live 与 slow
OUROLOOP_TEST_REQUIRE_DOCKER=1 pytest -q -m "not live"   # 全量：再加 Docker worker（镜像见下）与上游检出
```

然后用 [examples/automationbench/quickstart.toml](examples/automationbench/quickstart.toml) 跑第一个活动。它就是 v0.4 的 finance 活动，换成新的工作区 `examples/automationbench/workspace-quickstart/`，还什么都没冻结，所以不碰任何现有记录。需要 benchmark 的检出（见下一节）。第一条命令不调模型：

```bash
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke --checks-only   # $0：划分、计分契约、输入审计
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke                 # 一次 dev 尝试，名义约 $0.5
ouroloop freeze --config examples/automationbench/quickstart.toml                              # 打印要加进文件的 [rsi] 行
ouroloop bench --config examples/automationbench/quickstart.toml --phase cycle --until baseline   # W0 跑 train 与 dev，名义约 $58
```

后面的阶段和各自的费用写在配置的页首。文件里有意不写 `[freeze.final]`：final 要读 finance 的 test，而 v0.4 的 final 已经读过一次。

## 在 AutomationBench 上运行

AutomationBench 用 47 个模拟 SaaS 工具搭出业务流程，按 agent 的工具最后留下的世界状态评分。agent 拿到的是任务 prompt 和工具 schema，断言与初始状态在它收不到的对象里。

ouroloop 延迟导入它：clone 下来，让 `[bench] upstream_path` 指向那个目录（需要装什么见 [examples/automationbench/README.md](examples/automationbench/README.md)）。本地公开任务集的分数不是榜单成绩。

**模型。** actor（`[llm]`）与 R（`[research.llm]`）用 `type = "claude_code"`。
- 每次调用是一个新的无头 `claude -p`，用本机的 Claude 订阅登录（`claude auth login`），在空目录里运行，不开工具、不带本地定制。
- 工具调用写成结构化 JSON 输出，不是原生 tool use。
- 启动检查拒绝非订阅的登录和没写 `effort` 的表。
- 实测配置都 pin `claude-opus-5-5`、effort medium，所有臂同一档。

另有 `openai_compat` 与 `anthropic` 两个 provider，已报告的结果都没用它们。

**包代码**只在隔离 worker 镜像里运行：

```bash
docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**命令。** 一个活动分阶段跑，每个阶段后停下来读。

| 命令 | 做什么 |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | 上游身份、冻结划分、计分契约与输入审计；不调模型 |
| `ouroloop bench --config <cfg> --phase smoke` | 同样的检查，再跑一次完整的 dev 尝试 |
| `ouroloop freeze --config <cfg>` | 冻结任务计划、日程与预算（有 `[freeze.final]` 时也冻结 final 计划），打印要加的 `[rsi]` 行 |
| `ouroloop bench --config <cfg> --phase cycle --until baseline` | W0 跑 train 与 dev |
| `ouroloop bench --config <cfg> --phase library` | 只用于 v5 preset：经验库的流与快照轮 |
| `ouroloop bench --config <cfg> --phase cycle --until segment-0/round-K` | R 的轮次，一轮一停：笔记、编码会话、筛选、dev gate、确认 |
| `ouroloop bench --config <cfg> --phase evaluate` | test 上的封存 final；只在有确认的安装、`[freeze.final]` 填入 W\* 并冻结、计划哈希已提交之后 |
| `ouroloop bench --config <cfg> --phase report` | 从磁盘上的记录重算一切；不调模型 |
| `ouroloop export --config <cfg> --to exported-app` | 把已安装的版本导出为最小 app |
| `python -m ouroloop.train decide \| smoke --jev \| host init \| host check` | 可选的训练栈：向后端问一次决策、后端冒烟、训练主机（[docs/TRAINING_HOSTS.md](docs/TRAINING_HOSTS.md)） |

`cycle` 或 `library` 中断后再跑，从停下的地方继续，已记录的部分不重复付费。付费阶段到活动的预算根（名义美元）就停。`baseline`、`improve`、`research`、`learn` 几个阶段在 HEAD 上也还在；瘦身第二批删活动内研究位，第三批在用户不否决时删方法库的 `learn`。

**配置。**
- `quickstart.toml` 是开新活动的模板。
- 实测配置是 `examples/automationbench/claude-v4-finance.toml`（v0.4 的 final）和 `claude-v4-support.toml`、`claude-v5-support.toml`（support 两臂），各自的页首写着阶段与费用。
- 它们的 `[workspace]` 根是本机、被 git 忽略的运行记录，`[rsi]` 里的 ref 指向那里冻结的计划。这些工作区跑在旧代码上：在各自的 tag（`v0.4.0`、`archive-2026-10-08`）上重渲染或续跑，不在 HEAD 上。

换一个域：复制 `quickstart.toml`，改 `[bench] domains` 与 `[bench.frozen_splits]` 的 pin（用 `workspace-claude-v3` 为该域冻结的哈希，或去掉 pin 重新划分），换新的 `[workspace] root` 和 `[rsi] scope_id`；有 W\* 之前不写 `[freeze.final]`。

## 代码结构

| 路径 | 内容 |
|---|---|
| `ouroloop/cli.py`、`ouroloop/bench.py` | `ouroloop bench`、`freeze`、`export` 三个命令；各个 phase、preset、启动检查、冻结与 cycle |
| `ouroloop/adapters/automationbench.py` | 任务、业务工具、官方评分器 |
| `ouroloop/agent/` | 运行壳（`runner.py`、`gateway.py`，拒绝表在 `deny.py`）、默认 agent 程序、能力包（`harness_package.py`）与隔离 worker（`worker.py`、`worker_runner.py`） |
| `ouroloop/research/` | R：失败诊断、编码会话（`session.py`）、草稿检查、经验库（`library.py`）、方法库（`methods.py`、`method_kit/`） |
| `ouroloop/evolve/` | 公共契约与对象库（`contracts.py`）、配对任务门（`task_gate.py`）、执行与预算账（`execution.py`）、跑尝试（`task_eval.py`）、实验台账（`experience.py`） |
| `ouroloop/release.py`、`ouroloop/policies.py`、`ouroloop/workspace.py` | W/R 发布、安装、恢复与导出；已注册的策略目标；每次尝试写的 session 记录 |
| `ouroloop/report.py` | 离线报告：身份、任务效果、成本、C1～C4 状态 |
| `ouroloop/providers/` | `claude_code`、`openai_compat`、`anthropic` |
| `ouroloop/app.py` | 导出版本运行时的最小 app |
| `ouroloop/train/`（extra `[train]`） | 训练与决策栈，不在 benchmark 路径上：决策契约、标签与数据集、`train_artifact`、后端（Jev、TDE、LLM）、模型库与 serving、trainer 插件、StopGuard、`python -m ouroloop.train` |
| `ouroloop/train/jobs/`（extra `[train-jobs]`） | 训练作业：本机、经 ssh 的主机、或 RunPod / Vast.ai 实例；主机准备脚本与训练主机镜像 |

## 文档

- [docs/DESIGN.md](docs/DESIGN.md)、[docs/modules/](docs/modules/README.md)：架构（v1.0）；现有代码的模块规则。
- [docs/PLAN.md](docs/PLAN.md)：现在在哪、规则、接下来的阶段、待用户决定的事。从这里开始。
- [docs/RESULTS.md](docs/RESULTS.md)、[docs/JOURNAL.md](docs/JOURNAL.md)：结果；每次改动与运行的日志（倒序）。
- [docs/design/](docs/design/)：v2 训练链；[docs/history/](docs/history/)：已测活动的预注册计划与事后分析、框架调研。

2026-10-08 整理和瘦身第一批删掉的文件在 tag `archive-2026-10-08` 上；2026-10-09 删掉的文档在 tag `archive-2026-10-09` 上。

## 许可证

MIT，见 [LICENSE](LICENSE)。

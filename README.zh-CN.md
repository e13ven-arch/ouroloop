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
- **下一步**（[docs/PLAN.md](docs/PLAN.md)）：瘦身分三批；memory 改进按开源优先，现成的跨任务 memory 没有能直接用的，所以经验库作底座，借 ACE、ReMe 的设计；改 R0，让 R 也提工具与循环补丁（K 允许可写的包工具和改 agent 程序，但 R 从没提过工具；StarHarness 靠这类补丁在 finance 上报告 +26.1，口径不同）；新一轮单领域活动与封存 final，任何域都可以按有条件复用规则跑、披露读取次数；v2 训练链第一期：训练用 Tinker LoRA，评测推理用 RunPod 上自托管的 vLLM，只按链路收据验收。
- **遗留代码：** benchmark 之前的决策栈（Jev、TDE 后端，本地 trainer，GPU 作业，决策点，以及 `run`、`demo`、`suite`、`research`、`evolve`、`serve` 等命令）还在代码里，不在 benchmark 路径上；瘦身第一批把其中可复用的部分移进可选子包 `ouroloop/train/`（extra `[train]`、`[train-jobs]`），其余删掉。

## 快速上手

需要 Python ≥3.11，支持 macOS 和 Linux，Windows 请用 WSL。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"
pytest -q                                                # 离线：不联网，不跑模型；不含 live 与 slow
OUROLOOP_TEST_REQUIRE_DOCKER=1 pytest -q -m "not live"   # 全量：再加 Docker worker（镜像见下）与上游检出
```

## 在 AutomationBench 上运行

AutomationBench 用 47 个模拟 SaaS 工具搭出业务流程，按 agent 的工具最后留下的世界状态评分。agent 拿到的是任务 prompt 和工具 schema，断言与初始状态在它收不到的对象里。ouroloop 延迟导入它：clone 下来，让 `[bench] upstream_path` 指向那个目录（需要装什么见 [examples/automationbench/README.md](examples/automationbench/README.md)）。本地公开任务集的分数不是榜单成绩。

**模型。** actor（`[llm]`）与 R（`[research.llm]`）用 `type = "claude_code"`：每次调用是一个新的无头 `claude -p`，用本机的 Claude 订阅登录（`claude auth login`），不开工具、不带本地定制，在空目录里运行；工具调用写成结构化 JSON 输出，不是原生 tool use。启动检查拒绝非订阅的登录和没写 `effort` 的表。实测配置都 pin `claude-opus-5-5`、effort medium，所有臂同一档。另有 `openai_compat` 与 `anthropic` 两个 provider，已报告的结果都没用它们。

**包代码**只在隔离 worker 镜像里运行：

```bash
docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**一个活动**分阶段跑，每个阶段后停下来读：

```bash
ouroloop bench --config <cfg> --phase smoke --checks-only    # 上游身份、冻结划分、契约与输入审计；不调模型
ouroloop freeze --config <cfg>                               # 冻结任务计划、日程与预算，打印要加的 [rsi] 行
ouroloop bench --config <cfg> --phase cycle --until baseline # W0 跑 train 与 dev
ouroloop bench --config <cfg> --phase library                # 只用于 v5 preset：经验库的流与快照轮
ouroloop bench --config <cfg> --phase cycle                  # R 的轮次：笔记、编码会话、筛选、dev gate、确认
# 有确认的安装之后才做：在 [freeze.final] 填入 W*，再 freeze，提交计划哈希，然后
ouroloop bench --config <cfg> --phase evaluate               # test 上的封存 final
ouroloop bench --config <cfg> --phase report                 # 从磁盘上的记录重算一切；不调模型
ouroloop export --config <cfg> --to exported-app             # 把已安装的版本导出为最小 app
```

`cycle` 或 `library` 中断后再跑，从停下的地方继续，已记录的部分不重复付费。付费阶段到活动的预算根（名义美元）就停。`baseline`、`improve`、`collect`、`research`、`learn` 几个阶段在 HEAD 上也还在；瘦身计划删活动内研究位，用户不否决的话也删方法库的 `learn`。

**配置。** 实测配置是 `examples/automationbench/claude-v4-finance.toml`（v0.4 的 final）和 `claude-v4-support.toml`、`claude-v5-support.toml`（support 两臂），各自的页首写着阶段与费用。它们的 `[workspace]` 根是本机、被 git 忽略的运行记录，`[rsi]` 里的 ref 指向那里冻结的计划。开新活动时复制一份，换新的 `[workspace] root` 和 `[rsi] scope_id`，`[rsi]` 的计划 ref 换成自己 `freeze` 打印的行，有 W\* 之前不写 `[freeze.final]`。

## 代码结构（benchmark 路径）

| 路径 | 内容 |
|---|---|
| `ouroloop/bench.py` | `ouroloop bench` 的各个 phase、preset、启动检查、冻结与 cycle |
| `ouroloop/adapters/automationbench.py` | 任务、业务工具、官方评分器 |
| `ouroloop/agent/` | 运行壳（`runner.py`、`gateway.py`）、默认 agent 程序、能力包（`harness_package.py`）与隔离 worker（`worker.py`、`worker_runner.py`） |
| `ouroloop/research/` | R：失败诊断、编码会话（`session.py`）、草稿检查、经验库（`library.py`）、方法库（`methods.py`、`method_kit/`） |
| `ouroloop/evolve/` | 公共契约与对象库（`contracts.py`）、配对任务门（`task_gate.py`）、执行与预算账（`execution.py`）；遗留的 trainer 与晋升门也在这里 |
| `ouroloop/release.py`、`ouroloop/policies.py` | W/R 发布、安装与恢复；已注册的策略目标 |
| `ouroloop/report.py` | 离线报告：身份、任务效果、成本、C1～C4 状态 |
| `ouroloop/providers/` | `claude_code`、`openai_compat`、`anthropic`、`manual` |
| `ouroloop/app.py` | 导出版本运行时的最小 app |

## 文档

- [docs/PLAN.md](docs/PLAN.md)：现在在哪、规则、接下来的阶段、待用户决定的事。从这里开始。
- [docs/RESULTS.md](docs/RESULTS.md)、[docs/JOURNAL.md](docs/JOURNAL.md)：结果；每次改动与运行的日志（倒序）。
- [docs/DESIGN.md](docs/DESIGN.md)、[docs/ROADMAP.md](docs/ROADMAP.md)、[docs/modules/](docs/modules/README.md)：设计、历次活动、模块规则。
- [docs/design/](docs/design/)：瘦身清单、开源组件计划、v2 训练链、v0.4 与 v0.5 的计划。

2026-10-08 整理中删掉的文件都能在 tag `archive-2026-10-08` 上读到。

## 许可证

MIT，见 [LICENSE](LICENSE)。

<div align="center">

# reharness

**同一个请求，第二次就做对的 agent。**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Benchmark: AutomationBench](https://img.shields.io/badge/benchmark-AutomationBench-555.svg)](https://github.com/zapier/AutomationBench)
[![English](https://img.shields.io/badge/README-English-lightgrey.svg)](README.md)

</div>

reharness 让业务 agent 处理反复到来的请求。一个请求被退回后，一个**修订会话**（Revision Session）读 agent 自己那次运行，改 agent 的 **Harness 包**——工具、hook、技能、提示词——再用一次**实跑探针**（Live Probe）证明这个改动：候选在同一个请求上真跑一次。只有被接受的探针才会被采用，而且只对这个请求采用。没有人挑改动，没有模型当裁判，会话也从来看不到答案：至多每次尝试一个"被接受 / 被退回"的信号，和*哪条检查没过*。会话碰不到的东西全部冻结——评分器、划分、预算——包代码跑在隔离的 worker 里。

在 [AutomationBench](https://github.com/zapier/AutomationBench)（47 个模拟 SaaS 工具，按 agent 留下的世界状态评分）上实现和测量，agent 与会话都是 Claude Opus 5.5，跑在订阅预算上。finance 的 50 道 test 题上稳态通过率从 64% 到 68%，没有一道通过题变坏；train 题上会话在提示词没有任何暗示的情况下，自己学会了写入前打开每个没读的源、数字由代码算。

<p align="center"><img src="docs/assets/architecture.svg" alt="组件与架构：冻结核心 K 之下的 Harness 包 W、隔离 worker 与修订会话 R" width="100%"></p>

## 目录

- [核心想法](#核心想法)
- [架构](#架构)
- [一次提升如何发生](#一次提升如何发生)
- [反馈档](#反馈档)
- [实验设计](#实验设计)
- [结果](#结果)
- [安装](#安装)
- [运行](#运行)
- [仓库结构](#仓库结构)
- [引用](#引用)
- [许可证](#许可证)

## 核心想法

- **按题采用，凭一次被接受的实跑探针。** 会话的候选真跑这个请求，回来的裁决决定。被接受的题从不动，所以学习不可能弄坏本来能过的题。题与题只通过一个记录此前采用的经验库互相影响。
- **类型化、有界的学习信号。** 会话能学什么是封存协议的一个字段——反馈档：只有接受信号（每次尝试一个"被接受 / 被退回"），或者信号加上被退回检查的种类与对象（哪封邮件、哪一行——从不带值），再加一份只有条数的源清单。评分行从不离开宿主。
- **会话学的是习惯，不是答案。** 在这个信号下，它在互不相通的会话里建出了同样的两个机制，提示词里没有这些词：第一次写入前打开每个没读的源的守卫，以及按读到的输入算数字的工具。采用的包里每一个计算工具都是会话写的，种子里一个都没有。
- **只有三类守卫的冻结核心。** 钱：预算守卫把每次模型调用先按最坏情况预留、事后结算，超限即停。泄漏：按任务族划分，协议在第一次 test 尝试前封存并提交。正确性：worker 镜像与代码的完整性 pin，失败分类，只有基础设施重试。

## 架构

系统记作 S = (W, R, E; K)：会话可以改的 Harness 包 **W**、改它的修订会话 **R**、评判它所依据的宿主轨迹 **E**、约束这三者的冻结核心 **K**。

| 组件 | 是什么 | 在哪 |
|---|---|---|
| **K** 冻结核心 | 配置只能收紧的预设，连同划分哈希、worker 镜像 id、两个代码哈希和 CLI 版本（完整性 pin）一起冻结；封存协议；预算守卫；宿主侧评分器与评分行，从不离开宿主 | `reharness/activity/presets.py`、`checks.py`；`reharness/evaluate/repeat.py`、`budget.py` |
| **W** Harness 包 | 固定形状的目录，每个文件内容寻址：agent 循环（Stirrup，带 provenance 文件 vendored 在一个适配器后面）、工具、六个站点的 hook、提示词、技能、配置、测试、一个 manifest；形状、字节数、数量上限和 effort 上限来自 K | `reharness/harness/` |
| **R** 修订会话 | 每道退回题一个有界的编码会话：读运行摘要、源清单、被退回的检查和经验库；改包的副本；实跑探针（`try_task`）真跑；至多四次 | `reharness/improver/repeat.py`、`prompts/repeat.md` |
| **E** 宿主轨迹 | 宿主写的每次尝试的记录（调用、hook、写入、标记）、预算账本、事件；包只能加标明是自己写的行 | `reharness/runtime/trace.py`、`records.py` |
| 隔离 worker | 一次尝试：按镜像 id pin 住的 Docker 容器（无网络、只读根、非特权用户、标准库 runner，宿主每次加载核对 sha256）；到模型的计量网关；到业务工具的带拒绝表的世界 broker；hook 拿到的是只读的 | `reharness/runtime/` |
| 适配器 | 基准的任务、工具与官方评分器；回路需要的视图——被退回检查的种类与标识、源清单的条数 | `reharness/adapters/automationbench.py` |

种子带四个通用 hook（台账、第一次写入前的 triage、日期锚、收尾核对）和一个不写任何东西的工具 `plan_changes`。六个 hook 站点是模型调用前后、工具调用前后、每轮之后、收尾时；hook 以固定形状回答（block、modify、feedback、pushback），而*守卫*——在工具调用前拦住一次、给出理由、同一调用重发即放行的 hook——是会话手里最便宜的组件。

## 一次提升如何发生

<p align="center"><img src="docs/assets/one-improvement.svg" alt="一次提升如何发生：首次被退回，修订会话读运行、建候选、用实跑探针证明，被接受的探针按题采用，之后两次站住，教训进经验库" width="100%"></p>

一份封存协议点名题目和每道题遇到的次数（三次）。图里跟的是一个请求——季度税额预估——在 finance-v24 里的经过；每道题走的步骤相同。

1. **首次。** 第 1 次用 base 包跑。被接受的请求到此结束：不开会话、什么都不改、$0。
2. **修订会话。** 被退回的请求给 R 一个绑定这道题的编码会话。它读运行摘要——每次调用的路由和错误标记、出手的 hook 和它说的话、按路由的写入、最后的话；没有评分——源清单（每个源：agent 读过 / 只有会话自己的 hook 读过 / 没读过）、协议允许的被退回检查、经验库。
3. **候选与预检。** R 改包的一个副本。宿主跑免费检查：形状与限额、对改动行扫一个只属于一道 train 题的值、声明对 diff 双向核对、包自己的测试和 worker 里的冒烟。
4. **实跑探针。** 候选在同一请求上真跑一次。答复是新运行和第一次的并排，加上裁决——items 档下还有这次没过的检查。没改过的候选被拒，改过可以再探；每题四次。
5. **按题采用。** 最后一个探针被接受的候选被这道题采用；都没被接受就保留 base。会话没交上来但探针被接受的候选，由宿主从 diff 声明，照样算数。采用的树和教训进经验库。
6. **稳态。** 第 2、3 次用这道题自己的包跑。报告读第 1 次（首次）对第 3 次（稳态），按任务族配对、bootstrap 给区间。

题与题并行，一道题的三次按顺序，每次都在记录里，重跑不会付两次钱。

## 反馈档

<p align="center"><img src="docs/assets/feedback-levels.svg" alt="四个反馈档：none、outcome、attempts、items" width="100%"></p>

`feedback` 是封存协议的一个字段，所以每个活动报的数都说明了会话拿到了什么。量出来更差的两个档——裁判模型只凭运行判断——留作对比。

## 实验设计

- **基准。** AutomationBench 的 finance 域：100 道公开题，每道是跨模拟 SaaS 工具（表格、邮件、聊天、账务系统）的业务流程，按运行后世界状态上的断言评分，断言全部成立才算通过。公开题集上的本地分数不是榜单分数。
- **划分。** 按任务族哈希分到 train 与 test（50 / 50）并冻结；划分哈希是 K 的一部分。
- **封存。** 一份协议——题目、次数、臂、反馈档、base 版本——封存成一个文件，ref 在第一次 test 尝试前提交。会话只能打开 train 的会话和这道题自己的运行，轨迹库拒绝其他的。
- **臂。** 学习臂按题采用；对照臂每次跑 base。报的数是学习臂的首次对稳态，按题配对、按族重采样；base 自己的翻转率放在旁边（同一个包跑两次有 10～17% 的题翻转）。
- **模型与费用。** agent 与会话都是经无头 Claude Code 调用的 Claude Opus 5.5，所有臂同一 effort（未注明即 medium；`automationbench_v2_max` 把两臂放到 max）。费用是订阅上的名义 API 等价美元；预算守卫把每次模型调用先按最坏情况预留。
- **报数时披露什么。** 划分、反馈档、effort、样本量、配对差与区间、费用。

## 结果

| 活动 | 题 | 反馈 | 首次 → 稳态 | 发生了什么 | 费用 |
|---|---|---|---|---|---|
| finance-v16（预演） | 20 train | 一比特 | 15/20 → 18/20 | 5 道退回题修好 3 道且站住；被接受的题没动 | $51 |
| **finance-v18** | **50 test** | 一比特 | **32/50（64%）→ 34/50（68%）**，+4.0 [−4.0, +12.0] | 18 道退回，3 道修好并站住，没有一道通过题变坏（base 自己第 2 次翻了 32 道里的 4 道，第 3 次翻回 3 道） | $184 |
| finance-v20 | 2 train | items | 0/2 → 2/2 | 两个独立会话建出同一个"没读的源"守卫；agent 随后读了聊天频道，算出 13,162.5 和 105,792 | $11 |
| finance-v24（effort max） | 3 train | items | 1/3 → 2/3 | 一个会话建了源覆盖守卫、路由守卫和一个预估工具；税额题两次都 105,792，medium 下三个活动都没做到；代价 5～7 倍 | $61 |

依次说明：按题采用只留下实跑探针被接受的，所以学习从没让通过题退步；只有一比特时会话瞄不准没过的那一项（test 上 18 道修 3 道，12 道改在别处）；有被退回的检查和源清单之后它自己找到通用机制；max 下一个会话就找到。剩下的失败都记清了：薄信号分不出的任务文档歧义、从不告诉 agent 的世界时钟、基准的噪声底。全部活动——包括此前留出筛选下的八个（四次封存 final：Marketing +13.0 显著；finance +4.3、+8.0、+4.0 区间内；之后没有高于噪声底的）——见 [docs/RESULTS.md](docs/RESULTS.md)，每次运行的读数见 [docs/JOURNAL.md](docs/JOURNAL.md)。

## 安装

Python 3.11 或更新，macOS 或 Linux（Windows 用 WSL）；worker 需要 Docker。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # 装上 `reharness` 命令
pytest -q                                      # 离线：不联网、不调模型
docker build --provenance=false -f docker/worker.Dockerfile -t reharness-worker:py3.12 .
```

clone AutomationBench 并把 `[bench] upstream_path` 指向它；适配器要 import 什么见 [examples/automationbench/README.md](examples/automationbench/README.md)。agent 与会话经本机 Claude 订阅登录（`claude auth login`）下的无头 Claude Code 调用；启动检查拒绝非订阅登录。

## 运行

快速上手在新工作区用 5 道 train 题跑回路；前两条命令不调模型。

```bash
reharness bench --config examples/automationbench/quickstart-repeat.toml --phase smoke --checks-only   # $0：划分、评分器契约、输入审计、种子的字面量
reharness freeze --config examples/automationbench/quickstart-repeat.toml                              # $0：K、预算根、封存协议；打印要加进 [rsi] 的行
reharness bench --config examples/automationbench/quickstart-repeat.toml --phase repeat --arm learn    # 5 道 × 3 次，退回题各开一个修订会话：约 $15～25
reharness bench --config examples/automationbench/quickstart-repeat.toml --phase report                # $0
```

| 命令 | 做什么 |
|---|---|
| `reharness bench --config <cfg> --phase smoke --checks-only` | 上游身份、冻结的划分、评分器契约、输入审计、种子的字面量扫描 |
| `reharness freeze --config <cfg>` | K、预算根与封存协议；打印要加的 `[rsi]` 行 |
| `reharness bench --config <cfg> --phase repeat --arm learn\|control [--until task-K]` | 重复流；学习臂按退回题采用，对照臂每次跑 base |
| `reharness bench --config <cfg> --phase report` | 从磁盘记录出报告 |

| `[repeat]` 选项 | 行为 |
|---|---|
| `split = "train" \| "test"` | 协议点名哪个划分；test 协议在第一次 test 尝试前封存并提交 |
| `tasks = [...]` 或 `max_tasks = N` | 点名的题，或流序的前 N 道 |
| `encounters = 3` | 每道题遇到几次 |
| `feedback = "attempts" \| "items" \| "outcome" \| "none"` | 反馈档：会话被告知什么（见上） |
| `max_probes = 4` | 每题实跑探针次数 |
| `arms = ["learn", "control"]`、`concurrency = 4` | 要跑的臂；题并行数（机器设置，不进协议） |

配置点名一个预设（`[harness] preset`），只能收紧它（[docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9、[docs/design/EVALUATE.md](docs/design/EVALUATE.md) §6b）。`finance-v14.toml`～`finance-v25.toml` 是实测活动；工作区放本机的、git 忽略的运行记录。换领域：复制 quickstart，改 `[bench] domain`，给新的 `[workspace] root` 和 `[rsi] scope_id`，在为 test 封存并提交协议之前保持 `[repeat] split = "train"`。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `reharness/activity/` | 配置与预设到 K、工作区、阶段（`phases.py`：`repeat`）、启动检查、报告、CLI |
| `reharness/improver/repeat.py`、`prompts/repeat.md` | 逐题回路：无标签的运行摘要、实跑探针（`try_task`）、接受信号、被退回的检查、源清单、经验库 |
| `reharness/evaluate/` | 冻结的划分、尝试与结果、封存协议（`repeat.py`、`final.py`）、配对 bootstrap、预算账本、发布 |
| `reharness/harness/` | 类型化的包：组件种类、manifest、形状与字面量检查、vendored 的种子（`seeds/stirrup/`） |
| `reharness/runtime/` | 一次尝试：网关、broker、Docker worker 与 runner、出口、轨迹 |
| `reharness/adapters/automationbench.py` | 任务、业务工具、官方评分器、被退回检查与源清单的视图 |
| `docs/` | 设计（`DESIGN.md`、`design/`）、计划、结果、日志、发布稿（中文为主） |
| `reharness/improver/`（其余）、`reharness/memory/`、`reharness/train/` | 早先的留出筛选回路、memory sidecar（实测预设里都关着）、训练栈（没跑过）；留作记录 |

## 引用

```bibtex
@software{reharness2026,
  title   = {reharness: an agent that gets the same request right the second time},
  author  = {The reharness Authors},
  year    = {2026},
  url     = {https://github.com/e13ven-arch/reharness},
  note    = {Bounded self-improvement of an agent's harness package, measured on AutomationBench}
}
```

## 许可证

MIT，见 [LICENSE](LICENSE)。Stirrup 种子按它自己的许可证 vendored 在 `reharness/harness/seeds/stirrup/vendor/stirrup/LICENSE`；AutomationBench 保留上游许可证。

# ouroloop

[English](README.md) | 中文

一个有界递归自改进（bounded RSI）的工程原型：从任务反馈发现有效方法，验证并固化为可运行的 agent 应用，让经过验证的改进方法用于下一次改进。首版中这种更新只限于改进者自己的两段研究提示，评分、权限与预算固定；见 [设计 §7](docs/DESIGN.md)。

**结果（v0.1，2026-10-04）：** 在留出的 AutomationBench Marketing test 题上，框架的自动演化把 Claude Opus 5.5 的通过率从 **41.3% 提到 54.3%**（+13.0 个百分点，95% 置信区间 [+2.2, +26.1]），每次尝试的成本不变。比较计划在跑 test 之前已冻结并提交；改进由框架只从 train 失败中自动找到，没有人工挑选。设置、过程、费用（合计 API 等价 $153）与局限见 [docs/RESULTS.md](docs/RESULTS.md)。

[v0.6 设计](docs/DESIGN.md)围绕能力的发现、验证、固化和递归回用。用户提供环境、目标、权限、初始模型与预算；首版自动演化版本化能力包内的工具/中间件代码及技能/经验，通过固定 API 隔离运行、任务评测和回归后发布同 actor 的 app，并比较/更新/回用改进策略。第二版再完善教师引导蒸馏和训练；actor/controller 分离及已有决策 trainer 保持兼容。Tinker 是拟选训练后端，不是首版运行前提。

**已实现：** 决策与训练基础设施、AutomationBench adapter、配对完整任务评测、完整 W/R 发布、共享预算与恢复、任务改进循环，以及研究策略隔离比较和回用（工程验证与已知缺口见 [实现快照](docs/history/V0_5_IMPLEMENTATION.md)）。首版工程链路也已接通：train 失败的逐题诊断，版本化能力包（经验、技能、hook 与工具）及冻结前的草稿检查，包代码只在隔离 Docker worker 中运行，基于该循环的研究方法比较并核对已安装的 R1 确实被用上，带 preset 的 `cycle` 入口，以及把已安装版本导出为最小 app。以上都只用 fixture 世界和脚本研究员做了离线验证；CI 构建 worker 镜像并运行 Docker 隔离测试。2026-10-02 的复核发现 4 个高危缺陷：K 未冻结方法清单时，提案可以指定一个会被控制面 import 的模型 backend；研究裁决会接受两支相同或不配对的结果；续跑的实验不读冻结的计划。4 项现已全部修复（[路线图 §3](docs/ROADMAP.md)）。

**已有实测：** 2026-09-28，GLM-5.3 完成一个 Marketing dev 任务。2026-10-01 又有两次真实运行：一次把失败的 train 任务经逐题笔记、汇总提案、草稿检查与一次修订、准入、冻结实验走到构建候选，在 gate 前有意停止；另一次的草稿首检即通过，经筛选走到一次 dev gate。那次比较途中供应商账号余额耗尽（20 次 dev 尝试中 19 次无效），gate 判为 inconclusive，没有安装。那几次 GLM 运行不是改进证据。首个实测收益来自 2026-10-04 的 Claude Opus 5.5（[结果](docs/RESULTS.md)）；递归研究的收益（R 改进自身）还没有实测。actor 蒸馏、Tinker 接入与教师审阅属于第二版。详见 [日志](docs/JOURNAL.md)。

AutomationBench Marketing 仍是首个证明场：首版比较同一个所 pin 的 actor 在能力包演化前后（原定 GLM-5.3，2026-10-03 起先跑 Claude Opus 5.5，见 docs/ROADMAP.md §3 第 2a 步）及有效的 prompt/workflow；第二版换 Qwen3.5-9B 学生时 h* 在学生上重新演化后再蒸馏；并以领先模型公布数字作单独的外部能力参照；沿用最近的决定，默认不在本地运行领先模型。教师辅助采集分数与无教师部署成绩分开报告。

决策标签仍需环境事实或人工反馈；最终任务得分不能直接当作所有中间动作的标签。新增教师轨迹进入独立的模仿学习通道，不放开决策真值限制。现有 Jev 和本地决策 trainer 保留。

开发入口：[路线图](docs/ROADMAP.md)、[技术选型](docs/TECHNOLOGY.md)、[模块详细设计](docs/modules/README.md)。下方命令说明现有功能；第二版配置（蒸馏、教师、训练）在文档中标为待实现，读取时会被拒绝。

## RSI 是怎么发生的

`ouroloop bench --phase improve` 在业务 benchmark 上按轮运行这个循环（[ouroloop/bench.py](ouroloop/bench.py)）；自己的 harness 在每个 session 里跑在线的一半。只有通过准入的提案才会训练，没有新东西可学的一轮不花算力（`ouroloop plan` 给出同一份判断）。`ouroloop evolve` 每次调用都跑旧 decision profile 的一轮（训练、校准、过门、晋升），不管计划怎么说。

<p align="center"><img src="docs/assets/architecture.zh-CN.gif" alt="ouroloop 架构：一个环，上半圈每个 session 都在跑，下半圈每一轮跑一次" width="100%"></p>

图示对应现有决策学习循环，不是已测得的业务改进，也未覆盖 v0.6 的完整蒸馏路径；新架构以设计文档为准。

首版将受限能力包代码演化接入已有自动循环：R 从反馈提出包内修改，固定执行器负责隔离、权限、预算、评分和发布；新 W 用于后续任务，验证通过的新 R 用于下一次实际改进调用。蒸馏训练在第二版完善，递归核心仍是首版验收。真实 harness 演化与递归收益尚未证明。

现有接管策略有校准阈值、按有标签 session 的风险检查和运行时撤销，依赖相应反馈与统计假设，不构成“业务任务失败率最多 5%”的保证。benchmark profile 按独立的完整任务协议评测，保留原部署 profile。

`ouroloop research` 是旧 decision profile 自己的一轮，原样保留；业务循环的轮次走 `ouroloop bench --phase improve` 与 `--phase research`。

## 快速上手

需要 Python ≥3.11，支持 macOS 和 Linux（agent 的工具依赖 bash），Windows 请用 WSL。

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"      # 默认只依赖 httpx
pytest -q                    # 离线测试：不联网，不跑模型
ouroloop demo                # 用合成数据离线跑一轮研究循环
```

接入 Jev：把 key 写进仓库根目录的 `.env`（`TYPESAFE_API_KEY=...`，已被 git 忽略）。

```bash
ouroloop smoke                   # 三种题型各问一次 Jev
ouroloop demo --judge jev        # 研究循环里的判断交给 Jev
ouroloop decide --backend jev --type noul --question "The failure is transient" --state "ConnectionResetError"
pytest -m live                   # 真实 API 的冒烟测试
```

用 Claude：`pip install -e ".[anthropic]"`，并设置 `ANTHROPIC_API_KEY`。

```bash
ouroloop run "修好 tests 里失败的测试" --llm anthropic --judge jev     # agent 在当前目录干活，决策点交给 Jev
ouroloop run "..." --llm openai_compat --model qwen3 --base-url http://localhost:11434/v1
ouroloop run "..." --llm anthropic --verify "pytest -q" --small-model claude-haiku-4-5  # 验证命令和小模型档位
ouroloop gate freeze --config examples/ouroloop.toml                # 由人冻结一次：所有晋升都过这个门
ouroloop research --config examples/ouroloop.toml --rounds 3        # 对积累的账本跑几轮研究循环
ouroloop plan --config examples/ouroloop.toml                       # 数据现状，以及这一轮该不该训练
ouroloop evolve --config examples/ouroloop.toml                     # 用账本训练新的 TDE，过晋升门才替换
ouroloop calibrate --config examples/ouroloop.toml                  # 报告每个决策点的温度和阈值，不写入
ouroloop models --dir examples/workspace                            # 查看模型谱系；加 --rollback 回退并撤销
```

门模板只能收紧、不能放宽 5% 的风险、每次过门需要的 30 个新 session 和 bootstrap 次数。每轮的门记录、它的使用和结论都是账本里的事件，`gates/` 里只是副本。只有门记录接受过的候选才能晋升、作答和执行。

关于 TDE：TDE 是单独的项目，之后另行开源；本仓库里的 `tde` 训练器记录了 ouroloop 怎么训练它：作为一个任务（`ouroloop/worker.py`）在本机运行，或者设 `host = "user@box"` 通过 ssh 在 GPU 主机上运行，每个任务在那台机器上有一个新目录 `<jobs_root>/<job id>/`。任务把 run 发布到模型库 `[store]`，训练出的模型是一个引用：`type = "dir"` 时是任务 id（一个目录，有训练主机时在那台主机上），`type = "hf"` 时是 `<repo>@<commit>`（run 提交一次到私有的 Hugging Face 仓库）。`[serving]`（旧名 `[models]`）说明这些引用怎么问：`url`（和 `token_env`）指向能读模型库的主机上的 `ouroloop serve`，或者在进程内从模型库加载。过门的模型先部署再成为冠军（部署失败用 `ouroloop deploy <version>` 重试）。控制面、训练、模型库、服务是四个角色；一台 GPU 主机包下其中三个只是一种布局（docs/TRAINING_HOSTS.md）。没有 TDE 时，训练管线用内置的签名小模型（`type = "signature"`）也能完整跑通；接自己的模型就写一个训练器插件（`ouroloop/trainers/__init__.py`，docs/TRAINING_HOSTS.md §1）：`train` 作为作业在训练机器上跑，`backend(run, ref, options)` 为服务加载模型；配置里写 `type = "your.module"`。写成 `type = "your.module:YourTrainer"` 的训练器在当前进程里训练，后端也在这里建，不经过模型库和 `[serving]`，只用于测试和 demo。

要让 TDE 实时回答单个决策，就在 GPU 主机上常驻一个决策服务，harness 这边用 `type = "http"` 的后端连过去：

```bash
OUROLOOP_TOKEN=... ouroloop serve --config host.toml --host 0.0.0.0 --port 8765
```

绑定到本机以外的地址时必须设置 `OUROLOOP_TOKEN`，因为 `/outcome` 会往账本里写训练标签。

模型接入：研究 agent 和 `ouroloop run` 可以接任何模型。
- Claude 走官方 SDK：`[llm] type = "anthropic"`，默认 `claude-opus-5`，在 `.env` 里写 `ANTHROPIC_API_KEY`。
- Claude 也可以走无头 Claude Code，用订阅登录：`type = "claude_code"`（`claude -p`，不开工具，在空目录里运行，工具调用写成结构化 JSON），见 docs/modules/01_POLICY_RUNTIME.md §1。
- 其他模型走 OpenAI 兼容接口：`type = "openai_compat"`，智谱、OpenRouter、DeepSeek、vLLM、Ollama 等都行。遇到限流和 5xx 会自动重试。模型特有的请求字段写在 `[llm.extra]` 里，比如 GLM 的思考强度。
- 智谱的完整配置见 [examples/zhipu.toml](examples/zhipu.toml)。
- 配置里的 `env_file` 指定另一个 `.env`，路径相对于配置文件，在别的目录运行时也能读到 key。

任务集和 prompt 进化：[evals/tasks](evals/tasks) 里有 29 个小编码任务，其中 21 个开发任务、8 个 held-out 任务。每个任务是一个 TOML 文件，写着任务描述、初始文件、检查命令，以及跑完之后才写进去的隐藏测试，agent 改不到这些测试。得分是通过的检查项比例。

```bash
ouroloop suite --validate --tasks evals/tasks                # 不调用 LLM：确认每个任务原样不通过、参考答案全通过
ouroloop suite --config examples/zhipu.toml                  # 用当前冠军 prompt 跑一遍任务集
ouroloop research --config examples/zhipu.toml --rounds 2    # 配了 [suite] 时，研究 agent 也会改 harness prompt
```

prompt 候选按下面几步评测：

- 只改了空白字符的直接拒绝；
- 先在前几个开发任务上筛一遍，不比冠军差，冠军和候选才在全部任务（含 held-out）上都重新跑一遍，再过同一个晋升门；
- 研究 agent 看不到 held-out 任务的结果；
- 每次运行按（prompt、模型、任务、第几次）记在 `harness/runs.jsonl`，记下的结果只用于筛选；每轮只过一次门，同一个 prompt 不能靠重跑碰运气。

任务跑完时的检查结果，也会作为这次运行里 stop / route 决策的标签。跑任务集时 agent 会自动执行 bash：命令在临时目录里执行，黑名单照样生效。不放心模型的话，放在容器或虚拟机里跑。

可选的数据来源：`ouroloop hook print-settings` 会打印一段 Claude Code 的 hooks 配置。放进 `.claude/settings.json` 以后，日常 Claude Code 会话里的工具调用和结果会被记进账本。默认只记录，不改变 Claude Code 的任何行为。

工具调用默认要你批准（终端里问 y/N），加 `--yes` 自动放行；黑名单里的命令（`rm -rf /`、`curl ... | sh` 等）永远不执行。配置示例见 [examples/ouroloop.toml](examples/ouroloop.toml)。

`--verify` 给一条检查命令，退出码 0 表示任务完成。agent 想结束时先跑它，没通过就把输出发回去让 agent 接着干（最多 2 次）；检查结果同时是 stop 决策点的标签。`--small-model` 配一个同一接口下更便宜的模型，route 决策点打开之后决定每一步用哪个，在那之前每一步都用大模型；任务成败是小模型步骤的弱标签。交互运行时，结束后会问你任务做完没有，回答也记进账本。

## AutomationBench

[AutomationBench](https://github.com/zapier/AutomationBench) 用 47 个模拟 SaaS 工具搭出业务流程，按 agent 最后留下的世界状态判定任务是否完成。它是独立运行时，ouroloop 延迟导入：把它 clone 下来，让 `[bench] upstream_path` 指向那个目录（需要装什么见 [examples/automationbench/README.md](examples/automationbench/README.md)）。

```bash
ouroloop bench --config examples/automationbench/marketing.toml --phase smoke --checks-only   # 不调模型
ouroloop bench --config examples/automationbench/marketing.toml --phase smoke                 # 再跑几个开发任务
```

首版自动循环是一条命令，配置里有 `[improvement]` preset 和预算即可（[examples/automationbench/improvement.toml](examples/automationbench/improvement.toml)）：冻结未给出的计划，跑起点版本的基线、一个 worker 段、已计划的 research slot，再跑一段来确认已安装的 R 是否被用上，最后生成离线报告。中断后续跑，已记录的部分不重复付费。包代码只在隔离 worker 镜像中运行：

```bash
docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
ouroloop bench --config examples/automationbench/improvement.toml --phase cycle
ouroloop export --config examples/automationbench/improvement.toml --to exported-marketing
```

smoke 阶段报告这是哪个上游版本，把 train/dev/test 任务分组冻结到 `<workspace>/bench/<domain>/splits.json`（在看到任何结果之前），把本适配器与上游自己的 rubric 入口、服务订阅门和工具 schema 对比，审计有没有评分信息进到 agent 的输入里，然后完整跑几个开发任务。它不对能力下任何结论；本地公开任务集的分数也不是官方榜单成绩——榜单跑的是私有任务集。

agent 拿到的是任务 prompt 和工具 schema，断言与初始状态在它收不到的对象里；分数来自工具留下的世界，所以写错记录再汇报成功，得到的就是写错的那份分数。其余阶段跑的是同一条闭环：`baseline` 测合理基线（也支持显式配置的领先模型臂，但当前方案只引用其公布数字），`collect` 离线给完成判断打标签，`improve` 以固定研究策略跑任务改进，`research` 在隔离两支中比较两个研究策略，`evaluate` 运行一次密封的最终计划，`report` 从磁盘上的记录重算一切。它们都读工作区里的冻结计划对象，而不是配置文件里的可改设置；缺计划或缺模型表的阶段会在调用模型之前说明缺什么。

## 代码结构

| 路径 | 内容 |
|---|---|
| `ouroloop/decision.py` | 决策点和 spec（问题、候选、view 参数） |
| `ouroloop/policy.py` | 执行规则、阈值拟合、探索和倾向分 |
| `ouroloop/ledger.py` | 账本、原始上下文、冠军 spec |
| `ouroloop/runtime.py` | 把决策点、后端、默认回答者和账本串起来；每个 session 固定自己的发布三元组 |
| `ouroloop/release.py` | session 运行的发布：旧三元组，以及完整 W/R 版本的凭据、安装、恢复与撤销 |
| `ouroloop/policies.py` | 已注册的策略目标、每个目标可绑定什么、装配、会话 pin 与候选构建 |
| `ouroloop/agent/` | 最小 agent：loop、read / write / edit / bash 四个工具、harness prompt、任务集；能力包（`harness_package.py`）及运行包代码的隔离 worker（`worker.py`、`worker_runner.py`） |
| `ouroloop/app.py` | 导出版本运行时的最小 app |
| `ouroloop/points/` | 内置决策点 retry、compact、tool_gate、stop、route，以及命令黑名单 |
| `ouroloop/chat.py` | 与模型无关的对话格式 |
| `ouroloop/serve.py` | HTTP 决策服务 |
| `ouroloop/benchmark.py` | benchmark 运行契约：冻结的任务分组、运行身份、一次尝试的结果 |
| `ouroloop/bench.py` | `ouroloop bench` 的各个 phase、启动检查，以及一次改进活动共用的接线 |
| `ouroloop/report.py` | 离线报告：身份与范围、任务效果、成本、改进与 RSI 证据、C1～C4 状态 |
| `ouroloop/adapters/` | AutomationBench（任务、业务工具、官方评分器）；Claude Code hooks 适配器（可选的数据来源） |
| `ouroloop/backends/` | `mock`、`jev`、`tde`、`llm` 四种决策后端 |
| `ouroloop/providers/` | LLM 接入：Anthropic（官方 SDK）、OpenAI 兼容接口、无头 Claude Code、`manual`（请求与回复是文件） |
| `ouroloop/research/` | 问题转换、研究 agent 接口和默认实现、一轮研究流程，研究员可读什么（`context.py`）、实际跑了什么（`inspection.py`）、提案与准入（`experiment.py`）、研究策略 campaign（`campaign.py`）、train 失败的逐题诊断（`diagnosis.py`）、草稿检查与修订（`drafts.py`） |
| `ouroloop/evolve/` | 公共契约与对象库（`contracts.py`）、反馈构造器、建数据、数据检查、训练（签名小模型 / 本机 TDE / 远程 TDE）、校准、冻结的晋升门和门记录、配对任务门（`task_gate.py`）、执行状态机与预算账（`execution.py`）、模型注册表、经验记录 |

## 状态

当前已有 v0.5 工程闭环、首版能力包演化循环（[路线图](docs/ROADMAP.md)阶段 A–E，fixture 离线验证）以及上文“已有实测”所列的真实模型运行（尚无实测收益），2026-10-02 复核发现的 4 个高危缺陷中两个已修，另两个在任何研究位运行之前修（[路线图 §3](docs/ROADMAP.md)）；第二版（教学适配、蒸馏、训练）已有详细设计，尚未实现。历史测试和已知缺口保留在 [实现快照](docs/history/V0_5_IMPLEMENTATION.md)，测量记录见 [日志](docs/JOURNAL.md)。

文档入口：[整体设计](docs/DESIGN.md)、[技术选型](docs/TECHNOLOGY.md)、[路线图](docs/ROADMAP.md)、[模块与用例](docs/modules/README.md)、[研究协议](docs/RESEARCH_AGENT.md)、[设计审查](docs/RSI_REVIEW.md)、[演示和证据](docs/DEMO.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。

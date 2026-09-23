# ouroloop

[English](README.md) | 中文

一个轻量的 RSI 框架：不去改进大模型本身，而是让 LLM 提升一个小型决策模型的智能，再用这个决策模型提升 agent harness。

agent 运行中有很多判断：要不要重试、这段历史还留不留、这条命令能不能直接执行、任务做完没有。ouroloop 把这些判断交给一个校准过的决策模型（Jev，或者自己训练的小模型，比如 TDE），一次前向给出概率；置信度不够时，再交给 LLM 或人。每次判断的真实结果都写进本地账本。`ouroloop evolve` 用这些结果自动训练新模型；研究 agent（LLM 推理、决策模型判别）读评测结果，提出决策点、训练配方和 prompt 的改进，也可以换成你自己的实现。决策后端可以直接接 Jev API，也可以用自己训练的小模型（比如 TDE）：冷启动时用 Jev，自己的模型攒够数据后逐个接管决策点。所有改动都要通过留出评测和同一个晋升门，才会替换旧版本。

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
ouroloop research --config examples/ouroloop.toml --rounds 3        # 对积累的账本跑几轮研究循环
ouroloop evolve --config examples/ouroloop.toml                     # 用账本训练新的 TDE，过晋升门才替换
ouroloop calibrate --config examples/ouroloop.toml                  # 按决策点拟合温度和阈值
ouroloop models --dir examples/workspace                            # 查看模型谱系；加 --rollback 回退
```

关于 TDE：TDE 是单独的项目，之后另行开源；本仓库里的训练器记录了 ouroloop 怎么训练它（`local_tde` 在本机训练，`remote_tde` 通过 ssh 在 GPU 主机上训练，checkpoint 留在主机上）。没有 TDE 时，训练管线用内置的签名小模型（`type = "signature"`）也能完整跑通；接自己的模型只要实现两个方法：`train(job)` 返回训练结果，`backend(result)` 返回能回答决策的后端，然后在配置里写 `type = "your.module:YourTrainer"`。

要让 TDE 实时回答单个决策，就在 GPU 主机上常驻一个决策服务，harness 这边用 `type = "http"` 的后端连过去：

```bash
OUROLOOP_TOKEN=... ouroloop serve --config host.toml --host 0.0.0.0 --port 8765
```

绑定到本机以外的地址时必须设置 `OUROLOOP_TOKEN`，因为 `/outcome` 会往账本里写训练标签。

模型接入：研究 agent 和 `ouroloop run` 可以接任何模型。
- Claude 走官方 SDK：`[llm] type = "anthropic"`，默认 `claude-opus-5`，在 `.env` 里写 `ANTHROPIC_API_KEY`。
- 其他模型走 OpenAI 兼容接口：`type = "openai_compat"`，智谱、OpenRouter、DeepSeek、vLLM、Ollama 等都行。遇到限流和 5xx 会自动重试。模型特有的请求字段写在 `[llm.extra]` 里，比如 GLM 的思考强度。
- 智谱的完整配置见 [examples/zhipu.toml](examples/zhipu.toml)。
- 配置里的 `env_file` 指定另一个 `.env`，路径相对于配置文件，在别的目录运行时也能读到 key。

任务集和 prompt 进化：[evals/tasks](evals/tasks) 里有 10 个小编码任务，其中 7 个开发任务、3 个 held-out 任务。每个任务是一个 TOML 文件，写着任务描述、初始文件、检查命令，以及跑完之后才写进去的隐藏测试，agent 改不到这些测试。得分是通过的检查项比例。

```bash
ouroloop suite --validate --tasks evals/tasks                # 不调用 LLM：确认每个任务原样不通过、参考答案全通过
ouroloop suite --config examples/zhipu.toml                  # 用当前冠军 prompt 跑一遍任务集
ouroloop research --config examples/zhipu.toml --rounds 2    # 配了 [suite] 时，研究 agent 也会改 harness prompt
```

prompt 候选按下面几步评测：

- 只改了空白字符的直接拒绝；
- 先在前几个开发任务上筛一遍，不比冠军差才跑全部任务（含 held-out），再过同一个晋升门；
- 研究 agent 看不到 held-out 任务的结果；
- 每次运行按（prompt、模型、任务、第几次）记在 `harness/runs.jsonl`。冠军的结果跨轮复用，同一个 prompt 不能靠重跑碰运气。

任务跑完时的检查结果，也会作为这次运行里 stop / route 决策的标签。跑任务集时 agent 会自动执行 bash：命令在临时目录里执行，黑名单照样生效。不放心模型的话，放在容器或虚拟机里跑。

可选的数据来源：`ouroloop hook print-settings` 会打印一段 Claude Code 的 hooks 配置。放进 `.claude/settings.json` 以后，日常 Claude Code 会话里的工具调用和结果会被记进账本。默认只记录，不改变 Claude Code 的任何行为。

工具调用默认要你批准（终端里问 y/N），加 `--yes` 自动放行；黑名单里的命令（`rm -rf /`、`curl ... | sh` 等）永远不执行。配置示例见 [examples/ouroloop.toml](examples/ouroloop.toml)。

`--verify` 给一条检查命令，退出码 0 表示任务完成。agent 想结束时先跑它，没通过就把输出发回去让 agent 接着干（最多 2 次）；检查结果同时是 stop 决策点的标签。`--small-model` 配一个同一接口下更便宜的模型，route 决策点决定每一步用哪个；shadow 模式下约 10% 的步骤会试用小模型，任务成败是这些步骤的弱标签。交互运行时，结束后会问你任务做完没有，回答也记进账本。

## 代码结构

| 路径 | 内容 |
|---|---|
| `ouroloop/decision.py` | 决策点和 spec（问题、候选、view 参数） |
| `ouroloop/policy.py` | 三种模式、阈值拟合、探索和倾向分 |
| `ouroloop/ledger.py` | 账本、原始上下文、冠军 spec |
| `ouroloop/runtime.py` | 把决策点、后端、兜底和账本串起来 |
| `ouroloop/agent/` | 最小 agent：loop、read / write / edit / bash 四个工具、harness prompt、任务集 |
| `ouroloop/points/` | 内置决策点 retry、compact、tool_gate、stop、route，以及命令黑名单 |
| `ouroloop/chat.py` | 与模型无关的对话格式 |
| `ouroloop/serve.py` | HTTP 决策服务 |
| `ouroloop/adapters/` | Claude Code hooks 适配器（可选的数据来源） |
| `ouroloop/backends/` | `mock`、`jev`、`tde`、`llm` 四种决策后端 |
| `ouroloop/providers/` | LLM 接入：Anthropic（官方 SDK）、OpenAI 兼容接口 |
| `ouroloop/research/` | 问题转换、研究 agent 接口和默认实现、一轮研究流程 |
| `ouroloop/evolve/` | 建数据、数据检查、训练（签名小模型 / 本机 TDE / 远程 TDE）、校准、晋升门、模型注册表、prompt 候选评测、经验记录 |

## 状态

v0.1。已完成：决策层和端到端骨架（M0）、agent harness（M1）、训练管线和研究 agent（M2）、接入（M3）、五个内置决策点、prompt 进化（M5）。用真实 LLM（智谱 GLM 加 Jev）跑通了 agent、10 个任务的任务集和两轮 prompt 进化。

还没做：M4，也就是让研究 agent 无人值守地连续运行，证明自动执行的比例逐代上升。这一版提供的是机制；证据目前是 TDE 的实验记录和上面的流程测试。

设计见 [docs/PLAN.md](docs/PLAN.md)，研究循环的来龙去脉见 [docs/RESEARCH_LOOP.md](docs/RESEARCH_LOOP.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。

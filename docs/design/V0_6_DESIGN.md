# v0.6 设计：Stirrup 与 ReMe 接入，自研只做平台层

**状态：** 已定（用户 2026-10-08 批准接入 ReMe + Stirrup、下载一个小嵌入模型、出成绩顺延到 10-10）。三个构建者按 §1.4 并行实现；每个付费步骤仍要先经用户批准（§10）。
**日期：** 2026-10-08
**范围：** 除新增本文之外没改仓库，没跑付费阶段，没调真实模型，没读 `.env`。同日的评审意见已对照源码逐条核过，采纳、改写与驳回见 §13。

- 行号基于 slim/1 @ `eb8c505`，这是三个 v6 worktree 的起点。其中 `worker.py`、`worker_runner.py`、`harness_package.py`、`library.py`、`claude_code.py`、`app.py` 与 m1 相同；`runner.py` 比 m1 多 14 行，`bench.py` 比 m1 多 33–50 行。slim/1 在 eb8c505 之后又改了 `pyproject.toml`（package-data 与 pythonpath）和 `release.PACKAGE_DATA`（`release.py:18`），所以三个构建者都不碰这两处（§1.4）。
- 外部组件的事实来自 2026-10-08 在本机做的安装与离线冒烟（冒烟时用 `sandbox-exec` 封掉网络，只留 localhost），以及评审核查时读的 ReMe 0.3.1.10、Stirrup 0.2.0 源码和一次离线 chroma 试验（`$V6/probe2`）。
- 文中 `$V6` 指 `/private/tmp/claude-502/-Users-leon-workspace-ouroloop/c9591706-bc88-4f6c-9900-8380bdb28477/scratchpad/v6`。ReMe 源码路径写作 `reme/...`，指 `$V6/venv-reme/lib/python3.12/site-packages/reme/...`；Stirrup 源码路径写作 `stirrup/...`，指 `$V6/stirrup/stirrup-0.2.0/src/stirrup/...`。

**用户原话（2026-10-08）：**

> 可以，做吧，实际生产过程中也不可能无网络以及限制这么小的worker，机制上要完善，细节可以持续打磨提升效果，但是不能总是做一些看似有了的模块，类似之前的简化版agent和memory都是demo级别的

由此定下的两条落地规则：
1. 每个模块都要有一个真实的使用者，并在真实路径上端到端跑通。没有使用者的接口不做（§0.2 的"以后再做"）。
2. 自设上限一律按证据重新确定，写进新 preset（§8）。

**与其他文档的关系：**

| 文档 | 本文取代的部分 | 照用的部分 |
|---|---|---|
| design/MEMORY_AGENT_ITERATION | §0.3 的 D1（不接 Stirrup）与 D3（不跑开源臂）；§3 中"VEL 自己提炼、存储、检索"的部分；§4.4 | §1 的层级与验收梯子；§2.2 失败原因表；§4.2 写入守卫与 `plan_changes` 的文字与常量；§5.1 R0 第 (1)(4')(6') 段；toolcheck（§1.2 V2a）；§2.1 S1 读数 |
| design/OSS_COMPONENTS_PLAN | §3"底座选 VEL"，改为 ReMe 做提炼、存储、检索，VEL 退为平台层；§4 中 Stirrup 一项"由你决定"，现已定为接入 | §1、§2 的证据 |
| design/V0_5_PLAN | — | §1.4 的中止规则（流走完时 D ≤ 0 就不给快照付 gate）；重试附注规则（`retry_note`） |
| PLAN | P4、P5、P6 由本文的 v6 线代替，顺序见 §0.4 | §1 的全部规则 |

集成步负责更新 PLAN、JOURNAL、DESIGN 和 modules/04。

## 0 结论与本版范围

### 0.1 结论

| 部件 | 结论 |
|---|---|
| agent | W0-v6 的 agent 程序就是 **Stirrup 0.2.0**（MIT）。上游 `src/stirrup/` 的 42 个文件连同 LICENSE 原样放进 W 的 `vendor/stirrup/`，R 能改其中任何一个文件，和 StarHarness 的做法一样。<br>我们只写两样东西：一个适配层（`LLMClient` 接 `ctx.model`，`Tool` 接 `ctx.tool`），以及 Stirrup 上的扩展（写入守卫、日期锚、引用行、出站工具 `http_fetch`）。<br>适配层原型 364 行已经在真实 worker 协议下跑通（subprocess，以及 Docker 隔离：无网络、只读根、256m、64 pids）。定稿时要修 7 处，都是读源码确认的（§3.2 末） |
| memory | 提炼原样使用 **ReMe 0.3.1.10 的 task memory 流水线**（当前的 0.4 线已经删掉了 task memory），只把末尾的去重换成"只在批内去重"的子类，跟库比的去重由平台在同一向量空间里做（§5.7 第 1 条）。<br>存储、检索、导出、载入也用 ReMe 自己的 op，绑到每个集合各自的 chroma 实例上。<br>ReMe 跑在独立的 sidecar 进程里，用的就是 ReMe 自带的 HTTP 服务（FastAPI），我们只加鉴权、错误转换和两个路由。嵌入用本地 bge-small-en-v1.5（ONNX），LLM 经我们的网关桥走计量过的 claude_code。<br>VEL 退为平台层，负责只喂 train、留一、每条带 trace 出处、逐条记功与退役、整库快照过 gate。ReMe 自带的记账与删除（record、delete）不用：它们会丢计数，记功也是共享的（§5.7），改由 VEL 的记功代替 |
| 平台层 | 我们自己写的只有平台层：网关与 OpenAI 兼容桥（v6 只服务 ReMe 的研究调用）、trace 记录与检索、egress（mitmproxy 加我们的 addon）、从 K 读取 worker 资源、gate 与发布、toolcheck、train-only 与留一、出处与记功、跨业务线共享 |
| 上限 | 每个自设上限都按证据重新确定（§8）：<br>• 包：24 文件 / 256 KiB → 96 / 768 KiB。种子里的 Stirrup 就占约 550 KiB。<br>• worker：256 MB / 64 pids → 512 MB / 128 pids，关掉 swap。实测峰值 80 MB。<br>• VEL：取消 6 架的上限，改为 `max_active=500`。<br>• skills：12 → 32。<br>• memory.md：8,000 字不变。实际最多用到 4,978 字，而且它会整段进入每次提示 |
| 网络 | worker 的网络策略由 K 指定，v6 里两种都端到端实现并测过：<br>• `none`：基准默认。世界是模拟的，联网只会增加泄题面，比如查到真实日期。<br>• `proxy`：经宿主侧 egress 代理按 allowlist（主机、端口、方法）出站，每次出站请求都进 trace。真实使用者是种子里的 `http_fetch` 工具和 App 的生产路径；Stirrup 自带的 web 工具用不了（§4.4） |
| 成绩 | finance 上 E0.5 金丝雀加 E1–E4，名义约 $365–475（订阅，真实 $0），最早 2026-10-10 出读数。每个付费步骤都先经用户批准 |

### 0.2 本版范围

**发布要的**（每一项都要端到端走真实路径，见 §9）：

| # | 模块 | 真实使用者 | 构建者 |
|---|---|---|---|
| 1 | OpenAI 兼容桥：research 作用域的 `/v1/chat/completions`，流式与非流式，不带工具 | sidecar 里的 ReMe | gateway |
| 2 | trace 引用、跨摘要的轨迹重建、FTS5 检索（只限 train） | ReMe 的输入（§5.3）、R 的 `trace_search` / `trace_get` | gateway |
| 3 | egress：mitmproxy 加 addon、内部网络、按方法的 allowlist、每个尝试一个凭据、只读凭据、请求记录进 transcript | `http_fetch`、App | gateway |
| 4 | 网关经 view 把 usage 交给 agent，`worker_runner.py` 一个字节都不改 | Stirrup 的摘要阈值 | gateway |
| 5 | W0-v6 种子：vendor 进来的 Stirrup、适配层、写入守卫、`plan_changes`、`http_fetch` | 每次 v6 尝试 | agent |
| 6 | 包形状：`vendor` 与只在宿主侧使用的 `library` 两个 kind、`seed_package`、vendor 的 LICENSE 与 PROVENANCE 不可改、字面量检查只扫相对种子改动的行 | 草稿检查、R 的候选 | agent |
| 7 | worker 的资源与网络从 K 读；v6 镜像的依赖按哈希锁定，镜像按 id 钉住 | 每次 v6 尝试 | agent |
| 8 | memory sidecar：ReMe 0.3.1.10 自带的 HTTP 服务、chroma、bge-small，另注册几个平台 op | 流、检索、快照 | memory |
| 9 | VEL v6 平台层：出处、train-only、留一、引用记功、退役、容量、快照、断点续跑；修好 S1 发现的容量 bug | `--phase library` | memory |
| 10 | 跨业务线导入：K 里声明 `library.imports`，导入机制与测试 | 第二条业务线的活动 | memory（机制）、集成（接 K） |
| 11 | preset v6 与 K、R0 v6、toolcheck（V2a、V2c）、causes，以及 bench、release、app 的接线和配置 | — | 集成 |

**以后再做的**见 §12。主要几项：
- 桥的 attempt 作用域（含 tools 映射）和 `/v1/embeddings`：等到有只讲 OpenAI 协议的 agent 或宿主侧库真要用时，连同那个使用者一起做；
- 尝试时的 ReMe 重排与改写；
- sidecar 容器化，以及 amd64 支持；
- HTTPS 内容检查（换成 mitmproxy 之后只是一项配置）；
- 基于嵌入的 trace 检索；
- 跨业务线导入的效果读数。

### 0.3 待用户确认（都给了默认）

| # | 事项 | 默认 |
|---|---|---|
| 1 | 顺序：slim/1 一合入就开始 v6 集成。P2、P3 在独立 worktree 里继续写，E4 之后再合入（规则 13） | 是 |
| 2 | 方法库（MEMORY_AGENT_ITERATION D2）：toolcheck 要用的回放与判分函数搬进 `research/toolcheck.py` 和 `research/causes.py`，P3 再照原计划把 `methods.py` 整个删掉 | 是 |
| 3 | 日期只在 W 侧锚定（写入守卫）；`TURN_NOTE` 不改，actor 身份不变 | 是 |
| 4 | finance v6 开新的预算根，名义上限 **$480**（E0.5 金丝雀 + E1–E4），在 E0.5 之前批准 | 待批准 |
| 5 | preset 名为 `automationbench_harness_v6`，版本 v0.6.0（PLAN §3 第 14 项） | 是 |
| 6 | memory sidecar 的 venv 建在 `~/.cache/ouroloop/memory-venv/<锁摘要>/`，集成步按锁文件联网安装，161 个包 | 是 |
| 7 | 现在就开第 4 个 worktree `v6-research`，只写研究侧的新文件（§1.4），缩短集成步的关键路径 | 是 |

### 0.4 时间线

| 时段 | 内容 | 花费 |
|---|---|---|
| 10-08 夜 – 10-09 | 三个构建者（加上 `v6-research`）各在自己的 worktree 里并行开发，完成标准见 §9.1–9.3 | $0 |
| slim/1 合入 m1 之后（预计 10-09） | 从合入后的 m1 分出 `v6/1`，把各分支 rebase 上来后合并，做集成步；跑全量测试和 E0 | $0 |
| 10-09 晚 | E0.5 付费金丝雀（一次性工作区），通过后才 freeze | 名义 ≈ $5–10 |
| 10-09 晚 – 10-10 | 依次跑 E1、E2、E3、E4，每步之后停下，等批准再继续 | 名义 ≈ $360–465 |

slim/1 没合入之前，集成步等它，构建者不用等。

## 1 平台分层

### 1.1 进程与数据流

```
worker 容器（镜像、资源、网络由 K 指定）
  W 包：vendor/stirrup/**（Stirrup 0.2.0）+ agent/stirrup_agent.py（适配层）+ agent/http_fetch.py
        + middlewares/write_guard.py + tools/plan_changes.py
    │  stdio，worker_runner 协议（逐字节不变）：model_call / base_call / skill_read / mark
    │  K 为 proxy 时：http_fetch ──HTTP(S)_PROXY──→ egress 代理容器
    ▼
宿主进程（.venv；核心依赖只有 httpx）
  Gateway（角色、effort 上限、预算、记录）──→ claude_code CLI（actor）
  AttemptBroker（deny、基础工具、只读）──→ AutomationBench 世界 / 业务 API
  VEL 平台层（出处、train-only、留一、记功、退役、快照）──HTTP──→ memory sidecar
  OpenAI 兼容桥（127.0.0.1，只有 research 作用域）
     ←── sidecar 里 ReMe 的 openai LLM 后端；桥 ──→ MeteredProvider（研究账）──→ claude_code CLI
  trace 存储与检索（SQLite FTS5，只限 train）
  EgressPool（内部网络、代理容器、凭据登记、取回记录）
memory sidecar（独立 venv）：ReMe 0.3.1.10 自带的 HttpService + 我们的 op + chroma + bge-small ONNX
egress 代理容器（K 为 proxy 时）：mitmdump + addon（allowlist、凭据归属、JSONL 记录、控制端口）
App（发布出的 W）：worker + sidecar（只检索，不开 research 作用域，不依赖桥）
```

### 1.2 自研与接入

| 部件 | 归属 | 版本、许可、哈希 | 在哪运行 | 改不改 | 接口 |
|---|---|---|---|---|---|
| Stirrup | 接入，W 侧 | 0.2.0，MIT。tag v0.2.0 = `247f24d5…`；sdist `0783046d…` 与 wheel、tag 逐字节相同 | worker 内，随包分发，不装进镜像 | W0-v6 一个字节都不改；之后由 R 改 | ctx 的六个名字（`worker_runner.py:191-239`） |
| ReMe task memory | 接入，宿主侧 | `reme-ai[core]==0.3.1.10`，Apache-2.0。wheel `53ec8403…`；PyPI sdist 与 GitHub tag 逐文件相同 | memory sidecar | 不 fork。经它自己的注册表加几个 op 子类和一个嵌入后端；参数与 prompt 只经配置覆盖 | ReMe 自带的 HTTP 服务（§5.1）；它的 LLM 走桥 |
| bge-small-en-v1.5 | 接入 | MIT。revision `5c38ec7c…`；`onnx/model.onnx` 的 sha256 `828e1496…` 与 HF LFS 一致 | sidecar | 不改 | sidecar 的 `/v1/embeddings` |
| chromadb | 接入，作 ReMe 的存储后端 | 1.5.9 | sidecar，数据落盘 | 不改 | 只经 ReMe 的 `ChromaVectorStore` |
| mitmproxy | 接入，egress 代理 | MIT。gateway 构建者钉在构建当天的稳定版，`--require-hashes`，版本与哈希写进 `docker/egress.requirements.txt` | egress 代理容器 | 不改；我们只写 addon | HTTP 代理（`HTTP_PROXY` / `HTTPS_PROXY`） |
| claude_code CLI | 外部传输 | — | 宿主子进程 | 不改，`TURN_NOTE`（`claude_code.py:70-73`）也不改 | `provider.chat`（`claude_code.py:255`） |
| Gateway、AttemptBroker、worker 壳、K、gate、release、app | 自研平台 | — | 宿主 | 按本文改 | — |
| 桥、trace 存储、egress addon 与 EgressPool | 自研平台，新写 | — | 宿主 / 代理容器 | 新写 | HTTP |
| VEL 平台层、toolcheck、causes、跨业务线导入 | 自研平台 | — | 宿主 | 按本文改 | — |

**第三方许可声明：** 集成步在 README 里加一节"第三方组件与许可"，列出 Stirrup（MIT）、ReMe（Apache-2.0）、bge-small（MIT）、mitmproxy（MIT）。ReMe 的依赖树里全是宽松许可，MPL-2.0 只出现在 certifi、bidict、tqdm、orjson 四个包上，属于文件级许可，我们不修改它们。

### 1.3 边界规则

1. 平台代码不 import 开源组件，开源组件也不 import 平台代码。Stirrup 只经过 ctx 与平台交互；ReMe 只经过 HTTP，它反过来调 LLM 也走 HTTP；mitmproxy 只在自己的容器里。宿主的 `.venv` 不装这三者的依赖。由测试守住：跑完一次 v6 尝试和一次 library 阶段之后，宿主的 `sys.modules` 里没有 stirrup、reme、chromadb、onnxruntime、agentscope、mitmproxy（§9.4）。
2. 唯一的例外是注册进 ReMe 的平台 op（`ouroloop/memory/sidecar/ops.py`）。它们在 ReMe 进程里运行，只依赖 ReMe 的公开注册表和基类（`BaseOp`、`MemoryRetrieval` 等），不 import `ouroloop` 的其他模块。
3. 适配层属于 W：`agent/stirrup_agent.py` 是包文件，R 能改；宿主不认识 Stirrup。
4. 开源组件的失败一律要报出来，不能静默吞掉：
   - sidecar 把 `success=false` 变成 HTTP 500；
   - ReMe 的校验 op 会自己吞掉 LLM 错误（§5.7 第 6 条），所以平台在每次提炼之后核对这一步在桥上的全部调用，任何一个不是 200 就报错停下；
   - 桥把账户类错误透传出来，并关闭对应作用域；
   - ReMe 的导出、载入只在日志里报错，所以平台核对条数（§5.6）。

### 1.4 分工与契约

| 构建者 | worktree / 分支 | 只改这些文件 | 交出的契约 | 完成标准 |
|---|---|---|---|---|
| gateway | `.claude/worktrees/v6-gateway` / `v6-gateway` | 新文件：`ouroloop/bridge.py`、`ouroloop/trace.py`、`ouroloop/egress/{__init__,addon}.py`、`docker/egress.Dockerfile`、`docker/egress.requirements.txt`、`tests/fixtures/reme_requests.jsonl`。改动：`ouroloop/agent/gateway.py`（usage 出口）、`ouroloop/agent/runner.py`（只改 `PackageAgent.__init__` 加一个参数、`Gateway(...)` 构造处传入、:216 的 `model_call` 处理器）。另加测试 | `Bridge.open_scope()`、`Scope`（§2.2）；`TraceStore`（§6）；`EgressPool`、`EgressSession(network, env)`（§4.4）；`ctx.view["last_usage"]` | §9.1 |
| agent | `.claude/worktrees/v6-agent` / `v6-agent` | 新文件：`ouroloop/agent/seed_v6/**`、`docker/worker-v6.Dockerfile`、`docker/worker-v6.requirements.txt`。改动：`ouroloop/agent/harness_package.py`、`ouroloop/agent/worker.py`。另加测试 | `hp.seed_package()`；`hp.LIBRARY_PATH`；`vendor` 与 `library` 两个 kind；`literal_findings(..., baseline=)`；`DockerWorker(..., network=, extra_env=, memory_swap=, tmpfs_mb=)` 与 `start(egress=)` | §9.2 |
| memory | `.claude/worktrees/v6-memory` / `v6-memory` | 新文件：`ouroloop/memory/**`（含 `sidecar/`、`__main__.py`）、`tests/fake_openai_server.py`。改动：`ouroloop/research/library.py` 只改 :307 一行。另加测试 | `MemoryClient`（§5.1）；`StreamStep`（§5.3）；`retrieval_block_v6()`（§5.4）；`import_entries()`（§5.9） | §9.3 |
| research（建议，§0.3 第 7 项） | `v6-research`，从 eb8c505 分出 | 只加新文件：`ouroloop/research/causes.py`、`ouroloop/research/toolcheck.py`、`ouroloop/research/toolcheck_harness.py`，以及测试。`PROPOSE_SESSION_V6` 的文字写在 `research/r0_v6.txt` 草稿里，由集成步搬进 `policies.py` | `causes.build()`、`toolcheck.script_check()`、`toolcheck.hook_replay()`（§7） | 各自的单元测试过，变异检查过 |
| 集成（在 slim/1 合入之后） | `v6/1`，从合入 slim/1 后的 m1 分出 | `ouroloop/bench.py`、`ouroloop/policies.py`、`ouroloop/research/{session,drafts}.py`、`ouroloop/release.py`（`PACKAGE_DATA`）、`pyproject.toml`（package-data）、`ouroloop/app.py`、`ouroloop/cli.py`（子命令别名）；`runner.py` 里的检索点、`worker_session` 资源字段、`egress` 事件；`examples/automationbench/claude-v6-{quickstart,quickstart-proxy,finance}.toml`；文档 | — | §9.4 |

**跨分支的契约（现在写死，集成步只做组合）：**
- (a) memory 分支的 `StreamStep` 接收一个 `trajectory_of(session) -> list[dict]` 可调用对象和一个 `bridge_calls_of(step) -> list[dict]` 可调用对象。memory 分支的测试用固定的消息样例和假 LLM 服务器的请求日志；接上 `TraceStore.trajectory` 和 `Scope.calls` 的组合放到集成步测。
- (b) memory 分支用同一个字面量定义 `LIBRARY_PATH = "library/task_memory.jsonl"`，只处理包的文件字典（`dict[str, str]`）和快照文本，不调用 `build_package`。集成步改成从 `hp` 导入，并加一个测试钉住两者相同。
- (c) `DockerWorker` 的新参数名就是 `memory_swap`、`tmpfs_mb`、`network`、`extra_env`，`start(egress=None)` 的 `egress` 只要有 `.network: str` 和 `.env: dict[str, str]` 两个属性。gateway 分支的 `test_egress` 用原生 `docker run` 起容器；`DockerWorker` 接 `EgressSession` 的组合放到集成步测。
- (d) `pyproject.toml` 和 `release.PACKAGE_DATA` 只由集成步改。测试从检出运行（`pythonpath` 含 `.`），构建期不需要 package-data。

**构建规则：**
- 只在自己的 worktree 里改，不碰 `.claude/worktrees/slim-1` 和主检出。
- 不往 `/Users/leon/workspace/ouroloop/.venv` 里装包。测试需要的外部环境放在 `$V6/venv-<名字>`，通过环境变量指过去（§9）。
- 不调真实模型；运行记录只读。只在自己的分支上本地提交，不 push。
- 三个分支改的文件互不重叠。
- 每个机制测试都要做变异检查（在隔离副本里去掉该机制，测试必须失败），变异检查只在隔离副本里做（规则 7）。

## 2 网关 OpenAI 兼容桥（v6 只服务 research 作用域）

**目的：** 宿主侧的开源库只要设一个 `base_url` 就能原样接入，而所有模型调用照旧经过计量、写入记录，并记到正确的预算上。

**v6 只有一个使用者：** sidecar 里的 ReMe。它用自带的 `openai` LLM 后端，总是 `stream=True`，显式传 `tools: null`，请求体里只有 `model`、`messages`、`stream`、`tools` 四个键（冒烟记录 `$V6/reme/smoke/out/fake_logs/requests.jsonl` 的 51 条 chat 请求）。嵌入在 sidecar 进程内完成，ReMe 不经 HTTP 调嵌入。

所以 `/v1/embeddings`、`/v1/models`、embed 作用域、attempt 作用域和工具调用映射在 v6 里都不做：它们没有使用者（§12）。宿主代码要嵌入时直接调 `MemoryClient.embed`。

### 2.1 端点

服务器用标准库的 `ThreadingHTTPServer`，监听 `127.0.0.1:0`（临时端口），鉴权方式为 `Authorization: Bearer <作用域 token>`。

| 端点 | 请求 | 响应 | 后端 |
|---|---|---|---|
| `POST /v1/chat/completions` | `model`、`messages`、`stream`、`stream_options.include_usage`、`max_tokens` / `max_completion_tokens`、`reasoning_effort`；`tools` 只接受 null、缺省或空列表，`tool_choice` 只接受缺省、`none`、`auto`。其余字段（如 `temperature`）照收并记录，但不转发 | 非流式返回 `chat.completion`；流式返回 SSE 的 `chat.completion.chunk`，最后是 `data: [DONE]` | 作用域绑定的计量 provider（§2.2） |

非空 `tools` 返回 400，文字为 "tools are not served on this scope"。

### 2.2 作用域、预算与并发

**接口：**
- `Bridge().start() / stop()`；
- `Bridge.open_scope("research", *, scope_id, step, provider, record, models, effort_cap, max_concurrency) -> Scope`；
- `Scope.token`、`Scope.base_url`（`http://127.0.0.1:<port>/v1`）；
- `Scope.set_step(step)`：sidecar 的 token 在启动时就定死了，每一批的 step 标签只能由宿主在作用域上设置，ReMe 改不了；
- `Scope.calls(step=None) -> [{call_id, step, op, status, ms}]`：给平台做"这一步有没有失败的调用"的核对（§1.3 第 4 条）；
- `Scope.close()`：token 立即失效。

| 项 | 规则 |
|---|---|
| `model` 字段 | `research/reme` 或 `research/reme:<op>`。op 来自 ReMe 的按 op 配置 LLM（`base_op.py:50` 的 `llm` 参数）：flow 里写 `SuccessExtraction(llm="reme_success")`，配置 `llms.reme_success.model_name = research/reme:success`。不在 `models` 列表里的返回 404 |
| 后端与计量 | `MeteredProvider(build_provider(research.llm))`，与 `_research_completion`（`bench.py:3043`）相同；`operation = <scope_id>:research:reme[:<op>]`；`max_retries=0` |
| 预算 | 活动的账本，与 R、VEL 的提炼记在一起 |
| effort | 请求里的 `reasoning_effort` 经 `cap_effort`（`gateway.py:72-77`）截到 `effort_cap`（K 统一为 medium） |
| 角色 | research 作用域的调用不属于任何尝试，所以不受 manifest roles 约束 |
| 并发 | 每个作用域一个信号量，上限 `max_concurrency`（K 的 `library.research_concurrency`，v6 为 4）。超出时排队，不拒绝。理由：ReMe 的三种提炼是并行跑的（`|`），每批 4 道题，再叠加 8 路尝试，claude CLI 的并发会超过 12 |

### 2.3 消息映射

| OpenAI | Gateway / provider |
|---|---|
| 第一条 `system` | 映射为 provider 的 system。之后的 system 改为 user，文本前加 `[instructions]`，与适配层同一规则 |
| `user`（字符串或 text part） | `{role: user, content}`；含非文本 part 时返回 400 |
| `assistant`（只有文本） | `{role: assistant, text, tool_calls: []}`；带 `tool_calls` 时返回 400 |
| `tool` | 返回 400 |
| 形状检查 | 调 provider 之前先过 `malformed`（`gateway.py:113`）；不通过返回 400，附带它给出的文字 |
| 回复 `ChatTurn(text, tool_calls, stop, usage)` | `message{role: assistant, content: text}`。<br>`finish_reason`：`max_tokens` 对应 `length`，`refusal` 对应 `content_filter`，其余为 `stop`。provider 在无工具时仍返回了工具调用的，按 502 `provider_error` 处理。<br>`usage{prompt_tokens = input + cache_read + cache_creation, completion_tokens, total_tokens}` |

### 2.4 流式、错误与重试

- **流式：** provider 本身不流式。桥拿到完整回复后，依次发出 role chunk、content chunk、finish chunk；请求了 `include_usage` 时再发 usage chunk；最后发 `[DONE]`。
- **错误：** 统一用 OpenAI 的形状 `{"error": {"message", "type", "code"}}`。

  | 状态码 | 情形 |
  |---|---|
  | 401 | 未知或已关闭的 token |
  | 404 | 未知 model |
  | 400 | 请求形状不对，或带了工具 |
  | 429 `budget_exhausted` | 预算预留被拒（账本里不记账） |
  | 502 `provider_error` | provider 出错。若按 `claude_code.py` 的分类属于账户类错误，返回 `code=account`，同时关闭该作用域，免得这类错误被 ReMe 的重试吞掉 |

- **重试：** 桥自身不重试。ReMe 一侧在配置里设 `llms.<name>.max_retries=1`、`raise_exception=true`（`base_llm.py:24-25`），op 一侧在 flow 里写 `raise_exception=True`、`max_retries=1`（`base_op.py:63-64`）。源码里已经有这些键，所以不需要"同一请求第 3 次起返回 429"之类的兜底。

### 2.5 记录

- 每次调用向作用域的记录汇写一行 `bridge_call`：`{call_id, scope_id, step, op, model, request_digest, messages, extra_fields, response, usage, cost, elapsed, status}`，全文保留。研究调用的输入是 train 轨迹，可以全记。research 作用域的记录汇是活动目录下的 `rsi/bridge_calls.jsonl`，由 VEL track 打开。
- token 不进任何记录、K 或日志。

## 3 agent：Stirrup 作为 W0-v6 的 agent 程序

### 3.1 种子包 `ouroloop/agent/seed_v6/`

| 文件 | 内容 | 来源 |
|---|---|---|
| `manifest.json` | `api_version: harness_package.v2`；`agent: {entrypoint: "agent/stirrup_agent.py:run", roles: ["act", "aux"]}`；hooks 依次为 `ledger`（after_tool）、`triage`（before_tool）、`verify`（on_finish），都在 `middlewares/write_guard.py` 里；包工具 `plan_changes` | — |
| `vendor/stirrup/**` | Stirrup 0.2.0 `src/stirrup/` 下的全部 42 个文件（包括 `integrations/`、`prompts/*.txt`、`py.typed`），共 493,075 B，与 sdist 逐字节相同。另加 `vendor/stirrup/LICENSE`（MIT 原文，1,076 B）和 `vendor/stirrup/PROVENANCE.json`（上游地址、tag、commit、sdist 与 wheel 的 sha256、每个文件的 sha256）。这两个文件 R 改不了（§3.6） | `$V6/stirrup/stirrup-0.2.0/` |
| `agent/stirrup_agent.py` | 适配层（§3.2），由 `$V6/stirrup/demo/bundle_src/agent/stirrup_agent.py`（364 行，19,437 B）定稿，修 §3.2 末的 7 处 | 自研 |
| `agent/default_agent.py` | `ouroloop/agent/default_agent.py` 的逐字节拷贝（17,479 B，测试钉住两者 sha 相同）。适配层复用其中的 `_execute`、`_run_hooks`、`_turns_left`、`MAX_PUSHBACKS` | 自研 |
| `agent/http_fetch.py` | 出站工具（§4.4）：只用标准库 `urllib`，方法限 GET、HEAD、POST，按环境里的 `HTTP_PROXY` / `HTTPS_PROXY` 走代理，输出截到 `ctx.view["max_output_chars"]`。只有 worker 环境里有 `OUROLOOP_EGRESS=1` 时适配层才把它列给模型 | 自研 |
| `middlewares/write_guard.py` | ledger、triage、verify 三个 hook，文字与常量照 MEMORY_AGENT_ITERATION §4.2 | 移植自 StarHarness 的写前分诊和 SWE-agent 的 submit review |
| `tools/plan_changes.py` | 同上，照 §4.2 | 移植自 LangChain 的 Todo |
| `memory.md` | 空 | — |
| `tests/test_write_guard.py`、`tests/test_stirrup_adapter.py` | 包内测试，用 fake ctx 和合成数据，由草稿 smoke 运行 | — |

种子合计约 53 个文件、约 560 KB。

### 3.2 两个接缝

| Stirrup 的接口 | 落到 | 规则 |
|---|---|---|
| `LLMClient.generate(messages, tools)` | `ctx.model(messages, tools, role, effort)` | 第一条 system 原样传。之后的 system（来自子 agent）改为 user，前加 `[instructions]`。user、摘要和警告消息都当作 user。assistant 转成 text 加 `tool_calls[{id, name, args}]`。一轮里的多条 ToolMessage 合成一条 `{role: tool, results: [{id, name, output, is_error: not success}]}` |
| 摘要请求 | `role="aux"` | 用适配层 logger 的回调识别：`context_summarization_start` 置标志，`context_summarization_complete` 或异常清标志；标志在时的 generate 一律走 aux，并记 mark `stirrup_summary`。不再靠摘要 prompt 的前缀识别，这样 R 改了 `prompts/message_summarizer.txt` 也不会把摘要误发成 act |
| 上下文溢出 | `ContextOverflowError` | 网关返回的、看起来是上下文溢出的拒绝，转成这个异常；以 refusal 停止时，用它的文字结束 |
| `context_window_tokens` | W 里的常量 `CONTEXT_WINDOW_TOKENS = 200_000` | R 能改；Stirrup 在用量达到 0.7 时开始摘要 |
| `TokenUsage` | `ctx.view["last_usage"]` | 由 gateway 构建者交出（§4.2）。`TokenUsage.input = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`：claude_code 把缓存部分单独报，不加的话摘要阈值会被严重低估。拿不到时退回"字符数 / 4"的估计 |
| `Tool(name, description, parameters, executor)` | 直通的 Tool，带原 JSON schema | 执行器依次调用 `before_tool`、`default_agent._execute`（基础工具走 `ctx.tool`，包工具走各自入口，`read_skill` 走 `ctx.skills`）、`after_tool` |
| 被 before_tool 挡下 | 该调用返回 "Stopped before execution: …"；同一轮里剩下的调用（**包括 finish**）返回 "Not executed: …" | 分别记 mark `tool_blocked` 和 `tool_skipped` |
| after_tool 给出反馈 | 在这一轮的结果之后加一条 user 消息 | — |
| finish 工具 | 外面包一层 `on_finish`：遇到 block 或 feedback 时返回 `success=False`（即 pushback），最多 `MAX_PUSHBACKS` 次，最后一次 act 不触发。成功时返回 `finish_params.reason`，作为 `run()` 的字符串结果 | 记 mark `pushback`；结束时记 `stirrup_end`（finished、implicit、轮数、摘要次数、pushback 次数、maxrss） |
| 只回文字的轮 | 连续第 `IMPLICIT_FINISH_AFTER = 2` 次只回文字时，适配层把这次回复改成一次 finish 调用（reason 为这段文字），照样过 `on_finish` | 记 mark `implicit_finish`；R 能改这个常量 |
| Stirrup 自带的工具（calculator 等） | 种子默认不开；开了的话由适配层执行 | 每次调用记 mark `stirrup_tool`（name、args、output 前 2,000 字符），因为 broker 看不到它们 |
| 并发 | 所有 ctx 调用共用一把 `threading.Lock` | `exchange()`（`worker_runner.py:122-133`）不是线程安全的 |
| 子 agent | `Agent.to_tool()` 可用，但种子里不放 | 进出子 agent 时要保存并恢复适配层"当前在哪个 assistant 轮"的状态，否则 hook 的状态会记到错误的轮上 |

宿主会把不认识的 mark kind 记成 `package_mark`（`gateway.py:425-445`），所以 `MARK_KINDS`（`gateway.py:52`）不用改。

**定稿时要修的 7 处（都由读源码确认，每处一个测试并做变异检查）：**

1. **async 的包工具或 hook 必坏。** 原型用 `asyncio.run(_main)` 跑整个循环（`stirrup_agent.py:364`），工具执行器是 async 的（:294）；而 `default_agent._call`（`default_agent.py:93-97`）遇到 awaitable 又调一次 `asyncio.run`，hook（:171）和包工具（:284）都走它，在运行中的事件循环里会抛 RuntimeError。包 API 明确允许 async 入口，原型只测过同步工具。改法：适配层把 `DA._call` 换成一个认得运行中循环的版本：返回值是 awaitable、且本线程已有运行中的循环时，把它交给一个常驻的辅助线程（自带事件循环，只建一次，pids 只多 1 个）跑完再返回；否则照旧 `asyncio.run`。主循环此时正阻塞在这次工具调用里，所以 ctx 调用仍是一次一个。
2. **同一轮里被挡下之后，finish 照样执行。** 原型的 finish 包装（:322-335）不看 `self.blocked`。Stirrup 把 finish 排在同一轮最后执行（`core/agent.py:1305-1310`），所以"写操作被分诊挡下 + finish"的一轮会直接结束尝试，被挡的写再也不会发生。改法：finish 先查这一轮是否已被挡下，是就返回 "Not executed" 并记 `tool_skipped`。
3. **finish 的 `paths` 是必填。** `FinishParams.paths`（`tools/finish.py:16-19`）没有默认值，模型只给 `reason` 时 Stirrup 回 "Tool arguments are not valid"（`core/agent.py:1192-1199`），白费一轮。我们没有执行环境，`paths` 没有意义。改法：适配层的 finish 用 `reason` 必填、`paths` 默认 `[]` 的参数模型。
4. **摘要请求的识别**改用 logger 回调（见上表）。
5. **usage** 读 `ctx.view["last_usage"]`，三项输入相加（见上表）。
6. **只回文字的轮**按上表的隐式 finish 处理。理由：`TURN_NOTE`（`claude_code.py:70-73`）告诉模型 "To end your turn without calling a tool, leave tool_calls empty"，而在 Stirrup 下只回文字的轮会收到 "Please continue the task"（`core/agent.py:1586-1595`），可能一直跑到 50 轮上限。
7. **mark 名**定为 `stirrup_end`（原型叫 `stirrup_run`），并加 `implicit` 字段。v6 的读数一律读它，不读 `end.stop`：用 finish 结束时，`end.stop`（`runner.py:194`）记的是最后一次 act 的停止原因 "tool_use"。runner 不改，否则 v3 的字节会变。

### 3.3 Stirrup 上的扩展

| 扩展 | 放在哪 | 做什么 | 针对的问题 |
|---|---|---|---|
| 写入守卫 | `middlewares/write_guard.py`，由适配层的 hook 执行 | 记台账；第一次写之前做分诊；提交前做双向核对 | 失败原因 #1、#2、#3、#7（MEMORY_AGENT_ITERATION §2.2） |
| 日期锚 | 分诊消息里由 `DATE_ANCHOR` 控制的那一句 | 用已读数据中最晚的日期作为"今天"的下界，并说明请求之外附带的日期不是任务日期 | #8：103 次尝试用了真实日期，其中 76 次失败。`TURN_NOTE` 里已经有一句同义的话，没挡住 |
| 结构化计划 | `tools/plan_changes.py` | 让 agent 写出 changes 和 `excluded` | 给 verify 和 R 将来写的扫描 hook 打底 |
| 引用行 | 平台检索块里的要求（§5.4），适配层不用改 | finish 工具 `reason` 参数的最后一行写 `library-used: <id8,…>` | 逐条记功（§5.5） |
| 真实 usage | 适配层读 `ctx.view["last_usage"]` | 摘要阈值按真实的输入 token 计算 | 原型只能按"字符数 / 4"估 |
| 出站 | `agent/http_fetch.py` | K 为 proxy 时经 egress 代理访问 allowlist 里的主机 | 生产路径要联网（用户 2026-10-08） |
| 线程变量 | 适配层在 import Stirrup 之前把 `OPENBLAS_NUM_THREADS`、`OMP_NUM_THREADS`、`MKL_NUM_THREADS` 设为 1；K 的 `worker.env` 里也设一遍（§4.1） | 避免在多核机器上 pids 上限打断 import | 实测：18 核机器、pids 16 时，import 抛出 `KeyboardInterrupt` |

### 3.4 Stirrup 的运行设置

| 设置 | 值 | 理由 |
|---|---|---|
| `max_turns` | 剩下的 act 调用数（来自 `ctx.limits`） | 真正的上限在宿主那里 |
| `run_sync_in_thread` | False | 线程会占用 pids，而且 `exchange()` 不是线程安全的 |
| `cache_on_interrupt` | False | 不写 `~/.cache/stirrup`，也不装 SIGINT 处理器 |
| `skills_dir` | None | Stirrup 自带的 skill 加载器会直接读文件；skills 只经 `ctx.skills` 提供 |
| 代码执行、web 工具、calculator | 种子里都不开 | 世界只经 `ctx.tool` 访问。calculator 和本地代码执行在已经隔离的 worker 里运行，R 可以开；E0e 的条件成立时，在冻结前就在种子里开 calculator（§10） |
| 系统提示 | Stirrup 自己的模板，后接 "Follow these instructions from the User:"，再接我们的 system | 0.2.0 没有直通选项。保持上游原样，要不要改交给 R |
| 来源检查 | 适配层拒绝从 site-packages 导入 Stirrup | 实际运行的必须是包里的那份字节 |

### 3.5 R 能改它的全部源码

- `vendor/` 下除 `LICENSE` 和 `PROVENANCE.json` 外的任何文件 R 都能改。改 `vendor/` 或 `agent/` 都算 L4b，也就是 agent 程序的改动，每轮至多一个（MEMORY_AGENT_ITERATION §1.4）。
- 宿主不信任 vendor 里的代码。它只在 worker 里运行，权限和包工具相同。
- 发布说明按 K 里种子 ref 对应的文件哈希，列出相对上游的 diff，不读包里那份 PROVENANCE。这就是像应用一样发布时的变更记录。升级 Stirrup 是换种子，也就是换 K，不是 R 的候选。
- R 的工作树：`WORK.max_bytes`（`worker_runner.py:58`）已经从 K 的 `max_package_bytes` 取值。`core/agent.py` 86 KB，小于 `READ_CHARS` 131,072，一次读得完。
- 要写进 R0 v6 的几个陷阱（§7）：
  - session 里实际运行的是 `SessionAgent`，子类覆盖 `Agent` 不会生效，必须直接改 vendor 里的 `core/agent.py`；
  - 回合警告的阈值是 20，所以上限 ≤20 时每一轮都会收到警告；
  - system、task 和工具 spec 加起来超过上下文窗口的 70% 时，每一轮都会触发摘要；
  - Stirrup 的 `fetch_web_page` 用不了：它 `trust_env=False`（`tools/web.py:488-492`），不认代理环境变量，还在本地解析域名、钉住 IP、拒绝私有地址（:110-170、:188-205）。出站用 `agent/http_fetch.py`。

### 3.6 包形状的改动（`harness_package.py`）

| 改动 | 位置（eb8c505） | 对 v3/v5 的影响 |
|---|---|---|
| 新 kind `vendor`，归入 CODE_KINDS。PATTERNS 加 `^vendor/[a-z0-9_]+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+$`；内容必须是 UTF-8 文本、不含 NUL（空文件如 `py.typed` 允许）；`..` 照旧由 `classify` 拒绝 | :39-57 | K 里没列 `vendor` 的照旧拒收 |
| `vendor/stirrup/LICENSE` 和 `vendor/stirrup/PROVENANCE.json` 存在于种子时，`check_shape` 要求候选里这两个文件与种子逐字节相同（种子字节从 `ouroloop/agent/seed_v6/` 读） | `check_shape`（:346） | 只在 K 带 `vendor` 时生效 |
| 新 kind `library`，只在宿主侧使用，路径固定为 `LIBRARY_PATH = "library/task_memory.jsonl"`：<br>• 不进 `run_files`，所以不进 worker，`runner.py:204` 自然不会加载它；<br>• 不进 R 的工作树，R 的补丁碰到它就算结构错误（报错文字照 `library.py:404`）；<br>• `check_shape` 对它单独按 `max_library_bytes` 计量，不计入 `max_package_bytes` | 同上；`files_of` 在 :606 | 同上 |
| `literal_findings(files, literals, *, allowed, baseline=None)`：给了 `baseline`（种子的文件字典）时，`vendor/**` 文件只扫相对 baseline 同名文件新增或改动的行（difflib 的 insert / replace 段）。理由：凡是某道 train 题断言的答案值，即使是纯数字也在禁止名单里（:485-497），Stirrup 源码里有大量 20、30 这样的小整数，不排除就会让种子和 R 的每个候选都过不了草稿检查 | :507 | `baseline=None` 时行为与今天相同 |
| `Limits` 新增 `max_file_bytes` 和 `max_library_bytes`，默认 None；`to_json` 只在非 None 时输出它们 | :65-115 | v3/v5 的 K 字节不变，由金标测试守住 |
| 新函数 `seed_package(objects, directory, limits) -> ref` | :601 旁 | — |
| `package_api` 仍是 v2，manifest 结构不变；`vendor` 和 `library` 由 K 的 components 控制 | — | — |

## 4 worker 与 K 的生产级设置

### 4.1 K 的 `worker` 表

这是一张新表，加进 `PRESET_ONLY_TABLES`（`bench.py:1963`）。

| 键 | 含义 | v6 值 | 依据 |
|---|---|---|---|
| `image` / `image_id` | 镜像 tag 与内容 id。bootstrap 时冻结（`bench.py:2283`），运行时按 id 启动（`worker.py:197`） | `ouroloop-worker:py3.12-stirrup020-v6` / 构建出的 sha256 | 用新 tag，不覆盖 `ouroloop-worker:py3.12` |
| `cpus` | `--cpus` | `"1"` | 一次尝试的大部分时间在等模型；E0f 实测 CPU 后再定 |
| `memory` / `memory_swap` | `--memory`，以及同值的 `--memory-swap`，也就是关掉 swap | `"512m"` / `"512m"` | 实测峰值 78–80 MB（50 轮、约 59 万字符历史时为 79.5 MB），留 6 倍余量给 R 写的代码。关掉 swap 后，不会再出现 64m、96m 那样"靠 swap 才跑过"的情况 |
| `pids` | `--pids-limit` | 128 | 线程变量设为 1 后，8 个就够用；另外给 anyio 线程池（上限 40）、适配层的辅助线程、主线程和余量留位置 |
| `tmpfs_mb` | `/tmp` 的大小 | 256 | 包加字节码不到 5 MB，剩下的留给包工具写临时文件 |
| `network` | `"none"`，或 `{mode: "proxy", allow: [{host, port, methods}]}` | 基准用 `"none"` | 基础工具都走 broker，联网只会增加泄题面 |
| `env` | 追加在 `WORKER_ENV`（`worker.py:61`）之后的环境变量 | 三个线程变量，都设为 1 | 见 §3.3 |
| `request_seconds` | 每次 base call 的时间上限（K 的 `test_timeout_seconds`） | 30 | 不变 |
| `max_output_chars` | 工具输出的截断长度（`WorkerLimits`，`worker.py:117-121`），改为从 K 读取 | 20,000 | v3 的 22,172 次调用里只有 12 次被截断，p99 不到 9k |

### 4.2 `worker.py`、`gateway.py`、`runner.py` 的改动

| 文件 | 改动 | 对 v3/v5 的影响 |
|---|---|---|
| `worker.py` | • `DockerWorker.__init__`（:192）已有 `cpus, memory, pids`；新增 `memory_swap=None, tmpfs_mb=64, network="none", extra_env=()`。<br>• `command()`（:200-207）里的 tmpfs 字面量改成参数；`memory_swap` 非 None 时才加 `--memory-swap`。<br>• 网络：`network="none"` 时用 `--network none`，`start(egress=...)` 传了会被拒绝；`network="proxy"` 时 `start(egress=…)` 必须给，用 `--network <egress.network>`，并经 `env -i` 追加 `egress.env`（`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`、`OUROLOOP_EGRESS=1`）。<br>• `--read-only`、`--user 65534:65534`、`--cap-drop ALL`、`no-new-privileges`、`python -I` 继续写成字面量。<br>• `docker_worker(image, resources=None)`（:255-258）。<br>• `describe()` 带上 tmpfs、swap 与网络模式 | 默认值就是今天的字面量，所以不传参数时，命令行逐字节不变 |
| `gateway.py` | 新增 `Gateway(..., usage_out=False)`；为 True 时，`model()` 的返回值（:322）多一个 `usage` 字段 | 默认是 False |
| `runner.py` | `PackageAgent.__init__` 加 `usage_out=False` 并传给 `Gateway(...)`（:167）；`usage_out` 打开时，`model_call` 处理器（:216）的回复多带 `view: {"last_usage": usage}`。worker 端会把它就地合并进 `ctx.view`（`worker_runner.py:202-210`），所以 runner 文件本身一个字节都不用改 | v3 的 `attempt_behaviour_v3.json` 不变 |
| `worker_runner.py` | **不改** | `runner_digest`（:75）和所有已冻结的镜像都对得上 |

### 4.3 镜像

- **Dockerfile：** `docker/worker-v6.Dockerfile` 包含：
  - `FROM python:3.12-slim@sha256:05cda977…`；
  - `pip install --require-hashes --no-deps -r docker/worker-v6.requirements.txt`，即 Stirrup import 闭包需要的 42 个包，由 `$V6/stirrup/docker-ctx/requirements.txt` 定稿，不含 litellm；
  - `COPY ouroloop/agent/worker_runner.py /runner/`。
- **Stirrup 本身：** 不装进镜像，随包分发。
- **体积与架构：** 镜像约 687 MB（原镜像 214 MB）。只构建 linux/arm64，即本机架构；amd64 见 §12。
- **pillow：** moviepy 2.2.1 把 pillow 限制在 12 以下，锁里是 11.3.0，有 CVE。StarHarness 的做法是覆盖到 ≥12.3.0。agent 构建者在镜像上加一层 pytest，覆盖 pillow 后跑 Stirrup 自带的 198 个离线测试：全部通过就采用覆盖；不通过就把这一项记进风险（§11）。
- **构建命令：** 由集成步写进 CLAUDE.md 的测试一节。

### 4.4 网络策略：`none` 与经 egress 代理的 allowlist

| 部件 | 做法 |
|---|---|
| 内部网络 | 每个活动建一个 `docker network create --internal ouroloop-egress-<scope8>`。worker 只接这个网络，没有默认路由，直连外部一定失败；容器名 `egress` 由 Docker 的内置 DNS 解析 |
| 代理 | 运行容器 `ouroloop-egress:v6`，由 `docker/egress.Dockerfile` 构建：基础镜像是按 digest 钉住的 `python:3.12-slim`，`pip install --require-hashes` 装 mitmproxy，入口为 `mitmdump --mode regular --listen-port 3128 --set ignore_hosts='.*' -s /egress/addon.py`（HTTPS 一律透传，不做中间人）。<br>容器同时接入内部网络和默认的 bridge 网络 |
| addon（`ouroloop/egress/addon.py`） | • `http_connect`：校验凭据；allowlist 按 `host:port` 精确匹配，且该项的 `methods` 含 `CONNECT`；不符合就回 403 并断开。<br>• `request`（明文 HTTP）：校验凭据、allowlist 与方法。<br>• 记录：每个请求或隧道一行 `{token_id, method, host, port, path（只有明文 HTTP 有）, status, bytes_up, bytes_down, ms, allowed}`，隧道的字节数取自 `tcp_message`。`token_id` 是凭据的 sha256 前 12 位，凭据本身不记。<br>• 控制端口：在 `running` 钩子里开一个只发布到宿主 127.0.0.1 的端口，JSON 行协议：`register`、`revoke`、`drain(token_id)` |
| 归属 | 每个尝试一个凭据：`http://attempt:<token>@egress:3128`。宿主开会话时登记，会话关闭时撤销，之后再用就是 407 |
| 只读会话 | smoke、`check_package`、toolcheck 的脚本执行、V2c 回放这些只读会话发只读凭据：只放行 GET 和 HEAD，不允许 CONNECT。broker 的 `read_only` 管不到 egress，没有这一条，hook 和检查会话就能经 egress 往外写。<br>同一次尝试里的 hook 与 agent 在同一个进程里，只能共用这次尝试的凭据，所以它们的出站请求照样记录、归属到这次尝试，但不单独拦截 |
| 记录进 trace | 会话结束时，宿主经控制端口取回这个凭据的所有行，写成该尝试 transcript 里的 `egress` 事件，trace 里因此可以检索到（§6） |
| 看不到什么 | HTTPS 只能看到 host:port 和字节数，看不到路径和内容。将来要做内容检查，只需去掉 `ignore_hosts` 并给 worker 装 CA（§12） |

**接口：** `EgressPool(policy, scope_id, *, image="ouroloop-egress:v6").start()`；`EgressPool.session(attempt_id, *, read_only=False) -> EgressSession(network, env)`；`EgressSession.close() -> list[event]`；`EgressPool.stop()`。

**真实使用者：** 种子的 `agent/http_fetch.py`（只在 `OUROLOOP_EGRESS=1` 时列给模型），以及 App 的生产路径：K 的 `network=proxy` 贯通 `isolated_worker` 和 App。§9.4 的 quickstart-proxy 变体走完这条路径。

## 5 memory：ReMe 原样负责提炼，VEL 作平台层

### 5.1 进程模型

**启动方式**
- sidecar 的入口是 `ouroloop/memory/sidecar/serve.py`，用 memory venv 里的 python 3.12 启动，端口由 `MemoryClient` 选好传入。
- 工作目录设在 `~/.cache/ouroloop/memory-run/<scope8>/`：在仓库之外，也没有 `.env`。ReMe 的 `./logs` 会写在这里。
- chroma 的数据路径用绝对路径，指向活动目录下的 `memory/chroma/`。
- ReMe 以 `Application(..., enable_load_env=False, working_dir=<工作目录>)` 构造。
- 进程环境里不带代理变量，也不带 HF token，并设置 `HF_HUB_OFFLINE=1`、`HF_HUB_DISABLE_IMPLICIT_TOKEN=1`、`ANONYMIZED_TELEMETRY=False`。

**HTTP 服务：用 ReMe 自带的，不另写分发器**
- `serve.py` 直接构造 ReMe 的 `HttpService(app=app)`（`reme/core/service/http_service.py:18-93`；冒烟 C 用的就是它），在 `service.http_service` 上：
  - 去掉它默认加的 `CORSMiddleware`（`allow_origins=["*"]`，:33-39），并设 `http.host=127.0.0.1`（`service.yaml` 默认 0.0.0.0）；
  - 加 bearer 鉴权中间件，所有路由都要 token；
  - 加一个中间件，把响应体里 `success: false` 的改成 HTTP 500；
  - 挂两个路由：`POST /v1/embeddings`（与 ReMe 共用同一个嵌入对象）和 `GET /ol/health`（ReMe 版本、锁摘要、模型 sha、各集合条数）。ReMe 自己已有一个 `/health`，我们不覆盖它。
- 然后调 `service.run()`。每个 flow 的路由就是 `POST /<flow 名>`；SIGTERM 由 uvicorn 走 lifespan 关闭，ReMe 在其中 `app.close()`。

**进程里有什么**
- ReMe 0.3.1.10 的 Application 与 procedural memory 的 op（`reme/extension/procedural_memory`）。
- 我们注册的 op（`sidecar/ops.py`，下表）。
- 嵌入后端 `local_onnx`，由 `$V6/reme/smoke/local_embedder.py` 定稿（冒烟 B 已经跑通，CLS 池化加 L2 归一化，截断到 512 token），按模型 sha 在进程内做成单例。
- 一个 chroma `PersistentClient`（遥测关闭），所有集合共用。

**注册的 op（`sidecar/ops.py`，约 150 行，只依赖 ReMe 的基类）**

| op | 做什么 |
|---|---|
| `CollectionMixin` | 覆盖 `vector_store` 属性：按请求里的 `collection` 返回该集合的 `ChromaVectorStore` 实例（共用 client 与嵌入对象，按名字缓存）。集合名必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{1,510}[A-Za-z0-9]$`（chroma 1.5.9 的规则，试验里报过错）。写操作按集合加锁串行 |
| `OlMemoryAddition`、`OlDumpMemory`、`OlLoadMemory` | `CollectionMixin` 加 ReMe 原有的 `MemoryAddition`、`DumpMemory`、`LoadMemory`，逻辑不改 |
| `OlFilteredMemoryRetrieval` | `CollectionMixin` 加 `MemoryRetrieval` 的小子类：只把 `filters=None`（`memory_retrieval.py:43-47`）换成请求里的 `where` 条件，并把相似度另存进 `metadata["retrieval_score"]` |
| `OlGetById` | 按 id 取条目（`ChromaVectorStore.get`），写入前查重用（§5.7 第 12 条） |
| `OlRetireById` | 按 id 从集合里删除（`ChromaVectorStore.delete`） |
| `BatchOnlyDeduplication` | `MemoryDeduplication` 的子类，只把 `_get_existing_task_memory_embeddings` 改成返回空表：批内去重照旧，跟库比的去重交给平台（§5.7 第 1 条） |

**flow（`sidecar/flows.yaml`，由 K 的 library 表生成，进身份）**

| flow | flow_content |
|---|---|
| `summary_task_memory_v6` | `TrajectoryPreprocess(raise_exception=True) >> (SuccessExtraction(llm="reme_success", raise_exception=True) \| FailureExtraction(llm="reme_failure", raise_exception=True) \| ComparativeExtraction(llm="reme_comparative", raise_exception=True)) >> MemoryValidation(llm="reme_validation", raise_exception=True) >> BatchOnlyDeduplication(raise_exception=True)`；K 的 `origins` 关掉某种来源时，从并行段里去掉对应的 op |
| `add_task_memory_v6` | `OlMemoryAddition()` |
| `retrieve_task_memory_v6` | `OlFilteredMemoryRetrieval()`：不放 `BuildQuery`、`RerankMemory`、`RewriteMemory`，查询由平台直接给出，所以检索流里没有任何会调 LLM 的 op |
| `get_task_memory_v6` / `retire_task_memory_v6` | `OlGetById()` / `OlRetireById()` |
| `dump_memory_v6` / `load_memory_v6` | `OlDumpMemory()` / `OlLoadMemory()` |

每个 op 的 `max_retries=1`。各个 flow 的 `parameters` 里写明请求字段（`collection`、`where`、`top_k` 等）；ReMe 的请求模型是 `extra="allow"`（`reme/core/schema/request.py:9`），多余字段也能透传，写明是为了文档和校验。

**集合**
- 活库每个活动一个集合，名为 `ol-live-<scope8>`；每个快照一个集合，名为 `ol-snap-<快照摘要16>`。
- 快照集合超过 8 个时，按最近最少使用删除。

**ReMe 的 LLM**
- 有 research 作用域时（只在 `--phase library`）：`llms.reme_success / reme_failure / reme_comparative / reme_validation` 都用 `openai` 后端，`base_url` 指向桥的 research 作用域，`api_key` 用该作用域的 token，`model_name = research/reme:<op>`，`max_retries=1`，`raise_exception=true`，`request_interval=0`。这与冒烟 B 的配置相同，只是把假的 base_url 换成了桥。
- 没有 research 作用域时（dev、test、App，以及只做检索的场合）：所有 llms 指向一个关闭的本地端口（`http://127.0.0.1:9/v1`），任何 LLM 调用都会失败并变成 500。检索流本来就不调 LLM，所以照常工作。

**启动检查**
- venv 的 `pip freeze` 必须与锁文件 `ouroloop/memory/sidecar/requirements-macos-arm64.lock` 完全一致。锁文件由 `$V6/reme/reports/venv-reme.lock.txt` 定稿：161 个包，带哈希，按 `--uploaded-prior-to 2026-06-04` 解析。
- 模型文件的 sha256 必须等于 K 里记录的值。
- 任何一项不符都拒绝启动，不会改用别的模型。

**资源：** 常驻约 500 MB，峰值约 860 MB，所以放在宿主侧，不进 worker。

**宿主侧客户端**

`ouroloop/memory/client.py` 里的 `MemoryClient`，基于 httpx。

| 方法 | 作用 |
|---|---|
| `start(*, venv, workdir, chroma_path, settings, llm=None)` / `stop()` | 启动、停止 sidecar。`settings` 是 K 的 library 表；`llm` 是桥的 `Scope` 或 None |
| `summarize(trajectories, *, step)` | 调 `summary_task_memory_v6`，返回 `memory_list` |
| `get(ids, collection)` / `add(memories, collection)` | 查重、写入 |
| `retrieve(query, collection, *, where, top_k)` | 检索 |
| `retire(ids, collection)` | 退役 |
| `dump(collection) -> str` / `load(jsonl, collection)` | 导出、载入快照：平台做规范化并核对条数（§5.6） |
| `embed(texts) -> list[list[float]]` | 嵌入 |
| `health()` | 健康检查（`/ol/health`） |

**安装：**
- `python -m ouroloop.memory setup [--venv PATH]`：按锁文件建 venv（`python3.12 -m venv` 加 `pip install --require-hashes --no-deps`），并核对 HF 缓存里的模型。
- `python -m ouroloop.memory check`：只做核对。
- 集成步在 `cli.py` 里加一个同名子命令作为别名。

### 5.2 ReMe 的哪些部分用、哪些不用

| ReMe 的流或能力 | v6 | 怎么用 | 理由 |
|---|---|---|---|
| `summary_task_memory`（预处理 → 成功 / 失败 / 对比提炼 → 校验 → 去重） | **原样使用**，只把最后的去重换成 `BatchOnlyDeduplication` | prompt 不改。参数：`success_threshold=1.0`、`enable_soft_comparison=true`、`enable_similarity_comparison=false`、`validation_threshold=0.6`、`similarity_threshold=τ_batch`（初值 0.85，E0c 定） | 这正是要接入的提炼。阈值 0.6 的理由见 §5.7 第 14 条 |
| `add_task_memory` | 使用（绑集合） | 只写入盖好出处、通过了平台检查的条目 | — |
| `retrieve_task_memory` | 只用其中的 `MemoryRetrieval`（加过滤的子类） | 查询由平台给；rerank、rewrite 和 LLM 建查询都不用 | 原流的 `filters=None`，分不出业务线和 split |
| `record_task_memory` | 不用 | 由 VEL 的记功代替 | 它在调用方的副本上加计数，会丢计数；会把相似度写成校验分；对同屏展示的每一条都记功 |
| `delete_task_memory`（α/β） | 不用 | 由 VEL 的退役代替，执行时用 `OlRetireById` | β 是绝对通过率，而我们的记功是相对于基线 b(t) 的 |
| `dump_memory` / `load_memory` | 使用（绑集合） | 做快照（§5.6） | — |
| 存储后端 | chroma | — | `local` 后端只在 close 时落盘，SIGKILL 会把数据全部丢掉，冒烟里复现过 |
| 自带的 HTTP 服务 | 使用 | 加鉴权、错误转换、两个路由 | 不再自写 `/flow/{name}` 分发器 |

### 5.3 写入路径：`StreamStep`（`ouroloop/memory/track.py`）

在 v6 K 下，bench 的流循环（`library_track`，`bench.py:3190-3452`）照旧负责调度尝试，每批 `stream_batch=4` 道 train 题。每道题跑完后调用 `StreamStep.process(task, first, retry, baseline)`：

| 步 | 做什么 | 依据 |
|---|---|---|
| 1 | 首次尝试从活库检索，按留一排除本题来源的条目，并记录 `library_retrieval`（§5.4） | 现有的流 |
| 2 | 失败的题按 VEL 现有规则重试一次：附上失败断言的类型与参数投影（`retry_note`，`library.py:534`），只在 train。重试不记功 | V0_5_PLAN §1.4 |
| 3 | 取三条轨迹：该题 W_base 的 train 基线尝试、首次尝试、重试（如果有）。用 `trajectory_of(session)`（集成步接 `TraceStore.trajectory`，§6）重建成 OpenAI 格式的消息。平台负责截断：每条工具输出不超过 4,000 字符，整条轨迹不超过 60,000 字符，截断次数写进记录 | ReMe 自己不截断。基线轨迹是已经付过费的，还能多提供一组对比 |
| 4 | `scope.set_step(step)` 后调用 `summarize(trajectories=[{task_id, messages, score: 部分得分, metadata: {query: 题面}}])` | ReMe 原样 |
| 5 | 核对 `bridge_calls_of(step)`：任何一个不是 200 就报错停下（ReMe 的校验 op 会吞掉 LLM 错误，§5.7 第 6 条）；同时统计校验回复里没有 ```json 块的次数，写进读数 | 平台 |
| 6 | 平台逐条检查 `memory_list`：<br>• literal 检查（`literal_pass`，`library.py:194`）；<br>• reach 检查：用 bge-small 拿 `when_to_use` 去和本题以外的 train 题面算余弦，至少 2 道过 `min_score`；<br>• 长度上限；<br>• 跟库比的去重：拿 `when_to_use` 在活库里检索第 1 名，相似度 ≥ τ_store 就判为重复，记下 `dup_of`。这一步和库里的向量在同一空间（都只嵌 `when_to_use`）。<br>没通过的记下原因，不入库 | VEL 原有的规则，检索改成嵌入；去重见 §5.7 第 1 条 |
| 7 | 写入前先 `get(ids)`：id 已在库里（内容完全相同）就不再写，而是在平台账本里把本题追加为这条的又一个来源；否则盖上出处（§5.5）后 `add`。同一批 4 道题可以并行提炼，但写入按冻结排程里的题序串行，库的内容因此是确定的 | §5.7 第 12 条 |
| 8 | 记功：解析首次尝试的引用行（§5.5） | §5.5 |
| 9 | 退役：按 §5.5 的规则判定，用 `retire` 执行 | §5.5 |
| 10 | 写检查点：平台账本 `rsi/library_stream.json`（每条的出处、来源列表、展示、引用、记功、伤害题、退役），以及规范化后的活库快照 `rsi/library_live.jsonl`。先写库、后写账本；续跑时先对账：库里有、账本里没有的条目删除，账本里有、库里没有的按账本里的副本补写。已付费的步骤不会重复调用 | — |

流跑完之后：
1. 照 V0_5_PLAN §1.4 的中止规则：D = Σ(r_first − b) ≤ 0 就不给快照付 gate（`snapshot_abort`，`library.py:589`），如实报告，钱回到 R 的轮次；
2. 否则把活库规范化导出，去掉已退役的条目，写进候选包的 `library/task_memory.jsonl`；
3. 走现有的快照 gate：train 筛选、dev、确认。

success、failure、comparative 三种来源默认都开，也就是原样使用 ReMe 的流水线，每种来源各自记一个 D。K 的 `origins` 可以关掉其中某一种。ReMe 论文表 3 里"只加成功"的效果最好，所以要不要关，按我们自己的读数决定。

### 5.4 读取路径：`retrieval_block_v6`（尝试开始时）

在 v6 K 下，`runner.py:152-161` 的检索点改为调用 `retrieval_block_v6(snapshot_text, task_text, client, *, task_hash, leave_one_out, business_line, settings)`（快照文本由集成步从包的 `library/` 里取出），步骤如下：
1. 按"检索器摘要 + 快照摘要"找到对应的快照集合，找不到就 `load` 建一个。
2. `retrieve(query=前缀 + 题面, top_k=top_k×5, where={ol_line: L, ol_split: "train"})`。前缀由 E0d 决定：空，或 bge 的检索指令 "Represent this sentence for searching relevant passages: "。
3. 留一在平台侧做：剔除来源列表（快照行的 `ol_sources`，流里用账本）含本题 task_hash 的条目，记下剔除数；再按 `min_score` 过滤，取前 `top_k`。chroma 没法按 JSON 字符串里的成员关系过滤，所以多取再剔。
4. 由平台格式化结果：每条前缀 `[lib <id8>]`，给出 `when_to_use` 和 content；块末写明："When you call the finish tool, end its `reason` with one line `library-used: <id8,…>` listing the entries above that you used, or `library-used: none`."
5. 记录 `library_retrieval` 事件：ids、`retrieval_score`、检索器摘要、快照摘要、过滤条件、留一剔除数。

几点约束：
- 留一只在 train 上做，即筛选和流里。dev 和 test 不需要，因为条目全部来自 train。
- `top_k=2`，依据是 S1 的展示数读数和 ReasoningBank 的 k 消融。分数门槛由 E0d 确定：bge-small 下，不相关的句子之间也有 0.53–0.56 的相似度，所以不能沿用 TF-IDF 时代的 0.2。
- 查询嵌入截断到 512 token（bge-small 的上限），E0d 也用同样的截断。

### 5.5 出处、记功与退役（VEL 平台层，`ouroloop/memory/vel.py`）

| 项 | 规则 | 来源 |
|---|---|---|
| 出处字段 | 写进条目的 metadata，平台加的键一律带 `ol_` 前缀，避免和 ReMe 自带的 `score`、`content`、`author` 等键撞名：`ol_line`（检索过滤用的业务线）、`ol_source_line`（产生它的业务线）、`ol_split="train"`、`ol_task_hash`（第一个来源）、`ol_activity`、`ol_release_ref`（产生这条轨迹的 W）、`ol_sessions`、`ol_trace_refs`（§6）、`ol_origin`、`ol_reme_step_type`、`ol_born_at`、`ol_source_score`、`ol_validation_score`、`ol_imported_from`。值一律是标量；列表类字段存成 JSON 字符串（ReMe 会把 list 值用逗号拼接，`memory_node.py` 的 `to_vector_node`，所以不能直接放 list）。来源列表与统计放在平台账本，导出快照时合并进行（`ol_sources`、`ol_stats`） | ReMe 不把 `task_id` 写进条目，出处要由平台补上 |
| 只用 train | `add` 之前检查 `split == "train"`，并且来源业务线不在 K 的 `heldout_domains`（operations）里。违反时拒绝并报错，而不是静默丢弃 | VEL 规则 |
| 记功 | 被引用的条目记 r − b(t)。最终消息里没有引用行时，展示过的每一条各记 (r − b(t)) / len(shown)。引用了没展示过的 id 不算。伤害按不同的题计数（b(t) = 1 且 r = 0 的题） | ACE 的引用记功；MEMORY_AGENT_ITERATION §3 第 7–8 项 |
| 引用行的解析 | 先读首次尝试的 finish 调用的 `reason`（gateway 记录里最后一次 act 回复的 finish 参数）；读不到，再读最后一次 act 回复的文字；只认最后一个 `library-used:` 行 | — |
| 观察期 | 展示满 α = 3 次之前，不会因为记功被退役 | ReMe 的 α |
| 退役 | 满足任一条件即退役：<br>• 在 ≥2 道不同的题上造成伤害，且 `n_shown ≥ 5`；<br>• `n_shown ≥ 3`，且平均记功 ≤ −0.2 | `retire_reason`（`library.py:287-293`）；伤害改为按不同的题计数 |
| 容量 | 只有条目数超过 `max_active=500` 时才按容量淘汰：在 `n_shown ≥ 3` 的条目中，按 `(utility, born_at, entry_id)` 排序，淘汰最低的 | S1：35 条退役里有 34 条是因为书架满了被挤掉的 |
| 读数 | success、failure、comparative 三种来源各自的 D；引用率；记功的秩相关；各环节的条数 | — |

### 5.6 快照与 gate

- 快照的来源是 ReMe 的 `dump_memory`，但不能直接用：它的输出里带着向量（`list()` 取了 embeddings，`MemoryNode.model_dump` 原样写出），行序也不固定；导出、载入出错时只写日志，缺参数时也返回成功（`dump_memory.py`、`load_memory.py`）。所以平台做规范化：
  - 去掉 `vector`，按 `memory_id` 排序，每行内的键也排好序，并合并账本里的 `ol_sources`、`ol_stats`；
  - 导出的行数必须等于集合条数，载入后集合条数必须等于行数，否则报错。
- 快照放在包里的 `library/task_memory.jsonl`，这是只在宿主侧使用的 kind（§3.6）。这样包的 ref 就覆盖了快照，gate、release、export 不用新增对象种类就能带上它。
- 向量是派生数据，载入时重新计算（500 条约 0.5 s）。向量缓存不进身份。检索结果以记录里的 `library_retrieval` 为准。
- R 的候选会继承父包的库文件（照 `keep_parents_library`，`library.py:380`），R 改不了它。
- v5 的 `skills/library/*.md` 在 v6 里不再使用；v5 自己的行为不变。

### 5.7 ReMe 的缺陷与平台侧的处理

| # | 缺陷（源码或冒烟里确认过） | 平台侧的处理 | 测试 |
|---|---|---|---|
| 1 | 去重阈值默认 0.5，在 bge-small 下把 6 条校验过的条目删到只剩 1 条。另外，跟库比时两边的向量不同：库里只嵌 `when_to_use`（`to_vector_node`），新条目嵌的是 `when_to_use + " " + content`（`memory_deduplication.py:117`）；取库出错时返回 `[]`（:108-110），不报错地跳过了跟库去重 | 批内去重保留，阈值 τ_batch（初值 0.85：实测近重复 0.907、相关但不同 0.67、不相关 0.53–0.56）；跟库比的那一步由 `BatchOnlyDeduplication` 关掉，改由平台在 `when_to_use` 空间里做，阈值 τ_store。E0c 两个阈值都定。同一个 `similarity_threshold` 键也管 hard comparison 的配对，那一项保持关闭 | τ_batch 下 6 条至少留下 4 条；近重复写两次只入库一条；关掉平台去重后测试失败 |
| 2 | chroma 检索时把 `metadata["score"]` 改写成 1 − 距离（`chroma_vector_store.py:97-98`），转成节点时又取它作 score（`memory_node.py` 的 `from_vector_node`），结果里拿不到原来的校验分 | 盖出处时另存 `ol_validation_score`；`OlFilteredMemoryRetrieval` 把相似度另存进 `retrieval_score` | 检索两次后，导出的 `ol_validation_score` 不变 |
| 3 | `record` 的计数加在调用方的副本上 | 不用 record，计数放在平台账本里 | — |
| 4 | `local` 存储只在 close 时落盘 | 改用 chroma，并且每批都写检查点 | SIGKILL 掉 sidecar 再重启，条数不变 |
| 5 | 默认会读取工作目录往上 5 层以内的 `.env` | `enable_load_env=False`；工作目录放在仓库之外；不用 `reme2` 命令行 | 在工作目录的上一层放一个哨兵 `.env`，确认 sidecar 的环境里没有它的变量 |
| 6 | LLM 和 op 默认 `raise_exception=False`，LLM 会重试 10 次；校验 op 无论配置如何都会吞掉 LLM 错误，把条目判为无效（`memory_validation.py:132-139`） | 能开 `raise_exception` 的都开，重试压到 1 次；sidecar 把 `success=false` 变成 500；每次提炼后核对桥上本步的调用，任何非 200 就停（§5.3 第 5 步）；桥遇到账户类错误就关掉作用域 | 假 LLM 对校验请求返回 500 时，track 报错并停下，而不是写入 0 条后继续跑 |
| 7 | 轨迹不截断 | 由平台截断（§5.3 第 3 步） | 截断计数正确 |
| 8 | 没有租户和 split 字段，检索 `filters=None` 写死 | 平台补 `ol_` 出处字段，并用 `OlFilteredMemoryRetrieval` 过滤 | 留一、跨业务线、held-out 各一个测试 |
| 9 | 记功是共享的 | 用 VEL 的引用记功 | — |
| 10 | `segments` 永远用不上 | 不用 segments | — |
| 11 | 要求网关支持流式输出和 `tools: null` | 桥两者都支持（§2.4） | 用 openai SDK 按 ReMe 的方式调用 |
| 12 | 条目 id 是内容的 sha256 前 16 位，写入用 `collection.add`；对已有 id 再 add 一次，chroma 1.5.9 不报错，库里仍是第一次的 metadata（`$V6/probe2` 试验） | 写入前先 `get(ids)`，命中就只在账本里追加来源（§5.3 第 7 步） | 两道题提炼出同样内容：库里一条、账本两个来源；按 id 退役不误删别的题来源的条目 |
| 13 | 导出带向量、行序不定；导出、载入的失败只写日志 | 平台规范化并核对条数（§5.6） | 缺参数时报错；导出两次字节相同 |
| 14 | 校验回复里解析不出 JSON 块时，默认 `is_valid=True`、`score=0.5`（`memory_validation.py:101-102`），在阈值 0.5 下直接通过 | `validation_threshold=0.6`，这种情况被拒而不是被收；平台统计"校验回复无 JSON 块"的次数并写进读数；E0.5 金丝雀要求为 0 | 假 LLM 回一段没有 JSON 的校验文字时，条目不入库，计数为 1 |
| 15 | 自带 HTTP 服务默认监听 0.0.0.0、CORS 全开 | 监听 127.0.0.1、去掉 CORS、加 bearer 鉴权 | 没带 token 的请求得到 401 |

### 5.8 S1 的容量 bug

- 所有 preset：`library.py:307` 的排序键改成 `(-weight, name)`，只改 1 行。现在的写法对一个字符串集合排序、只按权重比较，同分时的顺序取决于集合的迭代顺序，也就取决于 `PYTHONHASHSEED`。改后 v5 在同分时的顺序变成确定的。v5 没有正在跑的活动，所以不涉及规则 13；v5 的 K 也不变。
- v6：不再有书架的概念，容量规则见 §5.5。

### 5.9 跨业务线共享

- 每个条目都带 `ol_line`，检索只在本业务线的条目里进行（作为过滤条件）。
- 导入是 K 里声明的输入，不是外挂命令：K 的 `library.imports = [{release_ref, snapshot_digest}]`，由 `library_track` 在流开始时调用 `import_entries`，记进账本，也进身份。
- `import_entries(snapshot_text, *, source_release_ref, source_line, target_line, client, collection, heldout_domains)`：
  - 把 A 已发布快照里的条目复制进 B 的活库，`ol_line` 改写成 B，保留 `ol_source_line = A`，标上 `ol_origin=imported` 和 `ol_imported_from=<A 的 release>:<memory_id>`；
  - 统计清零，观察期重新计算，只按 B 的 train 记功；
  - B 的快照照样要过 B 自己的 gate。
- 来源业务线不能在 `heldout_domains` 里，被导入条目的 `ol_split` 必须是 train，否则整个导入报错。
- v6 只做机制和端到端测试，效果要等第二个域的活动来测（§12）。

## 6 trace memory

| 层 | 内容 | v6 |
|---|---|---|
| 记录 | 宿主已经写下全部中间输出：<br>• `gateway_call`：每次模型调用的消息前缀长度与尾部、工具 spec、回复、usage（`gateway.py:308-318`）；<br>• `tool` / `broker_event`：每次工具调用及其结果，输出只留前 2,000 字符（`EVENT_CHARS`，`gateway.py:44`）；<br>• mark：hook、包工具、被挡下或跳过的调用、pushback、Stirrup 的摘要与隐式 finish；<br>• `library_retrieval`、`worker_session`、`end`。<br>v6 新增 `egress`（§4.4）和研究侧的 `bridge_call`（§2.5） | 记录格式不改 |
| 引用 | `trace_ref = <session>:L<行号>`。transcript 只追加，所以行号是稳定的；模型调用另有 `call_id`。引用在读取时推导出来，不改变记录的字节，因此 v3/v5 的逐字节对照不受影响 | 新 |
| 读取 | `TraceStore(workspace, split_of)` 提供 `get(ref)`、`events(session)`、`trajectory(session)`。`split_of` 由 bench 根据冻结的划分提供（task_hash → split）；task_hash 由 `task` 事件的全文按 `library.task_hash`（`library.py:83`）算出 | 新 |
| 轨迹重建 | `trajectory(session)` 返回 OpenAI 格式的消息：<br>1. 按 seq 把全部 `gateway_call`（act 与 aux）的前缀长度加尾部串起来，还原每次调用实际发出的完整消息（单次调用是无损的）；<br>2. 轨迹 = system + task + 每次 act 调用依次贡献：它尾部里新出现的 user 消息、它的回复（文字加工具调用）、这些工具调用的结果；<br>3. 工具结果取自之后第一个带着这些调用 id 的调用（act 或 aux）的消息，那是 actor 看到的全文；找不到时退回 `tool` 事件里的 2,000 字符摘录，并标为截断；<br>4. 紧跟在一次摘要（aux）调用之后的 act 调用，其尾部开头的摘要桥接与确认两条消息不算进轨迹，只作标注；aux 调用本身不贡献消息。<br>理由：Stirrup 做过摘要之后，最后一次调用的消息只剩 system、task 和摘要，直接取它会丢掉前半段 | 新。ReMe 的输入就由它产生 |
| 检索 | `search(query, *, kind=None, tool=None, task_hash=None, limit=20) -> [{ref, session, task_hash, kind, excerpt}]`。<br>索引用标准库里的 SQLite FTS5，文件是工作区的 `index/trace.sqlite`：它是派生数据，可以重建，被 git 忽略；按（会话，已索引到的行号）增量更新。索引内容包括助手文字、工具调用参数、工具结果全文（取自 `gateway_call` 尾部）、mark 和 `egress` 事件。<br>代码强制只检索 train：非 train 的会话不进索引，`get` 和 `trajectory` 对它们抛错。FTS5 不可用时直接报错，不退回其他检索方式 | 新 |
| 进入 memory | ReMe 读的是完整轨迹，包括工具结果、`plan_changes` 和 hook 的反馈。每条记忆都带 `ol_trace_refs`，指向来源会话及其中的写操作。R 的候选也可以引用 trace_ref | 新 |
| 给 R 用 | R 会话里增加两个宿主工具：`trace_search` 和 `trace_get`，都只能读 train | 集成步做 |

## 7 R 的配套

### 7.1 R0：新常量 `PROPOSE_SESSION_V6`

新常量写在 `policies.py`，v3 和 v5 的常量一个字节都不动。调用 `default_research_set(..., v6=True)` 时选用它，`session_format(..., v6=True)` 也跟着改。

**工具与流程优先。** 原样采用 MEMORY_AGENT_ITERATION §5.1 的第 (1)、(4')、(6') 段：
- 从 `inputs/causes.json` 起步。可以机械化、并且在 ≥2 道 train 题上复现的失败原因，用代码修：写一个可写的包工具、一个 hook，或者改 `write_guard.py`。新工具要附上 toolcheck 脚本。
- 属于判断类的原因，用文字修，并写明 `footprint`。
- 一个候选只改一层，每轮至多改一次 agent 程序。用 `subsumes` 让工具取代被它覆盖的条目。
- `inputs/tools.json` 和 `inputs/library.json` 的读法照原段落。

第 (1) 段里关于脚本的那句改成与 §7.3 一致："Scripts are JSON lists of tool calls; base tools in a script run read-only, so every change must come from the new tool; a value of one task in a script's arguments must appear in that task's own text."

新增以下三段：

> (8) The agent program is Stirrup, vendored under vendor/stirrup/ with agent/stirrup_agent.py as its adapter; you may edit any of it except vendor/stirrup/LICENSE and PROVENANCE.json, as one change to the agent program. Edit vendor/stirrup/core/agent.py itself: a subclass of Agent does not take effect inside a session. middlewares/write_guard.py runs as hooks inside the adapter. Stirrup's own calculator and local code execution run inside the isolated worker and may be enabled in the adapter; its web tools ignore the proxy and cannot reach the network here.
> (9) The library (library/) is written by fixed code from train rewards; you cannot edit it. inputs/library.json lists its entries with origin, provenance, times shown and cited, and credit; an entry credited on two or more train tasks whose procedure is mechanizable is a candidate to compile into a tool (`subsumes`).
> (10) trace_search and trace_get read the recorded train attempts: every model call, tool call, hook answer and egress request. Cite trace refs as the evidence for a candidate.

### 7.2 R 的输入

| 输入 | 内容 | 代码 |
|---|---|---|
| `inputs/causes.json` | 失败的 train 尝试按（断言类 × 行为）聚类，每类附带 attempt id、题数和 `mechanizable` 标记 | 新写 `research/causes.py`，复用 `assertion_class`、`behaviour_of`、`failure_class`，这些函数从 `methods.py` 搬过来（§0.3 第 2 项） |
| `inputs/tools.json` | 每个包工具的调用次数、错误数、在哪些题上被调用，以及在这些题上修好和弄坏了哪些 | 由 `method_telemetry` 推广而来 |
| `inputs/library.json` | ReMe 条目，带出处和记功 | `memory/vel.py` |
| `trace_search`、`trace_get` | 见 §6 | `trace.py` |

### 7.3 toolcheck（验收梯子的 V2a 与 V2c）

**V2a**（定义见 MEMORY_AGENT_ITERATION §1.2）：
- 脚本格式：JSON 调用列表 `[{tool, args}]`，每段至多 `max_calls_per_script` 次调用。
- R 为每一道它声称能修好的失败 train 题，提交一段脚本。宿主在一个全新的 train 世界里执行：脚本里的基础工具一律只读执行，新工具经可写的 broker 执行，所以世界的写操作只能来自新工具；然后用 `Episode.score()` 按真实断言打分，must-not 断言一并判定。
- 脚本参数要过字面量检查：属于单道题的字面量，只有出现在该题题面里的才允许。这样薄薄包一层的写工具加上写死的答案过不了，靠读操作查出来再写死的 id 也过不了，查找必须由工具自己做。
- 准入条件：每段脚本都确实调用了新工具；至少在 `min_tasks=2` 道声称修好的失败题上通过。
- 返回给 R 的只有每道题是否通过，以及失败断言的类别，不返回断言里的值。

**V2c**（hook 的触发回放）：
- 在 worker 里，把 TraceStore 里已记录的每个 train 会话的工具调用序列，逐条喂给候选 hook：before_tool 收 `{tool, args, turn}`，after_tool 收 `{tool, args, result, is_error, turn}`（result 用记录里的全文，截到 `HOOK_RESULT_CHARS`），on_finish 收最终消息。
- hook 拿到的是只读 ctx：它发出的基础工具调用经只读 broker 打到该题初始状态的新世界上（记录时的中间状态没法重建，这是 V2c 只作读数的原因）；出站用只读凭据。
- 读数：在失败会话和通过会话上分别统计挡下率与给出反馈的比例。
- 准入：在 ≥2 道声称的失败 train 题上触发；通过会话上的误触发率只报告，不作准入，伤害由 V3 的筛选挡。
- 执行器复用 `reach_check`（`methods.py:370-455`）的 worker 与新世界机制。

**代码落点：**
- `research/toolcheck.py`：`script_check(...)` 与 `hook_replay(...)`，需要的函数从 `methods.py` 搬过来；
- `research/drafts.py`：在 `kit_check` 的位置（:192）调用 toolcheck；
- `drafts.check` 的字面量检查传 `baseline=种子文件`（§3.6）；
- 新增一条静态检查：包内代码不得读 `library/`（库不进 worker，这一条是兜底）；
- toolcheck 的次数计入 `max_draft_repairs=3`。

**K 新表：** `toolcheck {min_tasks: 2, max_scripts_per_tool: 8, max_calls_per_script: 40}`。

### 7.4 筛选

照用 FINANCE §5.2 和 §5.5 中已采纳的改动（PLAN §3 第 11 项）。

## 8 v6 preset 的 K

`_v6_preset()` 以 v3 为底，去掉 `methods` 和 `cost_beta0/1`，得到一个新的 CoreManifest。

| 表.键 | v3/v5 | v6 | 依据 |
|---|---|---|---|
| `max_effort` | high | **medium** | 规则 10：所有臂用同一个 effort |
| `seed` | 空包（`bench.py:2294`） | `agent/seed_v6` | §3 |
| `cost_beta0/1` | 有 | 去掉 | PLAN P6 第 2 步 |
| `budget.max_steps`（act） | 50 | 沿用 | 4 个活动共 1,760 个 session，最多用到 25，p95 为 15，从没碰到上限。Stirrup 会多出一轮 finish，还可能多出 continue 轮，在 E1 里测量 |
| `budget.max_model_calls` | 200 | 沿用 | aux 摘要很少 |
| `budget.max_tool_calls` | 200 | 沿用 | 最多用到 84 |
| `budget.max_seconds` | 900 | 沿用 | 最长 283 s；容器启动加冷 import 约 4 s |
| `budget.max_tokens`（输出） | 400k | 沿用 | 最多用到 31.6k |
| `worker.*` | 写死在代码里 | 见 §4.1 | §4.1 |
| `package.max_files` | 24 | **96** | 种子约 53 个文件；R 写过的包最多 7 个文件 |
| `package.max_package_bytes`（不含 `library/`） | 256 KiB | **768 KiB** | 种子约 550 KiB，R 写过的包最多 10 KB。上限来自 R 工作树的导出行：`LINE_BUDGET` 为 900,000 字符（`worker_runner.py:64`），Stirrup 源码 JSON 转义后约膨胀 1.04 倍，768 KiB 约合 818k 字符 |
| `package.max_file_bytes`（新） | — | 256 KiB | 最大的文件 `core/agent.py` 为 86 KB |
| `package.max_library_bytes`（新） | — | 2 MiB | 500 条，每条约 2 KB；只在宿主侧使用，不走 worker 的协议行 |
| `package.max_memory_chars` | 8,000 | 沿用 | 最多只用到 4,978；memory.md 会整段进入每次提示，经验的增长应放在库里 |
| `package.max_skill_chars` | 8,000 | 16,000 | 按需读取，不进提示 |
| `package.max_skills` | 12 | 32 | 提示里只放索引；库也不再占用 skill 的名额 |
| `package.components` | v3 的列表 | 加 `vendor`、`library` | §3.6 |
| `package.package_api` | v2 | 沿用 | manifest 结构不变 |
| `library.backend` | VEL 自带（v5） | `{kind: reme, version: 0.3.1.10, wheel_sha256: 53ec8403…, lock_sha256, flows_sha256}` | §5 |
| `library.store` | 包内书架 | chroma | §5.7 第 4 条 |
| `library.retriever` | TF-IDF | `{model: BAAI/bge-small-en-v1.5, revision: 5c38ec7c…, file: onnx/model.onnx, sha256: 828e1496…, dims: 384, pooling: cls, normalize: true, max_tokens: 512, query_prefix: <E0d>}` | §5.1、§5.4 |
| `library.top_k` / `min_score` | 3 / 0.2 | 2 / 由 E0d 定 | §5.4 |
| `library.batch_dedup_threshold` / `store_dedup_threshold` | — | 0.85（E0c 复核）/ 由 E0c 定 | §5.7 第 1 条 |
| `library.validation_threshold` / `success_threshold` | — | 0.6 / 1.0 | §5.7 第 14 条 |
| `library.origins` | success、contrast、reflexion | success、failure、comparative | §5.3 |
| `library.max_active` / `probation_shown` | 6 架 × 6 条 | 500 / 3 | S1 的容量 bug |
| `library.retire_*` | 伤害 ≥2；或展示 ≥3 且平均记功 ≤ −0.2 | 伤害按不同的题计、且展示 ≥5；或展示 ≥3 且平均记功 ≤ −0.2 | §5.5 |
| `library.traj_tool_chars` / `traj_total_chars` | — | 4,000 / 60,000 | 工具输出的 p99 不到 9k；E0h 核对 |
| `library.stream_batch` / `cite` | 1 / — | 4 / 1 | MEMORY_AGENT_ITERATION D5；§5.5 |
| `library.research_concurrency` | — | 4 | §2.2 |
| `library.abort_d` | 0 | 0 | V0_5_PLAN §1.4 |
| `library.heldout_domains` | — | `["operations"]` | PLAN §0.2 |
| `library.imports` | — | `[]` | §5.9 |
| `toolcheck` | — | 见 §7.3 | §7.3 |
| `research` | v3 | R0 v6；每个活动 3 轮，patience 2 | §7；PLAN P6 |
| `concurrency` | 8 | 沿用 | — |
| `_host_contract`（`bench.py:2203`） | `runtime_ref` 仍写着 "ouroloop.agent.loop"，`context_policy_ref` 仍写着 "points.compact"，都已过时 | 两者分别改为 runner shell 加 worker_runner 协议、"W 包里的 agent 程序"。`required_capabilities` 加上 `memory_sidecar` 和 `embedding`；K 设为 proxy 时再加 `egress` | — |

以下各项都进 K（因而也进 `code_version`）：镜像 id、memory 锁的摘要、flows 的摘要、模型 sha、ReMe 版本、种子包的 ref。所以 v6 是一个新的实验身份，需要新的工作区和预算根，并要重新跑 W0 基线。

**身份漏洞要补：** `code_version` 只对 `.py` 文件和 `PACKAGE_DATA` 求哈希（`release.py:44-56`）。种子里的 `prompts/*.txt`、`manifest.json`、`LICENSE`、`PROVENANCE.json`、`memory.md`、`integrations/slack/{Dockerfile,README.md}`，以及 sidecar 的 `flows.yaml` 和锁文件都不在里面。集成步把它们加进 `PACKAGE_DATA` 和 `pyproject.toml` 的 package-data（两处必须相同）。

必须保持不变的：v3 和 v5 的 K；`tests/fixtures/attempt_behaviour_v3.json`；`test_record_compat`；`test_hot_path_boundary`；P1 的金标测试。

## 9 测试与验收

**原则：**
- 每个模块都走真实路径：真实的 HTTP、Docker、ReMe、chroma、bge-small、mitmproxy 和 Stirrup。只有模型是脚本化的：scripted provider、`tests/fake_upstream`、假 claude CLI 二进制（照 `tests/test_claude_code_provider.py:46-60` 的 FAKE 写法），以及 ReMe 那一侧用的 `tests/fake_openai_server.py`。
- 不允许 mock 掉被测的那个组件。
- 每个机制测试都要做一次变异检查：在隔离副本里去掉该机制，测试必须失败。

**环境开关：**

| 开关 | 作用 |
|---|---|
| `OUROLOOP_MEMORY_VENV=<path>` | 指向 memory venv |
| `OUROLOOP_TEST_REQUIRE_MEMORY=1` | 默认运行时，没有 venv 的 memory 测试会 skip；设了这个开关则改为失败 |
| `OUROLOOP_TEST_REQUIRE_DOCKER=1` | 照旧 |

构建阶段，构建者用 `$V6` 里的 venv。集成步之后，改用 `python -m ouroloop.memory setup` 建出来的 venv。

### 9.1 gateway

| 测试 | 真实路径 | 断言 |
|---|---|---|
| `test_bridge.py` | ThreadingHTTPServer + httpx 客户端 + 包着脚本化 provider 的 `MeteredProvider` + 真实的预算账本 | • ReMe 实际发过的 51 条 chat 请求（取自 `$V6/reme/smoke/out/fake_logs/requests.jsonl`，只留 `/v1/chat/completions`，复制为 `tests/fixtures/reme_requests.jsonl`）逐条回放，全部成功<br>• SSE 格式正确，`include_usage` 时有 usage chunk<br>• 401、404、400（含非空 tools）、429、502 都按 §2.4 返回：429 时账本里没有记账，账户类 502 后作用域被关掉<br>• `max_concurrency=2` 时第 3 个请求排队而不是被拒<br>• 8 个线程并发时，计费总数正确<br>• `set_step` 之后的记录带新 step；`calls(step)` 列出状态<br>• token 不出现在任何记录里 |
| `test_bridge_claude_code.py` | 桥 + 真实 `ClaudeCodeProvider` + 假 CLI 二进制 | 回复里的 ```json 围栏块原样到达客户端；model 名 `research/reme:success` 记成 `…:research:reme:success` |
| `test_bridge_openai_sdk.py`（需要 memory venv） | 用 memory venv 里的 openai 2.41 作客户端（子进程），`stream=True`、`tools=None` | 按 ReMe 的调用方式能拿到正确的文本和 usage |
| `test_trace.py` | 用 fake_upstream 分别在 v3 K 和 v6 K 下跑一次真实尝试 | • `trajectory()` 与网关实际发出的消息一致<br>• 强制触发一次摘要（小上下文窗口）后，重建出的轨迹仍然完整，且不含摘要桥接<br>• 工具结果是全文，不是 2,000 字符的摘录<br>• ref 稳定<br>• FTS5 能检索到工具结果里的词<br>• dev 和 test 的会话被拒绝<br>• v3 的 transcript 字节不变 |
| `test_usage_view.py` | 真实 worker（subprocess） | 打开 `usage_out` 时，`ctx.view["last_usage"]` 等于网关记录里的 usage；v3 下 view 里没有这个键 |
| `test_egress.py`（Docker） | 真实的内部网络 + `ouroloop-egress:v6` 代理容器 + 一个只接内部网络的客户端容器（`python:3.12-slim`，用 urllib）+ 只接 bridge 网络的目标容器（明文 `python -m http.server`，以及一个 TLS 服务） | • 名单内的 GET 得到 200，并有记录<br>• 名单外的主机得到 403，并有记录<br>• 名单内但方法不允许的 POST 得到 403<br>• CONNECT 到名单内的 TLS 目标能透传，记录里有字节数<br>• 只读凭据的 POST 和 CONNECT 得到 403<br>• 不经代理直连会失败<br>• `close()` 取回的事件齐全；会话关闭后凭据得到 407<br>• 记录里没有凭据原文 |

### 9.2 agent

| 测试 | 真实路径 | 断言 |
|---|---|---|
| `test_seed_v6.py` | 用 `hp.seed_package` 建包，过 `check_shape` 和草稿 smoke | • `PROVENANCE.json` 里的哈希与 vendor 里的文件一致，也与 `$V6` 的 sdist 一致<br>• LICENSE 在<br>• `agent/default_agent.py` 与 `ouroloop/agent/default_agent.py` 逐字节相同<br>• 不超出 v6 的限额<br>• v3/v5 的 K 拒收 `vendor` |
| `test_stirrup_worker.py`（Docker） | v6 镜像 + 真实的 Gateway、AttemptBroker、SkillReader、Marks + fake_upstream 的世界 + 脚本化 provider | • 把 `$V6/stirrup/demo/host_e2e.py` 的主场景改写成测试：block、skip、读 skill、after_tool 反馈、"Please continue"、aux 摘要、pushback 都出现<br>• 世界里只发生了一次查询和一封邮件<br>• `max_steps=3` 时 agent 自己停下<br>• `max_tokens=60` 时由宿主截断，并关闭 worker<br>• `last_usage` 驱动摘要阈值<br>• `stirrup_end.finished` 为真，而 `end.stop` 为 "tool_use" |
| `test_stirrup_claude_code.py`（Docker） | v6 镜像里的 Stirrup + 真实 `ClaudeCodeProvider` + 假 CLI 二进制 | `toolu_` 形式的 id 和 JSON 回合的形状能完整往返；只回文字两次后触发隐式 finish |
| `test_adapter_fixes.py` | 同上 | §3.2 末的 7 处各一个用例：async 包工具和 async hook 能跑；挡下 + finish 的一轮不结束尝试；只给 `reason` 的 finish 被接受；R 改了摘要 prompt 后摘要仍走 aux；usage 三项相加；隐式 finish；mark 名 |
| `test_write_guard.py`（包内 + 宿主侧） | — | MEMORY_AGENT_ITERATION §6.3 里种子部分的 8 条 |
| `test_worker_resources.py` | DockerWorker 生成的命令 + 真实容器 | • 默认参数下，命令与今天逐字节相同<br>• K 里的资源设置进入命令和 `describe()`<br>• pids 设为 16 时：设了线程变量 import 成功，不设则失败，以此证明线程变量起作用<br>• `memory_swap=512m` 时 swap 被关掉<br>• `network="none"` 时传 egress 被拒；`network="proxy"` 时不传 egress 被拒，传了则命令里有内部网络和代理变量 |
| `test_http_fetch.py` | 子进程里的 `agent/http_fetch.py` + 测试里起的一个标准库记录型 HTTP 代理 | 有 `OUROLOOP_EGRESS=1` 时适配层列出该工具，没有时不列；请求经代理发出并带 `Proxy-Authorization`；方法限 GET、HEAD、POST；输出被截断 |
| `test_vendor_editable.py` | 给 `vendor/stirrup/core/agent.py` 打补丁，把警告阈值从 20 改成 5 | 补丁通过 `check_shape` 和草稿 smoke，并且行为确实改变（警告提前出现）。这证明 R 改得动 vendor 代码 |
| `test_vendor_immutable.py` | — | 改 `vendor/stirrup/LICENSE` 或 `PROVENANCE.json` 的候选被 `check_shape` 拒收 |
| `test_literal_baseline.py` | — | 种子 vendor 里原有的 "30" 在 `baseline=种子` 下不报；R 新加的一行 "30" 报；`baseline=None` 时行为与今天相同 |
| `test_library_kind.py` | — | • `library/` 不进 run_files，也不进 worker<br>• R 的补丁碰到它会被拒绝<br>• 单独按 `max_library_bytes` 计量<br>• v3/v5 的 `Limits.to_json` 字节不变 |

### 9.3 memory

| 测试 | 真实路径 | 断言 |
|---|---|---|
| `test_memory_sidecar.py` | 真实 sidecar（ReMe 自带的 HttpService）+ bge-small + chroma + 假 LLM 服务器 | 走完全链路：summarize → 平台检查 → add → retrieve（带过滤）→ retire → dump → SIGTERM 重启 → load，最后的检索结果与重启前相同；没带 token 得到 401；`success=false` 变成 500 |
| `test_memory_pitfalls.py` | 同上 | §5.7 的 15 条各有一个测试（"—"的几条由 `test_library_v6.py` 覆盖） |
| `test_memory_no_llm.py` | 不带 research 作用域的 sidecar | 检索照样成功，假 LLM 服务器上没有任何请求；调 summarize 得到 500 |
| `test_library_v6.py` | 宿主侧 | `ol_` 出处字段；train-only 和 held-out 的拒绝；引用记功，以及没有引用行时的均分；引用行先读 finish reason、再读文字；伤害按不同的题计数；观察期；`max_active` 的淘汰；按来源分别计算的 D |
| `test_library_sort.py` | 子进程，PYTHONHASHSEED 分别取 0、1、2 | `shelf_overflow` 的结果都相同（验证 :307 的修复） |
| `test_memory_track.py` | 真实 sidecar + 4 道 train 题的固定消息样例（经 `trajectory_of`）+ 假 LLM | • 一批流写出检查点；续跑时不重复调用<br>• 在写库和写账本之间 SIGKILL，续跑时对账：孤儿条目被删除或补写<br>• `library_stream.json` 与活库一致<br>• 轨迹截断的计数正确<br>• 假 LLM 对校验请求回 500 时，`bridge_calls_of` 核对让这一步报错停下 |
| `test_memory_import.py` | 同一个 sidecar 里的两条业务线 | 导入的条目带出处，`ol_line` 是 B，B 的检索能返回它们；观察期重新计算；以 operations 为来源时被拒绝 |
| `test_embed.py` | — | 向量做了 L2 归一化；sha 不符时拒绝启动；超过 512 token 的输入被截断；`query_prefix` 生效 |
| `test_memory_setup.py` | `python -m ouroloop.memory setup --venv $V6/venv-memory` 和 `check` | 按锁建出的 venv 通过 check；改动一个包的版本后 check 失败、sidecar 拒绝启动 |
| `test_memory_boundary.py` | 宿主进程 | 用过 `MemoryClient` 之后，`sys.modules` 里没有 reme、chromadb、onnxruntime、agentscope |

### 9.4 集成（端到端）

配置：`examples/automationbench/claude-v6-quickstart.toml`。用 fake_upstream，actor、R 和 ReMe 的 LLM 都是脚本化的（actor 与 R 用脚本化 provider，ReMe 经桥打到同样脚本化的研究 provider），工作区放在临时目录。按顺序跑：

1. `freeze`
2. `--phase cycle --until baseline`
3. `--phase library`（1 批），然后过快照 gate；另跑一次让 D ≤ 0，确认快照 gate 没有付费
4. `--phase cycle --until segment-0/round-0`：脚本化的 R 提出一个写工具，附带 toolcheck 脚本，其中一种情形通过、一种失败（含一段把答案写死在参数里的脚本被拒）；再提出一个 hook，走 V2c
5. 安装
6. `--phase evaluate`（用假的 test）
7. `--phase report`
8. export
9. 用 `App` 拿发布出的 W 跑一道题：Stirrup 在 Docker 里，检索只走 sidecar（不开 research 作用域，不起桥）

记录里必须出现：`gateway_call`、`tool`、Stirrup 的 mark（`stirrup_end` 等）、带 ReMe id 的 `library_retrieval`、ReMe 产生的 `bridge_call`、带资源字段的 `worker_session`。

**quickstart-proxy 变体**（`claude-v6-quickstart-proxy.toml`，K 的 `network = {mode: proxy, allow: [<本地目标容器>]}`）：脚本化的 actor 调一次 `http_fetch`；断言 transcript 里有 `egress` 事件、`trace_search` 能检索到它；草稿 smoke 会话拿的是只读凭据。App 也用同一个 K 跑一次。

另加两条组合测试：
- ReMe → 桥 → `ClaudeCodeProvider`（假二进制）：ReMe 能解析回复里的 fenced JSON，四个 op 分开记账；
- 跑完一次 v6 尝试和一次 library 阶段之后，宿主的 `sys.modules` 里没有 stirrup、reme、chromadb、onnxruntime、agentscope、mitmproxy。

### 9.5 付费前的验收命令

```
docker build -f docker/worker-v6.Dockerfile -t ouroloop-worker:py3.12-stirrup020-v6 .
docker build -f docker/egress.Dockerfile -t ouroloop-egress:v6 .
.venv/bin/python -m ouroloop.memory setup && .venv/bin/python -m ouroloop.memory check
.venv/bin/python -m pytest -q
OUROLOOP_TEST_REQUIRE_DOCKER=1 OUROLOOP_TEST_REQUIRE_MEMORY=1 .venv/bin/python -m pytest -q -m "not live"
.venv/bin/python -m ouroloop bench --config examples/automationbench/claude-v6-quickstart.toml --phase cycle --until baseline   # 以及 §9.4 的其余各步与 proxy 变体
.venv/bin/python -m ouroloop bench --config examples/automationbench/claude-v6-finance.toml --phase smoke --checks-only
git status --short --ignored      # 被忽略的工作区与 *.local.toml 和开始前一样，一个不少
```

## 10 实验 E0–E4 与费用（finance，划分 `4fd91eaf9fb8daf5`）

**前提：**
- 全程走 claude_code CLI，effort 统一为 medium。
- 开新的活动和新的预算根，在干净的检出上执行 `freeze`。
- 活动进行期间，不往这个检出合入任何代码（规则 13）。
- 每个付费步骤都先经用户批准；跑完就停下分析，并写 JOURNAL（规则 3、5）。

**单价：** 每次尝试约 $0.50–0.55（Stirrup 会多出 finish 轮，可能还有 continue 轮）；8 路并发；每次尝试约 1.5 min。

| 步 | 内容 | 读法与事先登记的停止规则 | 名义费用 | 墙钟时间 |
|---|---|---|---|---|
| E0（离线） | (a) 日期锚相对 `current_time` 的差距分布<br>(b) 台账重放，与 broker 的写记录逐条对齐<br>(c) 在 v5 的 87 条提议上，用 bge-small 校准 τ_batch 与 τ_store<br>(d) 在 S1 已判过的 62 对 train 首次命中上，比较 bge-small（加与不加检索指令前缀）、BM25、TF-IDF 的 AUC，并定出 `min_score` 和 `query_prefix`<br>(e) 重算 finance 里"算术"类的失败，看纯粹算错的有几道<br>(f) 在 v6 镜像里跑种子包，测 RSS、线程数、pids 峰值、tmpfs 用量和冷启动时间<br>(g) 跑通 §9.4 的端到端流程与 proxy 变体<br>(h) 在 v4-finance 的 train 轨迹上，计算截断后 ReMe 输入的长度和预计调用次数<br>(i) 用 finance 的 train 字面量索引对种子跑一次 `drafts.check` | (a) 差距中位数超过 14 天时，删掉日期锚那一句，交用户决定<br>(b) 必须 100% 对齐<br>(c)(d) 结果写进 K<br>(e) 纯算术失误 ≥3 道题时，冻结前在种子里开 Stirrup 的 calculator，并把它列为 R 的编译目标<br>(f) 实测值超过 K 设定值的一半时，把 K 加倍后重测<br>(g) 必须通过<br>(h) 据此修正 E2 的预算<br>(i) 必须 0 命中 | $0 | 1 h |
| E0.5 金丝雀（付费，一次性工作区，不进活动） | • 2 次 W0-v6 train 尝试，在 Docker 里用真实的 claude_code<br>• 经桥跑 1 次 ReMe 提炼<br>• 1 次检索，并解析引用行<br>• 1 轮 R，走到草稿检查为止 | 在 freeze 之前抓出 finish 协议（§3.2 第 6 处）、引用行位置，以及 claude_code 的回复能否被 ReMe 解析。任何一项不过就修完再来，不进 E1。校验回复无 JSON 块的次数必须为 0 | ≈ $5–10 | 20 min |
| E1 | W0-v6 跑 train 50×1、dev 25×3；在同一时间窗里，W0-旧（空包、默认 agent、同一个 v6 K）跑 dev 25×3。必须同一时间窗，因为 CLI 附带的真实日期每天都在变 | **过程读数：** 分诊触发率；`plan_changes` 调用率；核对之后又写了什么；新增的 must-not 违规；写操作里出现真实日期的比例；只回文字的轮数（"Please continue"）与隐式 finish 率（读 `stirrup_end`，不读 `end.stop`）；aux 摘要次数；每次尝试的轮数和成本。<br>**护栏：** 如果 dev 上 W0-v6 ≤ W0-旧 − 3 题/25，就停下，关掉 triage 和 verify 后再读一次 dev（+$38，另行批准；这也是规则 11 的原样接入臂） | ≈ $100–120 | 45 min |
| E2 | `--phase library`，包括：<br>• 流：50 次首次尝试加约 28 次重试，约 $40<br>• ReMe 提炼：按 E0h 估约 250 次研究调用，$20–35<br>• 快照 gate：37 次 train 筛选、25 次 dev、25 次确认，约 $45 | • 各环节的条数：产出、通过校验、批内去重后剩下、跟库去重、literal 拒掉、reach 拒掉<br>• 活跃条目数；从没展示过就退役的条目必须是 0<br>• 引用率；三种来源各自的 D<br>• ReMe 的静默失败为 0：桥上每个非 200 都让该步停下并有记录<br>• **中止规则：** 流走完时 D ≤ 0，就不给快照付 gate（省约 $45），如实报告 | ≈ $55–130 | 分批跑，约 80 min |
| E3 | R 跑 1–2 轮（R0 v6）：候选依次过 V0–V2a/c，再进筛选、gate、确认 | 第 0 轮如果没有任何工具或 hook 候选走到 V3，就停下，和用户一起改 R0。读数：V0–V2 各筛掉了几个；V2a 通过的题与 V3 实际修好的题是否一致 | ≈ $45/轮 | 35 min/轮 |
| E4 | 先提交计划哈希，再在 test 上跑三臂 25×3：W0-旧、W0-v6、W\*-v6；最后出 report | 两种增益分开报：架构增益（W0-v6 − W0-旧）和 RSI 增益（W\* − W0-v6）。finance 的 test 之前读过 1 次，要在报告里披露 | ≈ $112–125 | 45 min |
| **合计** | | | **≈ $365–475**（申请上限 $480；E1 护栏的 +$38 另行批准） | 付费部分约 5.5 h，另加停顿等待 |

**现实预期：**
- dev 上 3 次对 3 次读数的半宽约 ±2.5 题/25。
- 架构增益估计在 +1 到 +3 题/25，主要来自日期锚和核对。Stirrup 底座本身的增益估计约为 0：StarHarness 原样接入的基线就是 57.1%。
- 外部证据显示，强模型从记忆里得到的增益很小（OSS §2）。
- 能不能拿出像样的成绩，取决于 E3 里 R 能不能写出过 V2a 的工具。这一点不作保证。

**发布：** E4 读数出来、全量测试通过后，在本地合入 main，打 tag v0.6.0，之后继续在 m1 上开发。

## 11 风险

| 风险 | 缓解 |
|---|---|
| ReMe 的 task memory 只存在于已冻结的 0.3 线，上游不会再修 | 按哈希钉死版本。缺陷全部在平台侧绕开（§5.7），不 fork。ReMe 只经 HTTP 接入，将来换库只需换掉 sidecar |
| 注册的 op 依赖 ReMe 的内部基类（`BaseOp`、`MemoryRetrieval`、`MemoryDeduplication`、`ChromaVectorStore`） | 版本钉死；op 合计约 150 行，每个都有测试；换版本时整组重测 |
| ReMe 提炼出的条目太泛，在 Opus 上没有增益 | 校验、literal、reach、跟库去重四道检查；三种来源分开记 D；快照必须过 gate；D ≤ 0 不付 gate；结果照实报告 |
| claude_code 的回复不合 ReMe 的解析格式（围栏 JSON） | E0.5 金丝雀专门检查；校验阈值 0.6 让解析失败的条目被拒而不是被收；平台计数 |
| Stirrup 带来额外的轮次（finish、"Please continue"）和更长的系统提示，成本上升 | 隐式 finish（§3.2 第 6 处）；E1 记录每次尝试的成本和只回文字的轮数，超过 +25% 时报告；R 也可以改 Stirrup |
| R 改了摘要器的 prompt，摘要请求被当成 act | 识别改用 logger 回调（§3.2 第 4 处），有测试 |
| sidecar 峰值 860 MB，还要承受 8 路并发检索 | 它在宿主侧，资源充裕；写操作按集合串行；E2 记录延迟 |
| 研究调用并发叠加尝试，claude CLI 进程过多 | research 作用域的并发上限 4，超出排队（§2.2） |
| 题面超过 512 token 时，查询嵌入被截断 | E0d 用同样的截断做评估 |
| 检索的浮点结果不能逐位复现 | 以记录里的 `library_retrieval` 为准 |
| 镜像 687 MB，只有 arm64；pillow 有 CVE | 见 §4.3；amd64 见 §12 |
| egress：看不到 HTTPS 的路径；Docker Desktop 与 Linux 的内部网络行为不一样；mitmproxy 的依赖较多 | 基准用 `none`；proxy 只在本机验收，Linux 部署前再测一次；mitmproxy 只在自己的容器里，按哈希锁定 |
| 泄题 | 库只来自 train；导入时拒绝 held-out 来源；trace 检索在代码里强制只查 train；toolcheck 脚本参数过字面量检查。都有测试（§5.5、§5.9、§6、§7.3） |
| 进度：slim/1 何时合入不确定；集成步只有一个人，E1–E4 期间不能合代码，E2 或 E3 里发现缺陷就要重新 freeze 并重付 E1（约 $110） | 集成步等 slim/1；各分支改的文件不重叠；研究侧新文件放第 4 个 worktree 先写；E0.5 金丝雀在 freeze 前抓缺陷 |
| 预算超支，或订阅额度用尽 | 每一步都要批准；遇到账户错误会关掉作用域并停下 |
| 修 :307 会改变 v5 同分时的顺序 | v5 没有在跑的活动；K 不变 |

## 12 留到以后打磨的

| 项 | 为什么现在不做 | 触发条件 |
|---|---|---|
| 桥的 attempt 作用域（含 tools 与 tool_calls 映射），让不经 ctx 的 OpenAI agent 在 W 里经代理访问桥 | Stirrup 走 ctx，目前没有使用者 | 要接入一个只会讲 OpenAI 协议的 agent 时，连同它的测试一起做 |
| 桥的 `/v1/embeddings`、`/v1/models` 与 embed 作用域 | 宿主代码直接调 `MemoryClient.embed`，没有使用者 | 要接入一个只认 `base_url` 的宿主侧开源库时 |
| 尝试时用 ReMe 的 rerank、rewrite 和 LLM 建查询（经 Gateway 用一个声明过的角色） | 每次尝试会多一次模型调用，目前没有证据说明需要 | E2 显示检索质量是瓶颈时 |
| sidecar 做成容器镜像；多条业务线共用一个 sidecar | v6 只在本机运行 | Linux 部署之前 |
| amd64 的 worker 镜像和 memory 锁文件 | 本机是 arm64 | 发布 Linux 包之前 |
| vendor 包的字节码缓存 | 冷启动 import 的开销还要等 E0f 测量 | E0f 显示冷启动占一次尝试时间的 5% 以上时 |
| HTTPS 内容检查 | 中间人有风险，worker 还要装 CA | 业务线要求审计 HTTPS 内容时（mitmproxy 去掉 `ignore_hosts` 即可） |
| 用嵌入检索 trace | 先看 FTS5 够不够用 | R 用 trace 检索的命中率差时 |
| 跨业务线导入的效果读数 | 需要有第二个域 | P6 的第二个域 |
| 工具检索 | 包工具超过约 15 个时才需要 | 包工具超过约 15 个时 |
| Claude Code 原生臂 | 见 PLAN §3 第 13 项 | 第一个 v6 final 之后 |
| `TURN_NOTE` 换版本，或者由宿主直接告诉模型沙箱日期 | 会换掉 actor 身份，也会偏离基准 | E1 里写操作带真实日期的比例仍高于 5% 时 |
| 升级 Stirrup | 种子钉在 0.2.0 | 人发起升级时，换种子即换 K，照样要过 gate |
| bge-small 与 sentence-transformers 的向量级比对 | 不影响 v6 | 换嵌入模型时 |
| v5 导出的 app 里，`k_methods` 读不到 SystemRelease（`release.py:511`） | P3 会删掉方法库 | P3 被否决时再修 |

## 13 评审意见的核查与采纳

评审意见（2026-10-08）逐条对照 eb8c505 源码、`$V6` 下的 ReMe 0.3.1.10 venv、Stirrup 0.2.0 源码和原型适配层核过。表中"本文位置"指改动落在哪一节。

### 13.1 采纳

| # | 意见 | 核查 | 本文位置 |
|---|---|---|---|
| A1 | async 包工具或 hook 在 Stirrup 下必坏 | 确认：`default_agent.py:93-97` 的 `asyncio.run` 在运行中的循环里调用 | §3.2 第 1 处（改法定为辅助循环线程） |
| A2 | 跟库比的去重用了两种向量，出错时静默跳过 | 确认：`memory_deduplication.py:85-117` 与 `to_vector_node` | §5.7 第 1 条（关掉 ReMe 的跟库比较，平台在同一空间里做） |
| A3 | 相同内容再写一次被 chroma 静默丢弃 | 确认：`chroma_vector_store.py:271-276` 用 `add`；`$V6/probe2` 复现 | §5.3 第 7 步、§5.4 第 3 步、§5.7 第 12 条 |
| A4 | 检索结果里拿不到原来的校验分；平台键要加前缀 | 确认：`chroma_vector_store.py:97-98` | §5.5、§5.7 第 2 条、§5.1 的 op 表 |
| A5 | 导入条目会被本业务线的过滤挡掉 | 确认（设计内矛盾） | §5.5、§5.9（`ol_line` 与 `ol_source_line`） |
| A6 | 摘要之后轨迹重建丢前半段 | 确认 | §6（另补：`tool` 事件只有 2,000 字符，工具结果要从之后调用的消息里取全文） |
| A7 | 检索时 sidecar 不能有 LLM 可调；App 不依赖桥 | 确认 | §5.1（检索流里不放 `BuildQuery`；无作用域时 LLM 指向关闭端口）、§1.1、§9.4 第 9 步 |
| A8 | 字面量检查会扫 vendor 代码 | 确认：`harness_package.py:485-497`、:507 | §3.6（改为只扫相对种子改动的行）、§10 E0(i) |
| A9 | `TURN_NOTE` 与 finish 协议打架 | 确认：`claude_code.py:70-73` 与 `core/agent.py:1586-1595` | §3.2 第 6 处 |
| A10 | E2 漏了中止规则 | 确认：V0_5_PLAN §1.4；代码里有 `snapshot_abort`（`library.py:589`） | §5.3、§10 E2 |
| A11 | usage 换算与摘要识别 | 换算一项原型已做（:199-200），定稿保留并改从 view 读；识别一项确认 | §3.2 第 4、5 处 |
| B12 | 桥的嵌入、models、embed 作用域没有使用者 | 确认：ReMe 只发 chat 请求，嵌入在进程内 | §2；同理把桥的工具映射也移出 v6 |
| B14 | V2c 实现不出来 | 确认 | §7.3（按 `reach_check` 的机制定义清楚，误触发率只作读数） |
| B16 | 导入做成 K 的输入 | 确认 | §5.9、§8 |
| B17 | R 能改 PROVENANCE 和 LICENSE | 确认 | §3.6、§3.5 |
| B18 | finish 结束时 `end.stop` 是 "tool_use" | 确认：`runner.py:194` | §3.2 第 7 处、§10 E1 |
| B19 | 引用行位置要写明 | 确认 | §5.4、§5.5 |
| C20 | 用 ReMe 自带的 HTTP 服务 | 确认：`http_service.py:18-93`，冒烟 C 用的就是它；另发现它已有 `/health`，所以我们的路由叫 `/ol/health` | §5.1 |
| C21 | 留给构建者核对的几项源码里已有答案 | 确认：`base_op.py:50-64`、`base_llm.py:24-25`、集合名规则 | §2.2、§2.4、§5.1 |
| C22 | calc 不用自己写 | 确认：Stirrup 自带 `tools/calculator.py` 与本地代码执行 | §3.4、§7.1 (8)、§10 E0(e) |
| C23 | egress 换成 mitmproxy 加 addon | 采纳 | §4.4 |
| D24 | `pyproject.toml` 由集成步负责；身份漏洞 | 确认：slim/1 已改 package-data；`release.py:44-56` 只哈希 `.py` 与 `PACKAGE_DATA` | §1.4 契约 (d)、§8 |
| D25 | 跨分支契约现在写死 | 采纳 | §1.4 |
| D26 | 关键路径；第 4 个 worktree；E0.5 金丝雀 | 采纳 | §0.3 第 7 项、§1.4、§10 |
| D27 | 研究作用域的并发与 step 归属 | 采纳 | §2.2 |
| E1–E7 | 缺少的真实路径测试 | 采纳 | §9 各表 |

### 13.2 改写

| # | 意见 | 改写的理由 | 本文位置 |
|---|---|---|---|
| B13 | 用 Stirrup 自带的 web 工具作 egress 的真实使用者 | 用不了：`fetch_web_page` 的客户端 `trust_env=False`（`tools/web.py:488-492`），不认代理变量；它在本地解析域名并钉住 IP（:130-170、:188-205），内部网络里解析不到外部域名；它还拒绝私有地址，测试用的本地目标容器都是私有地址。改为种子自带的 `agent/http_fetch.py`（标准库 urllib，认代理变量），只在 K 为 proxy 时列给模型；只读凭据一项照采纳 | §3.1、§4.4、§9.2、§9.4 |
| B15 | 脚本参数一律过 train 字面量检查 | 一律检查会让脚本没法把题面里的参数传给工具。改为：单道题的字面量只有出现在该题题面里的才允许；同时脚本里的基础工具只读执行 | §7.3 |
| A7 | 检索流写死 `BuildQuery(enable_llm_build=False)` | `enable_llm_build` 是从请求上下文读的，不是 op 参数（`build_query.py:37`）；更简单的做法是检索流里根本不放 `BuildQuery`，查询由平台给 | §5.1 |

### 13.3 核查时另外发现的问题

| # | 问题 | 证据 | 本文位置 |
|---|---|---|---|
| N1 | 原型的 finish 不看同一轮是否已被挡下，"写被挡 + finish"的一轮会直接结束尝试 | 原型 :322-335；Stirrup 把 finish 排在最后执行（`core/agent.py:1305-1310`） | §3.2 第 2 处 |
| N2 | `FinishParams.paths` 必填，只给 reason 的 finish 被判参数无效 | `tools/finish.py:13-19`、`core/agent.py:1192-1199` | §3.2 第 3 处 |
| N3 | ReMe 的校验回复解析不出 JSON 时默认有效、score 0.5，在阈值 0.5 下直接通过；校验 op 还会吞掉 LLM 错误 | `memory_validation.py:66-139` | §5.7 第 6、14 条，§5.3 第 5 步，`validation_threshold=0.6` |
| N4 | ReMe 的导出带向量、行序不定，导出与载入的失败只写日志 | `dump_memory.py`、`load_memory.py` | §5.6、§5.7 第 13 条 |
| N5 | 设计原文写"重试只给类别不给值"，与它引用的 `retry_note`（给类型与参数投影）不符 | `library.py:534-543`；V0_5_PLAN §1.4 | §5.3 第 2 步（按现有规则） |
| N6 | `DockerWorker` 已有 `cpus/memory/pids` 参数；无条件加 `--memory-swap` 会改变默认命令的字节 | `worker.py:192-207` | §4.2（`memory_swap` 只在给了时才加） |
| N7 | `tool` 事件只留 2,000 字符，用它重建轨迹或建索引会丢工具结果 | `gateway.py:44`、:362 | §6 |
| N8 | ReMe 把 list 类型的 metadata 用逗号拼接 | `memory_node.py` 的 `to_vector_node` | §5.5（平台字段一律标量，列表存 JSON 字符串） |

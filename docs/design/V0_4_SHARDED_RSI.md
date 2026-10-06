# v0.4 设计：按领域分片的搜索、运行时路由与跨领域借鉴

> **已被取代（2026-10-06）：** 用户批准的 v0.4 是更简单的 [V0_4_PLAN.md](V0_4_PLAN.md)（每个新领域一个单领域活动，不写路由）；本文保留作历史，不实现。

> 2026-10-06。设计稿：未实现、未运行，没有调用模型、没有付费阶段、没有读任何 test。本文是 v0.4 的规则来源；实现前 M0、M1、M3、M4、M5、M7 与 ROADMAP 按本文改写，改完之前两者冲突时以本文为准。
> 用户的方向（2026-10-06）：搜索按领域分片，运行时按领域路由；同时考虑领域之间的相互影响，包括 memory 上的借鉴，也保持一定的隔离性；当前版本已无可迭代之处就并入 main，在原分支继续开发。
> 来源：三份候选设计——strict-shards（隔离优先）、shared-plus-overlay（共享层 + 覆盖层，迁移优先）、transfer-experiments（以移植实验做借鉴）——与三位评审。三位评审都选 strict-shards 作底，本文以它为底，嫁接：两阶段 gate 与按噪声上界选门槛（overlay，改为每领域各自选）、final 的结构零（overlay）、路由作为 SystemRelease 的字段（评审 3：strict-shards 原写的新 target `worker.routes` 在 v3 的 K 下绑不起来，§2.2）、移植要约与迁移账目、运行身份逐行审计（transfer-experiments）、R 读其他领域的负结果（overlay）、首次网关调用的等价回放（overlay）。剪掉的部分与理由在 §12。
> 统计数字来自 analyst-e 校准过的任务模型的离线模拟：每题是稳定失败、稳定通过或不稳（p ~ U(0,1)）三者之一，不稳占比 f，每题重跑不一致率 d = f/3（实测 d ≈ 0.12，即 f = 0.36）。模拟只用 dev 的形状，不读任何数据；脚本在会话临时目录 `scratchpad/v04final/`（`gate.py`、`rules.py`、`aa.py`、`final.py`、`p5.py`），实现时入库为 `examples/automationbench/shard_sim.py`（§10）。

## 0. 目标与主张

### 0.1 v0.4 要显示什么

1. **效果（C1）。** 同一个所 pin 的模型（Claude Opus 5.5，effort medium，K 的 `max_effort` medium）下，框架从 W0 出发，只用四个新领域（sales、support、hr、finance）的 train 失败，自动找到并装上领域专科，使封存的 final 在这四个领域的 100 道 test 上 W\* − W0 的 95% 区间不含 0。原因：v0.3 用一个共用包跨五个领域，三轮在主张领域上的净题数是 −2、−1、−2（§1），没有任何安装，主张没有被检验；v0.4 的价值只能由同一种封存 final 证明。
2. **隔离（机制）。** 一个领域的安装不改变任何其他领域执行的字节，由构造保证（§2.3、§4.6），离线测试与付费记录的逐行审计证实（§4.7）。原因：v0.3 的 rulesmem 在 marketing 是真增益、在 sales 是真伤害（诊断 RC1），一个共用的可变层会把这种交换带进主张。
3. **借鉴（过程）。** 一个领域被接受的机制以要约的形式提供给其他领域，在接收领域按它自己的 gate 重新检验；漏斗（要约 → 尝试或拒绝 → 筛选 → gate → 接受）如实报告，**不作主张**。原因：迁移在别的框架里确实发生过（DGM 的 SWE → Polyglot 14.2 → 28.9，AutoSaddler 从 Opus 迁到 Haiku 仍 +5.6），但 v0.3 也显示“多读、多做”在该克制的领域有害，所以迁移只能在落地处测，不能假定。

负结果是合法的结局：没有安装就不跑 final，报告“按领域分片、每域 25 道 dev、k = 3 的搜索没有把任何主张领域推过它的 gate”。

### 0.2 主张及其边界

- **C1（唯一主张）。** 四个新领域的 100 道 test，W0 对 W\*，每臂 3 次重复；计划先封存、hash 先提交到 git，再跑第一道 test 题；在没有安装的领域上 W\* 的读数就是 W0 的读数、逐题差值为 0（结构零，§7）；配对差按家族 bootstrap 2000 次，95% 区间；下界 > 0 才写“框架有效”。
- **Marketing。** test 已读过三次、宣告用尽（`claude-v3.toml` 的 `spent = ["marketing"]`）；v0.4 不在 Marketing 上搜索、不跑 gate、不取读数、不作主张；它被接受过的机制只作借鉴的种子（§4.2），只带各自的 dev 收据，不带 test 数字。
- **不是主张的。** 每领域的 dev 增益（有赢家诅咒，v0.2 的 W\* 在 dev 80～84%、test 52%）、每领域的 test 行、迁移漏斗、R 的会话统计。C4 本版 not_run：没有研究位（原因：研究位要 dev ≥ 50 题的内层池，每域 25 题拆不出来，M6 §2）。
- **口径。** “同模型”，同 DESIGN §3；W\* 是“按业务流程分别演化的 app”，DESIGN §1 已允许一个业务流程一个 app；两臂都不被告知领域（§2.5），报告照写这一点。

### 0.3 事先登记的内容

第一笔付费（第 1 阶段）之前，以下内容随本文与 JOURNAL 的预注册条目一起提交，之后不改：

| 项 | 值 | 节 |
|---|---|---|
| 候选的 gate | 第一读数对 4 读数池净 ≥ +2 题才取第二读数；两读数均值对池净 ≥ T_D；退步 ≤ 2 题（均值下降 > 0.5）；成本项 β0 0.10、β1 2.0 | §5.1 |
| 门槛表 | δ_u = d̂_D + 0.5·SE_D 查表得 T_D（2～4.5 题），δ_u > 0.26 的领域不搜索 | §5.3 |
| 族 | G ≤ 12 个 gate（4 领域 × 3 轮），每个 ≤ 0.0035，并集界 ≤ 0.042；驱动拒绝第 13 个 | §5.4 |
| W0 池 | 每领域 4 次 W0 dev 读数（复用 1 + 新测 3）；旧读数漂移 > 6 题即弃用并补测 | §5.6、§9 |
| A/A | 48 组有向切分过生产 `judge`，≥ 2 组接受则所有 T_D 上调一级 | §5.6 |
| R 与借鉴 | 每领域每轮一个会话、k = 3、至多 1 个移植；要约规则；准入新增项 | §3、§4.3 |
| 轮次与停止 | 同步 3 轮；每领域 patience 2，接到新要约时重置；按预算的放弃顺序 | §8.4 |
| final | 模板、结构零、只在 ≥ 1 次安装时跑 | §7 |

第 1 阶段结束后，固定代码写出 `shard_calibration` 记录（每领域 d̂、SE、δ_u、T_D、A/A 与漂移检查的结果），提交到 git 之后第一轮才能开始；第一个 gate 开始后它不能再改。原因：门槛只能用 W0 的数据定，定在任何候选读数之前，否则就是看了结果再调。

### 0.4 分支与合并

v0.3 已经没有值得付费的迭代：诊断给出的余下三轮续跑（同一条合并管线，每轮约 $85）在主张领域上的期望净值约 −2 题，P1～P5 都要改框架。v0.3.0 已于 2026-10-06 并入 main（merge `b71502d`，tag `v0.3.0`），用户“合并进 main、在原分支继续”的条件已满足，本步不再做。v0.4 在 `m1-automationbench` 上先写文档、再写代码；封存的 final 跑完或负结果记录完之后再并入 main，然后继续在 `m1-automationbench` 开发。

## 1. 为什么分片

**(1) 主张领域没有被任何候选推动，净增益全在已用尽的 Marketing。** 三次收据的逐领域净题数：

| 轮 | 候选 | Marketing | 主张四域合计（100 道 dev） | 逐域（点，25 题） |
|---|---|---|---|---|
| r0 | rulesmem（memory） | +6 | −2 | sales −16、support −4、hr +8、finance +4 |
| r1 | writemem（memory） | +4 | −1 | sales −8、support 0、hr 0、finance +4 |
| r2 | withhold（hook） | +3 | −2 | sales −4、hr −4、其余 0 |

rulesmem 修好的 6 道 Marketing 题里 5 道是 W0 历史上 0/5 的题，Marketing 每题的邮箱读取从 3.0 次升到 5.12 次——这部分增益是真的；同一段“多读、多做”的文字在 sales/support 过度执行，四个 r0 退步可追到具体规则，sales/hr 的退步事件约一半是“不得做”的断言失败。R 看到的笔记里每轮约 5/30 条是“做多了”的失败，没有一个候选处理它们。合并的 125 题 gate 下，只帮 Marketing +6 的改动与帮主张领域 +6 的改动被接受的概率相同（0.27～0.28 对 0.27～0.29，诊断 P5 的模拟）。

**(2) 跨异质领域的一个共用可变层，帮一个领域的改动就可能伤另一个。** 上表中 r0 是“真增益加真伤害”（诊断的调和结论），不是噪声；一个共享的包没有办法同时对 Marketing 说“多读邮件里的修订”、对 sales 说“没要求就别发”。StarHarness 把整个 `vendor/` 框架开放给提案者，每个改动都有“跨 benchmark 的爆炸半径”，它只能靠每个 benchmark 的导入检查兜底；RRSI 给每个领域一条 `evolve/<domain>` 分支、一个在位版本。分片是同一结论：领域之间的交换不能经由运行时发生。

**(3) 合并 gate 的统计形状不合需要。** 按本文的模型（d = 0.12）对比诊断 P5 的“主张人群 gate”（100 道主张 dev、两次读数各 ≥ 5 题、3 读数池）与本文的每领域 gate（§5）：

| 候选的真实效应 | P5 合并 gate 接受 | 每领域 gate 接受（在受益领域） |
|---|---|---|
| 只在一个领域 +4 题 | 0.19 | 0.77（T 3.25） |
| 一个领域 +8、另一个 −2 | 0.76（装上 −8 个点的伤害） | 受益领域装上，受害领域执行的字节不变 |
| 四个领域各 +2（弥散） | 0.75 | 每域 0.14 |
| 无效 | 0.005 | 每 gate ≤ 0.0035 |

弥散的通用机制是分片的代价，如实写进风险（§11）：它由移植（§4.3）与版本边界的晋升（§4.8）部分补回。v0.3 的三个候选全是异质的（Marketing 涨、sales 跌），这是选分片的依据。

**(4) 噪声被读成了领域信号。** 三次收据都只有一次 W0 对照读数（run `630a9c7d`）。三个独立估计的重跑不一致率都约 12%；125 题上净值的空 sd 约 3.9 题，单领域 25 题约 ±7～9 个点；三轮的 +4、+3、+1 题约是 1.0、0.8、0.3 个 sd。R 看到的逐领域数字没有噪声带，“sales 总退步”约一半是对照的运气。分片后每个 gate 只读一个领域，所以每个领域需要自己的多读数池与按噪声定的门槛（§5）。

**(5) R 的输入把它推向一个方向。** 第 2 阶段第 1 轮的 30 条笔记是 marketing 27 + sales 3，第 2 轮全是 sales（按合并冻结顺序取前 30），support/hr/finance 一道都没进；R 每轮读 2～4 条失败轨迹、0 条通过的轨迹，三轮都没读过 support 或 hr 的轨迹；系统提示仍是旧的“每轮只提一个改动、偏好 memory”；字面量检查误拒 20 份草稿中的 14 份（'Not Sent'、'3000' 这类普通词），25 回合里 14 回合花在盲改上。分片让“R 看哪个领域”成为结构（每领域一个会话、只看本领域），而不是排序的副作用；P1、P2 随分片一起做（§3）。

## 2. 包结构与路由

### 2.1 W 的形状：冻结的共享层加每领域专科

- **共享层**就是 W0：`worker.system_prompt`、`worker.finish`、`worker.stop` 默认、`worker.actor`（Opus 5.5 medium）、`worker.harness` = W0 的空包 `package:sha256:43fc4d50…`，加上 `agent/default_agent.py` 的字节（随包发入 worker 的种子 agent）。v0.4 全程冻结。原因：任何共享的可变层都会让一个领域的改动改变其他领域的行为（§1(2)）；共享层改动只在版本边界经晋升发生（§4.8）。
- **专科**：每个主张领域至多一个，是一个普通的 `harness_package`（v1 或 v2）：`manifest.json`、`memory.md`、`skills/*.md`、`middlewares/*.py`、`tools/*.py`、`agent/*.py`、`tests/*.py`。没有新包格式、没有新 manifest 键（`parse_manifest` 照旧拒绝未知键，`harness_package.py:151`）；K 的每包上限对每个专科各自适用：24 个文件、262144 字节、memory 8000 字符、12 个技能、组件清单、`max_effort` medium。提示文本不在专科里：提示是共享层；memory.md 已经经 `PackageView.prompt_block` 进入系统提示，所以领域的“说明文字”写进专科的 memory。
- Marketing 与没有安装的领域没有专科，执行共享层，即 W0 的字节。

### 2.2 存储与 manifest

路由是 `SystemRelease` 的一个新字段，不是新的 target：

```
release:<W*>  SystemRelease{
    worker_ref     = policyset:sha256:ba65df92…   # 共享层，即 W0 的 worker 策略集，不变
    worker_routes  = {"sales": policyset:<S_sales>, "hr": policyset:<S_hr>}   # 新字段；None 时不写进 ref
    research_ref   = R0；core_ref = core:sha256:8d932781…；target_harness_ref 不变
    required_capabilities = [..., "routing.v1"]  # 有路由时追加，旧运行时拒绝而不是忽略
}
policyset:<S_d> = W0 的五个 descriptor，只有 worker.harness 换成 {builtin: harness_package.v1|v2, package_ref: <d 的专科>}
package:<d 的专科> = 普通 harness_package（上面的文件）
```

- **为什么是 release 字段而不是 target。** v3 的 K（`core:sha256:8d932781…`）冻结了 9 个 target，没有 `worker.routes`；`bind_release` 用 K 的 target 表校验每个策略集，未开放的 target 报 “not a target this core opens”（`policies.py:209-254`、`:519-533`）。新 target 要改 K，改 K 就是新 core_ref、新实验身份，W0 的已有读数一个也接不上（§9）。release 字段沿用 `target_harness_ref`/`required_capabilities` 的先例（`contracts.py:393-396`）：缺省 None、不写进 payload，旧 release 的 ref 不变；每条路由是 K 不变时就合法的普通策略集。
- **为什么每条路由是完整策略集。** R 的构建、草稿检查、准入、冻结今天产出的就是“只换了 `worker.harness` 的 worker 策略集”（`policies.build_candidate`）；路由只是把它放进 `worker_routes[d]` 而不是 `worker_ref`。构建路径一行不改。
- **共享层冻结的检查。** 固定代码在构造任何带路由的 release 时检查（`policies.check_route`）：路由集合与共享集合除 `worker.harness` 外每个 target 的 descriptor ref 都相同；不同即拒绝（`shared_layer_change`）。原因：路由集合在结构上可以带别的提示，冻结必须由代码保证，不靠 R 的自觉。
- **路由的键**只能是 `[bench] domains` 中、不在 `spent` 里的领域。
- **出处不进包。** `ported_from`、来源收据、轮次记在台账与安装事件里，不写进包文件。原因：包的字节就是 gate 测过的字节，出处字段改一次字节就要多一个身份。

### 2.3 投影 project(W, D)

`project(W, D) = W.worker_routes.get(D, W.worker_ref)`，是一个内容寻址的 worker 策略集 ref。由此按构造：

- `project(W0, D) == policyset:ba65df…` 对每个 D 成立；
- 某领域 D' 没有安装时，`project(W*, D') == W0 的策略集`；
- 一个领域的运行身份（RunManifest 的 `worker_ref`、`package_ref`、`prompt_hash`）就是其投影的身份，不是 release 的（`system_release_ref` 本就是标签，`bench.py:1432` 的 `RUN_LABELS`）。

三条后果：(a) 一个领域的改动不能改变另一个领域执行的字节；(b) 被接受的专科不用再测就能组合；(c) 在其他领域装东西之后，W0 的已有读数与每个领域的对照池都仍然有效（§9）。

### 2.4 宿主传什么、在哪里强制

- **传什么。** 任务文本与 v0.3 完全相同；外加按领域路由后的包。领域取自冻结划分里任务 id 的上游前缀（`benchmark.task_domain`，`benchmark.py:162`；合并划分已拒绝不带前缀的 id），部署时是 app 所服务的业务流程。领域只用来选投影，不进系统提示、任务文本或 `ctx.view`。原因：专科已经是路由过的，没有东西需要读这个标签；加进去只会改动 W0 的输入。
- **强制点都在固定代码里，按顺序：**
  1. `Improvement.run_identity(..., domain=D)` 绑定 `project(W, D)`；`bind_release(..., domain=)` 对带路由的 release 不给 domain 即拒绝（“a routed release runs one domain at a time”）。
  2. `run_arm` 拒绝跨两个领域的任务清单（带路由的 release，或任何分片计划）；一个 run 只含一个领域。gate 本来就按领域，final 按领域分臂（§7）。
  3. `make_agent_factory` 把 `domain` 写进 session_meta；`PackageAgent.run` 在第一次模型调用前核对 `session_meta["domain"] == task_domain(task_id)`，不符记基础设施故障，这次尝试不运行。
  4. worker 的加载包与 `SkillReader` 只含被路由的包；其他领域的文件不进 worker、不进提示。
- **不改的文件。** `agent/default_agent.py`（字节由测试钉住）、`agent/gateway.py`、`agent/worker_runner.py`。原因：worker 镜像只烤进 `worker_runner.py`（`docker/worker.Dockerfile`），K 冻结了镜像 id `sha256:dd858408…`，改它就要重建镜像、启动检查拒绝（`bench.py:4362`）；default_agent 的字节不在任何身份里，只能用测试钉住。

### 2.5 W0 的公平性

W0 看到的与 v0.3 的 W0 完全相同：同一个合成的系统提示（`prompt_hash` `f4e9c1648b32e159` 不变）、空包、同样的基础工具、五项硬限额、模型与 effort，没有领域标签。final 的 W0 臂对每个领域都是 `project(W0, D) = W0`；W\* 只差在路由进来的专科，专科是 R 只从该领域的 train 学来的。比较是“种子 app 对按领域演化的 app，同模型、同输入”。没有安装的领域两臂执行同样的字节。

### 2.6 导出

每个领域导出一个 app：`project(W*, D)` 按 M3 §4 原样导出（包 + 运行壳 + 模型服务描述），外加一个 `routes.json` 索引，写明哪些领域有专科、其余领域用共享层（W0）的 app。不建路由服务。原因：DESIGN §1 已允许一个业务流程一个 app；部署端选 app 即选路由，未知领域落到 W0——安全，无增益。

## 3. 每领域的 R

### 3.1 会话与轮次

- 每轮对每个主张领域开一个有界编码会话（Marketing 不开），K 的会话上限照旧：40 次模型调用、24 次编辑、单次读轨迹 65536 字节、会话累计 524288 字节；k = 3，R 说得出理由时可以少做。
- **同步轮。** 第 r 轮开始时，固定代码给四个领域做同一份快照：借鉴库与要约（§4.2）、台账摘要；四个会话都只读这份快照。轮内各领域按 T_D 由低到高（功效由高到低）依次跑完会话、筛选、gate 与安装，再进下一个领域。原因：快照让领域的顺序不影响任何一个领域的输入，知识只在轮边界跨领域，便于事后归因；顺序执行最简单，墙上时间的代价见 §8.3。
- 安装是逐领域的 CAS（§6），轮内另一个领域的安装不会让本领域的候选过期。

### 3.2 输入（全部由固定代码按领域 D 过滤）

| 输入 | 内容 | 原因 |
|---|---|---|
| `notes.json` | 每会话 7 条笔记，取自 D 在 `project(W, D)` 下当前失败的 train 题（W0：sales 33、support 27、hr 38、finance 23 道），在 D 内轮转（从未纳入的在前，`select_failures` 加 `domain` 过滤）；按笔记身份复用缓存；每条带 P1 的 `direction`（omission / over_action） | 4 × 7 = 28 ≤ K 的每轮 30 条笔记（`diagnosis.max_notes`），不用改 K；三轮 21 条基本覆盖 finance 的 23 道失败 |
| `trajectories.json` | 被笔记的失败尝试，加至多 4 条 D 的通过的 train 尝试，优先含“不得做”断言的题（P1 的反例） | v0.3 的 R 读过 0 条通过轨迹；该克制的领域需要看到“做对了的不做” |
| `ledger.json` | D 的全部台账行（声明、筛选、gate 摘要：以题计的净值、该 gate 的噪声 sd 与 T_D）；其他领域行的摘要（§4.2(b)）；v0.3 三个候选在 D 的切片（取自收据 `metrics.per_domain`），标“单次读数，±2 sd ≈ ±3.5 题” | 负结果也要跨领域；v0.3 的数字没有噪声带，R 曾追 ±2～3 题的摆动 |
| `histogram.json` | D 的 4 读数池上的 dev 失败断言类型与计数，没有题号 | 同 v0.3，限于本领域 |
| `package/` | `project(W, D)` 的包，只读 | R 只能产出 D 的包 |
| `donors/<offer>/` | 本会话打开的要约（§4.3），只读 | 借鉴的唯一入口 |
| `tools.json`、`package_api.md`、`coverage.json` | 同 v0.3；coverage 另给 D 的 W0 train 通过率 | — |

`read_trajectory`（`session.py:193` 的 `TrajectoryReader`）只列、只读 D 的有效 train 尝试；读其他领域的尝试返回 AccessDenied 并记录。原因：R 一旦读到别的领域的原始失败，那个领域的失败就会经由 D 的专科“隐式迁移”，归因和隔离都说不清。

### 3.3 系统文本与声明

- 去掉会话 system 前拼接的旧 `research.propose` 文本（`session.py:906`，“每轮只提一个改动、偏好 memory”）（P1）。R0 的策略集对象不改，改的是会话代码，记为偏离（§9.3）。
- `SESSION_FORMAT` 增加：你改进的是 D 的专科，只有它的包文件归你；`donors/` 是其他领域被接受的机制，只读，可以改写进你的候选，也可以说明不适用；每个候选写明何时不该起作用。
- 不挂 W 的提示文件（`PROMPT_FILES`，`session.py:83`）；候选目录里出现 `worker/` 文件即拒绝（`shared_layer_change`）。原因：提示是共享层。
- 声明字段（`DECLARATION_FIELDS`，`session.py:87`）在现有的 mechanism、site、carrier、predicted_affected、trigger_condition、notes、differs_from 之外加：`direction`（omission / over_action）、`not_when`（一句话，何时不该起作用）、`contrast_case`（读过的一条通过尝试的 ref）、移植时的 `ported_from`（要约 id）与 `adaptation`（一句话）。会话结束时 `finish` 另可带 `declined_offers: [{offer, reason}]`。RRSI 的提案声明里也有 `retroactive_check`（纠正 / 保持 / 迁移），是同一想法。

### 3.4 准入（在 v0.3 的核心检查、`duplicate_experiment`、`repeated_mechanism` 之后，固定代码）

- `predicted_affected` ⊆ D 的 train 题（v0.3 只要求是 train）。
- 至少一次 `read_trajectory` 落在候选的 `predicted_affected` 上（P1 的接地规则）。
- 不得改共享层：候选目录不得含 `worker/` 文件；构造出的路由集合须过 `check_route`。
- 移植候选：`ported_from` 是对 D 打开的要约；会话内读过 D 的一条失败尝试（要约的触发条件在其中出现）与一条通过尝试（机制在其中不该起作用）；`(要约的来源机制, D)` 以前被拒绝或筛掉、且没有新版本的，按 `repeated_mechanism` 拒绝。
- 字面量检查对全部领域的 train 字面量做（今天的 `train_literals` 已覆盖整个 train），所以一个 sales 的值不能经由移植进入 hr。

**字面量检查（P2，前置条件）。** `literal_hits`/`describe_hits`（`harness_package.py:448-470`）改为给出位置（文件、行、区段、字面量种类），区分大小写，豁免纯数字与出现在 ≥ 2 道 train 题或表头里的值；只在一道题里出现的值仍然禁止。回归测试：回放 v0.3 记录的 20 份草稿，14 次误拒应为 0，植入的单题值仍被拒。原因：借鉴文字天然带来别的领域的词汇，按 v0.3 的检查会照样误拒 14/20。

### 3.5 筛选（P4，每领域）

- 失败集：至多 6 道 D 当前失败的 train 题，取自笔记题 ∪ `predicted_affected`。
- 通过样本：4 道 D 的通过 train 题，每轮用种子 (order_seed, round, D) 重抽（K 的 `screen.passing_sample` 是 5，只能调低，4 合法）。
- 资格：≥ 1 个修复，且样本退步 ≤ 1；按净翻转排序，平局取更便宜者；只有最优者付 D 的 gate，没有合格者本领域本轮 `screened_out`。
- 费用：≤ 3 × 10 = 30 次 × $0.49 = $14.70。“之前”的读数是 W0 已有的 train 读数，或安装后的重测（§3.6）。
- 原因：v0.3 的筛选失败集 63/63 都是笔记里的题、通过样本 7 次都是同 5 道且没有 hr，rulesmem 在样本上 0/5 退步、在 dev 上退步 10 道；空候选清过 `min_net_flips = 1` 的概率 0.43～0.57。诊断按 N = 15 估：资格规则让 rulesmem 进 gate 的概率降到 0.20、空候选 0.18、好候选 0.92。

### 3.6 安装后的 train 重测

D 装上新专科后，只重测 D 此前失败的 train 题（finance 23 道约 $11.3，hr 38 道约 $18.6），作为下一轮的笔记来源与筛选的“之前”。其他领域的 train 读数按投影身份仍然有效，不重测。D 没有后续轮次时不重测（§6）。

## 4. 跨领域借鉴与隔离

### 4.1 什么跨领域、什么不跨

| 通道 | 是否跨领域 | 怎么保证 |
|---|---|---|
| 运行时 | 不跨 | 路由（§2.4）：D 的尝试只加载 `project(W, D)` |
| 证据（失败、轨迹、直方图） | 不跨 | R 的输入按领域过滤，`read_trajectory` 拒绝其他领域 |
| 测量（读数、对照池） | 不跨 | 投影身份；每领域一个池 |
| 改动 | 不跨 | R 的工作树里只有 D 的包；安装只换 `routes[D]` 并断言其他投影不变 |
| 知识 | 单向、在轮边界、只经被接受或被记录的东西 | 借鉴库、要约、台账摘要（§4.2）；接收方自己的 gate 重新检验 |
| 共享层 | 本版冻结 | 版本边界的晋升（§4.8） |
| 预算 | 共用一个根 | 事先登记的放弃顺序（§8.4）；这是唯一的“耦合”，只影响花不花钱，不影响任何 gate 的判定 |

“保持一定的隔离性”在这里的含义：一个领域的改动不能**悄悄**改变另一个领域的行为（运行时、改动、测量三条全隔离）；另一个领域的行为只会因为它自己的候选、在它自己的 gate 上被接受而改变（知识通道是显式、可审计的）。

### 4.2 借鉴的三条通道

**(a) 借鉴库与要约（机制的借鉴，含 memory）。** 借鉴库只追加，只由固定代码写，对 R 只读。一条条目含：id、来源领域与轮次；声明（mechanism、carrier、site、trigger_condition、not_when、direction）；专科相对共享层的确切改动文件（memory.md 的文本、技能、hook、工具或 agent 代码）；收据摘要（以题计的两读数均值净值、第一读数净值、池读数数、T_D、退步数、每次尝试的成本比）；迁移记录（对每个其他领域：要约过、尝试过或拒绝（理由）、筛选结果、gate 结果，只有计数与净值）。条目不含任何任务 id、笔记 ref、尝试 ref、dev 题号、断言参数或轨迹；校验器拒绝任何匹配 `(marketing|sales|support|hr|finance|operations)\.[a-z0-9_]+` 的串与任何笔记/尝试 ref。

种子（第 1 轮，$0）：只有被 gate 接受过的机制才是种子——
- v0.1 W\*：改写的结束检查，五项清单（Marketing dev 36% → 64%，另一次读数 60%）；
- v0.2 W\*：写前协议 memory（48% → 80%）与 on_finish 审计 hook（68% → 84%），文件另见 `examples/automationbench/ceiling/wstar_package/`；
- v0.3 试点：写前取全证据的 memory 清单（operations + Marketing 26 道 dev，42.3% → 69.2%）。

共四个种子条目（v0.2 的两次安装各一条），每个只带它自己的 dev 收据，不带 test 数字。v0.3 五领域活动被拒的三个候选不是条目，只作各领域的台账行。新条目：D 安装后，在本轮结束时把它的专科改动写成条目；移植候选在接收领域过了 gate 或被拒，都追加到来源条目的迁移记录。

**(b) 台账摘要（经验的借鉴，含负结果）。** 每个会话除 D 自己的全部台账行外，另读其他领域行的摘要：机制、carrier、site、direction、结局、以题计的净值 ± 该领域的噪声带，只有计数，没有题号；还有 v0.3 三个候选的逐领域切片（rulesmem：marketing +6、sales −4 等）。原因：一个领域的负结果是别的领域最便宜的证据——sales 的 R 在移植一个“多读、多做”的机制之前，应当先看到它在 sales 上已经输过一次。

**(c) 版本边界的晋升（共享层的借鉴）。** 见 §4.8；本版不运行。

### 4.3 移植（port）规则

- **要约由引擎写，不由 R 写。** D 安装成功，在本轮结束时对每个其他主张领域写一条 `offered` 台账行（来源领域、安装收据、条目 id、机制、carrier、site、目标领域，状态 open）。第 1 轮开始时，四个种子条目对四个领域各开一条要约。原因：v0.3 的试点里 R 在 k = 3 下只做了 1 个候选、没读轨迹——被动放在那里的材料容易被忽略；要约迫使 R 明确地用或不用，并写下理由。
- **会话内的处理。** 每个会话看到对 D 打开的全部要约，至多尝试其中一个（占 k = 3 中的一个名额）；尝试即把条目改写进 `candidates/<dir>/`（memory 按 D 的对象改写并带它的触发与 `not_when`；技能、hook、工具、agent 代码拷贝后修改），声明 `ported_from` 与 `adaptation`，并满足 §3.4 的两次接地读取。未尝试的要约在会话结束时关闭，带 R 的拒绝理由（`declined_offers`）或记为“未考虑”。
- **要约的生命周期。** 一条要约在尝试或关闭后不再开；只有来源领域装上该机制的新版本时才对 D 重开。被拒或被筛掉的 (来源机制, D) 不再要约，除非有新版本。
- **与原生候选同等竞争。** 移植候选就是 D 的普通候选：同一个筛选、没有加分、没有保留的 gate，也不增加 gate；每个领域每轮仍至多一个 gate。原因：迁移的先验是混合的（v0.3 的“多读、多做”在 sales/hr 有害，W0 的克制在那里是对的）；加分会把 12 个稀缺的 gate 花在先验上，而不是 D 自己的证据上。
- **费用。** 一个移植只花它的筛选（约 10 次尝试，约 $5）。
- **没有免 gate 的移植。** 被接受的专科也只对自己的领域生效；任何领域要得到它，都要由该领域自己的候选通过该领域自己的 gate。GEPA 的合并也只在不差于两个父本时才接受；DGM 与 DGM-H 的迁移结果说明机制会迁移，但都是在落地处测出来的。

### 4.4 多重检验：移植不增加 gate

族是 gate，至多 12 个，事先定死；移植不加 gate。候选之间的挑选发生在 train 的筛选上，与 dev 的噪声独立，所以无论筛了多少个移植，被选中候选的 dev 误收概率仍是该领域的 α_D。一次假接受可能把一个空机制以要约的形式散到另外三个领域，但每一次落地仍是那个领域自己的一个 gate、自己的 α_D，并集界照样覆盖。

### 4.5 迁移账目

台账与报告给出每个 (来源条目, 目标领域) 的漏斗：要约 O → 尝试 A / 拒绝 D（理由）/ 未考虑 → 筛选合格 E → 进 gate G → 接受 P；以及 A/O、G/A、P/G，与原生候选每个 gate 的接受率、筛选净翻转并列。预期规模：16 条种子要约，每次安装再加 3 条，至多 12 个移植名额，约 2～4 个移植 gate；所以迁移率只作描述（例如“3 个中 1 个”），并写上空效应下的期望接受数（Σ α_D ≈ 0.01），不作检验、不作主张。

### 4.6 隔离的保证（按构造）

- **G1 运行时。** D 的尝试绑定 `project(W, D)`；其他专科不在 worker 加载包、系统提示、`SkillReader` 里。
- **G2 改动。** R 的工作树只有 D 的包；候选只能成为 `routes[D]`；`worker/` 文件与共享层改动被拒（§3.4）；路由集合须过 `check_route`。
- **G3 安装。** 逐领域 CAS（§6）断言对每个 D' ≠ D，`project(W', D') == project(W, D')`，否则 `isolation_violation`。
- **G4 测量。** 运行身份是投影的身份：D 安装前后，D' 的 W0 池与 train 读数返回同样的行。
- **G5 证据。** R 的输入、`read_trajectory`、台账与直方图只含 D；跨领域只有借鉴库的被接受条目与台账的计数摘要，二者都不含任务数据。
- **G6 共享层。** W0 的提示、空包、`default_agent.py` 的字节与 `worker_runner.py`（K 的镜像）在本版不变，由测试与启动检查钉住。

### 4.7 隔离的测试与审计

离线测试（每个都先写成修正前会失败的样子，名字见 §10）：

- 哨兵测试：sales 的专科带一段哨兵 memory、一个打标记的 hook、一个包工具；跑 support 与 hr 的脚本化尝试，网关记录里没有哨兵文字，没有该 hook 的标记，worker 加载的文件只有共享层，每个 run 的 `worker_ref` 等于该领域的投影。
- 恒等：`project(W0, D) == ba65df…` 对每个 D；不带路由的 W0 的运行身份 hash 与 v0.3 的相同。
- 安装：在一个领域安装，其他每个领域的投影、对照池与 train 读数不变；两个领域在同一个父版本上被接受，按任一顺序安装得到相同的路由。
- 会话：D 的 R 读不到其他领域的笔记、轨迹、台账原文与直方图；`/work/inputs` 里用正则找不到其他领域的任务 id；改共享提示的候选被拒。
- 借鉴库：条目不含任务 id 与尝试 ref；只读。
- 等价：`default_agent.py` 的 sha256 钉住；`worker_runner.py` 的摘要等于 K 镜像的；**首次网关调用的等价回放**——每个主张领域取一条 v0.3 记录的 W0 尝试，在脚本化的 provider 下用 v0.4 的宿主重放第一次网关调用，messages、系统提示 hash 与 tool_specs 摘要须逐字节相同（$0）。

付费记录的审计（$0，进报告）：每个 run 的 `worker_ref`/`package_ref` 必须等于“它开始时的 active release”对“它的任务所在领域”的投影；每个领域的投影变化序列必须恰好是该领域的安装；没有安装的领域在每一行都是 W0 的 ref。final 的封存再核对一次（§7）。

### 4.8 共享层与晋升（规则写定，本版不运行）

- 一个借鉴库条目在 ≥ 2 个领域各自的 gate 上被接受（来源领域一次、移植一次也算，接收方的 gate 是独立的数据），标 `promotable`。
- 晋升只在下一版本开始时发生：下一版的 W0' = 共享层 + promotable 条目，在新版本反正要重做的基线上测。
- **本版不在版本内晋升，原因：** (1) 它改变每个领域的投影，四个池全部作废，重建约 4 × 4 × 25 × $0.47 ≈ $188；(2) 它需要一个合并的不受损 gate（约 125 × 2 × $0.49 ≈ $123），而 overlay 设计的模拟显示这样的护栏放过一个 −12 个点的领域伤害的概率是 6～14%；(3) 它改变不了主张——路由已经把该机制给了每个接受了它的领域。
- 若以后在版本内做：一个合并的不受损 gate，在每个没有接受该机制的领域上增益 ≥ −1 题、合并增益 ≥ 0 才接受，并计入族。

## 5. gate 与统计

### 5.1 规则（每个主张领域 D 一个 TaskEvalPlan）

- 任务：D 的 25 道 dev；`repeats` 1；`min_tasks` 25。
- 对照：`project(W, D)` 的全部有效读数的合并池，第一个 gate 之前补齐到 4 次读数（`control_readings` 4）。
- **第一阶段**：候选的第一读数对池的净题数 ≥ +2（`min_gain` 0.08），只看净值，不判退步与成本；不清过即 rejected，不取第二读数。
- **第二阶段**：取第二读数（复用确认读数的机制：`confirmation_repeats` 1，arm `confirm`）；接受当且仅当：两读数逐题均值对池的净题数 ≥ T_D（`min_mean_gain` = T_D/25）；退步 ≤ 2 题（逐题均值比池均值下降 > 0.5，`regression_drop` 0.5、`max_regression_rate` 0.08）；成本项（β0 0.10、β1 2.0，按两读数的合并判）；完整性。
- 收据 `kind="confirmed"`，`metrics.first_reading`、`metrics.gain`（两读数合并）照旧；另记 `domain`、`T_D`、`δ_u`、gate 序号。
- 原因：第二读数是否取，取决于第一读数——这是事先登记的两阶段规则，接受要求两个阶段都过，误收概率按两阶段联合计算，不是可选停止（诊断禁止的是在边界读数之后**临时**加读）。它的好处是钱：空候选第一阶段通过率约 8%（d 0.12），空 gate 期望花费 $13.27，而无条件两读数是 $24.50；功效只损失约 0.02。用“两读数均值”而不是“每个读数各自 ≥ 门槛”：后者在相近的 α 下功效低得多（§5.8）。

### 5.2 噪声

实测：三个独立估计的每题重跑不一致率都约 12%（Marketing 五次 W0 读数；withhold 在 94 次尝试中没有触发、系统提示与 W0 逐字相同，11/94 不一致；W0 对 r1、r2 各 15/125）。单对读数的粗估：finance 约 0～4%、hr 约 12%、support 约 16%、sales 约 20%。v0.3 的 W0 dev（每域 25 题）：sales 12、support 9、hr 15、finance 8 题。

每领域 25 题上净题数的空 sd（每题方差 p(1−p)，d = 0.12）：

| 比较 | 空 sd（题） | 点 |
|---|---|---|
| 单读数对单读数（v0.3 的形状） | 1.73 | 6.9 |
| 第一读数对 4 读数池 | 1.37 | 5.5 |
| 两读数均值对 4 读数池 | 1.06 | 4.2 |
| 两读数均值对 3 读数池 | 1.12 | 4.5 |

模型对领域通过率不敏感：μ 从 0.32 到 0.60，T 3.25 的 α 都是 0.0022、+3 题的功效都是 0.44。噪声由 d 决定。

### 5.3 门槛表（只用 W0 的数据，第一个 gate 之前冻结）

- **测量。** 每领域 4 次 W0 dev 读数；对每题 i，k_i 为 4 次中通过的次数，k_i(4−k_i)/6 是 2p_i(1−p_i) 的无偏估计；d̂_D 是它在 25 题上的平均，SE_D 是它的 sd / 5。
- **δ_u = d̂_D + 0.5·SE_D**，查下表得 T_D：

| δ_u ≤ | T_D（题） | `min_mean_gain` | α（在该行上界、4 读数池、两阶段） | 功效 +3 / +4 题（+12 / +16 点） |
|---|---|---|---|---|
| 0.04 | 2 | 0.08 | 0.0025 | 0.88 / 0.98 |
| 0.07 | 2.5 | 0.10 | 0.0028 | 0.73 / 0.94 |
| 0.11 | 3 | 0.12 | 0.0031 | 0.53 / 0.83 |
| 0.13 | 3.25 | 0.13 | 0.0031 | 0.45 / 0.76 |
| 0.15 | 3.5 | 0.14 | 0.0028 | 0.38 / 0.68 |
| 0.18 | 3.75 | 0.15 | 0.0031 | 0.32 / 0.60 |
| 0.21 | 4 | 0.16 | 0.0033 | 0.27 / 0.53 |
| 0.26 | 4.5 | 0.18 | 0.0028 | 0.19 / 0.41 |
| > 0.26 | 不搜索 | — | — | — |

每行取使该行上界处 α ≤ 0.0035 的最小 T（池均值以 0.25 题为步长，两读数均值以 0.5 为步长，所以 T 取 0.25 的倍数）。

- **为什么是 +0.5 SE。** 四次读数估出的 d 很不准（d = 0.12 时 SE 约 0.046，相对误差约 38%），低估会选出过低的 T。模拟把估计误差算进去，比较几种选法在 12 个 gate 上的实际误收合计：

| 选法 | d 均为 0.12 | 不均（finance 0.04、hr/support 0.14、sales 0.20） | d 均为 0.08 | d 均为 0.16 | 功效 +3 / +4（d 均为 0.12） |
|---|---|---|---|---|---|
| 点估计 d̂_D | 0.053 | 0.042 | 0.045 | 0.051 | 0.44 / 0.74 |
| d̂_D + 0.5·SE_D（本文） | 0.032 | 0.027 | 0.029 | 0.032 | 0.37 / 0.68 |
| d̂_D + 1·SE_D | 0.022 | 0.017 | 0.021 | 0.020 | 0.30 / 0.60 |
| max(d̂_D, 四域合并 d̂) | 0.025 | 0.026 | 0.020 | 0.026 | 0.39 / 0.71 |
| 合并 d̂ + 1·SE，四域一个 T | 0.017 | 0.031（sales 单 gate 0.0062） | 0.014 | 0.020 | 0.36 / 0.69 |

点估计会把族误收推到 1/20 以上；+1 SE 在不均噪声下让 sales 45% 的情况下不被搜索；合并估计会让稳定的 finance 失去它的功效优势（不均情形下 finance 在 +3 的功效 0.24，本文的选法 0.76）并让 sales 超过目标。+0.5 SE 在所有情形下都留出余量，代价是 d = 0.12 时约 7 个点的功效。

### 5.4 单次与族误收

- 单个 gate：表的每行在上界处 α ≤ 0.0035。
- 族：G ≤ 12（4 个领域 × 3 轮；被筛掉的轮不算 gate；移植用同样的 gate）。并集界 Σ α ≤ 12 × 0.0035 = 0.042 ≤ 1/20，对任何相依结构都成立（包括同一领域各轮共用一个池）；条件是 δ_u 不低于真实 d。把估计误差算进去的模拟：0.027～0.032（上表）。
- 驱动拒绝第 13 个 gate。没有跑的 gate 不花 α；按预算放弃的领域只会让族误收更低。

### 5.5 功效

- 每个 gate：见 §5.3 的表；混合效应（修 4 坏 1，净 +3）与纯 +3 相差 ≤ 0.02。
- 按本文的选法，把估计误差算进去，平均功效（+3 / +4 题）：d 均为 0.12 时 0.37 / 0.68；不均情形下 finance 0.76 / 0.96，hr 与 support 0.32 / 0.60，sales 0.18 / 0.36（且 29% 的情况下 δ_u > 0.26、不搜索）。
- 第一阶段：空候选通过率 0.013（d 0.04）～ 0.142（d 0.20），d 0.12 时 0.083；真实 +3 时 0.78。
- 弥散效应的代价：一个各领域都 +2 题的通用机制，每领域 gate 的功效约 0.14（§1(3)）。
- 现实的结局是 0～2 次安装。这是本版的约束性风险（§11）。

### 5.6 A/A 验证与旧读数漂移

- **A/A 回放（$0，第 1 阶段之后）。** 每领域 4 次 W0 读数：有序地取两次作候选的（第一、第二）读数、其余两次作对照，每领域 12 组、四域 48 组，过生产 `judge`（两阶段、该领域的 T_D、退步上限）。对照只有 2 次读数，比生产的 4 次更吵，所以回放是偏严的。模型下：正确时期望接受 0.2～0.4 组，P(≥ 2) ≈ 0.09～0.20（同一领域 12 组共用读数，接受是成簇的）；真实 d 是测得值的两倍时（d 0.24 对 T 3.25）期望 1.4 组，P(≥ 2) ≈ 0.51。**规则：≥ 2 组被接受，则所有领域的 T_D 上调一级**（第一轮之前，$0）。它只是粗报警：约束性的保证是 §5.3 的 δ_u。误报的代价是约 7～8 个点的功效，可以接受。
- **报告用的旁证（$0，不改任何规则）。** Marketing 在三个工作区的 6 次 W0 dev 读数（v0.1 × 3、v0.2 × 2、v3 × 1），按同一规则回放；它们是跨宿主的读数，只作噪声形状的旁证。
- **旧读数漂移。** 复用的 v0.3 读数（2026-10-05，run `630a9c7d`）是 v0.3 的代码跑的。若它在 100 道主张 dev 上的通过数与三次新读数的均值相差 > 6 题（单读数在 100 题上的 sd 约 2.45 题，两者之差的 sd 约 2.83 题，6 题约 2 sd），弃用它并补测一次（$46.60），池仍是 4 次。原因：宿主代码变了，首次调用的等价回放（§4.7）只证明第一步相同，漂移检查覆盖其余部分。

### 5.7 多重检验的处理（汇总）

1. 族在第一笔钱之前定死：G = 12，每个 ≤ 0.0035，并集界。
2. 门槛只用 W0 的数据定，在任何候选读数之前冻结（`shard_calibration`）。
3. 候选之间、移植与原生之间的挑选都在 train 筛选上，不花 dev 的 α。
4. 第二读数按事先登记的阶段规则取；接受要求两阶段都过；不因边界读数临时加读。
5. 不重判被拒的候选，不对被拒候选重测。
6. 续跑（第 4 轮或额外 gate）必须在新的预注册里把所有 gate 一起重新计数；本版不做。
7. 安装后的池含胜者的两个 gate 读数，它们被选择偏高，所以该领域此后的 gate 偏保守，不是偏宽。

### 5.8 与其他规则的比较（d 0.12，单个 gate）

| 规则 | α | 功效 +3 / +4 | 每个空 gate 的花费 |
|---|---|---|---|
| 本文：两阶段（+2，再均值 ≥ 3.25），4 读数池 | 0.0022 | 0.44 / 0.77 | $13.27 |
| strict-shards：两读数无条件、均值 ≥ 3，5 读数池 | 0.0029 | 0.52 / 0.83 | $24.50 |
| overlay：两阶段（+2，再均值 > 3），3 读数池，四域一个 T | 0.0037 | 0.46 / 0.77 | $13.40 |
| transfer：每个读数各自 ≥ 3，3 读数池 | 0.0025 | 0.34 / 0.63 | 约 $13 |

（strict-shards 的行 α 与功效较高来自 5 读数池与更低的 T；本文在 4 读数池上为同一目标 α 选 T 3.25。两者在功效上的差约 0.06～0.08，换来 $46.60 的读数与每个空 gate 约 $11.）

## 6. 组合与安装

- **逐领域 CAS。** D 的 gate 接受后，固定代码（`Improvement._install` → `Releases.install(..., domain=D)`）构造 W' = 当前 active，`routes[D] := 候选的路由集合`，共享层与其他每条路由逐字节带过。前置条件：`project(active, D)` 等于候选的父投影（否则候选过期，拒绝，不 rebase）；对每个 D' ≠ D，`project(W', D') == project(active, D')`（否则 `isolation_violation`）。然后按 M3 §3 的 CAS 提交指针。不同领域的安装改的是不同的键，同一轮内不会冲突，按执行顺序提交。
- **组合。** W\* = 共享层 + ⋃_D routes[D]。按构造正确：每个领域执行的正是它的 gate 测过的字节，所以组合不需要再测、不需要交互测试。封存 final 时固定代码断言：对每个 D，`project(W*, D)` 要么是 D 最后一次被接受的候选的投影，要么是 W0。
- **D 安装之后（无条件、事先登记，只在 D 还有后续轮次时做）：** (1) 重测 D 此前失败的 train 题（$11.3～18.6，§3.6）；(2) 新专科在 D 的 dev 上补 2 次读数（50 × $0.49 = $24.50），D 的池成为“胜者的两个 gate 读数 + 2 次新读数” = 4 次，§5.3 的表原样适用。其他领域什么都不付：它们的投影、池与输入都没变。D 的专科在本轮结束时进借鉴库并对其他领域开要约。
- **删除或回退一个专科。** 去掉 `routes[D]` 本身是 D 的一个候选，照常过 D 的 gate。本版不建回退机制。

## 7. final 与主张

- **封存。** 计划（`FinalizationPlan`）在任何 test 题之前封存、hash 提交到 git。内容：任务 = 非 spent 领域的 test（100 题）；repeats 3；W0 臂与 W\* 臂；装了专科的领域清单与每个的路由 `worker_ref`；结构零规则；统计计划（bootstrap 2000、种子、95%）。`--phase evaluate` 付费前再核对一次：对每个未安装的领域，`project(W*, D) == W0`；对每个已安装的领域，路由与清单一致。
- **结构零。** W0 臂跑全部 100 题 × 3（给出绝对通过率与逐领域行）；W\* 臂只跑装了专科的领域的 test 题 × 3；在未安装的领域上 W\* 的读数就是 W0 的读数，逐题差值恰为 0。原因：两臂在那里执行同一个程序，真实差值恰为 0；用 0 而不是两次独立读数的差，估计无偏、方差更小。这是封存计划的一部分，不是事后的选择；它也放弃了 test 上的 A/A——隔离由 §4.7 的哈希断言与逐行审计证明。
- **主统计（C1）。** 100 道题上 W\* − W0 的配对差（每题先平均 3 次），按家族 bootstrap，95% 区间；下界 > 0 才写“框架有效”。逐领域行只作报告，不作主张。
- **功效（d 0.12，不计 dev → test 的缩水；每个装上的领域的真实 test 增益相同）：**

| 装上的领域 | 每域 +3 题 | 每域 +4 题 | 不用结构零时（+3 / +4） |
|---|---|---|---|
| 1 | 0.24 | 0.46 | 0.17 / 0.26 |
| 2 | 0.69 | 0.92 | 0.51 / 0.77 |
| 3 | 0.91 | 0.99 | 0.86 / 0.98 |

装 1～2 个领域时区间半宽约 3.8～5.9 个点。dev 上被接受的增益有选择偏差（v0.2 的 dev 80～84% 对 test 52%），真实 test 增益会更小；预注册的读法只看 test 区间。
- **费用。** W0 臂 300 × $0.466 = $139.80；W\* 臂每个装上的领域 75 × $0.49 = $36.75。装 1、2、3、4 个领域：$176.55、$213.30、$250.05、$286.80。加 8 路在飞 $58.21 与 2 笔卡住的预留 $14.55，最多 $359.56 ≤ $390 的份额；`freeze` 封存前打印并检查。
- **不跑 final 的情形。** 没有安装：final 只会比较 W0 与 W0，按规则不跑；报告负结果。
- 可选的等成本 best-of-k W0 臂仍只在发表结果时跑（M3 §2.2），不进 C1 的裁决。

## 8. 预算、日程与分阶段运行

### 8.1 单价与额度

- 单价（实测）：W0 每次尝试 $0.466；候选与 W\* $0.49。
- 额度：`workspace-claude-v3` 的根 $1,500 − final 份额 $390 − 已花 $424.61 − 3 笔 unknown $21.83 − 8 路在飞 $58.21 ≈ **$605** 可用；final 另用 $390 的份额。

### 8.2 单位费用

| 项 | 费用 |
|---|---|
| 第 1 阶段：3 次 W0 读数 × 4 领域 × 25 题 | 300 × $0.466 = $139.80（漂移时 + $46.60） |
| 每领域每轮：笔记 7 条 + 会话 | ≈ $3（v0.3：30 条笔记 + 一个会话约 $5.35） |
| 筛选 | ≤ 30 × $0.49 = $14.70 |
| gate 第一阶段 / 第二阶段 | 各 25 × $0.49 = $12.25 |
| 一个领域一轮：筛掉 / gate 停在第一阶段 / 两阶段都跑 | $17.70 / $29.95 / $42.20 |
| 一次安装（有后续轮次时）：train 重测 + 2 次池读数 | $11.3～18.6 + $24.50 ≤ $43.10 |

### 8.3 阶段（每阶段结束停下来看，按事先的规则继续）

| 步 | 内容 | 费用 | 墙上时间 |
|---|---|---|---|
| 0 | v0.3.0 已并入 main（`b71502d`，tag `v0.3.0`），在 `m1-automationbench` 继续 | 已完成 | — |
| 1 | 本文；M0/M1/M3/M4/M5/M7、ROADMAP 的 v0.4 一节；JOURNAL 的预注册条目（§0.3） | $0 | — |
| 2 | 代码与测试（§10）；全套离线测试；P2 的 20 份草稿回放（0/14 误拒）；首次调用等价回放；假模型彩排：第 1 阶段 → calibrate → 一轮分片（含一次移植、一次安装、投影池检查、逐行审计）→ final 封存（结构零） | $0 | 实现 |
| 3 | 第 1 阶段：`--phase shard --until calibrated`；然后 $0 的 calibrate：漂移检查、d̂/SE/δ_u/T_D、A/A 48 组、Marketing 旁证；`shard_calibration` 提交 | $139.80 | 1～1.5 h |
| 4 | 分片轮：`--phase shard --rounds 1`，一次一轮，至多 3 轮、G ≤ 12 | 见 §8.4 | 每领域一轮 35～50 min，每轮 2.5～3.5 h |
| 5 | 若 ≥ 1 次安装：`[freeze.final]` 填 W\*、`ouroloop freeze`、提交 hash、`--phase evaluate` | $176.55～286.80 | 1.5～2.5 h |
| 6 | 报告、RESULTS、JOURNAL；v0.4 并入 main，继续在 `m1-automationbench` | $0 | — |

情景（每领域每轮按 0.6 的概率进 gate、进 gate 的 0.1 进第二阶段估算，每个领域一轮期望 $25.79）：
- 没有安装：第 2 轮后各领域都因 patience 停下（没有新要约），8 个领域轮，约 $206 + $139.80 ≈ **$346**。
- 第 1 轮有一次安装：其他领域在第 2 轮收到要约、patience 重置，约 12 个领域轮 + 一次安装 + 胜者的第二读数，约 $310 + $43 + $12 + $139.80 ≈ **$505**。
- 上限：12 个两阶段都跑的 gate + 2 次安装 + 漂移补测 ≈ $779 > $605，所以靠下面的放弃顺序；任何一个领域轮都不会在付不起它的上界时开始。

### 8.4 继续 / 停止规则（事先登记）

- **第 1 阶段之后：** 漂移 > 6 题 → 弃用旧读数、补测一次；A/A ≥ 2 组接受 → 所有 T_D 上调一级；δ_u > 0.26 的领域本版不搜索，它的钱回到可用额度。
- **每轮开始前：** 若剩余可用额度放不下本轮所有待跑领域的上界（每个 $42.20，加可能的安装 $43.10），按顺序放弃领域直到放得下：(1) 已经装过专科的领域；(2) T_D 最高（功效最低）的领域。原因：钱优先给还没有增益、功效最高的领域；放弃只会减少 gate，族误收只降不升。
- **每个领域的 patience 2：** 连续两轮没有安装的领域停下；**它收到新要约时重置**（新要约意味着新信息）。只影响花不花钱，不影响 α（G 已事先计满）。
- **G = 12 硬上限；没有第 4 轮。**
- **过程检查（$0，在付 gate 之前看记录）：** 连续两轮基础设施 invalid → 停下修好，不按负结果处理；同一天两次基础设施暂停 → 不续跑，先排除供应商问题（沿用 v0.3 的规则）。
- **结局：** 全部轮结束没有安装 → 不跑 final，报告负结果，v0.4 并入 main；≥ 1 次安装 → 封存并跑 final，inconclusive 也算完成。

## 9. 复用现有工作区

### 9.1 留在 `workspace-claude-v3`，用新 scope

新配置 `examples/automationbench/claude-v4.toml`，`[workspace] root` 仍是 `workspace-claude-v3`，`[rsi] scope_id = "v04-shards"`，`[bench] domains` 仍是五个（启动检查拒绝 bootstrap 之后改领域表）、`spent = ["marketing"]`；同一个根、同一个 bootstrap release、同一个 K（`core:sha256:8d932781…`）、同一个合并划分 hash `5e68b39fca26a675`。不需要新的根，也不需要用户另作根的决定。

### 9.2 为什么读数能按运行身份复用

运行身份是 run payload 去掉 `RUN_LABELS`（candidate、role、eval_plan_ref、budget_scope、notes、system_release_ref、research_ref、experience_ref、mode）之后的部分（`bench.py:1432`），比较的是 `worker_ref`、`package_ref`、`prompt_hash`、`core_ref`、划分 hash、`repeats`、单次尝试限额、environment（上游 commit、包、工具集、runner 摘要）与模型身份。核对过的两条记录：

| run | split | worker_ref | package_ref | prompt_hash | core_ref | data | eval_plan_ref |
|---|---|---|---|---|---|---|---|
| `630a9c7dc67e39ad` | dev，125 次（主张 100） | `ba65df92…` | `43fc4d50…` | `f4e9c1648b32e159` | `8d932781…` | `5e68b39fca26a675` | 无 |
| `45846f008cea79b9` | train，252 次（主张 200） | 同上 | 同上 | 同上 | 同上 | 同上 | 无 |

`core_ref` 取自已存的 release（不是由当前代码重算），所以代码变了它也不变；新 scope 与 R 的文本都是标签；投影使 `project(W0, D)` 与 W0 逐字节相同。两条都没有计划，`control_pool` 对没有计划的 run 不比 `conditions()`，所以每个领域 25 题的 gate 计划直接把它们算进池（不需要放宽 `conditions()`）。第 1 阶段的新读数在各领域自己的 gate 计划下取（每领域 3 个 run，标签 `pool-1..3`），条件与该领域以后的每个 gate 相同。

### 9.3 复用什么、不复用什么、要补测什么

- 复用（$0）：dev 读数 `630a9c7d` 作每个领域的池读数 #1；train 读数 `45846f00` 作笔记与筛选的“之前”；W0 的判分明细与轨迹（`read_trajectory`）；v0.3 三张收据的 `metrics.per_domain` 作各领域的台账行。
- 不复用：三个候选的 dev 读数与七次筛选读数（W 不同）；W0 的笔记缓存（P1 的 `direction` 改了笔记提示，笔记身份随之改变，按每领域每轮 7 条重写，含在 $3 里）；Marketing 的任何读数（只作 §5.6 的旁证）。
- 补测：每领域 3 次 W0 dev 读数（§8.2）；漂移时再 1 次。
- **记为偏离的改动**（K 的对象不变，与 v0.3 活动中途的代码改动同样处理，第 1 阶段前写一条 `v04_deviation` 事件与 JOURNAL 条目）：R 的会话代码（P1 文本、按领域的输入、要约、去掉提示载体）；字面量检查（P2）；筛选规则（P4）；每领域的 gate 规则（K 的 `gate_plan` 是 `{}`，gate 规则在任务计划里，新 scope 冻结自己的计划，不与 K 冲突）；路由（release 字段与 `routing.v1` 能力）；K 里的 `code_version` `6d7a94fc2571b286` 自 v0.3 中途起就已不是当前代码的 hash。K 冻结的 worker 镜像、模型身份、五项限额、组件种类与包上限都不变。
- **保持不变的条件：** 不重建 worker 镜像，不改上游 commit `4a8e106`、`[llm]` 表、`default_agent.py` 与 `worker_runner.py`；任何一项都会合法地改变运行身份。

### 9.4 新工作区为什么不选

`CoreManifest` 含 `code_version`（全部 ouroloop 文件的 hash，`release.py:47`）与 `gate_plan`，用 v0.4 的代码 bootstrap 的新工作区会得到新的 `core_ref`，已有读数一个也接不上。要复用就得引入一个忽略 `core_ref`（及 `worker_ref`、`package_ref`、`prompt_hash`）的“执行身份”与导入阶段——这把“哪些读数可以合并”交给一条更松的规则，是正确性风险；不导入则要 $140～176 重测基线，还要用户决定新根。

## 10. 代码改动清单

按模块；每个测试先写成修正前会失败的样子。

**M0 契约（`ouroloop/evolve/contracts.py`）**
- `SystemRelease.worker_routes: dict | None = None`（领域 → worker 策略集 ref），校验值是 policyset ref；None 不写进 payload，旧 release 的 ref 不变。
- 测试：`test_contracts.py::test_a_release_without_routes_keeps_its_ref`、`test_a_route_must_name_a_worker_policy_set`。

**M1 运行（`ouroloop/policies.py`、`ouroloop/release.py`、`ouroloop/evolve/task_eval.py`、`ouroloop/agent/runner.py`）**
- `policies.project(objects, release_ref, domain) -> worker_ref`；`bind_release(..., domain=None)` 绑定投影，带路由的 release 不给 domain 即 `BindError`；`check_route(objects, shared_ref, route_ref)`。
- `release.required_capabilities_of` 在有路由时追加 `routing.v1`；`candidate_release`/`accepted_release`/`_swap` 加 `domain=`：`routes[D] := set_ref`，`worker_ref` 不变。
- `task_eval.make_agent_factory` 把 `domain` 写进 session_meta；`run_tasks` 拒绝不属于该臂领域的任务。
- `runner.PackageAgent.run` 在第一次模型调用前核对领域；`harness()` 记录 domain 与路由 ref。
- 不改：`agent/default_agent.py`、`agent/gateway.py`、`agent/worker_runner.py`。
- 测试（新文件 `tests/test_routing.py`）：`test_w0_projection_is_w0_for_every_domain`、`test_an_unrouted_w0_run_has_the_v03_run_identity`、`test_a_task_runs_only_its_domains_specialist`（哨兵 memory、打标记的 hook、包工具）、`test_a_routed_release_without_a_domain_is_refused_at_bind`、`test_a_route_that_changes_a_shared_prompt_or_the_actor_is_refused`、`test_a_mismatched_domain_is_infrastructure_and_never_calls_the_model`、`test_default_agent_bytes_are_pinned`、`test_worker_runner_digest_matches_the_frozen_image`、`test_the_first_gateway_call_of_a_recorded_w0_attempt_replays_byte_for_byte`。

**M3 gate 与 final（`ouroloop/evolve/task_gate.py`、`ouroloop/bench.py` 的 evaluate）**
- `GatePlan` 新增 `confirm_rule`（None = v0.3 的“确认读数单独 ≥ min_gain”；`"mean"` = 两读数均值 ≥ `min_mean_gain`）与 `min_mean_gain`；`confirm_rule="mean"` 时第一读数只判净值 ≥ `min_gain`，退步与成本按两读数合并判。两字段进 `V04_GATE_FIELDS`，None 时不写进 `to_json`，旧计划 ref 不变。
- `TaskEvalPlan` 新增 `control_readings`（None = `repeats`）；`shortfall` 与 `integrity` 按它判池是否足够；补齐的读数各自一个 run（标签 `pool-<i>`），不与已有读数的尝试 id 冲突。
- `FinalizationPlan`：装上的领域清单、每个的路由 ref、结构零规则；`evaluate` 按领域分臂、付费前核对投影；报告逐领域行。
- 测试：`test_task_gate.py::test_a_first_reading_under_the_stage_one_net_is_rejected_without_a_second_reading`、`test_a_domain_gate_accepts_on_the_mean_of_two_readings_against_a_four_reading_pool`、`test_the_first_reading_of_a_mean_rule_gate_is_judged_on_its_net_only`、`test_a_plan_without_the_v04_fields_keeps_its_ref_and_its_rule`、`test_the_pool_is_topped_up_to_control_readings_in_distinct_runs`；`test_paid_run_safety.py::test_the_final_reads_w0_as_w_star_on_every_domain_without_a_route`、`test_a_final_whose_routes_moved_after_sealing_is_refused_before_paying`。

**M4 研究（`ouroloop/research/session.py`、`ouroloop/research/diagnosis.py`、`ouroloop/evolve/experience.py`、新 `ouroloop/research/library.py`、`ouroloop/agent/harness_package.py`）**
- `run_session_round(ctx, budget, domain=)`：`package/` = 投影的包；不挂 `PROMPT_FILES`；输入按 D 过滤；`inputs/donors/<offer>/`；`SESSION_FORMAT` 的分片说明；去掉 `research.propose` 前缀（`:906`）；`DECLARATION_FIELDS` 加 `direction`、`not_when`、`contrast_case`、`ported_from`、`adaptation`；`finish` 的 `declined_offers`；准入加 `shared_layer_change`、D 内的 `predicted_affected`、接地读取、移植检查；`TrajectoryReader` 加领域过滤与通过的尝试；`passing_sample` 按领域、按轮重抽；`pick_best` 的资格（≥ 1 修复、样本退步 ≤ 1）。
- `diagnosis.select_failures(..., domain=)`；笔记加 `direction`。
- `experience.ledger_row` 带 `domain`、`ported_from`；`ledger_projection(rows, objects, domain=)`：本领域全文、其他领域摘要、v0.3 切片、噪声带；`transfer_funnel(rows)`。
- `library.py`：从被接受的收据与四个种子建条目；校验器（任务 id 正则、尝试/笔记 ref）；渲染到 `inputs/donors/`；要约的生命周期（open → attempted / declined / not_considered → closed，新版本重开）。
- `harness_package.literal_hits`/`describe_hits`：P2。
- 测试：新 `tests/test_session_shard.py`：`test_r_for_a_domain_sees_only_its_notes_trajectories_ledger_and_histogram`、`test_reading_another_domains_attempt_is_access_denied`、`test_a_candidate_with_a_worker_prompt_file_is_refused`、`test_a_port_must_name_an_open_offer_and_read_a_failing_and_a_passing_trajectory`、`test_an_offer_not_attempted_closes_with_the_stated_reason`、`test_the_screen_needs_a_fix_and_at_most_one_sample_regression`、`test_the_passing_sample_is_redrawn_per_domain_and_round`、`test_the_legacy_one_change_prompt_is_not_in_the_session_system`；新 `tests/test_library.py`：`test_a_donor_entry_holds_no_task_id_or_attempt_ref`、`test_an_install_opens_one_offer_per_other_claim_domain_at_the_round_boundary`、`test_seed_donors_carry_only_their_dev_receipts`、`test_a_rejected_port_is_not_offered_again_without_a_new_version`；`test_diagnosis.py::test_notes_are_drawn_from_one_domain_and_carry_a_direction`；`test_harness_package.py::test_literal_hits_name_file_line_and_span_and_exempt_shared_values`、`test_the_twenty_recorded_drafts_replay_without_the_fourteen_false_refusals`。

**M5 执行与预算 / 驱动（`ouroloop/bench.py`）**
- `Improvement.plan(domain=)`：D 的 25 道 dev、`control_readings` 4、该领域的 `min_mean_gain`；`run_identity(..., domain=)`；`run_arm` 拒绝跨领域；`control_pool`、`prior_readings`、`_mainline_readings`、`final_estimate` 经 `run_identity` 自动按投影。
- `_install(domain=)`：逐领域 CAS 与其他投影不变的断言；安装后的两步只在 D 有后续轮次时做；`_preflight` 对目标 release 的每条路由各绑定、各探一次（带代码的专科要求合格的 worker，同 M3 §3）。
- 新阶段 `--phase shard`（cycle 的新段种类 `shard`）：同步轮与快照、每领域 patience 与要约重置、gate 计数（拒绝第 13 个）、冻结的 T_D 表、放弃顺序、`--until calibrated`；`calibrate` 步：漂移检查、d̂/SE/δ_u/T_D、A/A，写 `shard_calibration`，第一个 gate 之后拒绝重写。
- `startup_checks`：路由键 ⊆ 非 spent 领域；第一个 gate 之前池 ≥ 4；`shard_calibration` 已存在。
- 新 preset `automationbench_harness_v4_shards`：harness 段与 v3 相同（K 的能力仍然有效），另加 k 3、每领域笔记 7、失败集 6、通过样本 4、`control_readings` 4、两阶段 gate、G 12、3 轮、patience 2。
- 测试：`test_bench_phase.py::test_shard_rounds_stop_at_the_frozen_gate_count_and_per_domain_patience`、`test_a_new_offer_reopens_a_domain_stopped_by_patience`、`test_the_pool_reuses_the_v3_baseline_reading_by_projected_identity`、`test_an_order_spanning_two_domains_is_refused`、`test_installing_one_domain_leaves_every_other_projection_pool_and_train_reading_unchanged`、`test_two_domain_installs_in_either_order_give_the_same_routes`、`test_a_candidate_whose_domain_moved_under_it_is_stale`、`test_the_drop_order_removes_installed_domains_first_then_the_highest_threshold`、`test_calibration_is_frozen_before_the_first_gate_and_cannot_be_rewritten`、`test_a_drifted_old_reading_is_dropped_and_replaced`。

**M7 报告与工具（`ouroloop/report.py`、`ouroloop/cli.py`、`examples/automationbench/`）**
- 报告：每领域的 gate 行（T_D、δ_u、两阶段读数、噪声 sd）；借鉴库与迁移漏斗；逐行运行身份审计；final 的逐领域行与结构零声明。
- `aa_replay.py`：`replay_domains(readings_by_domain, thresholds)`，只读 dev，过生产 `judge`。
- `shard_sim.py`：本文 §1、§5、§7 的模拟（只用模型、不读数据），复现每张表。
- `claude-v4.toml`。
- 测试：`test_aa_replay.py::test_multi_domain_replay_counts_directed_splits_per_domain`、`test_report.py::test_the_report_audits_every_run_against_its_domains_projection`、`test_the_transfer_funnel_counts_offers_attempts_declines_and_verdicts`。

**文档。** DESIGN 加 v0.4 指针；M0（路由契约）、M1（投影与强制点）、M3 §2.1（每领域 gate）与 §2.2（结构零）、M4（分片会话、借鉴库、要约、P1/P2/P4）、M5（分片段、放弃顺序、calibrate）、M7（报告）；ROADMAP 的 v0.4 一节；JOURNAL。

## 11. 风险

1. **功效是约束性风险。** 每领域 25 道 dev：d 0.12 时 +12 个点的真实效应只有约 0.37 的机会被接受，sales 一类的吵领域约 0.18。现实结局是 0～2 次安装；1 次安装时 final 的主统计功效 0.24～0.46（结构零），2 次 0.69～0.92。缓解：按噪声定门槛（稳定的领域功效 0.76～0.88）、放弃顺序把钱留给功效高的领域、结构零；不以 α 换功效。
2. **弥散的通用机制被分片看不见。** 每领域各 +2 题的机制，每领域 gate 功效约 0.14，而合并 gate 约 0.75。缓解：移植把一个领域接受的机制交给其他领域在各自 gate 上再测；版本边界的晋升（§4.8）。v0.3 的证据是异质的，所以接受这个代价。
3. **噪声模型的错配与漂移。** 尾部形状、领域间的差异、供应商在第 1 阶段与 gate 之间的漂移都会改变真实 α。缓解：每 gate 0.0035 的目标（并集界 0.042，留出余量）、δ_u 的 +0.5 SE、A/A 报警、第 1 阶段与第一轮相隔不超过一两天。
4. **复用的 v0.3 读数由不同的宿主代码产生。** 缓解：首次调用的等价回放、`default_agent.py` 与 `worker_runner.py` 钉住、漂移检查与补测。
5. **负迁移。** Marketing 的种子都是“多收集、多做”的文字，v0.3 里这类文字在该克制的领域有害。接收方的 gate 会拒绝它们，但它们会占 k 的名额。缓解：每会话至多一个移植、要约可拒绝并写理由、`not_when` 与反例读取、R 能看到 v0.3 的负结果。
6. **领域内的过拟合。** 一个专科从约 50 道 train 学来，可能编码模板或评分约定；同一领域的 25 道 dev 至多被读 3 个 gate。缓解：dev 与 train 不相交、P2 的字面量检查、R 拿不到 dev 题号；主张只读封存的 test。
7. **赢家诅咒。** 抬高安装后的池（保守）并带来 dev → test 的缩水（v0.2：dev 80～84%、test 52%）。主张只读 test。
8. **预算紧。** 上限情景超过 $605；未知结算与订阅限额暂停（`ProviderAccountError`）会吃掉余量。缓解：放弃顺序、每个 gate 前的 `_verify_bound` 美元检查、每轮开始前的上界检查。
9. **墙上时间。** 领域轮顺序执行，至多约 10 小时，外加订阅暂停。不影响结果；第一轮之后若单轮超过 4 小时再考虑跨领域批处理（§12）。
10. **部署要知道领域。** 未知领域落到 W0 的 app：安全、无增益。
11. **公平性的质疑（“W\* 知道领域”）。** 两臂都不被告知标签；W\* 是按业务流程分别演化的 app，DESIGN §1 允许；报告照写。
12. **工程面。** 路由触及绑定、发布、安装、会话、gate、校准与 final。缓解：新字段都缺省 None、旧 ref 不变；W0 的尝试路径（default agent、网关、worker runner）不改；等价与恒等测试。
13. **多重检验的蔓延。** 任何续跑都必须在新的预注册里重新计数全部 gate；驱动拒绝第 13 个 gate。

## 12. 考虑过、不做

- **搜索期间的共享可变层**（含 shard 轮里的提示载体）。原因：v0.3 失败的正是这种耦合（rulesmem：marketing 涨、sales 跌）；领域的说明文字照样经专科的 memory 进入提示。
- **新 target `worker.routes`**（strict-shards 原稿）。原因：v3 的 K 冻结了 9 个 target，`bind_release` 拒绝其余；改 K 就失去全部读数复用。
- **`worker.harness` 下的新内建 `harness_routes.v1`**（评审 1 的嫁接）。可行，但 W\* 的 worker 策略集对每个领域都变了，每个按 `worker_ref` 查读数的地方都要先学会投影；release 字段让每条路由本身就是普通策略集。
- **一个包里放所有专科的 `domains/` 键**（transfer-experiments）。原因：改 `parse_manifest`，24 个文件、256 KiB 的上限要么对全部领域合计、要么改 K；还要一个从旧行重算的 `routed_ref`。
- **分层编译**（共享层 + 覆盖层、override/disable、命名的 memory 段、计算出的作用域，shared-plus-overlay）。原因：共享层本版冻结且为空，没有东西要编译；它为的是版本内晋升，而晋升本版不做。
- **版本内晋升与合并的基础 gate。** 原因：约 $123 的 gate 加约 $188 的池重建，护栏放过 −12 个点伤害的概率 6～14%，且改变不了主张；规则写在 §4.8，在版本边界用。
- **新工作区、执行身份与导入阶段**（shared-plus-overlay、transfer-experiments）。原因见 §9.4。
- **给模型或 `ctx.view` 的领域标签。** 原因：没有东西读它，只会改动 W0 的输入。
- **Marketing 的搜索、读数与护栏 gate。** 原因：路由使 Marketing 永远执行 W0 的字节，护栏多余；它的 test 已用尽，只作借鉴的种子。
- **合并的 125 题 gate 与诊断 P5 的主张人群 gate。** 原因：被每领域 gate 取代（§1(3) 的比较）。
- **两读数无条件**（strict-shards）。原因：事先登记的第一阶段每个空 gate 省约 $11，功效只损失约 0.02。
- **每个读数各自 ≥ 3**（transfer-experiments）。原因：在相近 α 下功效 0.34 / 0.63，低于均值规则的 0.44 / 0.77。
- **5 次读数的池**（strict-shards）。原因：多花 $46.60，d 0.12 时只是把 T 从 3.25 移到 3、功效约 +0.04～0.06；4 次读数已足以给 A/A 48 组。
- **先买 2 次、只给吵的领域买第 4 次**（评审 3 的省钱方案）。原因：最多省 $46.60，多一条按数据分支的规则。
- **按点估计选门槛 / +1 SE / 合并估计一个 T。** 原因见 §5.3 的比较表：分别超过 1/20、功效太低、伤 finance 且 sales 超标。
- **test 上的 A/A 臂与“装上的领域子集”的次要对比。** 原因：结构零在未安装领域上精确为 0，花 $36.75 一个领域什么也证明不了；有结构零时子集对比与主统计只差一个比例。
- **gate 每个候选。** 原因：G 变成三倍；筛选在 train 上挑，不花 α。
- **免 gate 的移植、筛选给移植加分、为移植保留 gate。** 原因见 §4.3。
- **R 声明“通用”的 `general` 标记**（shared-plus-overlay）。原因：晋升看的是 ≥ 2 个领域各自的 gate 证据，不是 R 的说法。
- **R 读其他领域的原始轨迹或笔记。** 原因：失败会经由 D 的专科隐式迁移，归因与隔离都说不清；跨领域只给被接受的产物与计数摘要。
- **跨领域并行批处理筛选与 gate、路由服务、单一多领域导出、专科去重、回退机制。** 原因：只省墙上时间或只是外观，不改结果；第一轮实测后若单轮超过 4 小时再考虑批处理。
- **本版的研究位（C4）、P4 的留出切片。** 原因：每域 25 题拆不出内层池；留出切片只记录、不判定。
- **推迟：** P3 的“生效 / 未生效尝试”翻转拆分（台账先给计数与噪声带）；第 4 轮（须新的预注册并重新计数全部 gate）；等成本 best-of-k 臂（发表时可选，M3 §2.2）。

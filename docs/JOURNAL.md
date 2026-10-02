# 开发日志

倒序，最新在上。一条记录一次有意义的改动：做了什么、证据（真实运行或测试量到的数，带数字）、因此改了什么设计、commit id。日期取自 git log。

证据只写量到过的：贪吃蛇（当时的 `examples/snake`）在一台 GPU 主机上的几次多代运行（下称 run4、run5、run6）、离线运行、决策集上的统计，以及先写失败测试再修的评审。没有证据的改动照实说"没有运行证据"。

哪些数字能复算，哪些不能：决策集上的统计从 `evals/replay_campaign/` 重算；M6 的真实验收从 `examples/workspace-m6/ledger/` 重算。**贪吃蛇场景包与多代演示循环已于 2026-09-27 删除**（见当日第二条记录），因此所有贪吃蛇数字——离线演示与 run4～run6——现在都无法从本仓库复算；它们留在下面的历史条目里，作为当时改动的依据，不作为可复现结果。run4、run5、run6 本来也跑在一台未发布工作区的 GPU 主机上。

---

## 2026-10-02 复核、首个真实活动协议与付费前彩排

- 做了什么：对 HEAD `9270a7d` 做 11 个范围的实现复核。每个范围一名审计员读规格、代码与测试，一名复核员找反证，最后由综合与批评各一轮收口，人工抽查高危项。另做了 RSI 定位与业务落地评估，以及三版迭代计划的评审。全程只读，没有调用模型。复核全文见 `history/V0_6_REVIEW_2026-10-02.md`。
- 发现（复核后成立）：
  - 高危 4 项：
    - K 未冻结 methods 时，准入放行 R 提供的 `spec.backend`，构建预检经 `serving.py:42-44` 到 `config.py:77` import 任意对象。离线 PoC 已复现。
    - 两支 W* 相同时，研究裁决仍可 accepted；`test_campaign.py:96-108` 把它断言成了预期。
    - 外层比较按各支剩余的有效任务求均值，不配对（`campaign.py:341-348`）。
    - 续跑不读 Experiment 冻结的 `eval_template_ref` 与 `budget_scope`（`research/loop.py:346-356` → `bench.py:748-762`）。
  - 暂停只在 worker 轮循环里写入；collect、续跑、基线、研究位提案、公共测量与外测遇到账号错误或截断时，以异常退出。
  - 示例研究模型的输出上限 16384 不够：上限 16384 时，账本中 `linkcheck2-gate` 的两次汇总调用一次输出 16384 被截断，另一次输出 16293（只差 91）；上限提到 32768 后，同等提示规模的一次输出 13332。
  - 所有 improve/cycle/research 集成测试都注入脚本研究员；`cycle_pairs` 在测试中一律被替换；Docker 里的代码草稿检查链没有测试。
  - R 的可演化面只有 `research.diagnose` 与 `research.propose` 两段提示。preset 只计划一个研究位；cycle 没有拒绝调高的 research_slots，调高后 R1→R2 可达，但未测试、未计划。
  - 文档状态过时、行号过期；“20 次中 18 次无效”混用了口径（磁盘上 20 条 dev 记录中 19 条无效）。
- 因此改了什么设计：
  - ROADMAP §3 新增首个真实活动协议，内容包括：付费前必修清单（能落成测试的条目注明测试名）、付费前彩排、独立的计价校准、活动结构与预算（$300 根，其中 final 分配 $50）、gate 口径与免费 A/A、检查点 K1～K3 与止损、final 时机。
  - 付费前彩排采纳用户的提议，由 Claude 充当 LLM：
    - 通过 `[research.llm] type = "manual"` 读请求文件作答；
    - actor 是对包内容作出反应的确定性假 actor，任务世界是以 marketing 域名导出的小型 fixture 上游；
    - 经 CLI 走生产 cycle，按步骤注入截断、读不出、形状非法的回复；
    - 由工作区名与本日志标注为 synthetic（报告本身不带标记），不计入证据。

    它替代不了计价校准与真实运行。Claude 作答 actor 另在一个以 manual pin 的工作区里做。
  - M5 §3 按故障所在的层写定暂停与结束规则：账本额度耗尽是有界结束，不是暂停。还写定了续跑的冻结输入。M4 §1 按步骤写定读不出的回复如何处理，截断则暂停。08 §3 写入彩排的验收与边界，M6、M7 和 RESEARCH_AGENT 同步。
  - DESIGN §7 与 README 开头如实写明：首版 R 只改两段提示，属于有界、一阶的改进策略更新，对外表述为 bounded RSI 工程原型。
  - 修正 README（中英）、DESIGN、DEMO、RSI_REVIEW、ROADMAP、examples README 与 M0 中过时的状态和行号；README 的任务数由 10（7+3）改为 29（21+8）。
- 文档改动经三个视角（代码事实、跨文档一致性、协议可执行性）审查，每条发现再由一名复核员找反证：53 条中 47 条成立，均已修正。不采纳的一条修法：研究位的 system 提示不去掉 R 自己的 propose 文本，因为 R 正是用它提出对自身的修改；改由研究位格式声明以哪段为准。
- 证据：703 passed, 1 deselected（未改代码）。
- commit：见本条之后的提交。

## 2026-10-01 真实 gate 三次尝试，与首版需求审计

- 做了什么：在 `linkcheck2` 工作区新开一个 scope（`linkcheck2-gate`，$6 上限），dev 计划取冻结顺序的前 5 个任务（5 个家族），其中 1 个做筛选，gate 门槛沿用 preset。跑生产的 `bench.improve` 一轮，不设停止点；复用已测得的 train 失败（`campaign_launch_checklist`）。
- 第一次：R 看到了 W 当前的结束检查，没有再加重复的 skill，而是提议把它合并进 `worker.finish`。诊断也比上一次准：未走的那个分支上的交付物被当成“不适用”，应当无论走哪个分支都给请求者发一份状态总结。但 R 用了包补丁的形状写这个提示改动，构建判为 invalid。原因是格式只示范过包改动的形状，现已同时给出两种（`20a9ea8`）。2 次调用，$0.11。
- 第二次：GLM-5.3 把汇总调用的 16384 个输出 token 大多用在推理上，JSON 在 proposal 之前被截断；`parse_json` 遇到残缺文本返回 {}，回退逻辑把“什么也没读到”变成了 collect。现在被截断的研究回复会被拒绝：本轮以 repair_runtime 暂停，并提示调大 max_tokens；读不出的回复记为 invalid 轮；任一模型的 max_tokens 超过 `[pricing] max_output_tokens` 时启动检查拒绝（`0e9cbe8`）。3 次调用，$0.07。
- 第三次（研究模型 max_tokens 调到 32768）：
  - R 的提案有效，依次经过准入、冻结、构建、筛选和比较。
  - 从筛选的对照臂开始，智谱账号余额耗尽：每次调用都返回 HTTP 429，错误码 1113，“余额不足或无可用资源包,请充值”。
  - 20 次 dev 尝试中 18 次无效，配对不全，判为 inconclusive，未安装。这些失败按基础设施处理，没有算作候选的失败；429 的预留被释放。本 scope 合计 $0.50，没有 unknown 结算。
  - 修正：429 原先被当作限流重试 4 次，整轮比较又把每次尝试都耗掉。现在账号类错误（余额、配额、被拒的 key）单独识别，不重试；一出现就停止这一次尝试，本轮以 repair_runtime 暂停，实验保持打开，充值后重跑会从记录的状态继续，而不是判为 inconclusive。
- 需求审计（5 个审计加 5 个复核，逐项对照 docs/modules/00–09 与 ROADMAP/DEMO）：首版 354 项中，186 项实现且有测试，23 项实现但无测试，136 项部分实现，7 项缺失；约 77 项属于第二版，按设计推迟。审计还发现一个真实缺陷：研究位提案的 origin 写成了 "automatic"，不在允许值内，真实 R 的每个研究位提案都会在准入前报错，此前只有脚本研究员能走通，已修正（`8954b9f`）。明细见 `history/V0_6_AUDIT_2026-10-01.md`。
- 待用户：智谱账号需要充值后才能继续真实运行；计价口径（open.bigmodel.cn 的地址与 Z.AI 的美元标价）仍待确认。
- 证据：703 passed, 1 deselected（Docker 运行时）。
- commit：见本条之后的提交。

## 2026-10-01 真实失败路径：从失败任务到构建好的候选，修正它暴露的问题

- 做了什么：在新的独立工作区（`linkcheck2`，$5 上限，按修正后的 bootstrap 建立：K 冻结 package_api 与镜像 id，release pin TargetHarness 与 required_capabilities），依次测 train 任务，直到出现一个失败，然后对它跑生产的 `run_worker_round`。screen handler 换成停止点，所以不跑 dev、不安装。
- 真实运行：
  - 第一个任务 `marketing.campaign_launch_checklist` 就失败了：partial 0.75，4 条计分断言中 `gmail_message_sent` 未过；28 次工具调用，217 秒，$0.128。
  - 研究步骤：一次笔记调用 → 一次汇总调用，提出一个 skill（`finish_deliverable_audit`）。首稿被草稿检查拒绝（skill 条目写成了字符串），一次修订后通过。
  - 之后准入 → 冻结 Experiment → 构建候选 W（只比 W0 多了新包）→ 在 screening 停止。
  - 14 次调用，$0.210。包是纯文本，没有启动 Docker。
- 独立核对（5 个审查按链路找反证，另加一个完整性审查）：
  - 只凭记录重放 28 次工具调用，得到同样的 0.75。
  - 运行记录带 mode=collection、宿主契约与 actor 配置；冻结的实验引用的是检查过的包；什么都没安装，也没有 dev 尝试。
- R 的诊断质量（仅此一例）：笔记找对了失败的检查，机制具有一般性，不含任务答案。但根因写错：它说邮件被“无声漏掉”，实际上 agent 在最后一条消息里明确说明了不发的理由，而 R 看到的摘要只保留每条前 600 字符，恰好截掉了这段。R 也看不到 W0 已有的结束前复查指令，于是提出了与之重复的 skill。该任务本身也有矛盾：Legal 被撤销，但评分仍要求发一封含 “ready” 的邮件。
- 发现并修正（再经 3 个审查找反证，又修正了其中发现的问题）：
  - 供应商在计费层之下自行重试：GLM 客户端默认超时 120 秒、重试 4 次，Anthropic SDK 默认重试 2 次，这些请求都不入账。那次 236 秒的汇总调用按时长与缓存判断是两次请求、一次预留。
    - 改为计费层接管重试：每次请求单独预留，只重试可重试的错误，并在调用期间临时关掉供应商自己的重试，不永久改动共享的供应商对象。
    - 确认没有到达模型的失败（连接失败、429/503/529）释放预留，不再以最坏值记为 unknown。
    - 默认超时改为 600 秒。
  - 摘要改为保留每条的首尾，最后一条消息保留到任务文本的上限。
  - R 在 targets 下看到 W 当前的指令文本，提示要求修改或合并而不是重复。
  - 告诉 R 包 API：条目形状、各 hook 位置的事件字段与动作效果、ctx.view、ctx.call_tool、测试约定。拒绝信息写明需要的键；修订时给出父包。
  - 重复提案检查原先从未生效（history 从未填充）。现在按同一父版本上已有结论的实验判重，判为 invalid 而不是 repair_runtime；因基础设施失败或未能安装的实验不算重复。
  - 汇总调用读取笔记跑完之后的预算。
  - 每轮记录 R 的完整 Requirements、修订请求，以及汇总时看到的预算。
  - 复核中还发现我的一处回归：`retrieve()` 已按新到旧排序，我把 `[:10]` 改成 `[-10:]` 反而取到旧行，已改回，测试改为经过 `retrieve`。
- 仍未在真实模型下运行：gate、安装、任务中运行的包、Docker 中的代码草稿检查（R 按 preset 优先写文本组件），以及 R 的递归。
- 证据：696 passed, 1 deselected（Docker 运行时）。`linkcheck2` 中停在 screening 的实验已标为 cancelled，任何续跑都不会让它进入付费的 dev 比较。
- 观察（未改）：评分器按子串判断 Slack 中的 “ready”，一条写着“NOT ready to launch”的消息也算通过；partial credit 在这里高估了正确性。
- commit：见本条之后的提交。

## 2026-10-01 真实链路检查：GLM-5.3 跑通一条，修正它暴露的问题

- 做了什么：
  - 提交阶段 A～E（`07af0a6`）。
  - 启动 Docker Desktop，构建 worker 镜像；加了 `.dockerignore` 白名单，构建上下文只含 runner。IT6-23 首次在真实 daemon 上运行，发现包代码能看到基础镜像自带的环境变量（GPG_KEY 等，不来自宿主）。runner 改为经 `env -i` 启动，只给 PATH 与 LANG，`/proc/self/environ` 也只有这两个；另加两项只在 Docker 下运行的测试：挂住的包记为候选缺陷且容器被删除；按 K 冻结的镜像内容 id 运行（`142a3e1`）。
  - 在独立工作区（`linkcheck`，$5 上限）用真实模型跑一条：一个 train 任务，再对其结果跑研究步骤，在准入和 gate 之前停止。驱动按 `run_worker_round` 的顺序调用同样的构件。
- 真实运行：
  - 任务 `marketing.contact_data_cleanup`，GLM-5.3 通过：10 条计分断言全过，另有 5 条被评分器排除。17 轮、42 次工具调用、250 秒。prompt 251,014 token（缓存命中 222,272），输出 22,645，$0.198。
  - 研究步骤：没有失败，所以没有笔记调用。一次汇总调用返回 next_action=collect，理由是没有失败证据。
  - 合计 18 次模型调用、$0.204，全部结算，没有 unknown。
- 独立核对：5 个审查按链路逐项找反证。
  - 评分来自官方 scorer：15 条断言的判定只凭记录即可复现；什么都不做的得分为 0/10，所以 1.0 不是空通过。
  - 没有评分信息进入 agent。
  - 计费逐次复算一致。
  - R 的输入不含 dev/test 的任务 id 或评分器文本。
- 发现并修正：
  - 诊断记录的 scope 被工作集标签覆盖，笔记轮换实际从未生效。改为工作集单独成字段；R 另外拿到工作集的测量数与通过数。此前 R 把“0 个失败”读成了“还没测量”。
  - 每次调用按最坏情况预留 $1.47，但没有与预算核对。$5 预算下每个研究分支只有 $0.75，每次调用都会被拒。现在启动检查与 cycle 会在付费前拒绝。
  - R 看到的预算是阶段账户自身的空限额。现在给出按祖先范围计算的实际剩余。
  - RunManifest 未写 mode 与 target_harness_ref。bootstrap 现在创建 TargetHarness 并写入 release 的 required_capabilities，首版的运行记录 mode（train 为 collection，其余为 task_evaluation）、宿主契约和固定 actor 的配置。这些字段进入运行 hash，所以在真实数据产生前修正。
  - Docker 或镜像不可用时，K 会冻结一个空的镜像 id；允许代码组件时改为拒绝 bootstrap。
  - 包代码只挂载到合格的 worker，测试 worker 须显式声明 `for_tests`，草稿检查同样检查。
  - 由草稿生成的包记录镜像。
  - 每次研究请求记录输入摘要。
  - 测试：680 passed, 1 deselected（Docker 运行时，无跳过）。
- 未跑到的环节：因为任务通过，以下都还没有在真实模型下运行：失败摘要、逐题笔记、带补丁的汇总、草稿检查（含 Docker 中的 probe/test）、准入与 gate、安装、任务中运行的包，以及 R 的递归。
- 需要用户确认：配置用的是智谱开放平台 `open.bigmodel.cn` 的地址和 `ZHIPUAI_API_KEY`，而 `[pricing]` 引用的是 Z.AI 的美元标价。如果实际按国内平台计费，账上的美元数只是按 Z.AI 价换算的估计，不是账单。
- 观察到的行为（未改）：没有失败时 R 返回 collect，驱动会补采：优先重跑最近的失败，没有失败就重跑前几个 train 任务。文档没有规定零失败时该做什么。
- commit：见本条之后的提交。

## 2026-09-30 首版阶段 B～E 实现：隔离 worker、导出、递归比较与 cycle

- 做了什么：
  - B 代码组件与隔离（`agent/worker.py`、`agent/worker_runner.py`、`docker/worker.Dockerfile`）：
    - 包代码只在 worker 进程内执行，宿主经 stdin/stdout 协议通信。
    - DockerWorker 是唯一合格的 worker：无网络、只读根目录、tmpfs、uid 65534、去掉全部 capability、有 CPU/内存/PID 上限，按 K 冻结的镜像内容 id 运行。ProcessWorker 只供协议测试。
    - broker 只能调用本回合的基础工具，hook 的 broker 只读。
    - 宿主在 before_tool、after_tool、on_finish 三处调用 hook。before_tool 的 block 停掉本回合其余调用；after_tool 的反馈作为单独一条消息；on_finish 的回推与 StopGuard 共用上限，最后一回合不回推。
    - 包工具不得与基础工具或 read_skill 同名，草稿检查与挂载时都会拒绝。
    - 草稿检查扩展为 isolated_probe → package_test，包测试只在由 train 构造的世界里运行。
  - 故障归属：包代码抛异常、调 sys.exit、杀死或挂住 worker 时，如果新起的 worker 仍能正常应答，记为候选缺陷。只有新 worker 也不应答才算基础设施，并以 repair_runtime 暂停活动。worker 的 stderr 边读边丢弃，只留末尾；输出与单行长度都有上限；超时的容器会被删除。
  - C 导出（`release.export_deployment`、`app.py`、`ouroloop export`）：
    - 按白名单导出 W、actor 身份、包的运行部分和 K 的运行投影，不含 R、测试和证据。
    - 先在临时目录构建，扫描凭据和本地路径后再移入目标目录。
    - App 只在合格且镜像一致的 worker 上运行含代码的包。
  - D 递归比较（`research/campaign.py`、`bench.research`）：
    - 两支都跑首版循环，工作区按 campaign 分开。
    - W0 的 train 测量在 campaign 级只做一次，判分明细与轨迹复制进各支。两支第一轮的笔记请求完全一致时，笔记在 campaign 级只写一次，作为共同输入。
    - 各支逐轮记录已完成的轮次，崩溃后只补缺，崩溃前中断的实验先按记录续完。
    - 暂停的一支（worker 失联，或末轮因基础设施失败）修复后续跑到冻结轮数；暂停期间 campaign 判 inconclusive，不做外测。
    - 已出 receipt 的 campaign 不再重跑，接受后的安装只做一次。
    - R1 改过的 prompt 必须出现在候选支的实际请求里，否则判 invalid；只改 manifest 的 R1 在付费前拒绝。receipt 的成本包含两支、共同输入和外测。
    - 每轮记录加载的 R、context 和各步请求摘要（`research_steps` 事件）。研究请求按步骤（note/propose/revise/research_slot）分别记账。
  - E 用户入口（`bench.cycle`、`[improvement]`、preset `marketing_harness_v1`、`examples/automationbench/improvement.toml`）：
    - reader 只接受已实现的枚举，第二版方法按名拒绝。
    - preset 的 harness 数值是上限，`[harness_evolution]` 只能收窄；镜像由 preset 固定。
    - 允许的方法冻结进 K，准入时强制执行。
    - cycle 先冻结缺少的计划（不得放宽 preset，[rsi] 已给的计划同样检查），然后：基线一次 → worker 段 → research slot → 下一个 worker 段（检查 R1 是否真的被用上）→ 离线报告。无提案不会吞掉 slot；暂停与预算耗尽按规则停止；重跑只补缺，基线也只补缺。
    - 首版的 research slot 由当前 R 读取经验投影和自身的 prompt，提出 R 候选。
    - 报告新增：包的组成；实际技能读取、hook、包工具与 worker 时长；每次测量的范围；笔记覆盖；草稿检查；按步骤的研究费用；gate 门槛；cycle 与回用状态；公开数字背景表。首版的 C3 与蒸馏标为 not_run。
- 证据：
  - `.venv/bin/python -m pytest -q` → 667 passed, 1 skipped, 1 deselected（阶段 A 结束时为 611）。跳过的是 IT6-23 Docker 隔离测试：本机 Docker daemon 未运行，镜像也未构建。
  - 覆盖的首版用例（含阶段 A）：CT6-01/14、PR6-01/10/11/12、RL6-11/17/18/20/22/23/25/26、EX6-12、ER6-06/14/15、RC6-10/11、IT6-11/16/19/21/22/23/28、BR6-01/09/10/12/14/15/16/17。
  - fixture 上的完整 cycle：冻结计划 → 基线 → 段 0 无提案 → slot 中 R1 被接受并安装 → 段 1 加载 R1、按 R1 的 propose 文本发出请求 → 报告。重跑 cycle 不新增任何尝试或研究请求。
- 独立审查：B/C、D、E 三个审查共报 31 条（分别为 12、7、12 条，含低严重度项），复核后全部成立并已修正。需要 Docker 的三处（删除超时容器、docker 客户端环境、按镜像内容 id 运行）本机无法测试，其余都补了测试。最重要的几条：
  - 包代码自行退出或挂住 worker 会被当作基础设施，活动永久暂停。
  - worker 的 stderr 无人读取，输出多时会挂住。
  - 超时的容器不会被删除。
  - 两个 campaign 用同名配对时，后一个会复用前一个已完成的分支。
  - 分支与段的进度只在结束时写盘，崩溃后会多跑轮次。
  - 已完成的 campaign 重跑会再付一次外测费用，并在安装时失败。
  - slot 的研究门槛组数取自 worker gate，示例配置下 R1 永远无法安装。
  - slot 暂停后 cycle 仍继续下一段。
- 设计补充（写入 M7 §2）：research slot 的配对规则（开发家族按冻结顺序对半，前一半为内层 gate，后一半为外层）与每支预算比例由 preset 固定；`test_timeout_seconds` 用作 worker 每个请求的时限。
- 证据边界：全部为离线 fixture 与脚本研究员，只证明接线，不是收益。没有付费运行，没有真实模型的能力包演化或 C4 结果。Docker 隔离尚未在真实 daemon 上验证（构建命令：`docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .`）。上一条记录中的未决问题（dev 回归被拒时的诊断信号）不因实现而改变。
- commit：未提交。

## 2026-09-30 首版阶段 A 实现：诊断输入与文本能力包

- 做了什么：按 ROADMAP §2 阶段 A 实现，不含 Docker、trainer 或 teacher。
  - 契约：既有记录加默认 None 的字段，旧 ref 与 v2 prompt_hash 不变；`from_json` 拒绝未知键与未知 schema_version；RunManifest 读取改为严格，新字段只在非 None 时写出。新记录 ModelIdentity、TargetHarness、HarnessPackage、CollectionRequest；新增 `generation` 绑定。
  - 目标：`worker.actor`、`worker.harness` 为可选目标，只在 K 列出时要求；完整性按 K 的 `core.targets` 校验，旧 core 与旧工作集照常绑定。`research.diagnose` 同时接受旧的 requirement_list 与新的 failure_summary.v1 → failure_note.v1；R 候选沿用父策略的契约。
  - 能力包（`agent/harness_package.py`）：固定目录形状与上限；运行判定与检查判定分开（tests 只影响检查）；K 不允许代码时任何 .py 都是结构错误；memory 未启用、清单外的技能文件、空 patch、与父包相同的 patch 均被拒绝；宿主把 memory 与技能索引放进系统提示，`read_skill` 作为宿主读取并记录。
  - 判分明细：`run_tasks` 写入 `grading.jsonl`，结果存储按冻结划分读取，默认只给 train。
  - 诊断（`research/diagnosis.py`）：只摘要 train 失败，含全部计分断言及其 passed，不含被排除的；逐题笔记的身份是渲染后的完整请求加模型配置；无效笔记不缓存；每轮读取数有上限，按全活动的诊断位置轮换；汇总调用只读笔记、覆盖、允许的种类、当前包，以及经验的固定投影（原因只保留枚举）。
  - 草稿检查（`research/drafts.py`）：冻结前检查形状、种类和字面量（邮箱/标识符/记录名，来自 train 断言 params 与 train 初始状态，按整词匹配，检查解码后的 manifest，回传只报类别数量）；初稿之后有界修订；每份草稿都存下来；空修订计为失败；重启后按记录的修订序号续跑，不再调用 R 重新提案；worker 不可用时暂停活动，草稿保持打开。
  - 驱动：v0.6 下每轮只测 train，改版后只重跑上次失败的任务，无安装则复用，重启时从记录重建；`collect` 记为 needs_collection 并指向 CollectionRequest，批次在重启后不重置，只有新执行的尝试算进展，没有后续轮次时不跑补采；预算耗尽时干净停止。
  - bench：`[harness_evolution]` 在 bootstrap 时把限值冻结进 K，把配置的模型固定为 actor（观测时间记为事件），装空包，用写笔记的 R0；release 固定的 actor 不被 `[llm]` 覆盖，前沿臂显式覆盖；基线对首版活动同时跑 train；K 允许代码或当前包含代码而没有 worker 时，smoke/freeze/各阶段在付费前拒绝；既有 v2 工作区加这个表会被明确拒绝。
- 证据：`.venv/bin/python -m pytest -q` → 611 passed, 1 deselected（原 554 条全部保留）。新增 `tests/test_harness_package.py`、`tests/test_diagnosis.py`、`tests/test_harness_loop.py`，覆盖 CT6-01/14、PR6-01/13/14、RL6-11/17–22/24–27、EX6-12、ER6-15、BR6-16/17、IT6-19/22（文本部分）/27/28。离线闭环用 fixture 世界与 scripted R：train 失败 → 笔记 → 汇总提文本包 → 首稿失败、修订通过 → gate 接受 → 安装 → 新会话挂载并读取技能，dev 通知任务由失败变通过。
- 独立审查：三个角度（规格、正确性、泄漏）共报 37 条，34 条经复核成立，已全部修正并补测试。其中最重要的几条：文本 K 下多余的 middleware 文件会挂起活动；字面量检查把普通词当答案；汇总调用读到的原始经验行含 dev 任务 id 与评分器报错；最终评测的前沿臂实际跑的是固定 actor；v2 release 的 prompt_hash 被改变。
- 证据边界：全部为离线 fixture，scripted R 只证明接线，不是真实收益；没有付费运行。（阶段 B～E 见上一条。）
- commit：未提交。

## 2026-09-30 对照 Harness-Zero 的 h* 产出流程补齐首版规格

- 做了什么：逐环节对照 Harness-Zero 的学生 harness 演化 skill（`.agents/skills/student-harness-evolve/SKILL.md`，@ aa63a77）与论文，把差距写成首版规格。诊断输入：失败的 train/fit 尝试给出失败摘要与逐条断言明细，串行做“逐题笔记（research.diagnose）→ 一次汇总（research.propose）”，产出至多一个 patch 或 next_action；dev 只给汇总，final 不进入。判分明细分存 `bench/<domain>/grading.jsonl`，由 M3 写入、M7 按冻结 split 读取。冻结前草稿检查与有界修订（初稿之后最多 3 次，纯文本草稿只查结构/路径/manifest）。组件优先级为 memory/skills → on_finish → before/after_tool → 包工具；纯文本包不需要 Docker。每轮 measure 在执行身份匹配时复用上轮 gate 的 dev 结果（只进 R 上下文），只为诊断重跑失败的 train 任务，dev gate 仍完整执行。交付分段改为 A 诊断输入与文本包、B 代码组件与隔离。首版 actor 定为 GLM-5.3；第二版换 Qwen3.5-9B 学生时 h* 在学生上重新演化。版本机制改为在既有记录上加默认 None 的字段（`Record.payload()` 跳过 None，`from_json` 拒绝未知键），不做 v3 reader 分派；RunManifest 与 RoundResult 的例外写明。
- 依据（读源码与论文得到）：Harness-Zero 每轮只评上一轮失败的 train 任务，不做回归；最多 10 个子 agent 逐题写笔记，主 agent 汇总、冒烟后才评测；组件在 agent 进程内 importlib 加载，任务环境在 Harbor 容器里；三个学生库共 12 个中间件、3 个 skill、3 个工具（工具只在 USPTO）。结果 base@h / base+h* / 蒸馏@h：SpreadsheetBench 31.0/39.0/44.0，AppWorld 26.8/48.2/58.9，USPTO 12.0/38.0/30.0。本项目现有研究上下文只给逐题 success/partial，不含断言和轨迹，且截到 12,000 字符（`research/context.py:50`、`:116-146`）。
- 一致性修正：ROADMAP 的测试数改为 554 passed, 1 deselected；那次 GLM 运行改为“14 条断言中 8 条计分、全部通过，另 6 条初始即成立、不计分”。IT6-18/20 标为第二版。新增用例 CT6-14、PR6-13、RL6-17～20、EX6-12、ER6-15、BR6-15/16、IT6-27/28；IT6-27/28 列入首版必做。
- 文档检查：本地链接/锚点、代码围栏与表格列数通过；159 个用例 ID 无重复；`git diff --check` 通过。用例是待实现规格，不是通过结果。
- 真实运行：GLM-5.3 的 train+dev 基线启动后，在第一个任务中途按“先写好文档再执行”的决定中止：结算 4 次模型调用（不到 $0.02），没有完成的尝试，不算基线；中断那次调用的预留按设计以最坏值留在预算账上。为这次运行做的判分明细存储代码已撤回，改为上面的待实现规格；本机 Docker daemon 未运行。
- 同日复核七项判断（用户确认并补充，随后逐条推敲、再经独立审查）：① 纯文本草稿也可修订，max_draft_repairs 指初稿之后最多 3 次；② 组件种类由 K 在 freeze 时固定（freeze/smoke 还核对当前包种类在 K 内、需要时确认 worker），运行中 worker 不可用按基础设施故障处理：重试后 infrastructure_failed，并以 repair_runtime 暂停活动，修复后以原草稿续跑。不中途收窄为文本菜单，也不结束 worker 段，否则同段前后与 M6 两支的条件或轮数会不一致；③ 上游评分器只排除“初始成立且结束时仍成立”或作者声明不计分的断言，被破坏的初始成立断言按失败计分，照常给 R（`automationbench/rubric/__init__.py` 的 free-assertion 分支）；④ 笔记上限改为每轮读取数（缓存加新生成），久未纳入的失败优先，覆盖按诊断工作集如实记录；⑤ tests/*.py 是代码：检查判定含 test bundle，运行判定不含，任务执行、安装与导出不因只有 tests 而要求 worker；⑥ 失败子集重跑只用于诊断，gate 每次重跑自身对照臂（`bench.py:571-591`），复用的 dev 结果只进 R 上下文并标明是被选中的抽样；每轮成本据此更正为约 $10–40、5–6 小时；⑦ 笔记身份为渲染后的完整请求摘要加模型配置；`research.diagnose` 改为逐题契约 failure_summary.v1 → failure_note.v1，旧 requirement_list.v1 路径保留，R0 形成新身份。另加：包文本字面量检查（防止把 train 断言中的具体值写进包）、草稿检查只在 fixture 或仅 train 构造的世界中运行、M6 第一轮笔记作为 campaign 级共同输入。
- 未决（待真实被拒候选出现后再讨论，不写入设计规则）：候选因 dev 回归被拒时，R 只能看到 dev 汇总；此前通过的 train 任务不重跑，所以没有 train 侧回归证据。同一议题还包括：已安装版本在未重跑 train 任务上的回归同样看不到，诊断工作集只会缩小；只重跑失败题时，偶然失败的任务回到均值，会被误记为修复。拒绝的具体原因与表现、补跑 train 能否提供有效信号，以及 gate 只给汇总这一机制本身是否需要调整，都尚不清楚。
- commit：未提交。

## 2026-09-30 首版收敛：受限能力包代码演化，训练完善后置

- 按本轮讨论调整交付边界：首版从任务失败自动生成/修改工具封装、中间件、技能和经验，验证后交付同 actor 的 h* 应用；第二版完善教师适配、蒸馏与新增训练。首版保留 R0/R1 比较、安装和下一真实改进调用回用，不把 RSI 核心交回人工。
- 编辑集中在交付范围和对应契约：M0 HarnessPackage/worker.harness、M1 固定 API/Docker worker/broker、M4 包内 patch、M5 隔离检查/预算/恢复、M3 回归/发布/同字节导出、M6 相同资源代码搜索与 M7 无训练依赖的 preset/报告。评分、权限、预算、发布核心、宿主 API/依赖不在自动修改范围内。
- 保留原训练/教学详细设计，仅标记第二版；保留旧 CLI、代码、历史证据与用例原义。同步设计入口、路线图、审查/演示与首/二版验收归属；新增 IT6-22～26 和相应模块边界用例。
- 文档检查：22 份 Markdown 的本地链接/锚点、围栏/表格与配置示例通过；134 个当前用例 ID 无重复，git diff --check 通过。用例是待实现规格，不是通过结果。
- 仅编辑相关文档，未改代码、未提交、未执行训练、模型付费调用或 benchmark；新包演化与真实递归收益仍待实现/验证。

## 2026-09-28 设计审查修订：新 harness、自动补采与教学源验证

- 根据定位对齐审查修复三处规格缺口，没有改变最小完整 RSI 的产品目标。只改文档，未改代码、未提交，未运行训练或 benchmark。
- 更换目标 harness：M3 准备保留 W/R/K 的受限新基线 B；M0 明确 parent 与 execution_base_ref，采集/训练绑定 B，系统 gate/CAS 仍对原生产 parent。补齐运行许可、无环引用、兼容失败与恢复规则，不允许仅替换配置就沿用旧基线。
- 自动补采：CollectionRequest 和 RoundResult 保留 needs_collection/next_action/action_ref，M4 的同一驱动在额度内采集、重建 context、继续提案；无进展/缺能力才停止当前 worker 段。M7 不吞掉后续获准 research slot；M6 两支共用该规则，重启不重置计数或重花钱。
- 教学源与发布分开：Experiment 冻结 purpose，source_validation 的 train 验证通过以 validated 完成并可立即适配，不必跑生产 dev gate、没有安装权；直接发布另建 deployment 实验。复用原执行器、评分与预算，不增加第二调度器。
- 同步总设计、路线图、研究协议、技术原则、模块接口/流程、审查和演示，新增跨模块 IT6-18～21 及相应边界用例。当前技术来源与领先模型默认仅引用公布数字的决定保持不变。
- 文档检查：22 份 Markdown 的本地链接/锚点、表格、代码围栏及配置示例通过；118 个当前 v0.6 用例 ID 无重复，撤销的 IT6-14 不计入。`git diff --check` 通过；新增规格仍待实现，未运行代码测试。

## 2026-09-28 定位修正：围绕最小完整 RSI 核心交付

- 修正上一条范围决定：用户要求减少外围功能，而非把自动发现交给人或取消 RSI 验收。框架输入是任务环境/目标/权限/初始资源，输出是可运行 app 和可继续演化的工作区；不要求用户逐轮准备优化工作流与配方。
- 主线改为自动发现 → 源方法真实验证 → 自动教学适配 → 学生在环 → 训练固化 → 无教师评测/发布 → 改进方法更新并实际回用。明确源 harness 可演化、目标 harness 在一次蒸馏内固定，首个模型/算法/后端不是框架定义。
- 实现继续复用 M0～M7，仅新增 M8。研究员保留为已有 provider 上的有界逻辑职责；首版复用 R0/R1 campaign 验证方法及下一调用的真实绑定，不新增研究平台。actor/controller 分离能力保留，应用可按效果关闭 controller。
- 更新总设计、技术选择、路线图、全部模块交接、演示/审查与中英文 README；撤销草案 IT6-14，增加零人工 source 启动、源方法实际执行、自动适配、递归伪阳性和 cycle 恢复用例。保留领先模型默认只引用公布数字的决定。
- 证据边界：只有文档变更；没有代码实现、付费调用、训练或新增 benchmark 结果。真实 C1/C4 仍待验证，不以 fixture 代替；本次未提交。
- 文档验证：22 份 Markdown 的本地链接/锚点、代码围栏、表格及 TOML/JSON 示例检查通过；95 个当前 v0.6 用例 ID 无重复，撤销的 IT6-14 不计入。`git diff --check` 通过；没有运行代码测试。

## 2026-09-28 范围收紧：研究员退出首轮交付（已被上条定位修正取代）

- 根据用户反馈：先完成一件可验证的事，暂停研究员新增开发。当前只做人工工作流/配方驱动的采集、actor 训练、无教师评测、发布；controller 之后单独决定，自动研究与递归比较不排期。
- 改了什么：整体流程图和路线图只保留 A～C；M7 人工入口直接复用 admission/executor，不依赖 `run_worker_round` 或研究 provider。M4/M6 新设计与用例标为暂缓，已有代码/CLI/完整 release 保持兼容，R ref 原样继承；C4 为 not_run。
- 验收边界：新增 IT6-14，要求缺少研究配置也能完成蒸馏全链，研究员构造/调用失败哨兵不被触发；研究、controller、Harbor 的后续用例与当前必做集合分开。
- 文档检查：22 份文档的链接/锚点、表格、代码围栏和配置示例检查通过；77 个保留/新增用例 ID 无重复；`git diff --check` 通过。
- 证据边界：只改文档，没有实现代码、训练或 benchmark 新结果；未提交。

## 2026-09-28 v0.6 设计：完整工作流蒸馏、独立决策与 RSI 回用

- 做了什么：修订整体设计、技术选型、路线图、研究协议、演示、审查和模块详细设计；保留 M0～M7，新增 M8 工作流蒸馏；新增 v0.5 实现快照，保留原测试和缺口记录。中英文 README 修正过时状态。
- 关键决策：先冻结目标 harness，以教师执行前 PASS/REPLACE 采集学生真实轨迹，按环境成功筛选后做 actor SFT，再无教师评测与发布；独立 controller 在同 actor 上验证增益；研究策略更新继续复用现有 campaign。Tinker 为首个生成训练后端，Harbor/DeepAgents 按需要可选；不建设另一套平台或应用市场。
- 开发交接：明确 schema 兼容、两类监督来源、模型实际绑定、token mask、模块所有权、错误/恢复/预算、部署白名单、实施顺序及新验收用例。保留最近“领先模型只引用公布数字”的决定，修正新 actor 路径下的 C3 对照映射。第三方只参考设计，不复制源码。
- 文档检查：22 份 Markdown 的本地链接/锚点、表格列数、代码围栏通过；TOML/JSON 示例可解析；76 个新增验收用例 ID 无重复；`git diff --check` 通过。未运行代码测试。
- 证据边界：本次仅文档与来源核对，没有代码实现、付费调用、训练或 benchmark 新结果；新用例均为待实现规格。已有真实单任务运行和历史工程验证不扩大为改进收益。
- commit：本次尚未提交。

## 2026-09-28 第一次真实模型运行：GLM-5.3 跑一个 Marketing dev 任务

- 做了什么：领先模型改为引用公布数字，不在本地运行（DESIGN §3.2、ROADMAP M2、模块 07）。来源两个：官方榜单（私有集）与上游 README 的公开集结果（含 GLM 5.2 max 26.17%，六域合计）。本地用智谱 GLM-5.3（OpenAI 兼容接口，thinking 为默认开启），经我们的 W0（含 finish check、业务停止视图、计量）跑 dev 任务 `marketing.product_adoption`（14 个断言，长度居中）：`ouroloop bench --config examples/automationbench/marketing.local.toml --phase baseline --task marketing.product_adoption`。
- 证据：通过（task_completed_correctly = 1，partial 1.0），15 轮、29 次工具调用、229 秒。prompt 共 225,196 tokens，其中缓存命中 197,568（88%）；输出 11,662，其中推理 6,441。
- token 从哪来：第一轮 prompt 只有 1,210 tokens（系统提示、工具定义与任务），之后每次请求都带上全部历史，平均每轮增加约 1.85k，到第 15 轮是 27,132。增长主要来自 `api_search` 返回的端点目录（top_k 10～15，每次约 2.5k～4k tokens）和 base64 编码的邮件正文；另有两轮花在未连接的 Salesforce、HubSpot（401），两轮花在回读已发邮件核对。
- 证据边界：一个任务、一次运行，不代表通过率。这次的 `cost_usd` 为空（运行时没有 `[pricing]`），费用由逐轮用量离线换算。
- commit：见本条记录的 Git 历史。

## 2026-09-28 付费运行前的补齐：计量、计费、续跑与计划冻结

- 做了什么：`MeteredProvider` 接到所有阶段，agent 与研究员的每次模型调用先预留、按实测结算，额度用尽的尝试记为 `budget_exhausted`（不补跑，比较记 inconclusive）；一个活动只有一个预算根，各阶段、campaign 各支与外层评测都是其子额度。每次尝试按 `[pricing]` 计算 `cost_usd`，先用 `normalize_usage` 统一供应商的用量词汇，缓存读写单独计价，缺价则 unknown。`evaluate` 与 `improve` 中断后从记录续跑：尝试 id 由计划、臂、任务、重复与重试决定，已记录的尝试原样保留，构建已完成的不重训，安装重放自身 operation 最多提交一次。补跑在写入结果库时就带上 `supersedes`。新增 `ouroloop freeze`，从 `[freeze.task_plan]`、`[freeze.schedule]`、`[freeze.budget]`、`[freeze.final]` 冻结对象并打印 `[rsi]` 引用。
- 更正：此前把 BR-05 记为已覆盖，实际 `evaluate` 恢复时会把全部尝试重跑一遍，只是不重复写状态文件；已按验收实现并在 07 文档里写明。
- 补齐过程中发现并修正的四处：其一，`settle` 把预留时没有上界的维度当作上界 0，第一次调用的 token 用量就会被判越界并停止一切付费操作——计量一接上，第一条集成测试就停在这里；其二，campaign 原先另开一个预算根并拿到同样的额度，等于把活动预算翻倍；其三，OpenAI 兼容接口报 `prompt_tokens`/`completion_tokens`，按 `input_tokens` 读会把费用算成 0，而 0 正是未知费用绝不能变成的数；其四，补跑的 `supersedes` 原本在结果写入之后才在内存里补上，只追加的结果库和据此生成的报告永远看不到它。
- 证据：`python -m pytest -q` → 553 passed, 1 deselected（新增 18 项在 `tests/test_paid_run_safety.py`）。模拟进程在最终评测第一臂中途死亡后再运行：8 个计划尝试各出现一次，崩溃前记录的全部保留；模拟在改进轮评测中途死亡后再运行：同一实验从 evaluating 续跑并安装，恰好一次安装事件。
- 证据边界：仍没有真实模型运行；计量与计费由夹具供应商的固定用量验证。
- commit：见本条记录的 Git 历史。

## 2026-09-27 删除贪吃蛇场景包与多代演示循环

- 做了什么：删除 `examples/snake/`（游戏、标签器、规则、场景、两份配置）、`tests/test_snake.py`（14 项）与 `ouroloop/generations.py`（场景包的多代循环）；`ouroloop demo` 只保留合成数据上的离线一轮，不再接场景包参数。`tests/test_counting.py` 去掉依赖 `generations._judged` 的那一项——同一处计数由它上面基于 `replay.summarize` 的用例覆盖；`tests/test_tde_compat.py` 原先从 `examples/snake/demo.toml` 读配方，改为内联一份同形状的小配方，并删掉只检查那两份示例配置的用例。
- 为什么：v0.5 的主线是 AutomationBench 业务闭环，贪吃蛇只作 smoke；场景包删除后 `generations.py` 没有可运行的住户，留着就是死代码。保留的是业务循环用得到的东西：`ouroloop/demo.py` 的合成离线一轮（四个测试文件在用）、`evolve/monitor.py`、`trainers/signature.py`、旧 decision profile 的全部路径。
- 证据：`python -m pytest -q` → 535 passed, 1 deselected（此前 550，减去 15 项贪吃蛇与世代计数用例）；套件耗时从约 90 秒降到约 55 秒。删除本身没有产生新的运行证据。
- 因此改了文档：README（中英）、DEMO、ROADMAP、DESIGN、RESEARCH_AGENT、TRAINING_HOSTS、modules/README、modules/05、modules/07 里指向贪吃蛇或 `generations.py` 的说法改为业务循环入口；本文件头部说明贪吃蛇数字已无法复算。历史条目原样保留。
- commit：见本条记录的 Git 历史。

## 2026-09-27 M0～M7 实现：完整改进闭环与研究策略回用

- 做了什么：按 v0.5 模块详细设计实现全部八个模块。新增 `evolve/contracts.py`（内容寻址身份、分区血缘、对象库与事件日志）、`policies.py`（七个注册目标、内置配置、装配、会话 pin、候选构建）、`research/inspection.py`、`research/context.py`、`evolve/feedback.py`、`evolve/task_gate.py`、`evolve/execution.py`、`research/experiment.py`、`research/campaign.py`、`report.py`；改造 `runtime.py`（`propose`：route `policy`，分别记录提议、执行与执行者）、`stop.py`（策略接线与 override 事件）、`agent/loop.py`（业务观察、工具事件、Observation/DecisionTrace）、`points/__init__.py`（`stop.business.v1` 视图）、`release.py`（完整 release、permit、preflight+CAS 安装、恢复、撤销）、`benchmark.py`（v2 字段不改旧 hash）、`evolve/build.py`、`evolve/pipeline.py`（`train_artifact` 无晋升副作用）、`research/loop.py`（`run_worker_round`）、`bench.py`（七个阶段、启动检查、`Improvement` 四个 handler、`research_slot`）、`cli.py`。
- 证据：全仓 `python -m pytest -q` → 547 passed, 1 deselected（此前 399 项既有测试全部保留通过）。新增 147 项按验收 ID 组织：CT-01～06、PR-01～07、DT-01～08、ER-01～10、RL-01～08、EX-01～08、RC-01～10、BR-01～08、IT-01～07。夹具为 `tests/fake_upstream` 新增的 `rsi` 域（更新后通知的要求写在请求末尾、合法 no-op、重复发送即回归）与脚本 worker/研究员/trainer；脚本 worker 依据请求、系统提示与工具结果响应，不看候选名字。IT-01：W0 漏通知（partial 0.5）→ R0 提出 finish check → 配对 dev 评测 accepted → 安装 → 新 session 的提示里确有该指令，该 dev 任务通过且其他任务不回归，`train_jobs` 为 0。IT-02：脚本 trainer 产出的决策器在“通知仍缺”时提出 no，StopGuard 实际打回，随后任务完成。IT-03：R0 提出 R1，两支隔离 campaign 在同起点同额度下比较各自产出的 W*，外层任务（两支内层都没碰过的家族）候选支 1.0 对照支 0.0，安装 R1，下一轮的研究请求 prompt hash 确为 R1。
- 实现中被测试推翻的四处：其一，只看训练池的一轮提不出缺口，于是每轮增加“当前版本在 dev 上的测量”，缺口必须来自将被评判的任务；其二，输入覆盖检查把从未出现的可选字段（本次没有截断，`truncation` 恒空）当成缺失而阻断训练提案，改为由 schema 声明 `required`；其三，verification 额度原先只挡别人不挡自己，改为双向限制，装不下验证的候选就不启动；其四，准入的反馈构造器默认空集合，会让所有训练提案因错误理由作废，改为取注册表。另修：经验记录的 `trained` 原本按“有 artifact”判断，prompt 候选会被写成训练过。
- 证据边界：**没有任何真实模型的业务成绩。** 夹具的数字由夹具定义；IT-03 的研究改动由脚本提出并标 `scripted`，不是自动发现；C1～C4 全部未运行。仍待交付：分支比较 builder、经济模式 campaign、进程级隔离、自动补跑与 calibration 接线，以及冻结计划下的付费实验。
- commit：2cc8f28, 2a255c0, 78eee0d, d10abb4, 4e1744a, 1f5015f, 550f60b, bf34d29

## 2026-09-27 v0.5 架构审查与模块开发交接

- 做了什么：对照 Raven 固定提交与当前源码审查 RSI 完整性，新增 RSI_REVIEW 和 modules 下的公共契约、七个实现模块及集成验收。明确代码所有权、依赖顺序、接口、状态、错误、预算、恢复和用例。
- 发现的缺口：业务 stop 输入截断、历史数据的 session 分组与重渲染、prepare_model 的旧门副作用、完整 W/R 授权安装、嵌套预算和研究 campaign 的隔离/回用都需要具体实现。本次补齐设计，未修改这些代码。
- 因此改了：首版只执行声明式策略；新 task/research profile 用完整 release 与单一 active；开发晋升与最终收益证明分开。家族签名测量保留，撤回“低重叠即可证明语义独立”的推断。
- 证据边界：源码与文档审查，没有新增模型、训练或 benchmark 运行；工程用例是待验收规格。完整框架实现与真实 C1～C4 证据仍待交付。
- commit：见本条记录的 Git 历史。

## 2026-09-27 完整 RSI 设计：任务策略与研究策略共同演化（v0.5）

- 做了什么：系统边界定义为任务策略 W、研究策略 R、经验 E 与实验内固定核心 K。任务和研究候选复用同一策略描述、评测、版本与安装机制；prompt、规则、workflow 与小模型都是载体。核心研究方向仍是从执行反馈中训练可复用决策策略，不扩建领域专家池。
- 设计依据：审查了 [Raven 固定提交的 Curator 设计](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/docs/design.md)和反馈迭代源码，吸收行为要求、实际装配检查、机制活动、失败/回归继承及验证后安装；参考 [DGM](https://sakana.ai/dgm/) 明确更新后的研究能力可以参与后续自改进，不把递归限定为训练研究员权重。外部能力比较仍以领先模型为参照。
- 因此改了：DESIGN、RESEARCH_AGENT、ROADMAP、DEMO 及中英文 README 升至 v0.5。M5 先接固定 R 的任务改进；新增正式 M7，以相同起点、经验和预算的隔离 campaign 比较 R/R'，通过后回用于下一轮研究。C1～C3 继续报告任务层效果；新增 C4 报告研究质量或成本效果。工程回用链与收益证据分开验收。
- 实现状态：保留 M1 的真实上游契约检查、固定 split，以及 M3 完成度探针和脚本干预的全部已有记录；修正设计中仍称这些未实现的状态。断言数量支持完成判断的研究假设，不证明真实模型失败原因；路线图将独立同可靠率的数学示意与实测分开，并修正示意数值。
- 证据边界：本次仅更新设计文档，未修改运行代码、调用模型、训练或复跑 benchmark，未产生新成绩。新策略描述、实际装配快照、集成研究循环和 M7 均尚待实现；不将完整设计写成已实现 RSI 能力。以下历史条目保留原时点的说法，当前目标与状态以 v0.5 设计和路线图为准。

## 2026-09-27 一个任务是被漏掉一项要求输掉的

- 做了什么：在真实上游上核验 M1，按核验里量到的东西选定干预点，并实现 M3 的信号路径 1——`CompletionProbe`：每个 stop 决策处留一份世界快照，episode 结束后用官方 rubric 给每份快照打分，把"当时活干完了吗"写成 `env` 标签。评分只在运行结束后发生，官方评分器不做在线 checker。只在 train split 上挂；session 的 `held_out` 改成 `split != "train"`（dev 是裁判数据）。
- 证据（真实 checkout：`zapier/AutomationBench` @ `4a8e106`、package 1.0.6、Python 3.12.14、`api` 工具集，装了 `verifiers` 和 `openai-agents`，所以每项比较都真做了）：25 个 dev 任务全部通过契约检查——官方 rubric 入口是 `['partial_credit', 'task_completed_correctly']`、权重 `[1.0, 0.0]`，服务订阅门与上游 `compute_allowed_services` 逐任务一致，工具 schema 的参数名与必填项和上游 `agents.function_schema` 一致，快照独立性在真实 pydantic world 上逐任务成立，与断言无关的写入不移动分数，0 处 leak。split 已冻结：train 52 / dev 25 / test 23。
- 决定干预点的测量：dev 任务需要中位数 **12** 个计分断言（均值 14.1，最多 39）全部通过，没有任务靠什么都不做能通过（中位数另有 7 条断言初始就成立、被排除出分母）。`task_completed_correctly` 是合取，所以每项 90% 可靠只有约 31% 的任务通过率，95% 是 53%，98% 是 76%。**任务是被漏掉一项要求输掉的**，于是"活是不是真干完了"这个判断——当时状态可见、训练任务上可核验、`policy.ACTING` 本来就只放它执行——是有依据的第一个干预点，不是因为"二分类适合小模型"。
- 离线验收（不花 API）：同一个脚本模型同一个任务，点不执行时失败在 partial 0.5 且这次漏掉被记成 stop 的 `no`；一个有把握的"没干完"把 agent 送回去，任务通过 1.0；答案低于阈值就走默认、什么都不变。快照代价在最大的真实 world 上是每份 1 ms、0.1 MB。
- 因此改了：`ToolSurface.mutates` 由工具面回答"这次调用可能改了世界吗"——原先循环拿 write/edit/bash 这三个工作目录的工具名去问，业务工具面永远答不上来。这不是洁癖：`Budgeted` 包住 episode 却没透传它，改动计数一直是 0，探针于是把每个 stop 决策都按第一次停下时的状态打标签，一次真实采集的全部训练样本都会是错标签。现在探针构造时就拒绝答不出改动的工具面。家族判定也修了：只按工具与断言类型算签名时有 172 对任务重叠超过 0.8，读了两对之后发现那只是"读表格、发邮件提建议"同一个形状；把断言的期望值也算进签名后，100 个任务里任意两个的重叠都不超过 0.42——一任务一家族从此是有证据的结论。
- 尚未做：没有真实模型运行，因此没有任何成绩；信号路径 2（有限分支比较）未实现；`prompt_best` 对照未做，而 DESIGN §1 要求小模型必须先比它更好。
- commit：1e1b56f、（本条）

## 2026-09-27 M1：先有运行契约，再有分数

- 做了什么：`benchmark.py`（冻结的任务分组、运行 manifest、`TaskResult`、预算）、`adapters/automationbench.py`（延迟导入上游、`api` 工具集、官方评分、快照、契约检查与输入审计）、`evolve/task_eval.py`（一次尝试、有效性、预算）、`bench.py` 与 `ouroloop bench --phase smoke`、`examples/automationbench/marketing.toml`，以及 `tests/fake_upstream/`——上游的微缩替身，使 88 个新测试不需要真实上游。
- 读源码确认的上游事实（`zapier/AutomationBench` @ `4a8e106`，2026-08-04）：默认工具集是 `api`，只有三个函数（`api_search`、`api_fetch`、`base64_encode`），都是 pydantic `WorldState` 上的普通同步函数，所以评分和工具都不需要 `verifiers`；`rubric.partial_credit` 和 `task_completed_correctly` 是 state dict 上的纯函数，后者读前者缓存的值，因此调用顺序是契约的一部分。公开 Marketing 是 100 个任务，`info` 只有 `zapier_tools` / `initial_state` / `assertions` 三个键，**没有 family 或 variant 字段**——所以默认一任务一家族，只保证同一任务的尝试、分支与重复不跨 split；`near_duplicates` 报工具与断言重叠高的任务对供人工声明。`automationbench.tools` 会连带导入整个 Zapier 工具注册表，其中一个模块在模块级 `from openai import OpenAI`，所以哪怕只用 `api` 工具集也必须装 `openai`。`tools/zapier/chatgpt/completions.py` 在有 `OPENAI_API_KEY` 时会真的调 OpenAI，没有 key 时返回固定串；公开 Marketing 的 100 个任务都没有 seed 或授予 chatgpt，上游的服务订阅门也会拒，episode 仍在工具运行期间隐藏这个变量，双重保险。
- 证据：测试过程发现两个真实缺陷，都先写了会失败的测试。其一，`WorldState(**grading.initial_state)` 与评分时重建的初始世界共享嵌套结构，agent 改完 HubSpot 阶段后，"初始状态"里也变了，于是那条断言被判成"本来就成立"而被排除出分母，`partial_credit` 从 0.5 变回 0.0——上游 runner 自己 deep-copy 过初始状态，适配器没有；现在两份都是深拷贝。其二，leak 审计原先在 `json.dumps(payload)` 上做子串匹配，一条粘进 system prompt 的断言在那里是转义的（`\"type\"`），搜不到；现在遍历结构里的每个字符串和每个键。契约检查也因此发现自己的洞：`contract_check` 原来在 `task_ids` 为空时回退到全部任务，会去读 test 任务的初始状态得分，现在必须指名。
- 因此改了：`ids()` 必须指名 split（空调用报错，不返回全部也不返回空）；冻结的 split 文件被改派时报错，工作区已有结果时连 `--replace-splits` 也拒绝；未测到的费用一路保持 `null`，汇总另报"缺在几次尝试上"；infrastructure 类终止不能是有效尝试；test split 的 session 带 `held_out`，已有的建数据路径取不到；M2～M6 的 phase 名报错并指向路线图，不悄悄做别的事。
- 尚未做：没在真实 checkout 上跑过（适配器是按上游源码写的，`contract_check` 正是为此而写），没有真实模型运行，没有真实成绩，`zapier` / `limited_zapier` 工具集未接。
- commit：a10572a

## 2026-09-27 主线更新：AutomationBench 业务闭环与领先模型比较（v0.4）

**目标。** 在 AutomationBench Marketing 上跑通执行反馈、改进、可选训练、完整任务评测和版本更新。外部比较对象为实验启动时业界领先模型；同底座前后比较验证迭代收益，同一 prompt/workflow 下的默认策略与小决策模型比较验证训练的增量价值。

**设计调整。** 重写 DESIGN、ROADMAP、RESEARCH_AGENT、DEMO，并同步中英文 README 的目标与实现状态。保留训练作业、模型库、服务、版本和账本；补齐环境适配、任务家族划分、业务反馈构造、task profile、完整任务评测与研究接线。先验证失败可被干预，再训练；prompt-only 候选不强制训练；第一版不扩专家池或元研究模型。

**评价边界。** 主指标是完整任务通过率，部分完成率用于诊断；同时报告运行与累计改进成本。开发迭代与最终测试分开，公开自留测试与官方私有榜单分开。删去当前设计中逐代单调、固定次数内必须提升、无 key 即可证明真实业务收益等预设，不要求组件归因矩阵作为首个交付前提。

**证据。** 本次仅更新文档，未运行模型训练或 AutomationBench，未产生新成绩。代码核对显示已有模型训练管线主要按历史局部标签回放评价；独立研究循环尚未接入世代训练循环。新增模块与 bench 命令均标为目标接口。

**历史说明。** 以下 v0.3 及更早条目保留当时的决策与运行证据，旧主线、接口设想与验收要求已由 v0.4 设计及路线图替代，不作为当前待办。

## 2026-09-27 重切：训练线为主线，部署线冻结，研究员回到设计实验

**证据。** 离线 demo 6 代（`ouroloop demo snake --config examples/snake/offline.toml`，2026-09-26 晚重跑）：冠军 food 0.7～0.9，与规则蛇的括号值持平；第 3～5 代晋升的每个冠军都只接管了 2 局、2 局都受害、被监控撤销（下界 22.4% > 5%），合并受害率 6/6 = 100%。GPU 上 run4～run6：模型推翻规则的步只对 15%～26%。也就是说，从 9 月 26 日起加的每一层护栏（按 session 计风险、增益门、运行时监控、撤销冷却、准入）都工作正常，而蛇一局都没有多吃到。根因是签名模型看不见死路（`rules.signature` 只看四邻格与食物方位），这是特征问题，被当成了安全问题处理。

**外部。** TypeSafe 9 月 19 日发布 Jev（typed decision model，闭源权重，4000 万美元种子），48 小时内出现 pi-warden、typesafe-mcp、openjev、dspy-typesafeify 等项目；REFLEX（arXiv 2609.26532）用 Jev 做 agent 的 selective control，无门、无账本、不学习。"self-improving agent" 是 2026 年最拥挤的标签（Hermes 9.5 万星，OpenClaw 34.5 万星）。

**因此改了什么。** 四份文档重写为 v0.3：`docs/DESIGN.md`（两条线、概念表、模块职责表、五个循环、训练线 9 条不变量、六条可度量的主张 C1～C6、基准分层 T0～T3 与消融矩阵）、`docs/ROADMAP.md`（W1～W11，每项有文件级改动与命令级验收；冻结项与推迟项各带触发条件）、`docs/RESEARCH_AGENT.md`（研究员产出 `Experiment`，可改 features / arch / hparams / data / spec，单变量靠对照臂；保留"看不到测试对局、不改标签、不碰门、无凭据"四条边界）、`docs/DEMO.md`（替代 `DEMO_PLAN.md`：本地交互流程、90 秒脚本、业界对照、叙事规则）。设计上的决定：训练线用策略模式（冠军 argmax 打整局）与对局评测器（冻结种子、配对 bootstrap）；接管模式、风险预算、校准阈值、监控、撤销、发布三元组、契约、prompt 管线整体冻结；准入砍到预算与重复锁；去掉"一轮只改一个配方键""架构从父继承"。

**没有运行证据的部分。** 本条目只重写了文档，代码未动；假设 2（环境允许改进）由 W5 用人当研究员验证，不成立就先修场景或训练器。

**同日追加。** 对照业界对 RSI 的定义（改进者自己也要被改进）重新审了设计：到 W11 为止是自我改进的训练循环，递归只在"策略改变数据"这一条；补上研究决策上环（`worth_running`、`pick_arm`、`stop_line` 三个决策点，门的结论只对它们是标签）作为 W12 与主张 C7，元指标是每次接受的改进消耗的实验数与算力随代数下降。把提升机制写成四条通道（数据、表示、规格、分布）加门的单调性；决策点准入条件用 Kahneman 与 Klein 的两个条件（封闭标签集、可观察结果，再加分布稳定）。部署线改称"同一产品的下半场"，第一个实例定为 Claude Code hooks 上的 `tool_gate`。

**同日第二次追加（流程审查）。** 把设计的来历写成九条第一性原理（DESIGN §1.0：决策与推理分开、真相只来自环境和人、策略改变分布、改进者碰不到评测器、一个结论一个变量、每个数字落盘、数据是用户自己的、机器故障不是证据、概念与依赖最少），按它们核对每个循环与适配器的数据路径，记录在 DESIGN §十。改了八处：arena 种子由公开公式改为随机抽取只存记录（P4）；准入移到开记录之前（P9）；C3 的误接受表述补上 α 的对象（统计）；研究决策点由三个收成 `worth_running` 一个，排序不否决、带 ε（P9、P2）；适配器补记权限模式、`stop` 列为已知缺口、条款与隐私写进 P7（P2、P7）；`adapters/` 从部署层移到环境层，与场景包并列为两种住户（架构一致性）；策略模式下"默认错误"定义为"规则蛇在此会错"（P3）；确认研究员看冠军历史 arena 汇总不泄漏（P4）。目的与一句话重写：ouroloop 提升 agent 的决策质量，证明场是模拟环境，目的地是真实 harness。

## 2026-09-26 一次撤销只买一次训练，准入不再拒错轮次

- 做了什么：`evolve/plan.py` 的准入重写。撤销带来的"新数据"只算冠军执行过、还没有候选训过、且本轮不留作测试集的 session（`Champion.new_sessions`）。
- 证据：原来把冠军执行过的 session 的所有有标签行都算成新数据，包含本轮测试 session 和上个候选已经训过的行，于是一次撤销能一轮接一轮地提计划，直到训练预算用完。同时查出五处判反：`min_cal_sessions` 不足时 `calibrate_point` 给每个阶段的阈值都是 None，这样的数据却能准入；单个稀薄的点否决整轮，而门的 `decide_points` 并不为此通过或阻塞；预测增益取"目标 − 已达到"，站着的模型接近目标后残差低于测试集分辨率，再也没有一轮能准入；`inconclusive` 被算成假设失败，一次训练任务崩溃就挡住自己的重试；`risk = 0`（模板限制仍允许）让 `sessions_needed` 死循环。
- 因此改了：两条校准检查都只对能执行的点做；判不了的点从计划里拿掉，所有目标点都判不了才拒绝整轮；预测增益取"离目标的差"和"本轮测试集能看见的最小比例"（`_detectable`）里的大者；只有门真的量过并拒绝（`AGAINST`）才算假设失败；risk = 0 直接报错，是不训练的理由，不是搜索。
- commit：6bcf9ea、8ac06d9（合并）

## 2026-09-26 主机上的训练任务也在信号下取消

- 做了什么：`evolve/train.py` 的 `_wait` 在本机和 ssh 主机上也跑在 `_signals_raise()` 下（此前只有云实例如此）。
- 证据：停 run6 时发现的：`_wait` 对任何 `BaseException` 都会取消任务，但默认处理的 SIGTERM 直接结束进程，没有 `finally` 跑，训练任务成了孤儿，占着 GPU 没人回收，只能手工杀掉。而 systemd、`docker stop`、一个普通的 `kill` 正是无人值守的循环被停下来的常规方式。新测试在没有这个修改时会把测试进程自己打死（exit 143）。
- 因此改了：取消的保证不依赖调用方，而依赖信号转成 `SystemExit`。
- commit：486d609

## 2026-09-26 研究报告不给 agent 看校准 session

- 做了什么：`research/loop.py` 的 `build_report` 按门记录自己的 `cal_frac` 切分，只读 `train` 一侧。
- 证据：原来用默认的 `cal_frac = 0` 切，非测试行全落进 `train`，agent 连同渲染出来的文本看到了它的候选之后要在上面拟合阈值的那些行。新测试把行数钉在记录的切分上，不修就失败。
- 因此改了：Learn-then-Test 要求候选在看到校准数据之前就固定；看过校准行再挑的候选，阈值带的 Clopper-Pearson 界不再成立，门报的风险是乐观的。agent 能看的，就是这一轮能训的。
- commit：4803744

## 2026-09-26 四个角色：模型库和服务从训练里拆出来，部署与回滚

- 做了什么：`ouroloop/stores/`（dir 与 hf 两种实现，一个接口，返回不可变 ref）、`ouroloop/serving.py`（训练好的模型只经此被问）、部署探针、按血缘的回滚。
- 证据：没有运行证据，是四处设计缺陷的结果，它们都是"全部跑在一台 GPU 主机上"留下的痕迹：transport `direct` 让训练来决定模型住在哪；评测的后端来自训练器而不是服务；晋升只是翻一个指针，没有任何一次加载；冠军被撤销后 session 掉回默认，而不是掉到上一个好版本。
- 因此改了：worker 经模型库发布并取回 ref，卡片上模型只按 ref 指认（backend spec 里只有 ref，服务、校准、session 钉和撤销名单都只用它），作业目录和主机名仍记在卡片的 `job` 一栏里备查；门接受之后，服务先加载、报出的模型 id 必须和卡片一致，冠军才移动，部署失败则冠军不动（`ouroloop deploy <version>` 重试）；回滚落到最近一个被接受、已部署、未撤销的祖先（`Registry.rollback`），没有就回到默认。
- commit：f22c64c

## 2026-09-26 没有计划不训练，没有数据不成计划

- 做了什么：一轮改为 数据 → 诊断 → 假设 → 计划 → 准入 → 训练（`evolve/plan.py`），只有最后一步花 GPU；`pipeline.schedule` 删除，它的三条反射变成规则计划器的三条提案分支（`plan.plan`：还没训过、默认错误翻倍、冠军被撤销），提案之后还要过 `admit`，撤销那条另加"带来新数据"的条件（`Champion.rows >= MATERIAL * rows`）。
- 强制点：这条规则落在 `ouroloop/generations.py` 的世代循环里，不在 `pipeline.evolve_model` 里。`ouroloop evolve`（`cli.cmd_evolve`）仍然不看计划就训一个候选；世代循环里 `[demo] plant` 指定的那一代也有意跳过 `decide`——植入的坏候选按定义要走同一道门。
- 证据：run6（把执行后监控放进运行时之后）：坏模型在害了 2 个 session 时就被撤销，但日程按"撤销即重训"立刻重训，新候选看到的几乎是同一批数据（被撤销的模型只在 2 个 session 里执行过，失败几乎没留下新数据），于是以同样的方式失败，连着两次。
- 因此改了：撤销是证据，不是触发器；准入的决定性检查在训练前就能算出来——risk r 下一个阈值需要 `sessions_needed(r)` 个一条错都没有的 session（r = 0.05 时 59），而只有含默认错误的校准 session 才买得到一个，不够就说明这份数据训出来的模型永远不可能执行。`ouroloop plan` 把诊断、提议和准入结论连理由一起打出来。
- commit：b0a01d4、8ac06d9（合并；合并时把"目标只算真的部署了的模型"补进 plan.py）

## 2026-09-26 训练器变插件，通用 worker，主机自举，云 GPU

- 做了什么：`ouroloop/trainers/`（插件契约，tde 与 signature 两个插件按契约声明自己的 `RECIPE`，tde 另有 `PATH_KEYS`；两者都是可选项，不声明就用框架的 `DEFAULT_RECIPE` 和 `PATH_KEYS = {"init_from"}`）、`ouroloop/worker.py`（主机上 `python -m ouroloop.worker`，版本不符就拒绝）、`ouroloop/hosts/`（bootstrap 与检查）、`evolve/cloud.py`（RunPod / Vast.ai 每个任务一个实例，用完必销毁）、`docker/worker` 镜像。
- 证据：没有运行证据，来自取舍清单（9195962、ee328c5 两个计划提交）和一次评审：任务的父模型只有同一插件训的冠军才算，host.json 要和 venv 里的实际代码对；云侧 create 不盲目重发、等 `start.sh` 的标记、实例 TTL 与 SIGTERM/SIGHUP、实例消失立即失败。
- 因此改了：训练路径只剩一条（本地或 ssh 或云），只有 spec.json、train.jsonl、val.jsonl 过界；transport `hf` 让云实例可用。
- commit：b5105f1、485a0a7、85bddc0、e4f22c5、9195962、ee328c5

## 2026-09-26 运行时自己跑执行后监控

- 做了什么：`Runtime.session` 在每个新 session 固定发布之前调用 `evolve/monitor.watch`，从只读增量账本行维护的计数里判断，下界超预算就撤销冠军；`revoke()` 跨进程幂等。
- 证据：run5（8 代，默认回答者很天真，模型有发挥空间）：门两次晋升了在已记录状态上安全、上线后有害的模型，160 个 session 里分别害了 158 个和 78 个。监控两次都抓到了，但当时只有演示脚本每代调一次，坏模型先害完一整代；内核本身根本没有跑过监控。
- 因此改了：会改变自己后续输入的决策，在已记录状态上校准不等于在它自己造出来的状态上校准——监控必须在运行时里、按 session 触发；已经在跑的 session 从下一个决策起取默认。README 的架构图改为动图（`docs/assets/architecture.py`）。
- commit：3e3c330、9a04490

## 2026-09-26 一条结果可以写多个正确答案

- 做了什么：`outcome` 的标签可以是一组都对的标签，目标在它们上均匀；校准、监控、增益、默认错误统计一律问"答案在不在正确答案里"（`replay.right`）。
- 证据：贪吃蛇账本上，一个训练得好的模型推翻规则的 6348 步里，4920 步是同样好的一步，被算成了错。
- 因此改了：风险不再被高估，增益不再惩罚同样好的推翻，任意的平局规则不再是训练目标；把一个对的默认换成另一个对的答案，增益为零。贪吃蛇的 labeler 给出所有和最好一样好的方向，默认回答者换成 `rules.keep_going`（一直往前、要撞死才转），好让模型有发挥空间。
- commit：ab921d6

## 2026-09-26 配方 focus：训练花在默认出错的行上

- 做了什么：`pipeline.focused`，保留全部默认出错的训练行，其余按种子抽样，使错误行占到指定比例；校准和测试行不动。
- 证据：模型只能在默认出错的行上产生价值，而在长跑的循环里这些行是账本的一小部分——贪吃蛇里约 7%。
- 因此改了：训练量变小，且花在有增益的地方；研究候选可以设 focus。
- commit：7c1cbc1

## 2026-09-26 以默认为基准的价值：风险按 session 计，门比增益

- 做了什么：路由默认（规则回答者在路由前给出的答案）、按 session 的风险、按增益判定的门，三件事一次改到位。
- 证据：run4（6 代）当时风险按决策计：模型执行的步里 96%～99% 本来就是规则蛇会走的；决定输赢的是那少数几次推翻，只有 15%～26% 是对的，其中最多一半当步致命；18%～52% 的局死在模型手上，而按决策计的 1%～4% 错误率轻松过了 5% 的预算。门按全部测试行的 Brier 晋升，这些行大多是默认本来就对的。
- 因此改了：风险按 session（模型执行过的一步错，这个 session 就算受害），阈值拟合、监控、门都用这个单位；候选按对路由默认的增益（对 +1、错 −1）判；按决策计数的 `count`（`first`/`sampled`）删除。
- 旁证（同期两次对照，说明问题不在标签也不在混合两个策略）：同样的种子、只跑游戏逻辑时，labeler 自己每步都走，8×8 上吃到 32.7、0 次死亡，规则蛇 15.3；把 labeler 的步随机混进规则蛇，比例越高越好，单调。问题出在模型自己执行的那些步上。
- commit：cb8ee5c

## 2026-09-26 阈值用固定顺序检验

- 做了什么：`policy.fit_threshold` 从最有把握的截断值往下按固定顺序（Learn-then-Test）检验，遇到第一个不过就停，不再"留下最低的那个过了的截断值"。
- 证据：原来是几十个嵌套检验、每个都在 5% 的 alpha 上，靠运气挑出过的那个：一个实际错 6% 的模型对着 5% 的预算，100 个 session 时 200 次校准里有 52 次拿到阈值（300 个 session 时 29 次），`first` 和 `sampled` 都一样。改后 200 次里 9 次。离线贪吃蛇演示只变了一个阈值（第 5 代执行 3%，不是 18%）。
- commit：1653371

## 2026-09-26 抽样计数每个 session 只抽一次

- 做了什么：`sampled` 计数改为按 session id 和行序的哈希一次排好名次，每个截断值取看得到的行里名次最靠前的那条，跨截断值嵌套。
- 证据：原来每个截断值重抽一次，几千个截断值里几乎总有一个是幸运抽样：一个实际错 8% 的模型在 40 次试验里 40 次拿到 5% 的阈值（`first` 是 4 次）。改后 40 次里 1 次。
- commit：d8dfcb2

## 2026-09-26 离线贪吃蛇演示：一代 120 局，每代旁边是默认打的同一批局

- 做了什么：每代 120 局；每代的局用同样的 session id、种子和棋盘，由默认单独再打一遍放在 `<workspace>/baseline/`，括号里并排打印；每局指标进账本成 metrics 事件，每行输出成 generation 事件，测试从磁盘重算每一个数。
- 证据：60 局时校准 session 太少，第 6 代模型只在 8% 的步上执行；120 局时第 4 代起执行 24%、第 6 代 63%，汇总实测错误 1.7%（CP95 3.3%），约 70 秒。另一头：离线模型打的局和规则蛇一步不差（执行了 63% 的步，每一步都是规则蛇自己的步），第 5 代食物和步数跳高只是因为棋盘变大了，屏幕上看不出来。
- 因此改了：屏幕上的每个数都要有对照、都要能从磁盘重算；被撤销的冠军那一列标注 `(revoked)`；汇总行覆盖账本里的所有代，不只本次调用的。
- commit：4140aec、287b8f9、0442a18、456880c、d8dfcb2

## 2026-09-26 贪吃蛇 README 改成实际跑出来的样子

- 做了什么：删掉"棋盘变大会退化、撤销、恢复"的承诺，写明离线版本只展示机制。
- 证据：离线时什么也不会退化（小模型只读头部周围的格子）；一次 `offline.toml` 运行里它执行了 14,014 步，每一步都是规则蛇自己的步，所以离线的局等于基线；GPU 主机上这一段没有跑过。
- commit：8bff773、5585675、d2ec16b、493c54d、a21193f

## 2026-09-26 门记录：没用过的记录在新 session 会改变它的测试集时让位

- 做了什么：`gate.choose` 允许用新记录替换一个尚未使用的旧记录。
- 证据：GPU 主机上两次出现同一情况：第一份记录把最新的 session 拿去当测试集，剩下能训的太少，这一轮在推理之前就停了（525 条训练样本），而记录被一直沿用，后面每一轮都饿着。
- 因此改了：没用过的记录，其测试 session 没有被任何推理碰过，可以换成用更新的 session 的新记录，旧的那些回到训练里。
- commit：c28d4fe

## 2026-09-26 TDE 配置里的浮点数

- 做了什么：训练器配置写成 YAML 也读得回浮点数的形式。
- 证据：配置是 JSON，用 `yaml.safe_load` 读；PyYAML 把没有小数点的尾数（json 的 `2e-05`）当字符串，TDE 拿字符串乘浮点数，GPU 主机上每一次训练都失败。
- commit：d4e7ded

## 2026-09-26 训练任务：一个 worker，本地或 ssh，模型按 ref

- 做了什么：三条训练路径（Local/Remote TdeTrainer、tde_remote 后端）合成一条：写 spec、在新任务目录里起 `worker.py`；worker 记下 TDE 的 git HEAD，按 ref 解析父模型，训到截止时间，写 `result.json`。
- 证据：没有运行证据，来自 9195962、ee328c5 两个计划提交里的取舍。为无人值守保留的：两侧都有截止时间、状态读取失败算仍在运行、进程没了立即算丢失、等待被打断就取消任务、进程里第一次训练先清理本工作区的孤儿、ssh 调用有时限、decide 分块并在多于一块时重试。
- commit：e4f22c5

## 2026-09-26 贪吃蛇场景与多代演示

- 做了什么：`examples/snake`（游戏、棋盘文本、move 决策点、环境 BFS 标签、局即 session 的运行器、指标）、`providers/manual.py`（把 API provider 的接口和数据协议落成文件）、`evolve/monitor.py`、本地 TDE 路径、`ouroloop demo <scenario> --generations N`。
- 证据：随后的几次运行（run4-run6）就是这套装置量出来的；本条自身没有量化证据。
- 附带修的：满盘时吃到最后一个食物即胜（否则 labeler 绕着尾巴走）；manual provider 的 usage 允许任意对象（OpenAI 兼容服务端会嵌套 `prompt_tokens_details`）；`prepare_job` 打乱 val 切分（TDE 只用 `calibration.jsonl` 的前 `eval_limit` 行选 checkpoint，按账本顺序就是最旧的那些 session）。
- commit：e27a132、37cdca9、90d936c、e99d3fe

## 2026-09-26 M8：一个冻结的门，每轮一份记录

- 做了什么：`gate freeze` 由人写下门模板（risk 0.05、min_sessions 30、cal_frac、min_cal_sessions、检查项、测试集规则、session 预算）；每轮派生 `gates/<round>.json`（模板哈希、包哈希、账本位置、测试与 held-out session id）；`gate.run` 是模型、spec、配方、prompt 四种候选唯一的入口，一轮一个结论；注册表用比较交换晋升，session 固定在 (model, spec, harness) 三元组上。
- 证据：评审在真实账本上发现两处会白白耗掉测试 session 的问题——测试 session 可能没有标签（M6 账本的前 125 个 session 里有 2 个在探针之前就崩了，于是第一份记录的 30 个只剩 28 个可评分，门判 `insufficient_evidence`，记录和 session 一起作废）；新 session 不够时冻结的记录会被一直沿用。随后的绕门评审找出 15 个洞，每个都先写了会失败的测试（`tests/test_gate_bypass.py`）：删掉 `used` 标记让测试 session 重新算新；手改 `champion.json` 指向被拒的候选；手改 `calibration.json` 的阈值；配方候选把 `init_from` 或 `replay` 指向本轮校准数据；`test.jsonl` 被拷给训练器；`gate freeze --replace --set risk=0.5` 放宽之后所有的门；研究 agent 读到 held-out 结果；session id 里的路径穿越写坏账本。
- 因此改了：门记录、它的使用和结论都是账本里的事件，门从不读 `gates/` 里的副本（但 `gates/r_*.used.json` 是运行时判断"这个版本是不是门接受过的"的唯一入口，`release.Accepted`）；模板在 `gate.LIMITS` 的四个字段（risk、`min_sessions`、`n_boot`、`suite.min_items`）和 `cal_frac` 上只能收紧不能放宽；只有门记录接受过的版本能晋升，且只能压在它评过的那个父版本上；阈值必须与接受结论里列出的校准完全一致才执行；回滚同时撤回当初晋升它的那份门记录的接受。
- commit：2a862fa、117f56c、ba71c4c、88e86b9、905e19f、679a987、c5ee835、853308f、e150cd2、6c8ec1c、091fe8b、931f33e、4e80ae2、2ccd8f2、0217777、19730ad

## 2026-09-25 M7：决策点只按执行规则执行，出错走默认

- 做了什么：一个 `act` 开关取代 shadow/gate/auto 三种模式；每个阶段声明默认回答者（固定标签、`rule:` 或 `llm:`）；校准按（模型 id、spec 哈希、决策点、阶段）存；只有带门记录哈希的阈值才让点执行。
- 证据：对抗性安全评审找到六个洞，每个都先写了会失败的测试：单个 NaN/负数/无穷大概率被重新归一成有把握的答案；自定义后端返回未归一的分数（如 2.0）在温度为 1 时跳过归一，越过任何阈值；runtime 建不起来时 Claude Code hook 吞掉异常、跳过黑名单，`rm -rf /` 进了 Claude Code 自己的许可流程；NaN 阈值、温度 ≤ 0、空模型 id 都会误放行；spec 哈希没覆盖 view 的截断方式；带引号的 `act = "false"` 被当成打开。
- 因此改了：任何不是"在问题标签上的分布"的答案都记为 error 路由并取默认；只有有名字的模型能有阈值；`act` 必须是布尔；`max_view_chars` 与 `keep` 进 spec 哈希。
- commit：b8afca9、706b068、6fb7b48、cb3959e、9249f17、96d270e、05dda06、656e575、c444f31

## 2026-09-25 M6：阶段、session 事件、StopGuard 和探针

- 做了什么：决策点声明阶段（阶段 → 默认动作），每行记下阶段和默认；`Runtime.session` 写 session 事件；`ouroloop/stop.py` 的 StopGuard 取代 `Agent._pushback`；`agent/probe.py` 在每个 stop 决策处复制工作目录，会话结束后写入隐藏测试、跑检查、给当时的决策打标签（运行中的任何东西都看不到结果）。
- 证据：真实验收（在固定于 M6 提交的独立工作树里采集）：前 50 个 session 里 yes 和 no 两种探针标签都出现；按起始顺序前 85 个 session 共 326 个 stop 决策（325 个有探针标签），per_turn 阶段 153 个 no、129 个 yes，on_finish 阶段 41 个 yes、2 个 no——收益集中在 per_turn。（这组数可从 `examples/workspace-m6/ledger/` 复算；M6 当时的进度记录写的"前 86 个 session、327 个决策"复算不出来，前 86 个是 330 个决策、154 no / 131 yes / 42 yes / 2 no。）任务集从 10 个扩到 29 个（8 个 held-out），每个都"照原样失败、带解法通过"。
- 因此改了：评审又修三处——per_turn 判"完成"要先跑 checker 才结束（模型不推翻规则）；最后一轮之后不再问 per_turn（记下的默认动作根本不会发生）；探针复制不了或检查不了的状态保持无标签，而不是中断整次运行或让任务集崩溃。
- commit：74686e5、4c183b0、ea0c384、a3f31a9

## 2026-09-24 评测装置本身的四个缺陷

- 做了什么：门只读 development 切分；候选顺序在呈现时按决策种子打乱；候选描述长度对齐；账本在每个一致率旁边打印最好的常数答案。
- 证据：在真实决策集上量的——参考答案在 53 个选择里只有 3 次是最后一项（写在最后的通常是"放弃"），"永远不选最后一项"在打乱前得 94.3%；29 个选择里有 22 个参考答案的描述最长，中位数多 28 个字符，"选最长的"（既不读状态也不读问题）得 73.7%，对同一后端的 84.2% 做精确 McNemar 检验分不开（p = 0.344）。长度对齐后启发式掉到 44.7%（随机是 36.4%），后端 78.9%，配对检验 p = 0.0072，而后端的答案只在 38 项里变了 2 项——它从来不是在读长度，是这套集子显示不出来。公开的 38 项与保留的 36 项：同一后端 84.2% 对 80.6%，差别是披露而不是难度。
- 因此改了：能否决候选的切分属于选择程序，不是对这个程序产物的独立估计——held-out 照跑照记，但不参与接受或拒绝。
- commit：f2942b8、c0b6f7d、94e799b

## 2026-09-23 ouroloop 0.1.0

- 做了什么：第一版骨架：决策层（类型化决策点、带校准阈值的策略、探索与倾向、只追加的 JSONL 账本）、决策后端（mock、Jev API、TDE、LLM 兜底、HTTP 服务）、最小 agent（读写编辑与 bash，五个内置决策点）、evolve 流水线（建数据集、数据检查、训练器、校准、配对 bootstrap 的晋升门、带回滚的注册表）、研究 agent 接口、带隐藏检查的任务集、Claude Code hook 数据源。
- 证据：无，这是起点。
- commit：43d3811

---

## 还没建的

日志记的是改了什么、什么时候改的。当前未完成项以 [研究员状态表](RESEARCH_AGENT.md)与[实施路线图](ROADMAP.md)为准。

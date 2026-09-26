# ouroloop 路线图

> 已经建好的是什么，下一步是什么，接手的人不用再问。
> 2026-09-26。里程碑清单（M6～M13）、每个里程碑的验收和已定的推迟项都在本文；权威设计见 `docs/DESIGN.md`，研究 agent 见 `docs/RESEARCH_AGENT.md`，设计的来历和证据见 `docs/JOURNAL.md`，训练与主机的细节见 `docs/TRAINING_HOSTS.md`。
> 代码是权威。本文与代码不一致时，代码对。

现在的位置：M6、M7、M8 已完成；M9 的训练路径和四个角色已完成，它在编码 stop 上的真实验收按 2026-09-26 的顺序调整延后；贪吃蛇场景包和 `ouroloop demo` 在开发机上离线跑通多代，用当前内核在 GPU 主机上的验收还没跑。M10～M13 要么未开始，要么只有一部分。`ouroloop/` 下 9,576 行 Python（`wc -l $(find ouroloop -name '*.py')`）；284 个离线用例，外加 1 个 `pytest -m live` 默认不选的（要 API key 的 `tests/test_live_jev.py`）。本机少装 pyyaml 时 `tests/test_train_config.py` 会 skip，`pytest -q` 于是报 283 passed、1 skipped、1 deselected。

主线与分层见 `docs/DESIGN.md` §一、§二。下面每一项只说清它在那条线上的位置。

## 1. 可以并行的工作项

除了 M13 要等 GPU 验收和 M11，其余都没有互相阻塞：它们改的是不同的文件，测试也分开。

M12 与 M13 的依赖按 2026-09-26 的顺序调整，从原计划的「M12 依赖 M9、M11」「M13 依赖 M6～M12」修订过。依据是 M12 的五件事都不需要研究 agent，所以 M12 只依赖 M9、可与 M11 并行；M13 真正被挡住的是 demo 的 GPU 验收和 M11。

| 工作项 | 状态 | 依赖（必须先落地的） | 可与谁并行 | 一句话验收 |
|---|---|---|---|---|
| demo 在 GPU 主机上的最终验收（§3.1） | 未开始 | 无，代码已齐 | 全部 | `ouroloop demo snake --generations 8` 在 GPU 主机上无人干预跑完，埋的坏候选被门拒，每代的行落盘 |
| M11 研究 agent（§3.2） | 进行中 | M8、M9 训练路径（都已落地） | M12、M10、基准、README | 一轮里 agent 给出一份计划或弃权，准入用账本可复算的两条硬检查判定 |
| M12 harness/prompt 管线（§3.3） | 未开始 | M9（已落地） | M11、M10、基准、README | 每个候选标签都有对应动作；同时改两个分量的候选被拒；`evolve/harness.py` 不再 import `ouroloop.agent` |
| M13 无人值守多代运行（§3.4） | 未开始 | demo 的 GPU 验收、M11 | 基准、README | 连续跑至少 3 代无人干预，回放快照上的曲线逐代上升，每代实际风险有上界 |
| M10 契约优先接入（§3.5） | 未开始 | M9 的 serve（已落地） | 全部 | 一个非编码示例只经 HTTP 契约跑完 200 个 session，迟到的结果按 id 关联 |
| 基准（§3.6） | 未开始 | 无 | 全部 | 选定的基准上有一条可复现的命令和一份落盘结果 |
| 公开 README / 仓库（§3.7） | 进行中 | 无 | 全部 | 一个外人按 README 从 clone 到看见一条多代曲线，中途不问人 |

并行的唯一硬约束在轮的层面，不在开发的层面：一份计划只改一个变量，而且同一轮不能既改 harness 又训模型——harness 一动，路由默认就变了，增益的基线和此前拟合的所有阈值一起作废（§3.3）。`ouroloop/release.py` 只存发布三元组，这条约束**尚未在代码里强制**；今天代码层面只有 `research/loop.py:run_round` 的"一份记录一个结论"，强制点列在 §3.3 的未落地里。M11 和 M12 可以同时写，但真实运行里要分开轮次。

M6～M13 全部通过验收，才算这一版完成。

**session 预算**（写进门模板；这条线上真正稀缺的不是算力，是没过过门的新 session）

| 用途 | 新 session |
|---|---|
| M9 编码 stop 的采集 | 约 450 个（非 held-out 约 330 个）；校准 session 到 120 个之后才打开 act |
| 每次过门 | 非 held-out 至少 30 个（模板的 `min_sessions = 30`）；同期的 held-out session 并列报告，不参与判定 |
| M9 执行对照 | act 组、shadow 组各 ≥ 100 个 |
| M11 过门 | 约 40 个 |
| M12 prompt 过门 | 30 个任务 × repeats × 2（冠军和挑战者都重跑） |
| M13 每代 | 约 40 个新 session（模板的 `budget.sessions_per_generation`），加上 act 组的 session |

测试集是上一份门记录冻结之后开始、从未过门的非 held-out session（`gate.choose`）。编码任务集这一侧合计超过 1,000 次任务运行，一个人做要几周，所以采集最先开始（§2.1）。

## 2. 已完成

### 2.1 M6 标签和数据

- **目的**：stop 的每次提问都有一个与实际动作无关的环境标签，yes 和 no 两种都有。
- **与主线的关系**：没有结果就没有标签，没有标签就没有训练和校准。它是整条线上等得最久的一步。
- **状态**：已完成（`74686e5` 核心：阶段、session 事件、`ouroloop/stop.py` 的 StopGuard、`ouroloop/agent/probe.py` 的探针、stop view 升到 v2；`4c183b0` 评审修掉三个问题；`ea0c384` 任务集从 10 个扩到 29 个，其中 8 个 held-out；`c444f31` 记进度）。真实采集在固定于 M6 提交的独立工作树里跑过，账本在 `examples/workspace-m6/ledger/`：按起始顺序前 85 个 session 共 326 个 stop 决策（325 个有探针标签），per_turn 阶段 153 个 no、129 个 yes，on_finish 阶段 41 个 yes、2 个 no——收益集中在 per_turn。（M6 当时的进度记录写的"前 86 个 session、327 个决策"复算不出来：前 86 个是 330 个决策、154 no / 131 yes / 42 yes / 2 no。）
- **依赖**：无。
- **验收标准**：`tests/test_probe.py`（5 个用例；全文的用例数一律是 `pytest -q tests/<file>` 收集到的数，不是 `def test` 的个数，参数化的文件上两者不同）——每个 stop 决策都有探针标签（包括被发回的）、运行期间工作目录里没有隐藏测试、view 里没有 checker 的结论、session 事件带 task 和 held_out。
- **边界**：探针不是 checker，StopGuard、view 和 agent 都看不到它的结果；M6 里两个阶段都只记录，不调用模型。

### 2.2 M7 能执行，而且安全

- **目的**：决策点第一次能按执行规则执行动作，出错和没把握时一律走声明的默认动作。
- **与主线的关系**：执行规则就是"模型只在有把握、且不同于路由默认的地方动手"这条不变量的落点。
- **状态**：已完成（`b8afca9` 核心：`act` 开关、按（模型、spec、决策点、阶段）存的阈值、按 session 计数的 Clopper-Pearson、出错走默认、声明的默认回答者、规则否决、只用于评测的来源；`6fb7b48`、`cb3959e`、`9249f17`、`96d270e`、`05dda06`、`656e575` 是对抗性安全评审找到并修掉的六个洞，每个都先写了会失败的测试；`706b068` 是同期 M7 评审的另一项：记录里写的是声明的默认回答者，改过的 spec 按它自己模型的温度重放）。
- **依赖**：无。
- **验收标准**：`tests/test_act.py`（30 个用例）——为模型 A 拟合的阈值不让 B 执行；view 版本一变旧阈值失效；来自 5 个 session 的 100 行不比 100 个独立 session 宽松；59 个全对的 session 有阈值、58 个没有；LLM 解析失败、后端抛错、空概率、渲染抛错各留一行 `error` 并走默认动作。
- **边界**：v0.2 里只有 stop（以及场景包自己定义的点）可以打开 `act`；`ouroloop calibrate` 只报告、不写入任何文件。

### 2.3 M8 冻结的门

- **目的**：所有晋升走同一份在这一轮开始前冻结的门；每次用没被用过的 session；晋升持久化并以比较交换写入。
- **与主线的关系**：门闭环的那一半。一份记录判一个候选，只判一次，判的是它在新 session 上相对路由默认的增益。
- **状态**：已完成（`2a862fa` 核心：门模板和每轮的门记录、新 session 规则、比较交换的持久晋升、发布三元组和 session 固定、撤销名单；`ba71c4c`、`88e86b9`、`905e19f`、`679a987`、`c5ee835`、`853308f`、`e150cd2`、`6c8ec1c`、`091fe8b` 这九个 `M8 gate-bypass review:` 是绕过门的对抗评审找到并修掉的 15 个洞，对应 `tests/test_gate_bypass.py` 的 15 个用例；`117f56c` 是另一组 M8 评审修复（测试 session 必须有标签，每次晋升都比较交换）；`c28d4fe` 让一份没用过、但已被更新的 session 超过的记录让位）。
- **依赖**：M7。
- **验收标准**：`tests/test_gate_record.py`（31 个用例）和 `tests/test_gate_bypass.py`（15 个用例）——冻结后在 TOML 里放宽 `[research] min_items` 或 `[checks]` 直接启动报错；同一批测试 session 上第二次过门只能 `validated`；同父的第二个候选晋升因比较交换失败；训练器在测试推理之前抛 `TrainError` 时记录和 session 都没用掉，之后失败则都算用掉。
- **边界**：门只看非 held-out session；held-out 的指标在卡上并列报告，不参与判定。基础设施失败一律 `inconclusive`（`ouroloop/evolve/gate.py` 的 `INFRA`），永远不算对候选不利的证据。

### 2.4 内核的四次改写

这四条不是计划里原有的里程碑，是 GPU 主机上的运行逼出来的改动，它们改的是所有决策点通用的定义。每条的数字与运行编号都在 `docs/JOURNAL.md` 2026-09-26 的对应条目里，这里只写因果。

- **按 session 计风险、按增益判门**（`cb8ee5c`，配方的 focus 见 `7c1cbc1`，固定顺序检验见 `1653371`）。目的：让风险预算约束真正会出事的东西。与主线的关系：按决策计的风险会漏掉真正决定输赢的那少数几次推翻——模型执行的步绝大多数本来就是规则蛇会走的（见 `docs/JOURNAL.md`「以默认为基准的价值：风险按 session 计，门比增益」）。状态：已完成。验收：`tests/test_counting.py`（20 个用例）。边界：删掉了按决策计数的 `count`（`first`/`sampled`），不再提供。
- **一个结果可以有多个正确答案**（`ab921d6`）。目的：同样好的答案不再算错，否则风险被高估、增益惩罚了正确的推翻（见 `docs/JOURNAL.md`「一条结果可以写多个正确答案」）。状态：已完成（`outcome` 接受一个标签、None 或一组标签；`replay.right` 是所有判对错的地方的唯一入口）。边界：账本照原样存，不做迁移。
- **监控进运行时**（`3e3c330`，架构动图 `9a04490`）。目的：会改变自己后续输入的决策，在已记录状态上校准不等于在它自己造出来的状态上校准，监控必须按 session 触发而不是每代调一次（见 `docs/JOURNAL.md`「运行时自己跑执行后监控」）。状态：已完成（`ouroloop/runtime.py` 在固定每个新 session 的发布之前调 `evolve/monitor.watch`，`revoke()` 跨进程幂等，对应 `docs/DESIGN.md` 不变量 8）。它和 M9 的 commit 串一起落地，所以 §2.5 里也能看到这个 commit。边界：已经在跑的 session 从下一个决策起取默认。
- **没有计划不训练**（`b0a01d4`、`6bcf9ea`、合并 `8ac06d9`）。目的：一轮 = 数据 → 诊断 → 假设 → 计划 → 准入 → 训练，只有最后一步花 GPU。与主线的关系：撤销即重训，新候选看到几乎同一批数据，以同样的方式失败（见 `docs/JOURNAL.md`「没有计划不训练，没有数据不成计划」）。状态：已完成（`ouroloop/evolve/plan.py` 的 `diagnose` / `plan` / `admit` / `decide`，`ouroloop plan` 打印同一份读数，`pipeline.schedule` 删除）。**这条目前只在 `ouroloop/generations.py:run` 里强制**：`ouroloop evolve`（`cli.cmd_evolve` → `pipeline.evolve_model`）仍绕过诊断与准入跑完一整轮，`ouroloop plan` 是只读的；世代循环里 `plant` 指定的那一代也有意跳过 `decide`。"给 `ouroloop evolve` 也加上准入"是这条不变量还缺的强制点，列在 §3.2 的未落地里。验收：`tests/test_gate_record.py` 里的计划与准入用例。边界：撤销永远不是训练的理由，只是证据；一次撤销最多换来一次训练（`Champion.new_sessions` / `rows`）。

### 2.5 M9 的训练路径和四个角色

- **目的**：训练、存放、服务从控制面里分出来，一份代码只差配置就能落在任意台机器上；无人值守时作业不会挂死或跑丢。
- **与主线的关系**：执行层只干活、不做判断；控制面只判断、不直接花算力。一台 GPU 主机是一种布局，不是设计。
- **状态**：已完成（`e4f22c5` 一个 worker，本地或经 ssh，模型按引用；`b5105f1` 训练器插件化、通用 worker、`ouroloop host init`；`485a0a7` 每个训练作业一台 RunPod 或 Vast.ai 实例；`85bddc0` 插件拥有自己的配方键，评审修掉主机、镜像和云实例上的缺陷；`f22c64c` 四个角色：`[store]`（`dir` / `hf`）和 `[serving]` 从训练里拆出来，部署探针与回滚；`486d609` 作业期间收到信号就取消它，和云上一致；`3e3c330` 运行时在每个 session 开局时跑执行后监控）。
- **依赖**：M7、M8。
- **验收标准**：`tests/test_jobs.py`（23）、`tests/test_roles.py`（17）、`tests/test_hosts.py`、`tests/test_cloud.py`、`tests/test_tde_compat.py`——用假 TDE 走完 launch → poll → 卡上记下模型引用 → serving 按引用加载 → 新 session 用新模型；配置之外的引用被拒；超时、断线、进程丢失、取消各有用例；`hf` 用假的 HF 走一遍。
- **边界**：消融掉的清单见 `docs/DESIGN.md` §六「计算与托管」，不要再提。

### 2.6 贪吃蛇场景包和 `ouroloop demo`

- **目的**：一条完整的 RSI 循环，本地跑得完，每个数字落盘可查。
- **与主线的关系**：它是框架的 demo，不是业务。内核只通过 `[demo] scenario` 这一个接口认识它（`ouroloop/generations.py`），行业词汇一个都不进内核。
- **状态**：已完成离线的一半（`e27a132` 多代循环、manual provider、执行后监控、本地 TDE 路径；`4140aec` 每代 120 局的笔记本版；`287b8f9` 每代并列同一批只由默认走的对局；`0442a18` 被撤销的冠军在行上标出；`456880c` 合并行覆盖工作区里的每一代；`37cdca9`、`5585675`、`d2ec16b`、`8bff773`、`493c54d` 标签规则、棋盘文本与文档；场景包在 `examples/snake/`）。`ouroloop demo snake --config examples/snake/offline.toml` 用签名小模型几分钟跑完，不需要任何 key。
- **依赖**：M7、M8、M9 的训练路径。
- **验收标准**：`tests/test_snake.py`（13 个用例）；离线跑一次，屏幕上每一列（`acted`、`harmed sessions <=CP95 vs budget`、`gain`、`food`/`steps`/`died` 及其括号里的默认基线、`round`）都能从 `ledger/`、`gates/`、`models/`、`harness/`、`baseline/` 重新算出来。
- **边界**：笔记本版演示的是机制，不是会玩的蛇——签名模型只看朝向、蛇头四周四格和食物方向，不往前看；撤销与恢复在离线版里只有单元测试覆盖。

## 3. 开放项

### 3.1 demo 在 GPU 主机上的最终验收

- **目的**：用当前内核（按 session 计风险、增益门、多标签、计划准入、运行时里的监控）在 GPU 主机上跑一次完整的贪吃蛇多代运行，拿到可以示人的一条曲线。
- **与主线的关系**：run4、run5、run6 量出的问题都已经改进代码，但改完之后没有在 GPU 上跑过。没有这次运行，"一代比一代好"只是设计。
- **状态**：未开始。代码已齐：`examples/snake/demo.toml`（8 代、每代 80 局、第 2 代埋一个用打乱标签训练的坏候选、第 5 代棋盘从 8×8 换成 10×10）、`ouroloop host init local` 的主机准备路径、`[store] dir` + 进程内 serving 的一箱布局。
- **依赖**：无，代码已落地。这一项不挡住 M10、M11、M12 的开发。
- **验收标准**：在 GPU 主机上 `~/.ouroloop/venv/bin/python -m ouroloop demo snake --generations 8` 无人干预跑完；埋在第 2 代的坏候选被同一份门拒掉，数字在屏幕上；每代的行和每局的指标分别是账本里的 `generation` 和 `metrics` 事件；每次晋升有一份冻结的门记录和一批新的测试 session；最后一行给出合并各代的受害局比例和 Clopper-Pearson 上界。
- **边界**：不保证第 5 代换棋盘一定带来退化（会不会退化取决于模型）；不做行业场景；不在开发机上训练或推理。

### 3.2 M11 研究 agent

- **目的**：研究 agent 是训练过程的指挥官——决定这一轮要不要训、设计并排序实验、停掉不值得跑完的方向。它只产出一份计划，什么都不执行。
- **与主线的关系**：门决定一个候选能不能上线，计划决定该不该花这次算力。本领域现有的做法都没有"决定不跑"这一环，对比见 `docs/RESEARCH_AGENT.md` §9。
- **状态**：进行中。已落地：计划器接口 `plan(rt, d: Diagnosis) -> tuple[Plan | None, str]`（计划和理由；None 时是弃权的理由）和内置的规则计划器、诊断和准入（`ouroloop/evolve/plan.py`）；把三者串起来并被 `ouroloop plan` 打印的是 `decide(rt, points) -> (Plan | None, bool, reasons, Diagnosis)`；候选的提出、评测和经验记录（`ouroloop/research/loop.py`、`research/agent.py`、`research/basic.py`）；研究报告只用本轮的训练切分，不含校准 session、测试 session 和 held-out 任务（`4803744`，`build_report` 的 docstring 写明了理由）。

  未落地，一句话：**LLM 计划器还不存在；准入的两条硬检查今天仍是近似；边界没有隔离强制。** 逐件状态只在 `docs/RESEARCH_AGENT.md` §0 的表里维护，这里不复述。另外两件本文自己负责的：
  - "没有计划不训练"还缺一个强制点：`ouroloop evolve`（`cli.cmd_evolve` → `pipeline.evolve_model`）今天不调 `plan.decide`（§2.4）。
  - 撤销后的恢复今天要等翻倍规则：`plan.plan` 的撤销分支读的是冠军指针，而监控是撤销与回滚一次做完的，所以漂移之后那批最该用上的数据要等默认错误自己翻倍（`docs/RESEARCH_AGENT.md` §4）。方向是安全的一侧——不会因为一次撤销就重训——但恢复慢了。
  - 白名单快照（`evolve/guard.py`）不存在：今天的边界靠 `Workspace` 接口划定，agent 与框架同进程，越界只能靠约定；而"读不到校准行与测试行"这一条已由 `build_report` 落地（`4803744`）。
- **依赖**：M8、M9 的训练路径，都已落地。可与 M12、M10、基准、README 并行。
- **验收标准**：一轮结束时账本和经验日志里有且只有一份计划或一条弃权理由；准入的两条硬检查任何人都能从账本重算出同样的结论；agent 写不到白名单以外的任何路径（越界的那一轮记为 `violation`，不晋升也不算证据）；基础设施失败记为 `inconclusive` 并且不进负面证据。
- **边界**：它只产出计划，不定义、不打分、不重标结果，不碰门的任何参数，产出的任何数字都不能成为准入、停止或晋升的输入。逐条的边界、每条挡住的那个具体失败，以及完整设计，见 `docs/RESEARCH_AGENT.md` §8。

### 3.3 M12 harness / prompt 管线

- **目的**：harness 的改动和模型的改动走同一个门，每次只改发布三元组的一个分量。
- **与主线的关系**：四条执行层管线里的第四条，和前三条只共享账本和门。
- **状态**：未开始。M12 的五件事在代码里一件都没有。可复用的前置已经有了，但它们不属于 M12：prompt 候选的任务集评测和过门（`ouroloop/evolve/harness.py` 的 `SuiteRunner` 和 `prompt_evaluator`，冠军和挑战者在同一批里都重新跑，那批运行的 session 就是门的测试 session；缓存只用于筛选）来自 v0.1 的 prompt evolution（M5），发布三元组本身（`ouroloop/release.py`）来自 M8。未落地：
  - 候选标签到 harness 动作的映射，以及启动时"每个标签都有对应动作，否则拒绝启动"的检查。
  - 新的候选类型 `capability`（spec 加一个标签，同一改动里带上实现它的工具或 skill，作为一个单元过门）。代码里目前没有 `capability` 这个 kind。
  - 一个候选只能改一个分量的约束：`release.py` 现在不做这个检查。
  - 解耦（两半）：`evolve/harness.py` 仍直接 import `ouroloop.agent.prompt` 和 `ouroloop.agent.suite`，设计要求改成依赖一个 `Suite` 协议（`tasks()` 和 `run(task, release) -> Run`）；`agent/loop.py` 里写死的决策点逻辑要拆成钩子——它现在直接 `from ..stop import StopCall, StopGuard`，在构造函数里建 `StopGuard`，并硬编码 `self._stop("on_finish", ...)` 和 `self._stop("per_turn", ...)`，黑名单、tool_gate、retry、compact、route 同样写死在这个 loop 里。
  - 晋升后的报告只取新发布的 session。
  - 沙箱：任务集在容器或单独系统用户下运行，看不到隐藏测试、也看不到 ouroloop 的工作区和账本。
- **依赖**：M9（已落地）。可与 M11 并行开发；真实运行里与训模型分属不同轮次。
- **验收标准**：某个标签没有对应动作时发布拒绝启动；`capability` 那一对作为一个单元过门；同时改模型和 prompt 的候选被门拒绝；`evolve/harness.py` 不再 import `ouroloop.agent`，一个玩具级的非编码任务集能走完 prompt 评测；一个读取 `evals/tasks` 的 skill 候选读不到隐藏测试。
- **边界**：v0.2 里只有 stop 执行动作，而 stop 的标签固定是 yes 和 no，所以 `capability` 只做离线验收，真实运行推迟到第一个 choice 决策点开始执行动作（§5）。

### 3.4 M13 无人值守多代运行

- **目的**：连续多代无人干预地运行，拿到证据：自动执行比例一代比一代高，而实际风险 ≤ 5%。
- **与主线的关系**：这是递归本身的证据——晋升的模型到达默认从未到过的状态，那些状态的结果是下一轮的数据。
- **状态**：未开始，但多代循环这一层已经在 `ouroloop/generations.py` 里（`ouroloop demo <scenario> --generations N`；计划里写作 `ouroloop loop`，实际落成的是 `demo`）。未落地：
  - 看门狗：GPU 小时、LLM 花费、连续 `inconclusive` 的代数各设上限，超限停下并通知人。现在只有门模板里的 `budget.max_trainings`（默认 10）和 `budget.sessions_per_generation`（默认 40）。
  - 离线回放曲线：冻结一份账本快照，用每一代的模型重放，得到可复现的曲线；对照组（冻结的通用 checkpoint、始终走默认、Jev）都用离线回放，在同一批带探针标签的行上算。
  - 只发布合成账本，真实账本不发布（`redact()` 只遮密钥，挡不住路径、用户名和主机名）。
- **依赖**：demo 的 GPU 验收（§3.1）和 M11。这是唯一一条真被挡住的线。
- **验收标准**：无人干预连续跑至少 3 代；在同一份冻结的回放快照上，第 k+1 代的自动执行比例高于第 k 代；每一代实际风险的点估计 ≤ 5%，并报告合并后的 Clopper-Pearson 上界；每次晋升都有冻结的门记录和新的测试 session；把标签随机打乱跑 20 次，误晋升不超过 1 次。
- **边界**：不要求执行比例本身上升——和路由默认一样的步既没有价值也没有风险，曲线看的是增益和受害的局。

### 3.5 M10 契约优先接入

- **目的**：已有的 agent 不用 ouroloop 的 harness，只通过契约就能接入模型轨道；用一个非编码的示例证明。
- **与主线的关系**：产品入口是契约。ouroloop 自带的 harness 只是契约的参考实现。
- **状态**：未开始。`ouroloop/contract.py` 不存在；`serve.py` 现在的端点是 `/decide`、`/load`、`/drop`、`/release`、`/ask`、`/outcome`、`/health`，没有 `/v1/*`；`examples/leadgen/` 不存在。按 2026-09-26 的顺序调整，行业示例延后。要建的四件：
  - `ouroloop/contract.py`：决策 spec 文件 `points.toml`（每个决策点的类型、问题、封闭的标签集合、各阶段的默认动作，以及哪些结果来源能当训练目标——比如 CRM 回调记 `env`、人工复核记 `human`），它的哈希就是决策 spec 的版本；接口 `Client`（进程内或 HTTP）、`Client.session(meta)`、`Session.ask(point, state, candidates=None, phase=None, veto=None)`、`Client.outcome(decision, label, source)`，label 是一个对的答案、一组同样对的答案或 None。候选必须是已声明标签的子集，阶段必须已声明，否则报错。
  - SDK 在客户端生成 decision id：连不上 serve 或超时就执行默认动作，把这一行（路线 `error`）写进本地缓冲，恢复后补发到 `/v1/ask?record_only=1`，serve 按 id 去重。
  - `serve.py`：用 `/v1/session`、`/v1/ask`、`/v1/outcome` 取代现在的 `/ask`、`/outcome`（现在的 `/ask` 只能问服务端已定义的点，也不固定 session）；`/v1/outcome` 拒绝 `points.toml` 里没声明的来源（今天任何客户端都能自称 `env` 或 `human`）；`/decide` 只给 ouroloop 自己的 runtime 和门用，令牌单独发，契约客户端的令牌只能调 `/v1/*`——否则它能绕过 `ask` 拿到概率自己执行。
  - `examples/leadgen/`：只用标准库、按种子生成的线索开发模拟器，两个决策点 `stop`（这条线索处理完了吗；执行动作，用 StopGuard）和 `next_step`（choice，只记录）。stop 的标签必须与这次决策执行的动作无关：模拟器按提问时刻的隐藏意向打标签，几个模拟日后按 decision id 送达。用事后的业务结果打标签会让执行过的决策自己确认自己（判了 yes 就不再跟进，线索变冷，"确认流失"又记成 yes）。真实业务里，执行了 yes 的决策只从两处取训练标签：按 ε 走了默认动作的那部分，以及人的确认。
- **依赖**：M9 的 serve（已落地）。不挡任何其他工作项。
- **验收标准**：模拟器经 HTTP 连本机 serve 跑 200 个 session，迟到的结果按 decision id 正确关联；请求未声明的候选或阶段、上报未声明的结果来源都返回 400；契约客户端的令牌调 `/decide` 返回 401；跑到一半杀掉 serve，恢复后每次 ask 在账本里正好一行；`evolve` 能从这个账本建数据并过门。真实（需 GPU 主机）：模拟器对 GPU 主机上的 serve 跑完一代，stop 模型用线索的结果训练、过门、在新 session 上执行动作；这项不与 §3.1、§3.4 的真实运行同时进行。README 补上契约接入的上手说明。
- **边界**：契约接入只进化模型轨道（决策模型、温度和阈值、spec 的措辞和标准）；增删标签要配上对应动作，属于 harness 轨道。

### 3.6 基准

- **目的**：给出一个外人能复现的数字。
- **与主线的关系**：主张是"改进决策，不是改进推理"，这需要在决策上、而不是在推理基准上被量到。
- **状态**：未开始。仓库里现有的是两份可回放的决策集——`evals/replay_tde/`（带 `results_jev-1.13.0.json`）和 `evals/replay_campaign/`（38 项，36 项按披露理由保留），用 `ouroloop replay <file> --backend <backend>` 跑。跑哪个公开基准、比什么，没有定论。
- **依赖**：无。
- **验收标准**：选定的基准上有一条可复现的命令、一份落盘结果，以及和什么比、在什么条件下比的说明。
- **边界**：决策集不是循环的基准，它只量单次作答；循环的证据在 §3.4 的曲线里。选哪个基准是 **候选**，见 §4。

### 3.7 公开 README 和仓库

- **目的**：一个外人从 clone 到看见一条多代曲线，中途不问人。
- **与主线的关系**：开源、本地、可审计是这个框架的定位的一部分。
- **状态**：进行中。已有 `README.md` 和 `README.zh-CN.md`（`f22c64c` 起同步更新）、`LICENSE`、`.github/workflows/tests.yml`、`docs/assets/architecture.gif` 及其中文版、`examples/snake/README.md`。README 里"Not built yet"一节现在写的是循环里的研究 agent——这一行要随 §3.2 更新。remote 是 `https://github.com/e13ven-arch/ouroloop.git`。
- **依赖**：无。
- **验收标准**：按 README 的 Quick start 在一台干净机器上跑通 `pytest -q` 和 `ouroloop demo snake --config examples/snake/offline.toml`；README 里每个"已经能做"的说法都能在代码里指到文件；主机名、仓库名和密钥只出现在被 git 忽略的 `*.local.toml` 里。
- **边界**：不发布真实账本，只发布合成账本；方案和示例里只写"GPU 主机"。

## 4. 候选（还不是决定）

下面这些没有人拍板，不要当成已定的工作项去做。

- **候选**：基准选哪个（§3.6）。
- **候选**：行业场景的先后。`docs/DEMO_PLAN.md` §4 记的顺序是补货与降价、然后客服，数据已调研（FreshRetailNet-50K、τ²-bench），但整节的优先级已经降低，决策点也还没定。
- **候选**：`ouroloop import`（把历史记录变成决策和结果）和回放，作为客服场景的前置能力。
- **已定的推迟项**不是候选，它们各自写着触发条件，见 §5。触发条件没到就不做。

## 5. 已定的推迟项

每一项都写着触发条件，条件没到就不做；它们不是候选，不必再讨论要不要做。

| 项 | 触发条件 |
|---|---|
| 训练 tool_gate 并执行动作 | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且 tool_gate 攒到约 2,000 条人的批准或拒绝，来自至少 200 个 session |
| retry | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且 agent 自己原样重跑的结果攒到至少 500 条 → 开一个 retry 探索阶段 |
| compact | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且 harness 用的模型接口支持编辑历史 → 开一个 compact 探索阶段 |
| route | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且两个档位成为常规配置 → 开一个 route 探索阶段 |
| 把记忆操作作为决策点；拆分 POLICY 和 STATE | 某个 harness（我们自己的或用户的）需要持久记忆，而发布三元组表达不了记忆的状态 |
| stop 之外的验证级联：步骤级和论断级验证、LLM 那一级、对"完成"按 ε 复核 | stop 已经在执行动作，执行后误判为"完成"的错误集中在某一类问题上，并且步骤级或 LLM 检查能抓到这类错误 |
| 结果时间分级和标签成熟度 | 在一个真实（非模拟）部署里，过门时最新 session 中超过 20% 的决策还没有结果 |
| 离线策略修正（IPS / DR） | 第二个决策点开始执行动作；或者某个执行动作的决策点，默认动作那一侧靠 ε 攒不够结果 |
| 专精模型（每个决策点单独的 LoRA 或单独的模型） | 回归检查显示，共用一个模型的决策点互相干扰 |
| 改进者自我改进（研究 agent 改进自己的提案方式）及其版本戳 | 研究 agent 攒到至少 10 轮带门结论的记录，有了晋升率的基线 |
| DSH 适配器 | 契约 v1 稳定下来（M10 之后没有破坏性改动），并且有用户提出需求 |
| `capability` 候选的真实运行 | 第一个 choice 决策点开始执行动作 |
| RunPod pod 自动启停 | GPU 主机上排队等待的训练超过一代的时长 |

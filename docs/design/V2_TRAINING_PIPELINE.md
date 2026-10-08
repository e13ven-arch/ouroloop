# V2 训练链设计：数据管线、试验设计、训练管线

> **现状（2026-10-08，support 对照停止后）：** 现行，已定稿；§7 第 9 项待用户定。用户已停止 support 两臂对照（JOURNAL 2026-10-08）：文中“等 support 两份 final 评完、H1 算完”的时机（§1“时机”、§2.6 第 8 条、§4.6 轨道 B 的排期、§7 同步清单）改为“瘦身第一批合入之后”，support 的重放现在就能做；support 没有跑 final，§7 第 2 项的“support（final 后读过 2 次）”应为读过 0 次。support 的 K（W\* 文本）现在只有 v0.5 臂经 dev gate 装上的经验库快照（release `9f07d95032b4`，6 个 library-* skill，没有 final）：拿它作 K，还是当 support 没有 W\*、K 为空，待用户定（§2.6 第 6 条、§7 第 4 项）。§7“核价更正待同步”清单里的 DESIGN、TECHNOLOGY、02、TRAINING_HOSTS、ROADMAP 五处已于 2026-10-08 整理时同步。瘦身第一批已于 2026-10-08 在分支 `slim/1` 上完成（[PLAN](../PLAN.md) P1）：子包 `ouroloop/train/`、`ouroloop/train/jobs/` 已建，入口 `python -m ouroloop.train` 已有（`decide`、`smoke --jev`、`host`），§2.5 与 §7 列的 7 处注释已改；v2.0 的代码从这里开始。同日用户定：不写金标身份测试，旧工作区的哈希身份不必保持；§3.6 “身份由金标测试守住”一句随之改写。
>
> **状态：** 已定稿（2026-10-08：用户按 §7 默认值全部确认；§7 第 9 项是核价后新增的，待用户决定；同日第 2 项由用户改为全部域可跑 final）。**日期：** 2026-10-08。
>
> **历史：** 2026-10-07 草案（待用户审），提交于 `b58f5cc`。2026-10-08 用户答"按默认"，§7 八项全部按默认值定下（见 §7 的"决定"列）。正文只把 extra 名按 §7 第 7 项改为 `[train]`/`[train-jobs]`（原 `[decision]`/`[decision-jobs]`），并在 §7 标出本轮已同步的文档。同日复核后另补：§2.5 的代码位置与时机、§3.3 的 extra 归属、§5 v2.2 的测试编号、§7 的同步清单。同日又按 Tinker/RunPod 核价（官方价目与文档，2026-10-08 核对）更正 §0.3、§3.1、§3.3、§3.5、§3.7、§4.2、§4.6、§5 v2.1、§6 和 §7 第 6 项，各处标"（2026-10-08 按 Tinker/RunPod 核价更正）"；§7 另补核价时没能确认的事项和新的待决第 9 项（学生思考档位）。同日（support v0.5 臂 3b 之后）用户问"不能全跑吗？"，§7 第 2 项改为全部域可跑 final；据此改 §4.1 的 C5、§6 的"test 被提前用掉"一行和 §7 第 2 项，各处标"（2026-10-08 用户改为全部域可跑 final）"。§7 第 9 项补注用户"保持统一的档位"的偏好。
>
> **用户决定（2026-10-07）：** "训练与蒸馏是框架的一部分；要有数据管线、试验设计、训练管线。" 交付物是一条开发者用自己选的 teacher、actor、student 和训练后端就能端到端跑通的链。Claude 只用来验收这条链是否完整、能不能跑通。
>
> **依据。** 在 HEAD `03f68b3` 上只读阅读代码和文档：SLIMMING_INVENTORY §0.1/§0.2、modules/02、09、DESIGN、TECHNOLOGY、ROADMAP、RESULTS。另外参考四份子报告（assets、jev、data、external）和两份评审（evidence、scope）。
> - 计数只读了 train/dev 记录，没有读 test。脚本在 scratchpad 的 `v2-train/{assets,data,stats,scope}/` 下。
> - file:line 以 `03f68b3` 为准（`ouroloop/` 与 tag `archive-2026-10-08` 相同）。瘦身第一批会把决策栈搬进子包，合入后按 SLIMMING §2.3、§3 改写这些引用。（2026-10-08，瘦身第一批完成后：搬进子包或被删掉的模块，引用已改成新路径，路径以 `train/` 开头，行号按 `slim/1` 的 `455ffe2`；删掉的写明 tag。§2.5 的代码位置清单与 §7 的注释清单也按 `455ffe2` 改了。其余文件没有搬，行号仍按 `03f68b3`，在 tag `archive-2026-10-08` 上对照。）
> - 行数是估计，误差约 ±25%。

## 0 结论

### 0.1 推荐做法

1. **分两条轨道。**
   - **轨道 A：链路。** 这是交付物，包括 v2.0（离线，$0）、v2.1（接真模型并跑首个试验 X1）、v2.2（审阅采集链路）。smoke 收据齐全、X1 读完，轨道 A 就结束。
   - **第一期不设效果指标（用户，2026-10-08）：** "第一期主要是把链路搭出来，不定效果指标。" 轨道 A 只以链路收据验收（每个环节都有 ref、离线套件通过）；X1 的读数（S_F − S0、写门上限、K1/K1b/K5）只作轨道 B 开不开的参考，不是第一期的验收条件，也不因它们的结果判第一期成败。
   - **轨道 B：效果。** 包括 C5、C3 和密封 final。它有自己冻结的计划，X1 读完后由用户决定开不开。老师用谁写在计划文件里，不是设计问题。
2. **一条链，两条线。**
   - **链：** 宿主记录 → 投影 → 离线重放打标 → 决策集或 SFT 集 → 检查 → 作业 → 产物 → 阈值 → serving → 作为 W 的组件绑定 → 同一个 `task_gate` → Release → 导出 app。
   - **两条线：** actor 蒸馏（垂域学生）和决策模型（第一个决策点是 `write_gate`）。
3. **只保留两条正确性规则，不按来源设闸门。**
   - 评测、阈值、gate 用的真值只来自 `env:*` 或 `human:*`。
   - 评分信息（断言、grading、重放结果）永远不进 actor、teacher、judge 的输入，也不进 SFT 输入。依据是 Harness-Zero（HZ）的 oracle 消融：审阅者能看到答案时，训练后只有 15%，看不到时是 30%。
   - `source` 只作普通的数据血缘。
4. **第一个里程碑 v2.0 完全离线。** 在现有 finance train 记录和 `tests/fake_upstream` 上，用脚本化的 provider 加 `signature` 和 `mock_sft` 两个 trainer 走完整条链。新写或修改约 2.2–3.2k 行。
5. **首个试验 X1 放在 v2.1 末。** 在 train 的 val+cal 族上比较学生 S0 和 S_F（S_F = S0 用现有老师成功轨迹做 LoRA SFT 后的学生），同时离线读出学生上写门的上限。Claude 名义费用 ≤ $35，真实支出 ≤ $80。X1 只读方向，不作主张。

### 0.2 保留、推迟、删除

| 项 | 判定 | 理由 |
|---|---|---|
| 血缘、投影器、重放、`final_success` 和 `replay_effect` 两个打标器、FamilySplit、两个 builder、`write_gate` 挂点、通用 target、role 分派、App.bind 接 Serving | v2.0 | 零成本；链路的骨架 |
| `judge_vote` 打标器（离线，provider 来自配置） | v2.0 用脚本化 provider，v2.1 换真模型 | 没有重放的领域，链路要靠它跑通（§2.4） |
| `replay_skip`、脱敏输出回填、`tinker_lora`、renderer、学生 serving、`tde` 修漂移 | v2.1 | X1 要用 |
| 网关的 role → provider 映射、老师审阅钩子、ReviewRecord | v2.2 | 只有在环审阅用到它们 |
| judge 的运行时绑定和回退链、`decider_lora`、完整评估移植（ECE、coverage@risk） | 轨道 B | 只有作主张时才需要 |
| 运行时 `stop` 决策点、`ctx.finish` | v2.5 | 要改包 API；完成后又弄坏的只有 7 例 |
| `contact_human` | v2.5 | 本基准没有真值；真实标签要来自部署审批 |
| 原文对象库（`messages_ref`） | 不做 | 改成只按宿主持有的密钥值精确脱敏（§2.2） |
| 在 Opus 上测 C3、为写门单独定 gate 规则、每个决策点各训一个模型 | 删 | 见 §7 末 |

### 0.3 相对上一版草稿的更正（评审核过的部分）

| 项 | 上一版 | 更正 | 依据 |
|---|---|---|---|
| 学生上下文 | chars/4 估出 11k/18k/30k；`max_seq` 65536 | 用 Qwen3 tokenizer 实测（中位/p90/最大）：v4-finance train 14.5k/22.0k/34.7k，v4-support 16.9k/25.6k/32.4k，约 3.1–3.25 字符/token；`max_seq` 定为 32768。它排除 X1 训练集 S1′ 的 4.8%（sales 17 条里排掉 3 条），不是原写的 0.7%（2026-10-08 按 Tinker/RunPod 核价更正） | `scope/tok2.py` |
| 决策视图上限 | 12,000 字符 | Qwen decider（`max_ctx` 4096）放约 10.5k 字符；TDE（≤900 token）放 ≤2,900 字符；或者按目标 tokenizer 截断 | `stats/tok.py`；jev `ops/decider_lora_train.py:24` |
| 配置形状 | `[trainer.actor]`、`[student]` | `build_trainer` 直接从 `cfg["trainer"]` 取 `type`（train/config.py:82-96），表一嵌套就报 KeyError。改为 `[train.actor]`、`[train.decision]`；学生就是 `[llm]` | §3.7 |
| actor 的 `module:Class` | 声称"配置即可" | 插件契约的 serving 端只返回决策 Backend（train/trainers/__init__.py:1-40）。§3.3 补上 actor 产物契约 | — |
| MLX 训 TDE | 列为 v2.1 的一条路径 | 不支持。`tde` 插件只走 torch，设备是 cuda 或 cpu（train/trainers/tde.py:9-10、:188-195） | — |
| 阈值 | 用 `fit_threshold` 控制"误拦" | `fit_threshold` 控制的是"拦下的会话里错拦的比例"（train/policy.py:96-124），不是"通过的尝试被误拦的比例"。另写函数，按族聚类，α 取 5%（§3.4） | — |
| `parse_inference` | "要加 threshold" | 已有 `threshold`、`threshold_source`、`normalization: temperature`（policies.py:173-194）；只缺温度值和 `calibration_ref` | — |
| `checks` | 只说 `min_train` 会阻断 | `max_mde` 按 `ds.splits["test"]` 算（train/checks.py:88-95），没有 test 时恒为"不足"。两项都改成非阻断的族级有效 n 警告 | — |
| K（W\* 文本）"只来自 train" | 是 | 不对。R 读过 dev 失败直方图（research/session.py:809-829，经 bench.py:1229-1239），W\* 又是 dev gate 选出来的。所以 finance 和 support 的 dev 读数会被 K 抬高（§2.6） | RESULTS v0.2：W\* 在 dev 80–84%、test 52% |
| `near_duplicates` | 对 test 只输出计数 | `task_signature` 会读所有题的断言值（adapters/automationbench.py:422-437），包括 test。改为冻结前跑一次，只出计数并记录（§2.6） | — |
| 计数口径 | 5 域含 operations | operations 整域留作 LODO，所有计数都去掉它（§2.1） | `data/out_main*.json` |
| 写门上限 | 14%/20%（SLIMMING §0.2） | 只因 must-not 失败的比例：train 37/456 = 8.1%，dev 116/725 = 16.0%。旧数是 49/345、108/543，两组分母的样本范围本身就不同 | `data/analyze.py` |
| dev gate 功效 | sd 0.34，+10 时功效 0.86 | 单次 dev 读数逐题差的 sd 中位 0.44（27 组比较，范围 0.28–0.59）。按 gate 全部条件模拟，P(接受)：真实效应 0 时 0.3–6%，+10 时 0.47–0.95 | `stats/sd2.py`、`sim.py` |
| C5 的 MDE | 9.5 点 | 只在采样解码下成立；近确定解码时是 12–14 点。计划要冻结一个大于 0 的温度 | `stats/sim2.py` |
| C3 | 在学生上测，G 可以是 judge，非劣界 −3 | judge 的成本项过不了：每次尝试 $0.06–0.25，学生在 Tinker 上约 $0.025–0.08，自托管 vLLM 约 $0.003–0.017（估；见下一行，2026-10-08 按 Tinker/RunPod 核价更正）。−3 点的界也窄于区间半宽（6–7 点）。改为：G 是 decider 或规则门，主终点是违反率之比，C3 作为 final 里的测量臂（§4.3） | `stats/sim3.py` |
| 学生单价 | 每次尝试 $0.01–0.02 | 9B、关思考，按 repo 实测的 Opus 轨迹负载（约 9 次调用；没有前缀缓存时累计 85–105k token，有缓存时新内容 13–19k；输出 3.4–4.2k）：Tinker 约 $0.025–0.08（缓存命中 / 不命中），RunPod 自托管 vLLM 约 $0.003–0.017（估）。所以评测时的大批量学生推理放自托管 vLLM（§3.5）。单价：Tinker models.json（现行价 2026-07-17 起生效，2026-10-08 核对），Qwen3.5-9B 每百万 token 训练 $1.463、生成 $1.995、prefill $0.66、缓存命中 $0.132；RunPod H100 SXM community $2.69/h、secure $3.49/h（代码默认 secure；价格页 2026-09-27 更新，2026-10-08 核对）（2026-10-08 按 Tinker/RunPod 核价更正） | Tinker models.json；RunPod 价格页 |
| 采集产出 | 接受率 40–60% | AutomationBench 上没有依据。Opus+W0 在 train 上的通过率只有 41%；学生最多 5 次替换，估 20–45%（§4.6） | `stats/base_train.py` |
| 臂 F 的数据 | 约 300 条老师轨迹 | 202/331 条含 `[REDACTED]`，中位在轨迹 20% 处就开始出现；多个包混在一起，与 DT6-13 冲突。改为只用 W0 包的轨迹，并按重放回填（§2.1） | `stats/redact2.py` |
| 缺一个臂 | 没有 S+W\* | 补上 C5′：S_d+W0 对 S+W\*。HZ 里学生带 h\* 是 41.7，蒸馏后 44.3 | — |
| 标签写到哪里 | 写进来源工作区的 ledger | 来源工作区一律只读，标签写进 v2 的数据计划目录（§2.3） | — |
| "改挂点会改 runner sha" | 是 | `runner_sha256` 只哈希 worker_runner.py（:76-79），挂点在宿主侧。真正会变的是 K：`default_core` 里有 `code_version()`（policies.py:800-813、release.py:45-51），任何合入都会产生新的 K | — |

## 1 目标与范围

**交付物。** teacher、judge、actor（包括学生）和 trainer 都是配置项。
- provider 由 `config.build_provider` 构造（config.py:39-54），支持 anthropic、openai_compat、claude_code 和 `module:Class`。manual 已随瘦身第一批删除（2026-10-08）。
- trainer 按角色由 `[train.<role>]` 指定，取值是插件名或 `module:Class`。插件名也可以是模块路径，自托管训练走这条（§3.3；2026-10-08 按 Tinker/RunPod 核价更正）。

| 线 | 产物 | 绑定位置 | 首个用例 |
|---|---|---|---|
| actor 蒸馏 | 学生 adapter 和它的 manifest（底座、tokenizer、renderer、sha256） | `worker.actor` 的 `ModelIdentity`（evolve/contracts.py:693-721） | Qwen3.5-9B，Tinker LoRA |
| 决策模型 | 模型 ref 加阈值 | `worker.decide.<point>` 的 `PolicyDescriptor`（evolve/contracts.py:281） | `write_gate`："按原样执行这次写？" |

**在 RSI 里的位置。**
- 训练产物就是 W 的一个组件变更。它和包候选走同一条路：`build_candidate`（policies.py:896-985）→ `task_gate` → Releases → 导出。不为模型另立 gate。
- 轨道 A 由 `python -m ouroloop.train` 驱动。轨道 B 加一个 `train` 阶段（现在 `PHASES` 里没有，bench.py:41），由 K 的排程触发。
- `LATER_METHODS`（bench.py:1825、:1965）对 R 继续拒绝训练方法。等至少一个主张记为"支持"之后（v2.5），才放开 R 提训练条目（research/experiment.py:286-307）。

**不在范围内：** "联系人工"的效果主张；在 Opus 上测 C3；RL、DPO/KTO、on-policy 蒸馏；每个决策点各训一个模型（DESIGN §4 规定最多一个 controller）。

**时机。**
- 先把 §7 列出的文档对齐。
- 代码要等 support 两份 final 评完、H1 算完、瘦身第一批合入后才合入。
- v2.0 可以先在隔离 worktree 里开发。

## 2 数据管线

### 2.1 来源与数据量（train，不含 operations）

| # | 来源 | 成本 | 规模 | 用途 |
|---|---|---|---|---|
| S1 | 现有 runner-shell 记录：`gateway_call`、`broker_event`、results、grading | 0 | 有效尝试 868 次，成功 415 次；252 题，其中 162 题至少成功一次。v3 以后带网关日志的成功轨迹约 305–320 条（support 还在增长），其中完全没有脱敏的 129/331 | 决策标签（经重放）；`teacher_rollouts` |
| S1′ | S1 中 W0 包的子集（v3 release `f5eb8531`；其他工作区同 digest 的由投影器计入） | 0 | 成功约 104 条，没有脱敏的约 40 条；回填后约 104 条 | X1 的 S_F 训练集（HZ 的"老师在 h 下"一臂） |
| S2 | S1 和 S3 的离线重放：在上游模拟世界里重放，并重映射生成的 ID | CPU | 复现率 98.2%（train+dev；sales 91.7%）；v4-finance train 151/152 | 逐次写的效果标签；跳过某次写的反事实；脱敏输出回填 |
| S3 | 学生运行：X1 里不带老师；v2.2 和轨道 B 里由老师审阅 | 学生推理；审阅按次计费 | 按 §4.6 定 | SFT 主数据（轨道 B）；学生上的写门数据 |
| S4 | judge 对已记录的写作答（离线，不影响轨迹） | 每次 $0.03–0.05 | 可以覆盖全部写 | 写门的模型标签；没有重放时的标签来源 |
| S5 | 部署日志（审批、编辑、撤销、转人工） | — | — | v2.5；schema 先留 `human:<channel>` |

**写与标签的量。** 写的口径是 adapter 的 `mutates`（adapters/automationbench.py:604）扣除只读 POST。

| 域 | 写 | 重放后有害的写 / 涉及题数 | `final_success`（是/否） |
|---|---|---|---|
| finance | 689 | 13 / 7 | 133 / 81 |
| hr | 280 | 14 / 13 | 13 / 41 |
| marketing | 1,331 | 53 / 15 | 155 / 176 |
| sales | 333 | 13 / 10 | 40 / 53 |
| 合计（4 域） | 2,633（可复现的 2,501） | 93 / 45 | 341 / 351 |
| support（还没重放） | 2,193 | 关键词匹配约 75 | 74 / 102 |

- 约 28% 的写是 neutral，评分器对它们没有表态。
- Opus 的行为近乎确定，所以标签按题聚集，有效样本数按题算。
- **不用的数据：** v0.1/v0.2 的 v1 循环轨迹。工具输出被截在 2000 字符，也没记系统提示。

### 2.2 记录点（都由宿主写，包写不到）

| 挂点 | 现状 | v2 改动 | 里程碑 |
|---|---|---|---|
| R1 `Gateway.model`（agent/gateway.py:267-322） | 写 `gateway_call`，经 `redact`（ledger.py:26-49） | 写网关日志时只按宿主持有的密钥值精确脱敏，外加密钥形态的模式（`sk-`、bearer、PEM 等），去掉宽泛的 `\w*token\w*[:=]` 规则。这条规则误伤了 12% 的 v4-finance 调用（QBO `SyncToken`、schema 文本）。另外加 `provider_role` 和逐调用身份 | v2.0 |
| R1′ 同上，采集模式 | 无 | `review_ref`，审阅钩子只作用于 act 调用 | v2.2 |
| R2 `AttemptBroker.call`（agent/gateway.py:340-380） | `gate` 只有 run/veto/refused（:346-360），输出只存 digest | 加 `write` 标志（`suite.is_write`）。写之前问 `worker.decide.write_gate`。`gate` 扩成 `{decision: run\|held\|default, point, decision_id, policy_ref, probs, threshold, explored, propensity}`。held 时返回 `is_error`，内容为 "Held by policy write_gate (<ref>): not executed."；后端失败时放行，记 `default` | v2.0 |
| R3 标签 | runner-shell 上 0 行 | 打标器写进**数据计划目录**，不写来源工作区的 ledger | v2.0 |

- **只读 POST 白名单**：`items:find`、`googleAds:search`、`*/search`、QBO `/query`、`chat/completions`。这类调用占 adapter 写计数的 3.7%，在 hr dev 占 16%。
- **写检测和打标器放在 adapter 钩子后面**：`suite.is_write(event)`、`suite.labelers()`。换了领域的开发者没有上游模拟器时，关掉重放，链路照样能跑（§2.4）。
- **已有记录**：带 `[REDACTED]` 的调用打上 `redacted_input`。v2.1 用重放回填被脱敏的工具输出，再用 `broker_event.output_digest` 核对：digest 对的是原文，454 处不匹配全部落在被脱敏的尝试里。核对不上的轨迹排除并计数。

### 2.3 模式

不新建轨迹存储类型。`Episode` 是投影器在内存里按 `attempt_id` 关联出来的视图。

| 记录 | 字段 | 由来 |
|---|---|---|
| DecisionRecord（ledger 的 decision 行） | `decision_id, attempt_id, anchor(call_id), point, spec_hash, view_ref, question, candidates, probs, route(act\|shadow\|default), threshold, explored, propensity, action, default, policy_ref, policy_kind, actor_identity_ref, package_ref` | train/runtime.py:94-161 的 `propose`；把 :151-156 写死的 `probs=None, explored=False, propensity=1.0` 改为取 `policy.route`（train/policy.py:34-45）的结果 |
| LabelRecord | `anchor(decision_id \| attempt_id+event_id), point, target{label: p}, source, rule "<labeler>@<ver>", weight, status(valid\|unknown\|conflicted), supersedes, evidence_refs` | 由 `FeedbackRecord`（evolve/contracts.py:666-686）泛化 |
| ReviewRecord（v2.2） | `call_id, attempt_id, teacher_identity_ref, verdict(PASS\|REPLACE\|ERROR), proposal_ref, replacement_ref?, rationale_ref, repairs, usage, cost` | 新写；偏好对 (proposal, replacement) 就存在这里 |
| DecisionExample 行 | 与 jev 的 `tde/schema.py:35-46` 逐字段一致，不加任何顶层键（jev 的 `from_dict` 是 `cls(**d)`） | `dataset` = 决策点名；`source_id = <domain>:<family>`（现在是 session，train/dataset.py:41）；`template_id = spec_hash`；`meta = {source, weight, task, family, attempt_id, decision_id, call_seq, rule, package_ref, input_schema, truncated, tokens}` |
| SFT 行 | `id, source_id, split(fit\|val), messages\|ref, tools, targets[{call_id, turn_index, producer: model:<id>, verdict: PASS\|REPLACE\|none}], meta{attempt_id, dataset, package_ref, actor_identity_ref, redacted_input, tokens}` | 新写；每条轨迹一行、多个目标；只取 `role="act"`；包压缩上下文导致前缀链断开时，在断点处切段 |
| 数据计划目录 | `plan.json`（来源工作区及其记录 digest、FamilySplit、来源选择、打标器版本、renderer、每题上限）、`labels.jsonl`、`decision/`、`sft/`、`manifest.json` | 由 `DataManifest`（evolve/contracts.py:609-630）补字段：各 JSONL 的 sha256、来源直方图、按原因的排除计数、split 断言结果 |

**`write_gate` 视图。**
- 顺序：待执行的写（method、url、params、body，截断处有标记）→ 任务文本 → 最近 k 次读取的摘要 → 步数和剩余预算。
- 上限是决策点 spec 的参数，进 `spec_hash`。TDE 用 2,900 字符，Qwen decider 用 10.5k 字符。
- 视图里永远没有断言。

**交给 trainer 的 split。** 作业目录里只放 `train.jsonl`（fit）和 `val.jsonl`（val）。tde 插件会把 val 改名为 jev 的 `calibration`（train/trainers/tde.py:77-81）。cal 只留在数据计划目录里，供阈值使用。

### 2.4 打标器（纯函数：Episode → LabelRecord）

| 打标器 | 用于 | 规则 | source | 里程碑 |
|---|---|---|---|---|
| `final_success@1` | 尝试级 | 本次尝试是否成功；用于 SFT 过滤和各项指标 | `env:final_success@1` | v2.0 |
| `replay_effect@1` | `write_gate` | 重放到第 k 次写，比较写前写后的断言向量（细则见下） | `env:replay_effect@1` | v2.0 |
| `judge_vote@1` | `write_gate` | judge 看同一视图加 K，看不到断言；输出 `{p_execute, reason}`，`samples` 可配；provider 来自 `[judge]` | `model:<identity_ref>` | v2.0 用脚本化 provider，v2.1 换真模型 |
| `replay_skip@1` | 只用于评估 | 删掉第 k 次写，以及依赖它所建 ID 的后续写，重放到底，看最终成功是否翻转 | `env:replay_skip@1` | v2.1 |
| `teacher_review@1` | actor 目标；`write_gate` | PASS/REPLACE；REPLACE 删掉或改动了一次写时，记一个 write_gate 标签 | `model:<teacher_ref>` | v2.2 |

`replay_effect@1` 的细则：
- must-not 断言从通过翻成失败 → harmful → "no"。
- 正向断言从假翻成真 → helpful → "yes"。
- 没有变化 → neutral → "yes"，权重 0.3（可配）。
- must-not-contain 翻转 → 仍标 "yes"，另记 `content_violation`。这类写本来就该发，是"重写内容"的信号，不是闸门信号。
- 重放出的向量与记录不一致 → `unknown`，排除并计为 `replay_mismatch`。

**没有重放时的通用路径。** 只用 `final_success` 加 `judge_vote`。v2.0 为这条路径单写一个测试。

### 2.5 血缘与来源选择

- **source 文法**：`env:<labeler>@<ver>`、`human:<channel>`、`model:<identity_ref>`。
  - provider 的 `source` 由网关按调用身份写入。
  - claude_code 权限弹窗那一路（train/claude_code.py:145-149）记为 `human:claude_code_permission`。
- **选择写在数据计划里**：

| 用途 | 默认 |
|---|---|
| `sources_fit` | `["env:*", "model:*"]`，可改为只用 env |
| `sources_threshold` | `["env:*"]` |
| `sources_eval` | `["env:*", "human:*"]` |

  - env 与 model 冲突时以 env 为准，记为 `conflicted`。
  - 没有 env 标签可用来定阈值的模型，状态为 `uncalibrated`，只能以 shadow 模式绑定。
- **要改的位置**（行号按 `455ffe2`，瘦身第一批之后；括号里是 `03f68b3` 的原位置）：
  - ledger.py:20-27（:20-24 的注释；:25-27 的 `SOURCE_WEIGHTS`/`EVAL_ONLY`/`SOURCES` 从闸门改为血缘；原 :19-23）；
  - evolve/contracts.py:619、:630-632（原 :671、:682-684）；
  - train/feedback.py:60-61（跳过逻辑）、:22、:68、:123（标签集和 kind 改为从 builder 取；原 evolve/feedback.py:83-84、:21、:91、:146）；
  - providers/__init__.py:4、claude_code.py:247、anthropic.py:45、openai_compat.py:44 的默认值；
  - providers/manual.py:87（整个文件）与 research/loop.py:99（旧决策研究那一段）也引用不变量 6，已随瘦身第一批删掉。
  - 时机：注释已于瘦身第一批改为指向 DESIGN §8 不变量 6（`41d702a`：上面 providers 的四处、ledger.py:20-24、train/backends/llm.py:1-3、train/runtime.py:167 的 `_default` docstring，原 runtime.py:372）；闸门与默认值随 v2.0（§3.8 第一行），因为 ledger.py:147、:156（原 :143、:152）只收 `SOURCES` 里的来源。

### 2.6 划分与防泄漏（全部由固定代码执行）

| # | 规则 | 执行方式 |
|---|---|---|
| 1 | 采集、打标、建集、选 checkpoint、阈值、K 的构建，全部只用各域冻结 split 里的 train 题 | 加载器按题名只实例化 train 题，不先解析再过滤。builder 遇到 dev 或 test 行直接失败（`split_violation`），也不发 GPU 请求。重放原型（`data/replay.py:52-56`）曾在内存里解析过全部题的 info，包括 sales 和 hr 的 test 题，但没有输出它们。这一点照实披露，正式加载器修掉 |
| 2 | FamilySplit：每个数据计划一张，只含 train 题，按 `<domain>:<family>` 哈希分成 fit/val/cal（70/15/15） | 在任何打标器运行前冻结；决策和 SFT 共用。重复尝试和增强副本与原题同侧。每题上限：被接受的轨迹 ≤ 4 条，决策行 ≤ 30 行。复用 train/dataset.py:113-207、:221-238（原 evolve/build.py:164-258、:272-289） |
| 3 | 交叉拟合 | C3b 的分数、K5/K6 的召回、写门阈值，都用全部 train 族按族 5 折得到的折外分数，不在调过参的 val/cal 上读 |
| 4 | 用途隔离 | checkpoint 只看 val loss，不看 val 任务成功率；dev 只给在线 gate 用，dev 记录不进任何数据集；test 只给密封 final 用 |
| 5 | 输入隔离 | teacher 和 judge 只看学生可见的 messages、proposal、tool specs 和 K；只有打标器读断言。投影后做字面量扫描（DT6-02） |
| 6 | K 不是只来自 train | W\* 是 dev gate 选出来的，R 也读过 dev 失败直方图（research/session.py:809-829）。所以 finance 和 support 上 S1 的 dev 读数会被 K 抬高：dev gate 按域报告，sales 和 hr（没有 K）单列，主张只看 test；继续与否的主要依据是 val 族上的 K3 |
| 7 | 近重复检查 | `near_duplicates` 会读所有题的断言值（adapters/automationbench.py:422-437、:525-541）。规定冻结前在合并 suite 上跑一次，只输出计数并记录；train 内部的近重复声明为同一族 |
| 8 | 跨域合并 | 训练域是 finance、support、sales、hr、marketing 的 train，约 252 题。operations 整域留出作 LODO，19 道 pilot 题也不进训练。support 的重放等它两份 final 跑完再做 |
| 9 | 来源工作区只读 | 每条命令运行前后对来源工作区做 digest，必须不变（测试 14） |

### 2.7 数据量

- **证明链路跑通**：v4-finance train 一个工作区就够（152 次尝试，约 500 次写，50 族）。量只够链路跑通，模型学不到东西。
- **作主张需要**：
  - SFT：300–500 条被接受的轨迹，与 HZ 每域 282–500 条同量级，只能靠审阅采集（轨道 B）得到。
  - 决策：≥ 3k 行，跨 ≥ 3 个域，交叉拟合后至少 40 个含有害写的族。
  - `checks.min_train=1000`（train/checks.py:38）从阻断改为"不足以作主张"的警告。
- **瓶颈是负例**：Opus 记录里有害的写约 170 次，集中在约 60 题上。学生的有害写预计更多，所以 C3 改在学生上测。
- **扩量顺序**：多域合并 → judge 给全部写打标 → 学生运行 → 每题多采几次 → 噪声种子副本（与原题同侧，不算独立样本）→ 部署日志（v2.5）。

## 3 训练管线

### 3.1 流程

```
DataPlan（冻结：来源工作区及 digest、FamilySplit、来源选择、打标器版本、renderer、每题上限）
 → project → label → build(decision | sft) → check → manifest(sha256)
 → train_artifact(role)：先预留预算 → 提交 TrainJob → 续跑时 attach，不重复付费（GPU 作业只部分成立，见 §6）
 → 产物（store ref / adapter + manifest）→ 按 role 探针
 → [只对决策模型] 阈值：折外分数 + env 标签
 → PolicyDescriptor（worker.actor | worker.decide.<point>）→ build_candidate
 → task_gate → Release → export（App.bind 带 Serving）
```

### 3.2 建集与检查

| 组件 | 复用 | 新写或修改 |
|---|---|---|
| `build_decision_dataset` | `build_task_dataset`（train/dataset.py:113）、`to_example`（:31-49）、`DatasetBuild`（:64-76） | `source_id`、split 映射、`template_id`；DecisionExample 校验器（与 jev 字节兼容，不依赖 torch）；manifest sha256 |
| `build_sft_dataset` | — | 用 `prev[:prefix]+tail` 重建 messages（v4-finance 3,068 次调用 0 处不一致）；按 `after_call_id` 关联 `tool_specs`、`broker_event`；按 role、split、success、verdict、`package_ref` 过滤；`redacted_input` 默认排除；超过 `max_seq` 的样本排除并计数，不截断 |
| 检查 | train/checks.py：截断、答案位置偏斜、13-gram | 适配 `DatasetBuild`（约 10 行）；`min_train` 和 `max_mde`（:88-95）改为族级有效 n 警告；SFT 另查长度、mask 覆盖、私有字面量、tool-call 往返 |

### 3.3 作业、插件与产物契约

- **作业层**（契约 `TrainJob`/`TrainResult`/`Trainer` 与 `train_artifact` 在 `[train]`，`JobTrainer` 与云、主机作业层在 `[train-jobs]`，SLIMMING §0.1 A、C）：
  - `TrainJob`/`TrainResult`/`Trainer`（train/train.py:45-63）；
  - `JobTrainer`（:84 起），可在本地、ssh、RunPod/Vast 上运行，有价格上限、截止时间和取消；
  - `train_artifact` 先预留预算，续跑时 attach（train/train.py:127-188）。但 attach 只在 trainer 有 `attach` 时才调（:155-156），`JobTrainer` 没有：重启后清理残留实例和作业、整轮重跑（train/jobs/trainer.py:53-56），所以对 GPU 作业"续跑不重复付费"只部分成立，见 §6（2026-10-08 按 Tinker/RunPod 核价更正）。
- **`train_artifact` 按 role 分派**：
  - 决策探针按决策点取问题。现在问题写死为 stop（train/train.py:216、train/serving.py:21）。
  - actor 用生成探针。

**actor 产物契约（新增，写进 train/trainers/__init__.py）。**

| 端 | 契约 |
|---|---|
| 训练端 | `train()` 写出 adapter 和 `manifest.json`：`{base, tokenizer, renderer, sha256, max_seq}` |
| serving 端 | `provider(run, ref, options) -> dict`，返回一张 `build_provider` 能构造的表：`openai_compat`（`base_url`，加上与 ref 同名的服务模型名；评测默认，指向自托管 vLLM），或 Tinker 采样（只作冒烟或兜底）（2026-10-08 按 Tinker/RunPod 核价更正） |
| 自托管 | 文档写明唯一一步手工操作：在 RunPod 上起 vLLM，默认服务合并后的完整权重，命令见表下（2026-10-08 按 Tinker/RunPod 核价更正） |
| 生成探针 | 核对服务名等于 ref；做一次 tool-call 往返（包括 `_unparsed`）；核对上下文长度 ≥ `max_seq`；用 Tinker 训练时的 renderer 和 vLLM 的 HF chat template 各渲染同一条轨迹，逐 token 比对，不一致就不过，否则学生评测时看到的提示格式与训练时不同（2026-10-08 按 Tinker/RunPod 核价更正） |

自托管 vLLM 的启动参数（2026-10-08 按 Tinker/RunPod 核价更正）：

```
# 默认：服务合并后的完整权重，不用管 LoRA 参数
vllm serve <merged_dir> --served-model-name <ref> --max-model-len <≥ max_seq> \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --language-model-only
# 备选：以 LoRA 方式服务 adapter（--max-lora-rank 默认 16，必须给 32）
vllm serve <base> --enable-lora --lora-modules <ref>=<dir> --max-lora-rank 32 --max-model-len <≥ max_seq> \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --language-model-only
```

- vLLM 的 Qwen3.5 recipe 只给了 35B 和 397B 的命令，9B 沿用同样的参数是推断；Qwen3.5 能否以 LoRA 方式服务、前缀缓存是否可靠，都没有确认（§7）。

**插件。**

| 插件 | role | 里程碑 | 说明 |
|---|---|---|---|
| `signature` | decision | 现有 | 纯标准库，离线测试用 |
| `mock_sft` | actor | v2.0 | 离线，写出符合契约的假 adapter |
| `tde` | decision | v2.1 | 修漂移：`ARCHITECTURE` 补 `topology`/`max_total`（train/trainers/tde.py:40），`max_state_tokens` 对齐（:32），pin `tde==0.1.x`。在 CPU 或经 `[train-jobs]` 租的 CUDA 上训。截断到 1,024 token 对链路验收可以接受，记进 `truncated` |
| `tinker_lora` | actor | v2.1 | validate → forward_backward → optim_step → val loss → save_state → 采样 ref（TECHNOLOGY §2）。默认配方见 §3.7 的配方表：只做 LoRA；HZ 的 α 32 在 Tinker 上设不了，lr 改为扫描（2026-10-08 按 Tinker/RunPod 核价更正） |
| `decider_lora` | decision | 轨道 B | 移植 jev 的 `ops/decider_lora_train.py`（112 行）和 `scripts/export_decider.py`（64 行）。加上：按 val 选 checkpoint；保留软标签，或者记明已 argmax；CUDA 检查；`version = ref` |
| 插件模块路径（如 `mypkg.sft`） | 任意 | — | 自托管训练（TRL、PEFT，跑在 RunPod 或自有集群上）走这条：`JobTrainer` 按模块路径加载插件（train/trainers/__init__.py:50-54），作为作业运行，有云上生命周期（train/jobs/cloud.py）、价格上限和截止时间；遵守上面的契约（2026-10-08 按 Tinker/RunPod 核价更正） |
| `module:Class` | 任意 | — | 在控制平面进程里直接构造（train/config.py:89-90），没有作业、没有云上生命周期，也没有价格上限；只用于测试（TRAINING_HOSTS；demo 已随瘦身第一批删除），不用于自托管 GPU 训练（2026-10-08 按 Tinker/RunPod 核价更正） |

### 3.4 阈值与温度

- **温度**：`fit_temperature`（train/calibrate.py:10）。结果写进 `parse_inference`（policies.py:173-194），只加 `temperature` 和 `calibration_ref` 两个字段。
- **阈值**：新函数 `fit_hold_threshold`。
  - 风险定义："通过的尝试所在的族里，至少有一次写被拦下的族所占比例"。用 Clopper-Pearson 上界 ≤ α，按族聚类，像 `fit_threshold`（train/policy.py:96-124）一样按固定顺序逐个检验（Learn-then-Test）。
  - 默认 α = 5%。零错误时上界能降到 α 所需的最少族数：α = 5% 要 59 族，α = 2% 要 149 族。只有一个 cal 划分（约 38 族）时，2% 根本无法认证，所以用全部 train 族的折外分数。
- 拿不到阈值的模型状态为 `uncalibrated`，只能 shadow。换 actor 后必须重定阈值，不继承（02 §4）。

### 3.5 ref、存储与 serving

| 对象 | 做法 |
|---|---|
| 决策模型 ref | 沿用 `train/stores/`（dir、hf）。新增 hub ref `hf:<repo>@<rev>[:<subfolder>]`，因为现在的 `JOB_ID` 正则不收带点的名字（train/stores/__init__.py:36、train/stores/hf.py:59-63） |
| actor ref | 下载 adapter，sha256 写进 `ModelIdentity.artifact_digest`，同时填 `adapter_ref`、`tokenizer_ref`、`renderer_ref`（evolve/contracts.py:693-721，这几个字段从没被填过）。release 只钉 digest |
| 决策模型 serving | 用一个 `/v1/systemone` 后端，复用 train/backends/jev.py。key 改为可选（:40-42 现在 key 为空就报错）；以 `--name <ref>` 启动，让身份探针对得上。tde-serve、decider serve、TypeSafe 共用这一个后端 |
| 学生 serving | openai_compat（vLLM/SGLang/Ollama）或 tinker 采样 provider。评测时大批量的学生推理走 RunPod 上自托管的 vLLM（合并权重）；Tinker 采样和它的 OpenAI/Anthropic 兼容接口（beta，官方说明只供低流量测试）只作冒烟或兜底（2026-10-08 按 Tinker/RunPod 核价更正）。runner shell 至今只跑过 claude_code，所以 v2.1 必须先过能力探针 |
| judge（轨道 B 的运行时绑定） | 任意 chat provider。`train/backends/llm.py` 只给独热答案（:48-51），要改为解析结构化概率 |

### 3.6 绑定、导出与 RSI 接口

- **通用 target**：把 `TargetRegistration("worker.stop", …)`（policies.py:66）推广为 `worker.decide.<point>`。
  - v2.0 的 kind 为 `rule|model`，mode 为 `shadow|act`。`judge` kind 和 model → judge → default 回退链放到轨道 B。
  - 去掉 stop 的硬编码（bench.py:1580-1588、train/train.py:216）。`worker.stop` 迁为 `worker.decide.stop`，身份由瘦身的金标测试守住。（2026-10-08 用户定，不写金标测试：现有活动都不在 HEAD 上续跑，迁名不必保持旧工作区的哈希；要守住的是 HEAD 读得了旧的 train/dev 记录，`tests/test_record_compat.py`。）
- **学生 actor**：作为 `worker.actor` 的新 `ModelIdentity`，经 `_bind_one`（policies.py:542-571）由 `provider_factory` 构造。
- **导出**：
  - `_export_into`（release.py:543-624）的 `model_refs` 现在只收 actor 身份（:600-618），要加上决策模型 ref、阈值 ref 和 adapter digest。
  - `App.bind`（app.py:25-34）构造 `Services` 时带上 Serving，否则 model 策略会报 BindError（policies.py:605-606）。
- **训练条目**：`{target, data_plan_ref, recipe_ref}` 经 `build_candidate` 成为候选。v2 期间只由 K 的排程或 CLI 产生。

### 3.7 开发者接入自己的模型（配置示例；换模型只改这里）

```toml
[llm]                        # 活动的 actor。学生臂就是把这里换成学生
type = "openai_compat"
base_url = "http://127.0.0.1:8000/v1"   # 评测时指向 RunPod 上的 vLLM（合并权重，§3.3）
model = "<actor ref>"        # 服务名 = 产物 ref，生成探针会核对

[teacher]                    # 只给宿主（K）用：v2.2 起在采集时审阅 act 调用
type = "claude_code"         # 链路验收用 Claude；开发者换成任意 provider 或 module:Class
model = "claude-opus-5-5"
capabilities = ["generate"]  # 有 logprobs、tokenizer 相同的老师才开 on-policy KD（v2.5+）

[judge]                      # 只给宿主用：离线打标
type = "claude_code"
model = "claude-opus-5-5"
samples = 1

[train.actor]
trainer = "tinker_lora"      # 或 mock_sft；自托管 GPU 训练写插件模块路径，由 JobTrainer 运行，不用 module:Class（§3.3）
recipe = { base = "Qwen/Qwen3.5-9B", rank = 32, epochs = 2, lr = [2e-4, 5e-4, 1e-3], batch = 8, warmup = 0.05, max_seq = 32768, seed = 42 }   # 见下方配方表；α 在 Tinker 上设不了

[train.decision]             # 取代旧的 [trainer]；旧表按 [train.decision] 读，以保持兼容
trainer = "tde"              # 或 signature / decider_lora / module:Class

[trainers.tde]               # 插件选项（怎么运行），沿用现有的 plugin_options
device = "cpu"

[data]
sources_fit = ["env:*", "model:*"]
sources_threshold = ["env:*"]
sft_targets = "accepted"     # PASS + REPLACE 的动作部分；"pass_only" 见 §7 第 3 项
per_task_cap = 4

[pricing.openai_compat]      # 学生必须有价（按 token 或 GPU 小时），否则 gate 的成本项判为 inconclusive（task_gate.py:449-458）
```

- `teacher` 和 `judge` 只属于宿主；包能调用的 role 仍然只有 `act`/`aux`。
- 审阅调用不计入学生的五项硬限额，计入采集预算和费用。
- 学生训练和评测都关掉 thinking，因为 claude_code 不返回思考内容，训练目标只取可见回合。是否改为 low/medium 是 §7 第 9 项，待定（2026-10-08 按 Tinker/RunPod 核价更正）。
- 训练放 Tinker LoRA（参考实现）；评测时的大批量学生推理放 RunPod 上自托管的 vLLM，合并权重后经 `openai_compat` 接回；Tinker 采样及其 OpenAI/Anthropic 兼容 beta 接口只作冒烟或兜底。自托管训练不走 `module:Class`，而是由 `JobTrainer` 运行的插件模块（§3.3）（2026-10-08 按 Tinker/RunPod 核价更正）。

**学生训练配方（`tinker_lora` 默认）**（2026-10-08 按 Tinker/RunPod 核价更正）

| 项 | 取值 | 依据 |
|---|---|---|
| 方式 | 只做 LoRA；不做全参，也不做 QLoRA。全参只在轨道 B 上 LoRA 明显欠拟合时再考虑 | "LoRA Without Regret"（2025-09-29）：所有层（含 MLP）都加 LoRA、容量够时，SFT 与全参持平；只加在注意力层上明显更差。它测的是指令和推理数据集，多轮 agent SFT 没有直接验证过。Unsloth 的说明："not recommended to do QLoRA (4-bit) training on the Qwen3.5 models" |
| rank / α | r32，所有层（含 MLP）。α 在 Tinker 上设不了，以导出的 `adapter_config.json` 为准（HZ 在 Tinker 上报告的是 α 32）。线性注意力层的投影有没有被覆盖，看导出的 `target_modules` | 9B 在 r32 下约 87M 个 LoRA 参数（含 lm_head 约 95M）；X1 每个 epoch 参与训练的 assistant token 约 0.4M，轨道 B 约 1–2.2M |
| 学习率 | 扫 {2e-4, 5e-4, 1e-3}，按 val NLL 选；在 Tinker 上约多花 $10 | cookbook 的 `get_lr` 公式算出 4.7e-4，HZ 的 2e-4 可能偏低 |
| batch / epoch / 调度 | batch 8，2 个 epoch，5% warmup 后线性衰减；seed 42 | batch、epoch、seed 沿用 HZ |
| loss | 只在 assistant 回合上算 | — |
| 超长样本 | 超过 `max_seq` 的丢掉，不截断 | §3.2 |
| `max_seq` | 32768，排除 S1′ 的 4.8%（sales 17 条里排掉 3 条），不是 0.7%。Tinker 上可提到 65536（9B 是 64K 上下文），只多约 $0.5；提了之后 vLLM 的 `--max-model-len` 跟着调 | S1′ 的 token 计数 |
| 导出 | `train_unembed=False`；或者合并成完整权重再交给 vLLM（评测默认，§3.3） | — |

### 3.8 复用与新写（行数）

| 环节 | 复用（file:line） | 新写或修改 | v2.0 | v2.1 | v2.2 | 轨道 B |
|---|---|---|---|---|---|---|
| 血缘与精确脱敏 | ledger.py:20-49；evolve/contracts.py:671、:682-684；train/feedback.py:22、:60-61、:68、:123 | source 文法、选择器、按值脱敏 | 90–130 | | | |
| 写分类与 adapter 钩子 | adapters/automationbench.py:604 | `is_write`、`labelers()`、只读 POST 白名单 | 40–60 | | | |
| 投影器 | `assets/scan.py` 原型 | `ouroloop/train/stream.py`，带逐行 token 计数 | 250–350 | | | |
| 重放 | `data/replay.py` 原型（201 行）；adapters/automationbench.py:604-650 | 按题名加载、ID 重映射、复现率；`replay_skip`；输出回填 | 250–350 | 160–240 | | |
| 打标器与 FamilySplit | train/feedback.py:78-138；train/dataset.py:113-238 | 3 个打标器、FamilySplit、交叉拟合折 | 180–260 | | 60–80 | |
| 两个 builder 与检查 | train/dataset.py:31-49、:64-76；train/checks.py | 见 §3.2 | 450–650 | | | |
| `write_gate` 挂点 | agent/gateway.py:340-380；train/runtime.py:94-161；train/policy.py:34-45 | shadow/act、fail-open、决策行字段 | 200–280 | | | |
| 通用 target | policies.py:66、:294-333、:542-616；bench.py:1580-1588 | `worker.decide.<point>` | 120–180 | | | judge 绑定和回退链 150–200 |
| 训练入口与探针 | train/train.py:106-228、:45-63；train/jobs/trainer.py:29 起（`[train-jobs]`） | role 分派、按决策点探针、生成探针、actor 契约 | 150–220 | 60–100 | | |
| trainer 插件 | train/trainers/__init__.py、signature.py、tde.py | `mock_sft`；`tde` 修漂移；`tinker_lora`；`decider_lora` | 60–80 | 320–490 | | 200–300 |
| renderer | — | 学生 chat template、tool-call 编码、loss mask | | 150–250 | | |
| 阈值与评估 | train/calibrate.py:10；train/policy.py:96-124 | `fit_hold_threshold`；召回、误拦、AUROC，按族聚类的 bootstrap | 120–180 | | | ECE、coverage@risk 100–150 |
| ref、store、serving | train/stores/；train/serving.py:21；train/backends/jev.py:40-42 | hub ref、tinker ref 与 digest、key 可选、能力探针、学生计价 | | 200–330 | | |
| 网关角色与审阅 | evolve/contracts.py:774-802（`CollectionRequest(mode="reviewed")`）；M2 §1a | role → provider 映射、审阅钩子、ReviewRecord、K 构建、有界修复 | | | 450–700 | |
| 导出 | release.py:543-624；app.py:25-34 | 导出模型 ref；App.bind 带 Serving | 60–100 | | | |
| CLI 与臂运行 | train/config.py:82-96、train/cli.py；evolve/task_eval.py:247、:366 | `python -m ouroloop.train`；`arm` 子命令（在 val+cal 族上跑学生） | 150–220 | 100–150 | | `bench --phase train` 与计划字段 300–450 |
| **合计** | | | **2.2–3.2k** | **1.0–1.6k** | **0.5–0.8k** | **0.75–1.1k** |

- 测试：v2.0 约 1.2k 行，v2.1–v2.2 约 0.8k 行。
- 另外沿用瘦身第一批搬进 `[train]` 的约 2.4k 行，复用 `[train-jobs]` 约 2.0k 行（同一子包，代码只搬一次）。2026-10-08 实际搬完：`ouroloop/train/` 3,042 行（不含 jobs），`ouroloop/train/jobs/` 1,697 行（另有 sh 与 Dockerfile）。
- jev 一侧保留模型代码、tde-serve 和 releases。ouroloop 不 import jev 的内部函数，只 pin `tde` 版本。

## 4 试验设计

### 4.1 命题与对照（评测时都没有老师）

| ID | 问题 | 比较 | 层级 | 轨道 |
|---|---|---|---|---|
| E0 | 链路完整 | §5 的命令、测试和 smoke 收据 | 工程验收 | A |
| X1 | 首个试验 | S0 对 S_F，在 train 的 val+cal 族上；学生上的写门上限 | 方向读数，不作主张 | A |
| C5 | 蒸馏让学生在同一 h 下变强 | S1 = S_d+W0 对 S0 = S+W0，五个域的 test × 3（finance、support、sales、hr 各 25 题，marketing 23 题，共 123 题；按 §7 第 2 项的有条件复用规则，报告按域披露每份 test 被读过几次）（2026-10-08 用户改为全部域可跑 final；原为 test 100 题，不含 marketing） | 主张 | B |
| C5-LODO | 跨域泛化 | 同上，用 operations 81 题 × 3（从未训练，从未读过） | 主张 | B |
| C3 | 写门在学生上的增量 | S1+G 对 S1。S1 没过 K4 时改为 S0+G 对 S0。G 预注册为 decider 或规则门二者之一 | 主张（测量臂） | B |
| C5′ | 训练是否胜过直接给学生 W\* | S1 对 S+W\*（finance、support 用 W\*；sales、hr 用 W0） | 次要主张，附成本 | B |
| C3b | 小 decider 能否便宜地替代 judge | decider、judge、未训练底座、规则门、永远放行；用折外分数，在 env 标签上比 | 报告级 | B |
| C6 | 成本 | S0、S1、S1+G、Opus+W0（现有读数）每次成功的成本；摊销采集和训练后的盈亏平衡 N | 只报告 | B |

**对照组。**
- 每个学生臂都配一个同底座、未训练的 S0。S0 在 X1 里 thinking 开、关各跑一次，取较好的一个作对照，这样比较偏保守。
- 决策线另有 no_state、shuffled_state、规则门、永远放行，以及只用 env 标签训练对 env+judge 训练。
- 规则门的定义：收件人或目标对象在任务文本和已读结果里都没有出现过。
- 每臂都报告畸形 tool call 率，以及因格式失败而丢掉的题。

**采集方式（v2.2 链路，轨道 B 放量）。**
- 学生在 W0 下行动。
- 老师带着 K 在网关对每个 act 调用给出 PASS 或 REPLACE。K 是各域 W\* 的 memory 和 skills 原文，用固定模板渲染；没有 W\* 的域 K 为空。
- 每次最多替换 5 次，每次替换可修复 1 次；仍失败记 ERROR，episode 记 incomplete。
- 只保留成功的 episode。训练目标为 `accepted`，替换理由永不进目标。

### 4.2 X1：首个试验（v2.1 末）

| 项 | 内容 |
|---|---|
| 臂 | S0：Qwen3.5-9B + W0，thinking 开、关各跑一次。S_F：S0 底座用 S1′（W0 包的成功轨迹，只取 fit 族，回填后的）做 LoRA SFT |
| 题 | 各域 train 的 val+cal 族，约 60–75 题（finance、sales、hr、marketing；support 没有 final，加不加入待用户定，§7 第 4 项），每臂 1 次 |
| 免费读数 | 在 S0 的记录上跑 `replay_effect` 和 `replay_skip`，得到学生上写门的上限（被救回的失败 ÷ 尝试数） |
| 链路收据 | 记录 → 投影 → 打标 → 建集 → 检查 → 训练 → 探针 → 阈值 → shadow/act 绑定 → build_candidate → 导出 → App.bind 跑 1 题，每个环节都有 ref；全程不碰 dev/test |
| 费用 | Claude $0（加 judge 标签时 ≤ $35）；GPU 和 Tinker < $80（按核价，Tinker 上的训练、扫学习率、推理加开思考的 S0 臂合计约 $20–45，明细见 §4.6；2026-10-08 按 Tinker/RunPod 核价更正） |

**底座的外部证据与 K1 的"更大底座"**（2026-10-08 按 Tinker/RunPod 核价更正）

唯一的外部来源是 Artificial Analysis 的 AutomationBench-AA（2026-10-08 保存的页面 JSON）。AA 报的是"不违反护栏前提下完成目标的比例"，用的是它自己的 harness，不是我们的 W0；换算系数 0.6 是粗估。

| 模型 | AA 分数 | 换算成我们的完整成功率（粗估） |
|---|---|---|
| Claude Opus 5.5（max） | 69.5% | 我方 Opus+W0 实测约 41%，由此取系数约 0.6 |
| Qwen3.8-27B：xhigh / medium / low / 关思考 | 48.2 / 40.3 / 30.1 / 12.4% | 约 29 / 24 / 18 / 7% |
| Qwen3.5-9B（开思考） | 1.0% | 约 0–1% |

- **9B 保留为第一期链路验收的默认底座。** 第一期不设效果指标，S0 弱不判第一期失败，只是 X1 的读数参考价值低。按上表，K1 在 9B 上很可能触发。
- **K1 的"更大底座"写成 Qwen3.8-27B。** 思考档位在 X1 计划冻结前定（§7 第 9 项）：关思考约 7%，正好卡在 K1 边上。
- **可选的冻结前 S0 小探针：** 15–20 道 train 题，跑 Qwen3.8-27B 的 low、medium 和关思考，约 $10–20。9B 的开、关思考本来就在 X1 里跑，不重复。
- **不考虑的尺寸：** 14B 砍掉（Qwen3.5 没有 14B；Qwen3-14B 不在 Tinker 上，BFCL 多轮 34.8，还低于 Qwen3-8B 的 41.8）。4B 不当 actor，只作 decider 候选。

**预注册读法（计划 hash 在第一次学生运行前提交）。**

| 读数 | 规则 | 动作 |
|---|---|---|
| 链路 | 收据每个环节都有 ref | 记"链路通过"；否则列出断点，轨道 A 不结束 |
| K1 | S0 < 5% | 换更大的底座 Qwen3.8-27B（思考档位在冻结前定）重测一次；仍 < 5%，C5 记 not_run（2026-10-08 按 Tinker/RunPod 核价更正） |
| K1b | S0 ≥ 同一批题上 Opus+W0 的读数 − 5 点 | C5 没有空间，记 not_run。这里跨了 K 身份（`code_version` 变了），只作门槛 |
| S_F − S0 | 报点估计和按题聚类的 95% 区间（n≈75 时 MDE 约 11–15 点） | 只作方向。区间上界 < 0 时记"teacher_rollouts 有害"（与 HZ 的 12→12、12→3 一致），轨道 B 不用它作主数据 |
| 写门上限 | `replay_skip` 上限 ≥ 3 点 | C3 列入轨道 B；< 3 点时 C3 记 not_run（K5） |

### 4.3 轨道 B 的预注册读法

- **安装**：按现行 M3 规则：`min_gain` 0.05、确认复测、退步规则、成本项 β（bench.py:1885-1887）。不为模型改规则。
  - 冻结 ≥ 3 次合并的 S0 对照读数，学生推理很便宜。
  - 提交计划时附上把 gate 全部条件算进去的接受率模拟曲线（`stats/sim.py`）。
- **多重比较**：{C5, C5-LODO, C3} 用按题 cluster bootstrap 求 p 值，做 Holm 校正。C5′、C3b、C6 是次要或报告级。
- **C5、C5-LODO**：
  - 计划冻结一个大于 0 的采样温度，每题 3 次。
  - Holm 校正后显著且差值 > 0，记"支持"；显著且 < 0，记"有害"，不发布；其余记"不确定"。按域分列，不按域另作主张。
- **C3**：主终点是每次尝试的 must-not 违反率之比 (S+G)/S；成功率差作为次终点一并报告。
  - 比值上界 < 1，且成功率差的上界 ≥ 0，记"支持（风险下降）"。
  - 再加上成功率差的下界 > 0，记"支持（增益）"。
  - 成功率差的上界 < 0，记"有害（误拦）"。
  - 其余记"不确定"。
  - G 不以安装为前提，是 final 里预注册的测量臂。G 选 decider 还是规则门，由 train 上的折外读数决定：误拦 ≤ 5% 时对 env-harmful 召回更高的那个；打平选规则门。
- **C5′**：差的下界 > 0，记"训练优于给 W\*"；否则记"不确定"。与 C6 的成本并列报告。
- **C3b**：用折外分数，按族聚类求区间。
  - 含有害写的族 < 40 时只作报告。
  - 达到 40 族时：decider 对 judge 的 AUROC 差下界 > −0.05，且每次决策成本 ≤ judge 的 1/10，记"同质更便宜"；decider 对未训练底座的下界 > 0，记"训练有效"。
  - 不显著一律读作"不确定"，不读作"无效"。

### 4.4 功效

| 设置 | 数值 | 来源 |
|---|---|---|
| 单次 dev 读数，逐题差的 sd | 约 0.45（实测 0.28–0.59，中位 0.44）；100 题时 SE ≈ 4.5 点 | `stats/sd2.py` |
| 合并 dev gate，全部条件 | P(接受)：效应 0 → 0.3–6%；+10 → 0.47–0.95；+15 → 0.72–1.0 | `stats/sim.py` |
| final 100 题 × 3，采样解码 | sd 0.27–0.37，MDE ≈ 9.5 点；+10 时功效 0.87–0.99 | `stats/sim2.py` |
| final 100 题 × 3，近确定解码 | MDE 12–14 点 | 同上 |
| final 50 题 × 3 | +10 时功效 0.50–0.84 | 同上 |
| operations 81 题 × 3，采样解码 | MDE ≈ 10.5 点 | — |
| K3（val 约 38 题） | P(停 \| +10)：每臂 1 次读数 14–22%，2 次 8–17%；P(继续 \| 0)：21–29% | `stats/sim3.py` |
| C3 非劣界 −3 点 | 差值为 0 时 P(下界 > −3) 只有 0.15–0.46，所以改了终点 | 同上 |
| C3b | 约 15–20 个阳性族时，AUROC 差的 SE ≈ 0.06 | 交叉拟合后再算 |

- **预期**：C5 如果 HZ 的效应能迁过来（平均 +21 点），≥ +12 点时测得稳，+10 点在边缘。C5′ 预期小，HZ 只有 +2.6。
- **重复的作用**：重复只压低题内噪声，题间异质性是下限。种子副本不算进 n。

### 4.5 指标

- **任务层**：通过率、partial credit、按题 fixed/regressed、`cost_per_attempt`、每次成功的成本、延迟、畸形 tool call 率、PASS/REPLACE 分布、打满替换上限的比例。
- **决策层**：触发率、通过的族里被误拦的比例、对 env-harmful 的召回、AUROC、每次决策的成本和延迟、每次尝试的 must-not 违反数。ECE 和 coverage@risk 在轨道 B 才算。
- 所有区间都按题聚类。

### 4.6 预算与时间

Claude 走订阅，金额是按牌价折算的名义费用。GPU 和 Tinker 是真实支出，每个计划冻结上限，preflight 时按当期价目核算。

| 阶段 | 内容 | Claude 名义 | 真实 |
|---|---|---|---|
| v2.0 | 离线 | $0 | $0 |
| v2.1 + X1 | judge 给 v4-finance train 约 500 次写打标（$15–25）；Claude 作 actor，带写门 shadow/act 跑 10 次（约 $5）；X1 只有学生推理；Tinker SFT：S1′ 里放得进 32768 的 99 条 × 2 个 epoch 约 3.3–3.7M token，训一次约 $5，扫 3 个学习率 $15–16；按 §4.2 只取 fit 族时约 2.3–2.6M（约 $3.6）。学生推理 150–300 次：Tinker $4–23，RunPod vLLM 约 $2–6（估）。另有 RunPod 冒烟约 $1–2（§5 v2.1）、可选的 27B S0 小探针约 $10–20（§4.2）（2026-10-08 按 Tinker/RunPod 核价更正） | ≈ $20–35 | ≤ $80 |
| v2.2 | 审阅采集 smoke 10–20 次（每次约等于步数 × $0.05） | ≈ $10–20 | ≤ $10 |
| **轨道 A 合计** | | **≈ $30–55** | **≤ $90** |
| 轨道 B：采集 | 试点 100 次 $50–75；按试点的步数和产出重算后再采，上限 1,200 次，约 $600–900 | ≈ $0.65–1.0k | — |
| 轨道 B：训练与 gate | SFT 9–20M token；val 和 dev 读数只有学生推理；decider 约 0.5 H100 小时 | — | ≤ $250 |
| 轨道 B：C3b 与 final | judge 给约 5k 次写打标 $150–250（只为 C3b 的 judge 对照）；final 只有学生推理；可选 Opus 参照 $70 | ≈ $0.15–0.32k | ≤ $80 |
| **轨道 B 合计** | | **≈ $0.8–1.3k** | **≤ $0.33k** |

- **吞吐（实测）**：
  - v3：850 次尝试，$407，约 10.2 小时；
  - v4-finance：327 次，$136，约 6.6 小时；
  - 每次尝试中位 86 秒。
- 1,200 次审阅采集约需 1–2 天。轨道 B 和 support 的两臂共用一个账户，要排在 support 两份 final 之后。
- 老师换成别的模型时，费用按 token × 单价重算，K2 的产出阈值也要重新做试点。

## 5 里程碑与验收（先离线；真实 smoke 单列，不进默认测试）

### v2.0 链路离线跑通（$0，约 2.2–3.2k 行，测试约 1.2k 行）

**范围：** §3.8 中 v2.0 一列。

```
python -m ouroloop.train plan      --source examples/automationbench/workspace-claude-v4-finance --split train --out <plan>
python -m ouroloop.train project   <plan>
python -m ouroloop.train label     <plan> --labelers final_success,replay_effect
python -m ouroloop.train build     <plan> --role decision --point worker.decide.write_gate
python -m ouroloop.train build     <plan> --role actor --dataset teacher_rollouts
python -m ouroloop.train check     <plan>
python -m ouroloop.train fit       <plan> --role decision          # 用 [train.decision]
python -m ouroloop.train threshold <plan> <model_ref>
python -m ouroloop.train eval      <plan> <model_ref> --controls always,rule
python -m ouroloop.train smoke     --offline
pytest -q tests/train/test_train_data.py tests/train/test_train_decision.py tests/train/test_train_actor.py \
          tests/train/test_evidence_training.py tests/train/jobs/test_jobs.py tests/train/test_train_config.py tests/test_export.py
```

**验收（按命令的实际输入，即 v4-finance 的 train）：**
- **project**：152 次尝试、1,445 次 `gateway_call`、3,412 个 `broker_event`；前缀链 0 处不一致；digest 不匹配只出现在被脱敏的尝试里；输出按 `package_ref` 的成功轨迹计数和逐行 token 数。
- **label**：写的数量与投影器的计数一致（约 500 次）；复现率 ≥ 97%（实测 151/152）。
- **build**：manifest 带 sha256；没有一族同时出现在两个划分里；同一张 FamilySplit 重建出的字节完全相同。
- **smoke --offline**：在 `tests/fake_upstream` 上，用脚本化的 actor 和 judge，加 `signature` 和 `mock_sft`，走完 §4.2 收据里的每个环节。gate 用测试 preset，判决结果不论。

**测试必须断言：**
1. 消息重建 0 处不一致。
2. dev/test 尝试被拒；test 题的 info 从未被解析（投毒夹具：一解析就抛异常）。
3. 同一张 FamilySplit 重建出的字节相同。
4. source 文法正确；阈值集里只有 env 标签。
5. 在 fake_upstream 上重放能复现断言向量，并定位翻转的那次写；不一致时记 unknown。
6. 只读 POST 不算写。
7. judge 的输入和 SFT 行里扫不到断言或评分字符串（DT6-02）。
8. 每题上限生效。
9. 没绑定写门时 transcript 逐字节不变；shadow 模式下只多一行决策行；act 模式下 held 返回错误输出，并记下 probs 和 propensity；后端失败时放行，记 default。
10. 与 jev 夹具往返，没有额外的顶层键。
11. App.bind 绑定 model 门不报 BindError；导出物带模型 ref。
12. actor 产物送进决策探针时被拒；生成探针经假的 openai_compat 服务完成 tool-call 往返。
13. 关掉重放的通用路径（只有 `final_success` 加脚本化的 judge 标签）能建集并训练。
14. 每条命令运行前后，来源工作区的 digest 不变。

### v2.1 接真模型跑链路，并跑 X1（Claude ≤ $35，真实 ≤ $80）

**范围：** §3.8 中 v2.1 一列。

```
python -m ouroloop.train smoke --real --config examples/automationbench/v2-chain.local.toml
python -m ouroloop.train arm   --config examples/automationbench/v2-x1.toml --arm S0 --tasks val,cal
python -m ouroloop.train arm   --config examples/automationbench/v2-x1.toml --arm S_F --tasks val,cal
python -m ouroloop.train label <x1-plan> --labelers replay_effect,replay_skip
```

- **决策链**：
  1. Claude 作 judge，给 v4-finance train 的写打标。
  2. 用 `tde` 训（CPU，或经 `[train-jobs]` 租的 CUDA）。
  3. 以 `--name <ref>` 启动 tde-serve。
  4. 绑定 `worker.decide.write_gate`，在 10 道 finance train 题上先跑 shadow 再跑 act。
  5. 导出，用 App.bind 跑 1 题。
- **actor 链**：
  1. 能力探针。
  2. 回填 S1′。
  3. 用 `tinker_lora` 训出 S_F。
  4. 合并权重后以 RunPod 上的 vLLM（openai_compat）接回，过生成探针（含 renderer 与 chat template 的 token 比对）；Tinker 采样只作冒烟或兜底（2026-10-08 按 Tinker/RunPod 核价更正）。
  5. 在 W0 下经 runner shell 跑 X1。
- **RunPod 第一次付费前**（2026-10-08 按 Tinker/RunPod 核价更正）：用一张 H100 跑约 20 分钟（约 $1–2），一个训练任务加 30 次尝试的推理测试。这也是 `train/jobs/cloud.py` 第一次对真实 API 运行。
- **验收**：smoke 收据每个环节都有 ref；全程不碰 dev/test；X1 按 §4.2 的读法写进收据。

### v2.2 审阅采集链路（Claude ≤ $20）

**范围：** 网关的 role → provider 映射、审阅钩子（只作用于 act 调用）、ReviewRecord、K 构建、有界修复、`teacher_review` 打标器、`build_sft_dataset` 按 verdict 过滤。

**验收：** 在 finance train 上用 Claude 作老师审阅 10–20 次学生尝试，建出 `reviewed_student` 集并训一次；轨道 A 结束。

**测试**按 §7 改写后的 DT6-01、DT6-09–14、DS6-02–08（DS6-01、DS6-09–11 是适配用例，v2 不做），在 fake_upstream 上用脚本化的学生和老师断言：
- REPLACE 后包拿到的是替换响应，proposal 只存在于 ReviewRecord；
- 老师 ERROR 时 episode 记 incomplete；
- aux 调用不进 SFT；
- 私有字面量不出现在 SFT 行里；
- 不同包采的轨迹不能进同一个计划。

### 轨道 B（v2.3–v2.4，计划冻结，用户决定开不开）

| 步 | 命令 | 停止条件 |
|---|---|---|
| 冻结 | `ouroloop freeze --config examples/automationbench/v2-distill.toml`：冻结 FamilySplit、学生和老师身份、K 的 ref、配方、采样温度、上限、预算根、预注册读法、≥ 3 次 S0 对照读数 | — |
| 采集 | `ouroloop bench … --phase train --role actor --until collect`：先跑 100 次试点 | K2 |
| 训练 | `… --until fit`：建集 → 检查 → 训练 → 探针 → val 族上不带老师，每臂 2 次读数 | K3 |
| dev gate | `… --until gate`：合并 dev 100 题，S1 对 S0，按域报告 | K4 |
| 写门 | 在学生记录上做交叉拟合的 C3b 离线报告 → 选 G → 在 train-cal 上 shadow | K5、K6 |
| final | `… --phase evaluate` 和 `--phase report`：S0、S1、S1+G（或 S0+G）、S+W\*；operations 的 S0/S1 | — |

评完按版本合入 main。

### v2.5 以后

`ctx.finish` 与运行时 `stop`；在学生上 re_evolve（M2 §3a）；R 提训练条目；部署日志回流；`contact_human`（choice 点加 KnowNo 式的共形弃权带）；在 REPLACE 偏好对上做 DPO/KTO；用断言做奖励的 RL；on-policy 蒸馏。

## 6 风险与止损

| # | 停止条件（预注册） | 动作 |
|---|---|---|
| K0 | v2.0 的离线套件或投影复现没通过 | 不进任何付费步骤 |
| K1 | X1：S0 < 5% | 换更大的底座 Qwen3.8-27B（思考档位在 X1 冻结前定）重测一次；仍 < 5%，C5 记 not_run，只交付链路。按外部证据，9B 上很可能触发（§4.2）（2026-10-08 按 Tinker/RunPod 核价更正） |
| K1b | X1：S0 ≥ Opus+W0 − 5 点 | 没有空间，C5 记 not_run |
| K2 | 试点 100 次：成功 < 20%，或被接受的 < 25 条，或 REPLACE 超过 act 步的 50%，或超过 40% 的尝试打满替换上限 | 停止采集，报告产出率；调 K 或学生后重新冻结 |
| K3 | val 族上（每臂 2 次读数）S1 − S0 的点估计 < +5 点，或畸形 tool call 增多 | 不付 dev gate。它对真实 +10 的误停率是 8–17% |
| K4 | S1 没过 dev gate | C5 记 "rejected at dev"；C3 改为 S0+G 对 S0 |
| K5 | 学生记录上 `replay_skip` 的上限 < 3 点 | 写门不以 act 方式绑定，C3 记 not_run 并写明原因 |
| K6 | 误拦 ≤ 5% 时，judge 对 env-harmful 的召回 < 50% | decider 只用 env 标签训练 |
| K7 | 建集时发现 dev/test 题号，或 13-gram 重叠超阈值 | 构建失败，不发 GPU 请求 |

**其他风险：**

| 风险 | 处理 |
|---|---|
| 共适应：W\* 是在 Opus 上演化的 | 采集和评测用 W0 加 K；C5′ 直接测 S+W\*；在学生上演化放到 v2.5 |
| K 身份：任何合入都会改 `code_version`，从而产生新 K | 每个主张的对照臂都在同一计划里重测；X1 的 K1b 跨了 K，只作门槛 |
| 重放不忠实（sales 91.7%） | 不复现的尝试排除并计数；某个域复现率 < 95% 时单列 |
| 标签按题聚集 | 只报按题聚类的区间；用交叉拟合，不作逐行主张 |
| 同模型审阅出过两次反例（ceiling 修好 1 道、弄坏 2 道；send_review 弄坏 2/6） | judge 只作离线标签和 shadow 参照；G 是 decider 或规则门 |
| 被拦后 agent 换个方式重试同一个有害写 | 报告拦截后的重试率；结论以现场读数为准 |
| 订阅限流（429/529） | 按现行规则不计费重试；采集墙钟时间按限流估算 |
| Tinker 不可用或涨价 | 能力探针在提交前就会失败；回退到由 `JobTrainer` 运行的自托管插件模块（如 RunPod 上）必须在配置里显式声明，不静默换后端（2026-10-08 按 Tinker/RunPod 核价更正） |
| 已知缺口：GPU 作业续跑会重复付费。`JobTrainer` 没有 `attach()`，重启后清理残留实例和作业、整轮重跑（train/jobs/trainer.py:53-56、:325、:403）；丢掉的 GPU 时间要再付一次，也不能从 checkpoint 续训 | 随 v2.1 的 `[train-jobs]` 漂移修复（SLIMMING §0.1 C）一起修（2026-10-08 按 Tinker/RunPod 核价更正） |
| test 被 v1 final 反复读 | 2026-10-08 用户改为全部域可跑 final，原来"sales/hr 的 test 先被 v1 读了，C5 的池降为 finance+support 50 题"的情形不再是止损条件。C5 的池是五个域的 test（123 题），每份都可能已被 v1 final 读过：按有条件复用规则（构建学生的任何产物都不读之前 final 的逐题记录，由结果库按 split 过滤、代码强制），报告按域披露读取次数。operations 81 题是唯一从未读过的留出集，C5-LODO 是唯一的全新读数（2026-10-08 用户改为全部域可跑 final） |

## 7 未决问题与决定（2026-10-08：前八项已定下，第 9 项待定）

用户 2026-10-08 答"按默认"：下表前八项全部按默认值定下。第 9 项是同日按 Tinker/RunPod 核价后新增的，待用户决定。同日第 2 项又由用户改为全部域可跑 final（见该行的"决定"列）。

| # | 问题 | 默认 | 影响 | 决定 |
|---|---|---|---|---|
| 1 | 轨道 B 开不开、什么时候开 | X1 读完后再定；老师写在计划文件里 | 预算 $0.8–1.3k 名义；发布要的"留出集上的可测增益"只有轨道 B 能给 | 按默认，2026-10-08 |
| 2 | test 复用规则 | sales、hr 的 test 留给 v2，v1 活动在这两个域不跑 final。finance（读过 1 次）、support（final 后读过 2 次）可以复用，条件是：构建学生的任何产物都没读过之前 final 的逐题记录（由结果库按 split 过滤，代码强制），报告里披露读取次数。marketing 读过 3 次，已用尽。operations 整域留出 | C5 的 test 池 | 按默认，2026-10-08；**2026-10-08 用户改为全部域可跑 final**（用户问"不能全跑吗？"，采纳）：finance、support、sales、hr、marketing 都可以跑密封 final，一律按有条件复用规则（构建学生的任何产物都不读之前 final 的逐题记录，由结果库按 split 过滤、代码强制；每份报告披露这份 test 被读过几次）；只有 operations 整域从未读过，留作 v2 的 LODO。取代默认里的"sales、hr 的 test 留给 v2"和"marketing 已用尽"。C5 的池随之是五个域的 test（123 题，§4.1）。marketing 的 `spent` 是 `[bench]` 的配置项，新活动的配置不列它即可 |
| 3 | SFT 目标 | `accepted`（PASS + REPLACE 的动作部分，同 HZ）；`pass_only` 作只看 dev 的消融 | 要改 M2 §2 第 5 步、DT6-01/10/12、09 的 DS6-06 | 按默认，2026-10-08 |
| 4 | 采集用的 harness | W0 加 K（W\* 文本），同 HZ | 要改 M2 §3a 第 2–3 步和 DT6-14 | 按默认，2026-10-08 |
| 5 | 跨域合并 | 5 个域合训一个学生，并报 LODO | HZ 是按域分别训练的 | 按默认，2026-10-08 |
| 6 | 学生底座和训练后端 | Qwen3.5-9B 加 Tinker LoRA 作参考实现；9B 是第一期链路验收的默认底座，K1 的"更大底座"是 Qwen3.8-27B（§4.2）。自托管训练不走 `module:Class`（它在控制平面进程里直接构造，没有作业、云上生命周期和价格上限），而是由 `JobTrainer` 运行的插件模块（写模块路径）。评测时大批量的学生推理走 RunPod 上自托管的 vLLM（合并权重，经 `openai_compat`）；Tinker 采样及其 OpenAI/Anthropic 兼容 beta 接口只作冒烟或兜底（2026-10-08 按 Tinker/RunPod 核价更正） | v2 不维护第二套生成训练栈 | 按默认，2026-10-08；自托管的接法和推理位置同日按核价更正 |
| 7 | 命名与搬家 | 在瘦身第一批之前定下来：子包叫 `ouroloop/train/`，extra 叫 `[train]`/`[train-jobs]`，入口是 `python -m ouroloop.train`。代码只搬一次 | 要改 SLIMMING §2.3、§3 第一批的命名 | 按默认，2026-10-08 |
| 8 | `contact_human` | 本基准上不作效果主张；真实标签来自 v2.5 的部署审批 | — | 按默认，2026-10-08 |
| 9 | 学生的思考档位（关，还是 low/medium） | 关（现行 §3.7） | 关：claude_code 给的老师轨迹没有思考内容，训练目标只有可见回合，训练与评测一致；但 27B 关思考按 AA 换算只有约 7%（估），卡在 K1 边上。low/medium：27B 约 18% / 24%（估），K1、K1b 大概率不触发，但训练目标里没有思考，训练与评测的格式不一致。要在 X1 计划冻结前定，可先看 §4.2 的 S0 小探针（2026-10-08 按 Tinker/RunPod 核价更正） | 待用户决定。注（2026-10-08）：用户对 actor 的 effort 说"不要，就保持统一的档位"（OSS_COMPONENTS_PLAN §9 第 4 项）。那一条管的是 v1 的 Opus actor，学生档位仍是本项、仍待定；但"同一比较里各臂用同一档位"的偏好在这里同样适用。§4.1、§4.2 的"S0 开、关思考各跑一次，取较好的一个作对照"与这一偏好有出入，随本项一起定 |

**核价时没能确认的事项**（只是待核实，不是决定；2026-10-08 按 Tinker/RunPod 核价更正）：

| 方面 | 没能确认 |
|---|---|
| Tinker | 新用户有没有免费额度；限流数值和训练耗时；训练费是否也算权重为 0 的 token（按算进去估，只算 assistant token 时训练费约为 1/4）；多轮推理时前缀缓存的实际命中率；OpenAI 兼容接口是否支持 `tools`；Qwen3.8-27B 能否用 `reasoning_effort` 设档位 |
| RunPod 和 vLLM | 吞吐、耗时和推理费用都是估算；Qwen3.5 在 vLLM 里的前缀缓存是实验功能；vLLM 能否以 LoRA 方式服务 Qwen3.5 的 adapter（线性注意力层、lm_head）；9B 沿用 35B/397B recipe 的启动参数是推断；RunPod 没有 spot 实例；jev 那次的 RunPod 花费没有发票 |
| 模型能力 | AA 是唯一来源，换算系数 0.6 是粗估；没有 9B 关思考时的 AutomationBench 数据；K2 Horizon 7B（AA 20.9%，不在 Tinker 上）的许可证没核实；27B 的费用都是粗估 |

**需要同步改的文档**（2026-10-08 定稿时同步。标"本轮已同步"的是这一轮改的文档；代码一律不改，等 support 两份 final 评完、H1 算完再动：注释随瘦身第一批，其余随 v2.0；2026-10-08：注释已随瘦身第一批改完）：
- **DESIGN**（本轮已同步）：不变量 6（ledger.py:19 引用它）、§4 的 controller 行、§8 的 C3 行（改为在学生上测）。
- **00_CONTRACTS**（本轮已同步）：约 :181 处"不能通过改 source 绕过 EVAL_ONLY"那段，改为血缘加选择。
- **02**（本轮已同步）：
  - :16 的旧规则；
  - §1a；
  - §2 第 5 步；
  - §3a；
  - DT6-01/07/10/12/13/14；
  - 写进 `[train.<role>]` 和 actor 产物契约。
- **09**（本轮已同步）：DS6-06；复核后另补 §1 的注（不做 `adapt_workflow`，DS6-01、DS6-09～11 不适用）。
- **TECHNOLOGY**（本轮已同步）：:32（底座与上下文长度改为 32k）、:61；复核后另补 :16（教师与适配一行）。
- **SLIMMING_INVENTORY**（本轮已同步）：
  - §0.1 第 5 点改为已决定（本轮）；
  - §0.2 的 14%/20% 改为 8.1%/16.0%，并注明两组分母不同（草案 `b58f5cc` 已改）；
  - 309/148 改为投影器的计数（同上）；
  - :167 那一条按本稿重写（同上）；
  - 第 10、12 项标为已决定（本轮）；
  - 按第 7 项改第一批的命名（本轮；全文的 `[decision]`/`[decision-jobs]` 一并改为 `[train]`/`[train-jobs]`）。
- **代码注释**（本轮不改）：providers/__init__.py:4、claude_code.py:235、anthropic.py:45、openai_compat.py:44、backends/llm.py:1-3、runtime.py:372、ledger.py:19-20 这 7 处注释随瘦身第一批（2026-10-08 已改，`41d702a`；现在的位置见 §2.5）；默认值随 v2.0 的血缘改动（§3.8 第一行），因为 ledger.py:143、:152 只收 SOURCES 里的来源（§2.5）。现在改任何 .py 都会改 `code_version`，从而改动正在跑的活动的 K。
- **ROADMAP**（本轮已同步）：v2 一节指向本稿。
- **复核后另补**（2026-10-08）：M3（03）§1 共适应注、M4（04）§3 材料准备、集成验收（08）IT6-01/38、modules/README 的 M2/M8 两行、TRAINING_HOSTS 页首、V0_4_PLAN 与 V0_5_METHOD_LIBRARY §7.4 里 sales/hr 的 test 计划，各加注；README 与代码随瘦身第一批。
- **核价更正待同步**（2026-10-08，本轮只登记、不改；2026-10-08 整理时已同步）：TECHNOLOGY :13、:42（自托管写成 `module:Class`；vLLM 命令只有 LoRA 参数）、:32（排除比例 0.7%）；TRAINING_HOSTS :3；02 :87、:92（HZ 配方 α 32、lr 2e-4；`module:Class`）、:100；ROADMAP :511；DESIGN :90、:213。
- **第 2 项改为全部域可跑 final**（2026-10-08，本轮已同步）：DESIGN §8 与 C4 段、ROADMAP（v0.4 读法、瘦身 (a)、轨道 B 的 C5、v2 决定第 2 项）、V0_4_PLAN 读法、V0_5_METHOD_LIBRARY §7.4、SLIMMING (a)、OSS_COMPONENTS_PLAN §9 第 2 项，各加注，原文保留。

**考虑过、不做：**

| 方案 | 不做的理由 |
|---|---|
| 在线逐步世界快照 | 重放复现率已有 98.2% |
| 在线分叉反事实 | 要付 actor 调用费；上游用 uuid4 生成 ID 是难点 |
| 拿 dev 记录做离线读数 | dev 只给在线 gate 用 |
| 写门的 `ask_human` 选项 | 本基准没有真值 |
| 为写门另立 gate 规则（去掉 `min_gain`） | 等于另起一套 gate；C3 改为测量臂，不必如此 |
| 在 Opus 上测 C3 | 完美写门的上限只有 train 8.1%、dev 16.0%（按失败数算），低于合并 dev 的检出能力 |
| 每个决策点一个模型 | DESIGN §4 规定最多一个 controller |
| 单独的 `ModelArtifact`/`CalibrationRecord`/`TrajectoryRecord` 类型 | 现有的 PolicyDescriptor、ModelIdentity 和 DataManifest 已经够用 |
| M8 的 `adapt_workflow` | K 直接用 W\* 原文 |
| 多老师投票、`mustnot_match` 打标器、逐轮 `replay_done` | 重放已经给出逐次写的效果；逐轮标签要等运行时 stop |
| 原文对象库 | 精确值脱敏加重放回填已经够用 |
| 在一个 cal 划分上认证 2% 的阈值 | 族数不够（需要 149 族） |

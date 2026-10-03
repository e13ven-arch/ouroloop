# 付费前彩排：fixture 世界与假 actor

> ROADMAP §3 首个真实活动协议第 2 步的规格。写于 2026-10-03，先于实现。彩排经 CLI 走生产路径，R 由 Claude 通过 manual provider 作答；它证明接线与故障协议，不证明任何模型的效果。

## 1. 边界

- 生产路径：`ouroloop freeze` 与 `ouroloop bench --phase cycle`。不注入研究员，不替换 `cycle_pairs`，不改任何框架代码。
- R 由 manual provider 作答。答题人（Claude）同时是这个世界的设计者，知道假 actor 认什么。因此：
  - 作答只依据请求文件里的内容（失败摘要、笔记、经验投影、当前提示）。
  - 彩排的任何结果都不说明 R 的能力，JOURNAL 照此标注。
- 记录不打 synthetic 标记。synthetic 由工作区名（带 rehearsal）、scope（`rehearsal`）与 JOURNAL 标注；这些报告不计入 C1～C4。

## 2. 任务世界

位置：`examples/rehearsal/upstream/`。目录结构照 `tests/fake_upstream` 的 AutomationBench 形状，只有 marketing 域。冻结后不再改动：改了就是上游漂移，启动检查会拒绝。

服务与端点（都在 `api_fetch` 后面）：

| 端点 | 作用 | 对应断言 |
|---|---|---|
| `PATCH https://api.hubapi.com/crm/v3/objects/contacts` `{"id","stage"}` | 改联系人阶段（每题的主步骤） | `hubspot_contact_stage` |
| `POST https://slack.com/api/chat.postMessage` `{"channel","text"}` | 收尾 A：在频道发一条消息 | `slack_message_contains` |
| `POST https://api.hubapi.com/crm/v3/objects/contacts/tags` `{"id","tag"}` | 收尾 B：给联系人打标签 | `hubspot_contact_tag` |
| `POST https://api.hubapi.com/crm/v3/objects/tasks` `{"contact_id","title"}` | 收尾 C：为联系人记一条跟进任务 | `hubspot_task_exists` |
| `PATCH https://api.hubapi.com/crm/v3/objects/contacts/owner` `{"id","owner"}` | 收尾 D：指定联系人负责人 | `hubspot_contact_owner` |

每题是一句请求：某联系人（名字与 id）发生了某事，把他移到某个阶段；收尾类的题在句末多一个收尾步骤。无收尾的题（P 类）W0 能做对。

任务（名称 `marketing.reh_NNN`；每题自成一个家族；`seed = 0`、默认比例 0.5/0.25/0.25 下的 hash 划分已核对）：

| 划分 | 行序中的任务 | 角色 |
|---|---|---|
| train | 000、001、002、004 | T_A、T_B、T_C、T_D：各类收尾的线索题，W0 都失败 |
| train | 005、006、008 | P；005 的请求要求先查一下联系人，假 actor 会调用一个未提供的工具 |
| dev 前一半 | 013、014、018、019、027、031 | I_A、I_B、I_C、I_D、P、P |
| dev 后一半 | 034、054、056、064、065、067 | O_A、O_B、O_C、O_D、P、P |
| test | 003、007 | P（cycle 不运行） |

由此：
- train 失败的题恰好 4 道（不超过 5）。dev 有 12 个家族，满足 gate 的 min_groups 5。W0 在 dev 上 4/12 通过。
- 研究位按冻结顺序对半分开发家族：前 6 个是两支的内层 gate，后 6 个是外层比较。每类收尾在两半各有一题，所以 segment-0 之后剩下的缺口两半都有。
- gate 算术：修好一类收尾，主线 gate 增益 2/12，内层 gate 与外层比较增益都是 1/6，都高于 0.05；P 题不受任何规则影响，不会退步。
- 第一道 dev 题（I_A）是单轮请求，无关的 Slack 消息改变不了它的分数，满足启动时的评分契约检查。

## 3. 假 actor

位置：`examples/rehearsal/rehearsal_actor.py`，`[llm] type = "rehearsal_actor:RehearsalActor"`，运行 CLI 时用 `PYTHONPATH` 指到 `examples/rehearsal`。

- 确定性：输出只取决于 `(system, messages, tools)`，不读文件、时间或随机数。tool call 的 id 按轮次与序号生成。
- usage：按输入字符数的 1/4 估输入 token，输出 token 固定为 30，都远低于 `[pricing]` 的上限。
- 不抛异常：读不懂的请求也以一条结束消息收尾。

每次尝试的步骤：
1. 系统提示里列有技能且提供了 `read_skill` 时，先读完每一个还没读过的技能。
2. 用 PATCH 把联系人移到请求要的阶段。
3. 请求有收尾步骤、且识别到对应规则时，调用该收尾的端点。
4. 结束消息：
   - 做完时写 `Done: ...`。
   - 跳过收尾时写明跳过了什么：`Not done: the closing step at the end of the request, <这一类的描述>; nothing in my instructions covers closing steps of this kind.`。这句线索进入失败摘要，经逐题笔记交给 R。
5. 收到收尾推回（结尾是 `Keep working on the task, or explain why it is already done.` 的用户消息）时：推回文字识别出本题收尾的规则，就补做收尾再结束；否则重复结束消息。

规则识别（不区分大小写）：

| 类 | 收尾 | 识别用的正则 | 在哪里识别 |
|---|---|---|---|
| A | 频道消息 | `(post\|message)[^.\n]{0,80}\b(slack\|channel)\b` 或其反序 | system 全文、读到的技能正文、推回 |
| B | 打标签 | `\btag(s\|ging\|ged)?\b` | 同上 |
| C | 跟进任务 | `follow[- ]?up tasks?` | **只认推回**（on_finish hook 的 block 或 feedback） |
| D | 负责人 | `\bowners?\b` | system 全文、读到的技能正文、推回 |

- system 全文包括上游系统提示、W 的提示与包里的 memory 和技能索引。上游系统提示与 W0 的 `FINISH_CHECK` 都不含这些词，W0 因此没有规则。
- 请求本身（用户消息）不参与识别。
- C 只认推回，所以写在 memory 或技能里的 C 规则不起作用，只有收尾检查（on_finish hook）能修好 C。这是让彩排覆盖 Docker 草稿检查链与 hook 执行事件的设计，也给 R 的方法比较留了真实差异的可能：R0 的 propose 提示把 memory 与技能排在收尾检查之前。

## 4. 预期走向（不是验收条件）

- 设 `[harness_evolution] max_failure_notes = 1`。每轮只读一题的笔记，按 train 行序轮换，segment-0 至多看到 T_A、T_B、T_C，T_D 一定留到研究位之后。
- 按 R0 的偏好作答时，大致是：
  - segment-0：round 0 修 A（memory），round 1 修 B（技能），round 2 为 C 写 memory，gate 拒绝。
  - 研究位：R0 从经验里看到 C 的笔记被拒，可能提出“笔记被拒后改用收尾检查”的 R1。
  - 两支：都从 W_AB 出发，失败的是 T_C、T_D。哪一支修好 C，取决于各自的提示把答题人引向哪种组件。
  - 外测：比较两支在 O_C、O_D 上的差异。
  - segment-1：在 R1（若装入）下修 D 与 C。
- 实际走向取决于作答，如实记录。两支 W* 相同、或研究裁决不是 accepted 时，外测、补跑或 R1 回用若没有覆盖，记为覆盖缺口。

## 5. 配置与运行

- 配置：`examples/rehearsal/rehearsal.toml`。
  - `[bench] domain = "marketing"`、`upstream_path = "upstream"`；`[bench.budget]` 三项单次尝试上限。
  - `[llm]` 指向假 actor，不带 `max_tokens`。`[research.llm] type = "manual"`，`dir` 用相对路径，所以 CLI 必须在 `examples/rehearsal/` 下运行；表里不带 `max_tokens`。
  - `[pricing]` 用有界的占位价格，`source` 写明不是价格。
  - `[improvement]` 用 `marketing_harness_v1`；`[harness_evolution] max_failure_notes = 1`。
  - `[rsi] scope_id = "rehearsal"`；`[freeze.budget] usd = 300`，其中 `[freeze.budget.final] usd = 50`。
- 运行目录 `examples/rehearsal/` 下没有 `.env`，CLI 不会读到任何 key。
- 顺序：
  1. `ouroloop freeze`，把输出的三个 ref 写进 `[rsi]`；
  2. `--until baseline`，核对 K1；
  3. `--until segment-0/round-0`，核对 K2；
  4. `--until segment-0`，核对 K3；
  5. 复制工作区（研究位重跑与第二工作区都从这份副本出发）；
  6. 去掉停止点跑完。
- 作答：`examples/rehearsal/answer.py` 列出待答请求、显示请求内容、原子地写回复文件。回复正文由答题人按请求写，脚本只负责格式与写入。

## 6. 故障注入

| 注入 | 在哪里做 |
|---|---|
| 某一步 `stop = "max_tokens"` | 主工作区 segment-0 round 0 的笔记：暂停，续跑重问，账本每次调用只记一次 |
| 笔记空回复、非 JSON、缺字段 | 第二工作区（`max_failure_notes = 2`）：一条读不出时本轮继续；主工作区一条也读不出时本轮 invalid |
| 汇总空回复、拒答、非 JSON、形状非法 | 主工作区 segment-1，或第二工作区的 segment-0 |
| 修订空回复或读不出 | 写 hook 的那一轮：先交一份在 Docker 中首检失败的代码草稿，第一次修订交空回复，第二次交改好的补丁 |
| 研究位读不出、拒答或形状非法 | 第二工作区的研究位（研究位只有一次） |
| manual 判为非法的回复（带 tool_calls） | 最后做，它会留下一笔 unknown 预留 |
| 假 actor 调用未提供的工具 | 内建在 train 题 005 |

第二工作区独立 bootstrap（`max_failure_notes = 2` 要在 bootstrap 前定），用同一个上游与 actor，`scope_id = "rehearsal-faults"`，跑到研究位为止。

## 7. 检查点与产物

- K1（baseline 后）：19 次尝试全部有效（7 train + 12 dev）；open 与 unknown 预留为 0；W0 的 dev 通过率 4/12。
- K2、K3 按 ROADMAP §3 第 6 步核对。K3a 中的技能索引与 `read_skill`、hook 启用与 worker 会话，在装入相应组件的下一轮核对。
- 产物：
  - manual 的请求与回复按 id 成对存档，作为回放 fixture；
  - 每个工作区的 `cycle.json`、报告与预算账本；
  - JOURNAL 条目：逐项写明通过条件与覆盖缺口。

## 8. 离线检验

实现时同时写 `tests/test_rehearsal_world.py`：
- 上游能被 adapter 读入，划分与行序和第 2 节一致；
- W0 只在 P 题上成功；每类规则只翻转本类的题，不影响其他题；C 只认推回；
- 第一道 dev 题通过评分契约检查；
- actor 对同一输入输出相同；
- 用脚本研究员按第 4 节的走向跑一遍 cycle，核对 segment-0 装入、研究位两支 W* 不同、外测运行。这只是离线演练，不替代经 CLI 的彩排。

## 9. 结果（2026-10-03）

三个工作区都经 CLI 运行，都没有注入研究员。请求与回复按 id 成对存档在 `examples/rehearsal/archive/`；actor 检查用的是真实上游的题目文本，不入库。

作答依据请求文件，有两处例外（事后核对发现）：
- 主工作区研究位的回复引用了 C 笔记的放弃条件。这一条不在请求的经验投影里，来自答题人自己在 segment-0 round 2 的回复。
- 故障工作区研究位的回复复用了主工作区研究位的正文，只把 `proposals` 改成对象。其中“两条笔记装入、第三条被拒”与该工作区的经验（1 次装入、2 轮 invalid）不符。这条回复只用来检验形状，正文没有被使用。

- **主工作区**（`workspace-rehearsal`，scope `rehearsal`），R 作答 22 次：
  - baseline：19 次尝试全部有效；open 与 unknown 预留为 0；W0 dev 4/12。K1 通过。
  - segment-0：
    - round 0 先注入截断：暂停，续跑时重问同一步，两次调用各记一次账。然后装入 A 的 memory，gate 12 对完整，增益 2/12，无退步。K2 通过。
    - round 1 并入 B 的规则：装入。round 2 并入 C 的规则：gate 拒绝（增益 0）。
    - K3a：每次装入后下一轮都用新包测量，T_A、T_B 随之通过。
    - 免费 A/A：基线 dev 与 round 0 的对照臂都是 4/12，结果一致。
  - 研究位：
    - R0 从经验里读到 C 的笔记被拒，提出 R1：“同一失败的笔记或技能已被拒且无增益时，改提收尾检查”。
    - 公共测量后笔记共享；外测与两支份额都预留后又释放。
    - 对照支（R0）：round 0 为 C 写技能，被拒；round 1 装入 D 的 memory。
    - 候选支（R1）：round 0 为 C 写 on_finish hook。首稿按故障表故意多一行 `import requests`，在 Docker 的 isolated_probe 失败；第一次修订注入空回复，占用一次修订；第二次修订通过。装入后重测的两道失败训练题（T_C、T_D）都通过：hook 对任何 Not done 都推回，D 也一并修好。round 1 回复 stop。
    - 外测：两支 W* 不同，6 题配对完整，候选 6/6 对照 5/6，裁决 accepted，R1 装入。
  - segment-1（R1）：
    - round 0 装入 D 的 memory。segment-1 结束后，回用检查 verified：3 条 research_steps 记录加载 R1，计 5 个请求（ProviderError 那次调用没有记录；segment-1 实际向 R 发了 6 个请求）。
    - round 1 的汇总注入非 JSON：本轮 invalid。
    - round 2 的汇总注入 manual 非法回复：ProviderError，暂停，留下一笔 unknown 预留。续跑后改提 hook，装入，dev 12/12，以 rounds_done 结束。
  - 报告：C1 inconclusive（开发记录，不是正式比较）；C4 inconclusive（accepted，但只有 1 个独立组，不显著：外测区间退化为单点 1/6，reliable=false）。报告 detail 当时写“区间含 0”，与收据不符，已修（见下）。K3b：on_finish 18 次 continue、4 次 block，worker 会话 18 次，技能读取 0，包工具 0。
- **故障工作区**（`workspace-rehearsal-faults`，`max_failure_notes = 2`），R 作答 9 次：
  - 两条笔记中一条注入非 JSON：记入 coverage.invalid（含原因），本轮照常汇总，并装入。
  - 汇总拒答：本轮 invalid。汇总带不合法提案（没有 changes）：本轮 invalid。
  - 研究位的 `proposals` 写成单个对象：研究位 invalid（stage=reply）；C4 写明研究位回复无效。再次运行不重问：没有新请求，账本不变；这次重跑没有留日志。
- **actor 检查**（`workspace-actor-manual`，真实 Marketing 上游，`--phase baseline --limit 1`）：
  - Claude 作为被 pin 的 manual actor，作答 5 个回合，含并行的工具调用与读表。
  - dev 与 train 各一次尝试，都有效。按设计没有做完，都以 task_outcome 失败。
  - 运行身份记的是 manual actor。
- **通过条件**：
  - 笔记 → 汇总 → 草稿检查 → 准入 → 构建 → gate 裁决：多次走通。
  - Docker 草稿检查链：含 hook 的草稿走完检查链，结果 ready；首检失败的代码草稿覆盖了修订回路；gate 候选臂的 transcript 中有 hook 执行事件。
  - 安装且下一轮满足 K3a；研究位走到裁决（accepted），下一段核验 R1 被加载。
  - 故障注入表每行至少注入一种情形，结果都符合预期；每次暂停后续跑，都没有重复记账。
- **覆盖缺口**：
  - 没有装入技能：技能索引与 `read_skill` 只在对照支被拒的那次 gate 里出现过。
  - 没有包工具。
  - 外测没有基础设施失败，补跑没有在 CLI 路径上发生。
  - 两支份额占不住的路径没有发生。
  - 故障表中没有注入的情形：笔记全部读不出时本轮 invalid（§6 原计划在主工作区做）、笔记空回复与缺字段、汇总空回复、修订读不出与修订用尽。
  - 这几项都只有离线测试。
- **找到并已修**：
  - 研究位的 R 看到的 budget 是 `{}`（`ffe3aff`）；
  - 拒答的原因把“declined the request”写了两遍（`ffe3aff`）；
  - manual 回复被截断时，消息写成“(None tokens)”（`ffe3aff`）；
  - 研究裁决 accepted 而不显著时，收据一律写“区间含 0”；只有 1 个独立组时区间并不含 0，现在写明独立组不足 3 个（`test_campaign.py::test_an_acceptance_says_why_it_is_not_significant`）。

# 付费前彩排：fixture 世界与假 actor

> ROADMAP §3 首个真实活动协议第 2 步的规格。写于 2026-10-03，先于实现。彩排经 CLI 走生产路径，R 由 Claude 通过 manual provider 作答；它证明接线与故障协议，不证明任何模型的效果。

## 1. 边界

- 生产路径：`ouroloop freeze` 与 `ouroloop bench --phase cycle`。不注入研究员，不替换 `cycle_pairs`，不改任何框架代码。
- R 由 manual provider 作答。答题人（Claude）同时是这个世界的设计者，知道假 actor 认什么。因此：
  - 作答只依据请求文件里的内容（失败摘要、笔记、经验投影、当前提示）。
  - 彩排的任何结果都不说明 R 的能力，JOURNAL 照此标注。
- 记录不打 synthetic 标记。synthetic 由工作区名（带 rehearsal）、scope（`rehearsal`）与 JOURNAL 标注；这些报告不计入 C1～C4。

## 2. 任务世界

位置：`examples/rehearsal/upstream/`。本节是第一次彩排时的世界；第二次彩排增补了两类收尾并调整了行序，见 §10.1。目录结构照 `tests/fake_upstream` 的 AutomationBench 形状，只有 marketing 域。冻结后不再改动：改了就是上游漂移，启动检查会拒绝。

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

## 10. 第二次彩排（规格，2026-10-03）

目的：补上第一次的三处覆盖缺口——装入技能、装入并执行包工具、在 CLI 路径上发生补跑，并顺带注入故障表里第一次没有注入的情形。世界、actor 与配置都在第一次的基础上增补；上游版本改为 `0.0.0-rehearsal.2`，第一次彩排对应提交 `5a6c7cb` 的上游。

### 10.1 世界增补

新增两类收尾，各有一道 train 题，在开发顺序的两半各有一道 dev 题：

| 类 | 收尾 | 端点 | 断言 | train | dev 前一半 | dev 后一半 |
|---|---|---|---|---|---|---|
| E | 把联系人加入邮件列表 | `POST https://api.hubapi.com/crm/v3/lists/members` `{"contact_id","list"}` | `hubspot_list_member` | 010 | 071 | 076 |
| F | 给联系人加一条通话备注 | `POST https://api.hubapi.com/crm/v3/objects/contacts/notes` `{"id","note_b64"}`，备注须 base64url 编码；给明文时返回 400 | `hubspot_note_exists` | 011 | 072 | 080 |

行序：
- train 依次为 T_E、T_F、T_C、T_A、T_B、T_D，再接 3 道 P 题。
- dev 前一半依次为 I_A、I_B、I_C、I_D、I_E、I_F、P、P；后一半依次为 O_A～O_F、P、P。

由此 train 失败 6 题（超出第 2 节“不超过 5”的规模，这次需要多两类），dev 16 题，W0 在 dev 上 4/16。主线 gate 修好一类的增益是 2/16，内层 gate 与外测是 1/8。

### 10.2 actor 增补

- **E 只认技能。** 规则 `mailing lists?` 只在读到的技能正文里识别，memory、W 的提示与推回都不算。跳过时的线索：`...adding the contact to the mailing list; I follow list sign-ups only from a skill that describes them, and none does.`
- **F 只认包工具。** 若 tools 里有一个包工具，其名字或描述含 `note`、输入 schema 有 `contact_id` 与 `text` 两个属性，就以这两个参数调用它。否则先用明文调一次备注端点，得到 400，再结束。线索：`...adding the call note; the notes endpoint wants the note encoded, and none of my tools adds a note to a contact from its plain text (contact_id, text).`
- **补跑注入。**
  - `[llm]` 可带 `fault_dir`。目录里有 `infra_once` 文件时，actor 在一次尝试的第一轮读出文件中的文字；请求含这段文字，就删掉文件，并以 `ProviderError(retryable=True, unbilled=True)` 失败一次。
  - 这一次尝试记为基础设施失败。补跑时文件已不在，尝试照常进行。
  - 文件由操作者（答题人）在需要的时刻写入，属于故障注入。

### 10.3 运行

- 配置 `examples/rehearsal/rehearsal2.toml`：
  - 工作区 `workspace-rehearsal2`，scope `rehearsal-2`；
  - `fault_dir = "workspace-rehearsal2/actor-faults"`；
  - 其余同第一次，仍是 `max_failure_notes = 1`。
- 预期走向（不是验收条件）：
  - segment-0：round 0 看到 T_E，round 1 看到 T_F，round 2 看到 T_C。
  - 研究位：R0 按经验提出 R1。
  - 两支都从 W_EF 出发，先看到 T_C，再看到 T_A。
  - segment-1：在 R1 下进行。
- 注入计划：
  - segment-0 round 0 的汇总作答前，写入 `infra_once`，内容是 dev 后一半一道 P 题的文字。gate 的候选臂在这道题上失败一次，按 retry_policy 补跑。
  - 两支最后一次作答前，再写一次。外测在这道题上失败一次并补跑。
  - 对照支 round 1 的汇总注入空回复，本轮 invalid（汇总空回复）。
  - segment-1 round 0 的笔记缺 `mechanism`：笔记读不出，本轮只有这一条，因此本轮 invalid，不调用汇总（笔记缺字段，以及笔记全部读不出）。
  - segment-1 round 2 写 hook 时，首稿在 Docker 中失败，修订依次交非 JSON、仍失败的补丁、仍失败的补丁。修订用尽，本轮 invalid（修订读不出，修订用尽）。
- 通过条件：
  - 技能：装入技能的下一轮，系统提示有技能索引、tools 有 `read_skill`，transcript 中有 `read_skill` 调用。
  - 包工具：
    - 含包工具的草稿在 Docker 中走完检查链，结果为 ready；
    - gate 候选臂的 transcript 中有 `package_tool` 事件，且有经 broker 的基础工具调用；
    - 装入后下一轮的 tools 中有这个包工具。
  - 补跑：
    - 主线 gate 与外测各有一次基础设施失败的尝试，各被补跑一次；
    - 补跑记 supersedes，原尝试保留；
    - 配对完整，裁决不因此变成 inconclusive。
  - 上面列出的故障情形逐项得到预期。

## 11. 第二次彩排的结果（2026-10-03）

工作区 `workspace-rehearsal2`（scope `rehearsal-2`），经 CLI 运行，没有注入研究员。R 作答 21 次，请求与回复存档在 `examples/rehearsal/archive/2026-10-03-second/`。

作答依据请求文件，有三处例外（事后核对发现），其中前两处决定了工具与 hook 能否生效：
- segment-0 round 1 的汇总回复写了包工具，用到了备注端点的 URL、字段名 `id` 与 `note_b64`、基础工具名 `api_fetch` 及其 `method/url/body` 参数。汇总请求里都没有这些（请求里的笔记只说经基础 API 工具发到备注端点、要 base64url 编码），它们只出现在同一轮的笔记请求里。
- 候选支 round 0 的汇总回复写了 on_finish hook，按字面串 `Not done:` 判断，并把其后的原话推回。汇总请求里的笔记只转述了 actor 的结束消息，没有这个字面串；它只出现在笔记请求里 actor 的原话中。外测 8/8 对 4/8 与 accepted 裁决都建立在这个 hook 上。
- 研究位的回复逐字复用了第一次彩排的 R1 正文，其中“(an on_finish hook)”不在该请求里。

由此看到的设计问题：汇总请求只有笔记、包与 targets，没有失败摘要，也没有基础工具的名称与参数。真实 R 要写包工具或 hook，所需的接口细节只能由笔记带过去，而 R0 的 diagnose 提示没有要求笔记带这些。已记入 ROADMAP §2。

- **baseline**：25 次尝试全部有效；open 与 unknown 预留为 0；W0 dev 4/16。K1 通过。
- **segment-0**：
  - round 0：
    - 汇总作答前写入 `infra_once`（目标是 dev 后一半的 P 题 065）。
    - 装入 E 的技能。gate 候选臂在 065 上的 `subject#0` 记为基础设施失败，`subject#1` 补跑有效，supersedes 指向原尝试，配对完整。
  - round 1：
    - 装入 F 的包工具 `add_contact_note`：用标准库编码明文，经 broker 调用 `api_fetch`，带包测试；草稿在 Docker 中走完检查链。
    - gate 候选臂的 transcript 里有 2 个 `package_tool` 事件，各带一次 `api_fetch` broker 调用，另有 2 个 worker 会话。
    - 这一轮的测量中，actor 读了 6 次技能：装入技能后的 K3a。
  - round 2：为 C 写 memory，gate 拒绝。这一轮的测量中，actor 拿到并调用了 `add_contact_note`：装入包工具后的 K3a。
- **研究位**：
  - 请求带出了剩余预算（remaining $249.80；研究位自身尚无用量，used 为空），`ffe3aff` 的修正生效。
  - R0 的理由依据请求里的经验（技能与工具被接受，C 的笔记被拒、增益 0）；R1 正文逐字复用第一次彩排的回复（见上方例外）。
- **两支**：
  - 对照支（R0）：round 0 为 C 写技能，被拒；round 1 的汇总注入空回复，本轮 invalid（汇总空回复）。没有装入，W* 是起点。
  - 候选支（R1）：round 0 为 C 写 on_finish hook，装入；round 1 没有失败，回复 stop。作答前再次写入 `infra_once`。
- **外测**：
  - 对照 W* 在 065 上的 `outer_control#0` 记为基础设施失败，`outer_control#1` 补跑有效，supersedes 指向原尝试。
  - 8 题配对完整，候选 8/8 对照 4/8，裁决 accepted，R1 装入。
- **segment-1（R1）**：
  - round 0：笔记缺 `mechanism`（也缺 `generality`），读不出。本轮只有这一条，因此 invalid，不调用汇总（笔记缺字段，以及笔记全部读不出）。
  - round 1：装入 B 的 memory。
  - round 2：
    - 轮换到 T_D，不是 §10.3 注入计划设想的为 C 写 hook。按 R1 先写笔记，所以首稿的失败是形状检查（技能文件没有列入 manifest），不在 Docker 中；Docker 中的修订回路已在第一次彩排覆盖。
    - 第一次修订注入非 JSON，占用一次修订；后两次仍交失败的补丁。修订用尽，本轮 invalid。
  - 以 rounds_done 结束。回用检查 verified：计入 2 轮（round 1、2 的汇总由 R1 的正文生成），4 个请求。round 0 只有笔记、没有汇总；round 2 的 3 次修订记为 revise 阶段，都不计入。segment-1 实际在 R1 下向 R 发了 8 个请求。
- **报告**：
  - C1 inconclusive；C4 inconclusive，detail 按修正后的措辞写明“1 个独立组，不足 3 个”。
  - 行为（报告按主工作区的 186 次会话统计，含主线与外测，不含两支内部的 63 次会话）：技能读取 143 次；on_finish 4 次 block、8 次 continue（都在外测候选臂）；包工具调用 13 次；worker 会话 20 次。
- **账本**：settled $0.41；released 5 笔：3 笔外测与两支的份额预留，2 笔注入的基础设施失败（unbilled，没有用量）；没有 unknown。
- **覆盖**：
  - 第一次的三处缺口都已覆盖：技能、包工具、补跑（主线 gate 与外测各一次）。
  - 故障表第一次没有注入的情形，这次覆盖了：笔记缺字段、笔记全部读不出、汇总空回复、修订读不出、修订用尽。
  - 仍只有离线测试：笔记空回复（与其他读不出的笔记同一路径）；两支份额占不住。

## 12. 第三次彩排：基础工具清单（规格，2026-10-03）

目的：检验方案 2 之后，R 能否只凭请求内容写出有效的包工具与 hook。方案 2 在汇总与修订请求里加入基础工具清单，见 ROADMAP §2 与 M4 §1。

- 世界与 actor：同第二版，不改。新开工作区 `workspace-rehearsal3`，scope `rehearsal-3`，`max_failure_notes = 1`，不注入故障。
- 范围：baseline → segment-0 → 研究位与两支，用 `--until slot-0` 停在研究位之后。
- 作答纪律，这次逐条遵守，结束后由独立核对员逐条检查：
  - 逐题笔记只写本条请求里有的内容。失败过程中出现的端点、方法、字段名、错误原文与 actor 结束消息的原话，凡是改动会用到的，写进笔记（root_cause 或 mechanism）。
  - 汇总只用汇总请求里的内容：笔记、包、targets、`tools`、经验投影，以及系统提示里的格式与包 API。
  - 研究位的回复只用研究位请求里的内容，不复用以前的回复。
- 通过条件：
  - 汇总请求与修订请求带有 `tools`，列出 `api_search`、`api_fetch`、`base64_encode` 及其参数。
  - segment-0 中为 F 写的包工具通过检查链，被 gate 接受，并在下一轮被调用。
  - 候选支为 C 写的 on_finish hook 被接受，外测运行。
  - 核对员确认每条回复都只用了本条请求里的内容。若仍有例外，如实记录，并说明方案 2 还缺什么。

## 13. 第三次彩排的结果（2026-10-03）

工作区 `workspace-rehearsal3`（scope `rehearsal-3`），经 CLI 运行到 `--until slot-0`。R 作答 13 次，存档在 `examples/rehearsal/archive/2026-10-03-third/`。

- 汇总请求带有 `tools`（`api_search`、`api_fetch`、`base64_encode` 及其参数）。
- segment-0：
  - round 0 装入 E 的技能。
  - round 1 装入 F 的包工具，下一轮的测量中它被调用。
    - 笔记从本条失败过程里写下了端点（POST 与 URL）、被拒的请求体、400 错误原文（`note_b64`，base64url）与 actor 要的接口 `(contact_id, text)`。
    - 汇总只凭笔记与 `tools` 写出工具：先用 `base64_encode` 编码，再用 `api_fetch` 发出 `{id, note_b64}`。
  - round 2 为 C 写 memory，gate 拒绝。
- 研究位：R1 只用请求里的词（“a check before finishing”“final message”），没有出现请求之外的字符串。
- 两支：
  - 对照支（R0）：C 的技能被拒，A 的 memory 装入。
  - 候选支（R1）：凭共享笔记里引用的 actor 原话（`Not done:`）与系统提示里的 on_finish 接口写出 hook，装入；round 1 没有失败，回复 stop。
- 外测：候选 8/8、对照 5/8，裁决 accepted，R1 装入。
- 独立核对：13 条回复都没有用到本条请求之外的世界事实（URL、字段名、工具名、参数、标记、错误原文）；§11 的三处问题都没有再出现。余下的问题：
  - 两份代码草稿的测试按文件路径加载包代码，这一点包 API 没有写，写法沿用了前两次彩排。已在包 API 里补上：测试按路径加载，包根不在 sys.path 上（`test_harness_loop.py::test_the_package_api_says_how_a_test_reaches_package_code`）。
  - 研究位的 R1 是前两次正文的改写，不是逐字复用，但子句顺序与部分措辞相同。
  - 六条汇总的 abandon_if 用了请求里没有的“gate”一词，无关接口。
- 结论：
  - 方案 2 加上“笔记写下本条失败过程里的接口细节”，足以只凭请求写出有效的包工具与 hook。
  - 但笔记写下这些细节，是因为这次的作答纪律要求；R0 的 diagnose 提示并没有要求。真实模型若只转述（例如“端点要编码后的备注”），汇总就拿不到 URL、字段名或 `Not done:` 这样的标记。见 ROADMAP §2。
  - 这次只覆盖了容易的情形：actor 自己的失败调用正好打到了正确的端点，错误原文也给出了字段名。若一类收尾的端点从未被调用过，工具只能在运行时经 `api_search` 去找，这条路径没有被检验。

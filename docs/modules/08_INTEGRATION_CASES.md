# 集成验收：从任务反馈到递归回用

> 开发交付的验收规格，不是已通过的测试结果。以离线夹具证明工程属性，以真实冻结实验证明收益，两类记录分开保存。

## 1. 公共夹具

复用 `tests/test_automationbench.py` 的 fake upstream 结构；新增若干彼此独立、显式 family 的小业务任务：读取记录并更新、更新后发通知、无需变更的合法 no-op、重复发送产生回归。另有与训练任务不同的 dev/final 内容，包含任务尾部重要约束。fixture 有官方接口形状的 scorer、合法工具、可快照世界；隐藏断言只在 evaluator。任务数足以验证分组逻辑，不暗示具有真实统计功效。

脚本 worker/研究员根据实际输入、工具状态与步骤响应，而非看到 candidate 名字就给成功。训练器可用已有 fake backend 产出可加载、真正读取 stop 输入的有限决策器；其性能是夹具定义，不是训练研究结果。每个 fixture 报告必须标 `synthetic_contract_test`，正式报告拒绝混入。

## 2. 集成场景

### IT-01：不训练也能完成任务改进

Given W0 在“更新后通知”任务漏通知，轨迹保留实际工具返回；R0 收到 train/dev 的 Requirement。

When R0 提出 worker.finish prompt，完整构建、screen、dev 配对、gate、安装，随后在新 session 执行。

Then 新 session 使用 W1 的实际 prompt；该任务完成，其他任务的回归被检查；数据、gate 和安装引用相连，train_jobs=0。它证明完整框架可优化流程，不预设小模型必胜。覆盖 PR-01、RL-05、ER-01、EX-03、BR-08。

### IT-02：训练得到的决策器确实影响执行

Given train 中“未完成/已完成”的决策快照，任务家族在 fit/validation 与外层 dev 间分离。

When 构建版本化数据集、提交 fake train job、加载模型，完成同最终 prompt 的默认/模型完整任务配对并安装。

Then no 标签对应的输入使 StopGuard 实际继续，yes 使其结束；数据中没有 oracle，模型与输入 schema 相符；训练、推理、额外工具和失败作业均计入费用。真实实验另以同样接口训练真实小模型，再决定 C3 是否成立。覆盖 DT-01～06、PR-02～04、ER-05。

### IT-03：真正回用更新后的研究方法

Given 研究日志暴露“输入缺字段仍反复训练”，R0 根据这些事实提出 research.preflight 的已注册检查配置。

When R0/R1 在同 W/E/预算的隔离 campaigns 中改 W，经固定 research gate 接受 R1，安装并执行下一 worker round。

Then R1 的检查真实运行，产生并评测 W2；R1 artifact、调用 trace、W2 来源相连。两支不能读对方新经验，分支 W* 不覆盖主线，只有 R 更新。fixture 记录 scripted 发现；正式自动递归验收必须由真实研究模型根据事实提出改动，并另报 C4。覆盖 PR-06、RL-07、RC-01～10。

### IT-04：完整安装的崩溃恢复矩阵

依次在 build 完成、accepted、preflight 后、pointer 提交前、pointer 提交后但 receipt 前注入崩溃。恢复后 artifact 不重复生成，未知外部调用不重复视为免费；active 始终是完整合法 release。CAS 冲突为 stale_parent，不自动混装。覆盖 CT-05、DT-08、ER-06～10、EX-02/05。

### IT-05：阻止结果与权限污染

依次尝试：dev probe、final 衍生摘要进训练、research 改 scorer、rule 执行 Python、读取另一 arm 的 E、把候选异常报成可无限重试的 infra。

每次必须在拥有该边界的模块拒绝并记录原因；无模型训练/安装副作用，已有合法运行仍可继续。不是让 LLM 回答“我不会这么做”。覆盖 CT-03/04、DT-05/07、PR-07、ER-03、RC-03/04。

### IT-06：严格总预算与未知收费

两支研究各预留子预算，中途发生 provider 重试、训练失败、响应丢失；故意让剩余预算只够构建不够验证。

断言预算按叶子实际操作汇总，父子不双计、不重置；unknown 留占用，不能启动不可完成验证的候选；最终报告包含无效支出。覆盖 EX-01～08、RC-06、BR-04。

### IT-07：最终报告不能反向选冠军

锁定 base、prompt_best、evolved、两家 frontier 和可选 C4，运行部分 final 后中断再恢复。让 evolved 在 fixture final 中退步。

断言不改候选、不删合法失败、不按 final 选较早代；报告 C1/C3 未支持、其他主张各自状态。重新 report 数值一致，不触发网络调用。覆盖 BR-02～08、RC-09。

## 3. 从工程验收到真实效果

| 层次 | 完成条件 | 不能推出 |
|---|---|---|
| 模块验收 | 相关用例和既有回归测试通过 | 业务收益 |
| 工程闭环 | IT-01～07、跨重启/回滚，真实路径而非名字切分 | 真实模型能自动找到好策略 |
| 自动递归实例 | 真实 R 从经验提出研究候选，比较/安装/实际回用 | 普遍、自加速或无限改进 |
| 任务效果 | 冻结独立任务比较 C1/C2；C3 按需 | R 本身变强 |
| 研究效果 | 独立 campaign 的 C4 支持 | 各业务域均有效 |

后续实现 agent 应在各模块文件末尾补实际测试命令、结果和已知缺项，最后汇总到 ROADMAP/JOURNAL。文档里的 use case 不能直接打勾当通过，不能把历史的 382 项测试写成新架构已验证。

# 模块设计文档（阶段 1 的产物）

> 2026-10-09。按 [DESIGN v1.1](../DESIGN.md) §9 的布局，每个包一份设计文档：职责、不做、数据与文件、接口（签名）、不变量与错误、与现有代码的对应、必须有的测试、开发顺序。开发按这些文档做；文档与代码冲突时先改文档。

| 文档 | 包 | 依赖（只能 import 谁） | 阶段 |
|---|---|---|---|
| [CONTRACTS](CONTRACTS.md) | 公共契约：对象与引用、工作区、K、release、记录、错误、测试约定 | — | 1 |
| [HARNESS](HARNESS.md) | `reharness/harness/`：包模型、组件类型、种子与 vendor、字面量、包侧契约（ctx、hook、工具、agent 入口） | contracts | 1 |
| [RUNTIME](RUNTIME.md) | `reharness/runtime/`：网关、broker、worker 与运行器协议、egress、运行壳、导出的 App | contracts、harness | 1 |
| [EVALUATE](EVALUATE.md) | `reharness/evaluate/`：划分、硬限额、尝试、配对 gate、final 封存、发布与 model 绑定 | contracts、harness、runtime | 1 |
| [ACTIVITY](ACTIVITY.md) | `reharness/activity/`：配置与 preset → K、工作区、阶段编排、预算根、启动检查、报告、CLI | 以上全部 + memory、improver | 1 |
| [MEMORY](MEMORY.md) | `reharness/memory/`：sidecar、规则、写入步、检索与 `recall`、快照、升级 | contracts、harness、runtime | 2 |
| [IMPROVER](IMPROVER.md) | `reharness/improver/`：证据、诊断、分层、编码会话、验收阶梯、筛选、档案、记功、台账、策略 | contracts、harness、runtime、evaluate、memory | 3 |
| [V2_TRAINING_PIPELINE](V2_TRAINING_PIPELINE.md) | `reharness/train/`：训练链（已定稿，不改） | contracts、evaluate | 5 |

阅读顺序：CONTRACTS → HARNESS → RUNTIME → EVALUATE → ACTIVITY → MEMORY → IMPROVER。

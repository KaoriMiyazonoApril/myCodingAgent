# 03: 收窄 TaskState 并为单次 ToolResult 建立硬边界

**What to build:** 多步骤 Turn 可通过窄 update_plan 工具维护有界计划，Harness 仅显示客观 command
evidence；所有进入 canonical Conversation 的大型 ToolResult 都是明确标记的 head/tail bounded result。

**Blocked by:** 02: 引入可替换 TokenEstimator 与 ContextBudgetPolicy。

**Status:** completed

- [x] TaskState 不再包含 goal/constraints，只包含可选 TaskPlan 与有界 Harness evidence。
- [x] update_plan 支持整体 create/replace，合法状态为 pending/in_progress/completed，最多一个 in_progress、最多 20 步。
- [x] trivial task 无需计划；非法参数返回稳定工具错误；计划更新不修改既有 canonical messages。
- [x] command evidence 仅记录工具实际知道的 command/status/exit/result/timestamp，不推导整体正确性。
- [x] 小 ToolResult 不变；大结果保留 head/marker/tail 并记录 original/retained/omitted/partial metadata。

# 07: 支持 Policy 拒绝与暂停审批

**What to build:** 工具协调可以按 Policy 自动允许、结构化拒绝或暂停等待外部审批；未来前端能够批准、拒绝或取消，不需要修改 Agent Loop。

**Blocked by:** 04: 提供公开 Snapshot、TurnSummary 与阶段事件; 05: 实现 Turn 重试、预算、重复失败与取消.

**Status:** complete

- [x] 默认 Policy 允许所有合法工具调用，且不宣称能够识别危险命令。
- [x] `DENY` 产生 `POLICY_DENIED` tool result 并保持历史完整。
- [x] `REQUIRE_APPROVAL` 暂停后续工具、切换公开状态并发出事件。
- [x] 批准、拒绝、取消和独立审批超时都能恢复或终止 Turn。
- [x] 审批等待不消耗 Turn 执行预算。

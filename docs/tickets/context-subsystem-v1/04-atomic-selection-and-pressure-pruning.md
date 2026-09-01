# 04: 实现 atomic recent-tail selection 与压力期 ToolResult pruning

**What to build:** Context 达到 soft pressure 时先在 detached history 中 prune 旧而大的闭合结果，仍有
压力时按 atomic interaction unit 选择 recent raw tail；最新/未闭合 interaction 与所有 call/result
pairing 始终合法。

**Blocked by:** 02: 引入可替换 TokenEstimator 与 ContextBudgetPolicy。

**Status:** completed

- [x] 普通消息和单/多 tool call interaction 被解析为不可拆 atomic units。
- [x] incomplete interaction 强制保留，跨 target 边界的 group 整体保留。
- [x] recent raw tail 从后向前按集中配置的 20% usable budget 选择并返回 compact region/metadata。
- [x] pressure pruning 只改 detached old result 内容，保护当前 interaction，不删除 result message 或 call ID。
- [x] prune 前后估算、数量、selector boundary 与 retained tokens 可从 ContextPlan 观察。

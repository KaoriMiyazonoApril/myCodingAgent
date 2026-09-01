# 02: 引入可替换 TokenEstimator 与 ContextBudgetPolicy

**What to build:** 完整 model request 使用语言感知 heuristic 估算，output reserve、安全余量、soft
pressure 和 hard limit 由独立策略集中管理，ContextPlan 能解释每个预算数字。

**Blocked by:** None (can start immediately).

**Status:** completed

- [x] 英文、中文、mixed、tool schema 和大型 ToolResult 的估算处于独立已知合理范围，且不等于 UTF-8 bytes。
- [x] role、block、tool call/result、schema 与 envelope overhead 全部计入。
- [x] usable input 明确等于 window 减 output reserve 与 safety margin，soft 默认 80%，hard 必须 reduction。
- [x] Provider context-window capability 继续决定 Turn 的有效窗口，非法配置 fail closed。

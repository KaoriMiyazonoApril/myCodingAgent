# 06: 串联有界 Context reduction pipeline

**What to build:** 每个模型 step 按 assemble/estimate → old-result prune → estimate → select/compact/checkpoint
→ reassemble/estimate 的固定顺序处理压力，最多各尝试一次，只有安全缩减后仍超限才返回 CONTEXT_LIMIT。

**Blocked by:** 01: 修复 Context 边界并加载根 Project Instructions；03: 收窄 TaskState 并为单次 ToolResult建立硬边界；05: 实现 rolling semantic compaction 与 durable checkpoint。

**Status:** completed

- [x] 初始请求和 tool execution 后的下一 step 都先 reduction 而非直接失败。
- [x] normal context 保持 provider-lazy；需要 compaction 时可通过窄 compactor seam 使用 Provider。
- [x] 每 step 最多一次 prune 和一次 compaction，无无限 retry；compaction failure 有明确错误路径。
- [x] ContextPlan 完整记录 instructions、预算、pressure、prune、selection、checkpoint、compaction 与 final fit。
- [x] stable/project/runtime/summary/plan/history/current 的最终 assembly 顺序稳定且 cache-friendly。

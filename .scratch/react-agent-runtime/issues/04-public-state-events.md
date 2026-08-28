# 04: 提供公开 Snapshot、TurnSummary 与阶段事件

**What to build:** 调用方可以获得 JSON-compatible Thread 当前状态、本轮结构化结果和有序执行事件，以便未来 HTTP adapter 和前端无需读取内部 Python 对象即可重建对话与执行过程。

**Blocked by:** 03: 支持多轮 Thread 与版本化模型设置.

**Status:** complete

- [x] `ThreadSnapshot`、`TurnSummary` 和公开消息只包含版本化、安全、JSON-compatible 数据。
- [x] 阶段事件包含稳定 envelope 和每 Turn 单调递增 sequence。
- [x] 有界 ring buffer 不会因消费者缺席或缓慢而阻塞 Agent。
- [x] 事件过期可通过重新读取 Snapshot 恢复当前状态。
- [x] reasoning 默认隐藏，debug 模式下只作为独立事件发送。

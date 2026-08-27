# 01: 实现最小单 Turn ReAct 闭环

**What to build:** 用户可以创建一个绑定 workspace 的 Thread，提交一条消息，并观察模型在完整响应与顺序工具执行之间循环，直到返回无工具调用的最终文本。调用方通过 `ThreadRuntime` 获得基础 Thread 状态和 `TurnSummary`，无需操作内部历史或循环模块。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] 无工具调用的模型响应能完成 Turn 并返回最终文本。
- [x] 一个或多个工具调用按原始顺序执行，全部结果进入下一轮模型历史。
- [x] 可恢复工具错误作为 tool result 继续循环，不会直接令 Turn 失败。
- [x] 当前 Turn 活跃时不能向同一 Thread 提交另一条消息。
- [x] 测试从 `ThreadRuntime` seam 观察行为，并使用脚本化 `LLMProvider` 与现有 `ToolRegistry`。

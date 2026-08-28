# 08: 生成本轮 diff 并检测乐观文件冲突

**What to build:** 用户可以查看一次 Turn 通过文件工具造成的 original-to-final diff；Agent 在覆盖已知文件前能发现外部修改，并诚实标记命令造成的变化是否完整可追踪。

**Blocked by:** 01: 实现最小单 Turn ReAct 闭环; 04: 提供公开 Snapshot、TurnSummary 与阶段事件.

**Status:** complete

- [x] 文件第一次修改前保存 original，后续多次修改最终只产生一份统一 diff。
- [x] 新文件和最终文件状态在 Summary 与事件中正确表达。
- [x] 已读取文件被外部改变后写入返回 `FILE_CHANGED`，模型可重新读取后重试。
- [x] 文件工具修改保持 `diff_complete=true`。
- [x] 可能通过 `run_command` 修改文件的 Turn 明确报告 `diff_complete=false`。

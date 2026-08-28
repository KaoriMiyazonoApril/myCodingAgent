# 10: 收口上下文、幂等、隐私与整体验收

**What to build:** 完整 Runtime 在长期 Thread、重复请求和调试场景下保持有界、幂等且不泄漏敏感数据，并通过规格定义的课程演示验收流程。

**Blocked by:** 04: 提供公开 Snapshot、TurnSummary 与阶段事件; 05: 实现 Turn 重试、预算、重复失败与取消; 06: 实现相交 workspace 的并发互斥; 07: 支持 Policy 拒绝与暂停审批; 08: 生成本轮 diff 并检测乐观文件冲突; 09: 强制严格 workspace 与 sandbox 链接禁令.

**Status:** complete

- [x] 历史达到保守上下文预算时返回 `CONTEXT_LIMIT`，不静默删除或总结消息。
- [x] Turn idempotency key 防止网络重试创建重复任务。
- [x] Snapshot、Summary、事件和日志均不泄漏 API key、provider secret 或内部 traceback。
- [x] 默认隐藏 reasoning，debug reasoning 仍遵守独立事件与内存保留规则。
- [x] 规格列出的正常、失败、取消、并发、安全及多轮验收场景全部通过。
- [x] 最终设计文档与实现保持同步。

# 05: 实现 rolling semantic compaction 与 durable checkpoint

**What to build:** old compact region 通过低层 Provider 生成明确的 synthetic handoff summary；成功结果
以 canonical coverage checkpoint 持久化，后续只滚动合并 previous summary 与新 old region，失败不损坏
历史或旧 checkpoint。

**Blocked by:** 04: 实现 atomic recent-tail selection 与压力期 ToolResult pruning。

**Status:** completed

- [x] Compaction prompt 覆盖目标、约束、关键工作/判断/文件/修改/findings/validation/open work，并删除冗余与 bulk output。
- [x] CompactionSummary 在模型 context 中有 synthetic 类型/metadata，不伪装成普通 assistant history。
- [x] checkpoint 记录版本、summary、canonical coverage 与必要 metadata，并通过 ThreadStore migration round-trip。
- [x] 第二次 compaction 使用 previous checkpoint 加新增候选，不重新发送已覆盖 raw history。
- [x] compaction request 自身有预算；失败/非法 summary 保持旧 checkpoint；成功后重新估算。

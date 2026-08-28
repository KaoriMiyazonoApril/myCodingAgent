# 06: 实现相交 workspace 的并发互斥

**What to build:** 用户可以同时运行多个不相交 workspace 的 Turn，同时相同、祖先或后代 workspace 的并发启动会立即得到 `WORKSPACE_BUSY`，而多个空闲 Thread 仍可打开同一目录。

**Blocked by:** 03: 支持多轮 Thread 与版本化模型设置; 05: 实现 Turn 重试、预算、重复失败与取消.

**Status:** complete

- [x] 不相交 workspace 的 Turn 能真正并发运行。
- [x] 相同、祖先和后代 workspace 关系均被规范化路径 lease 拒绝。
- [x] 同一 Thread 的第二个活跃 Turn 被原子拒绝。
- [x] 全局活跃 Turn 默认限制为四并可配置。
- [x] 完成、失败、取消、预算终止和审批等待后的所有路径都正确释放 lease。

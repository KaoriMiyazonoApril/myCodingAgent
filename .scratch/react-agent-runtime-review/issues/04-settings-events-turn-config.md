# 04: 补全设置事件与冻结的 Turn 配置

**What to build:** 前端在空闲或运行中的 Thread 修改默认设置后，都能通过有序事件与 Snapshot 观察到同一个版本；每个活跃 Turn 同时冻结其 reasoning 可见性，运行期间不受后端后续状态影响。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] `TurnConfig` 包含冻结且经过校验的 `reasoning_visibility`。
- [ ] 空闲与活跃 Thread 的成功设置更新均产生版本化 `settings_updated` 事件。
- [ ] 设置事件能够由现有事件 cursor 读取，并与最新 Snapshot 的设置版本一致。
- [ ] reasoning 默认隐藏和 debug 独立事件行为继续通过 Runtime seam 测试。

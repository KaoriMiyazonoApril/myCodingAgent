# 05: 收口 Runtime 验收证据与 ticket 状态

**What to build:** 课程审查者能够从 Runtime seam 的完整验收测试与同步文档确认所有用户故事已经实现；非法工具参数、默认预算、事件恢复和公开状态一致性都有直接证据，tracker 不再把已完成工作标为待实现。

**Blocked by:** 01: 保证同步工具取消后的 workspace 静止性; 02: 拒绝 workspace 路径中的所有 symlink 父组件; 03: 管理命令 sandbox 的 seccomp 资源生命周期; 04: 补全设置事件与冻结的 Turn 配置.

**Status:** ready-for-agent

- [ ] Runtime 端到端测试证明非法 raw tool arguments 原样保留，并产生匹配的结构化错误结果后继续循环。
- [ ] 默认设置版本、迭代、工具调用、执行时间和全局并发限制都有显式断言。
- [ ] 事件、Snapshot、Summary 与最终工具历史的一致性验收覆盖新增行为。
- [ ] 主规格、原 Runtime tickets 05–10 和本组 tickets 的状态与当前实现及测试同步。

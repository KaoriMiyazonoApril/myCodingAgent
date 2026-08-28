# 04: 打通非阻塞 Turn 提交纵向链路

**What to build:** 用户可以从 Composer 提交任务并立即得到 accepted，页面在 Runtime 正式
开始前显示 starting，重复提交被拒绝，启动前失败可观察，且用户可以在 preflight 或运行阶段
点击 Stop。长 workspace validation 不阻塞 Host event loop。

**Blocked by:** 03: 打通 Thread 创建与设置纵向链路.

**Status:** ready-for-agent

- [ ] Runtime workspace validation 在 worker thread 中执行并异步等待，原验证规则、budget、lease 和 fail-closed 语义保持不变。
- [ ] Runtime 在 `turn_started` 之前分配可观察 Turn ID；所有已知 preflight 失败产生脱敏 `turn_rejected` 事件。
- [ ] preflight cancellation 释放 workspace lease 和其他已取得资源，不修改 Conversation，也不遗留 worker-owned Agent 写操作。
- [ ] TaskManager 以小 interface 管理 start、cancel、inspect 和 shutdown，注册 mapping 后创建后台 task。
- [ ] Turn command 在 task 注册后返回 202 accepted，不等待模型、工具或终态 Summary。
- [ ] TaskManager 在 preflight 期间向 Thread view 暴露 `submission.status = starting`，不复制 Runtime status。
- [ ] starting 或 active 期间第二次提交返回稳定 conflict；task 完成、拒绝、取消和异常后 mapping 都清理。
- [ ] pending Stop 取消 scheduled task；active Stop 委派 Runtime cancellation；idle Stop 返回明确 conflict。
- [ ] Composer 可以发送 multiline task、显示 starting/running，并提供真正调用 cancel command 的 Stop。
- [ ] 没有 SSE 时该 ticket 仍可通过短间隔 Thread view refresh 演示从 accepted 到 terminal 的完整行为。
- [ ] 测试覆盖响应延迟、event-loop 响应性、duplicate、starting refresh、preflight reject/cancel、active cancel 和 cleanup。
- [ ] 现有 Runtime cancellation、workspace lease、Conversation 和 EventBuffer 测试继续通过。


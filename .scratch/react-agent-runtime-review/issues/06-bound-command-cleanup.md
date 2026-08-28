# 06: 为命令 timeout 与取消清理设置硬边界

**What to build:** 命令超时或 Turn 取消后，即使 detached descendant 持有进程或管道状态，sandbox 也会在固定清理预算内返回，Runtime 不会无限占用 workspace lease。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] `CommandSandboxBackend` 在 SIGTERM 与 SIGKILL 后的所有 wait/capture 路径都有明确时间上限。
- [ ] timeout 与 coroutine cancellation 均不会在最终 `gather(wait_task)` 上无界等待。
- [ ] detached descendant 回归测试证明命令在清理预算内返回且不遗留可见进程。
- [ ] 本地工具规格的 bounded cleanup 声明与实现及测试一致。

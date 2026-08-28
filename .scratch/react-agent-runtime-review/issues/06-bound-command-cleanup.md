# 06: 为命令 timeout 与取消清理设置硬边界

**What to build:** 命令超时或 Turn 取消后，即使 detached descendant 持有进程或管道状态，sandbox 也会在固定清理预算内返回，Runtime 不会无限占用 workspace lease。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] `CommandSandboxBackend` 在 SIGTERM 与 SIGKILL 后的所有 wait/capture 路径都有明确时间上限。
- [x] timeout 与 coroutine cancellation 均不会在最终 `gather(wait_task)` 上无界等待。
- [x] detached descendant 回归测试证明命令不会等待其保留的 pipe，并在清理预算内返回。
- [x] 本地工具规格的 bounded cleanup 声明与实现及测试一致。

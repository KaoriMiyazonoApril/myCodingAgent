# 01: 保证同步工具取消后的 workspace 静止性

**What to build:** 用户取消或执行预算终止一个正在运行同步工具的 Turn 后，Runtime 只在该工具不再可能修改 workspace 时发布终态并释放 lease；已经发生的文件变化仍被诚实记录，后续相交 Turn 不会与后台线程并发写入。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] 取消或超时时，Runtime 等待同步工具执行真正静止后再释放 workspace lease。
- [ ] 同步工具在取消请求后完成的文件变化进入 diff/event，或在无法完整恢复时明确报告 `diff_complete=false`。
- [ ] 相交 workspace 的下一 Turn 在旧同步工具仍可能写入时继续得到 `WORKSPACE_BUSY`。
- [ ] 命令工具的进程组取消语义保持不变，并有 Runtime seam 回归测试。

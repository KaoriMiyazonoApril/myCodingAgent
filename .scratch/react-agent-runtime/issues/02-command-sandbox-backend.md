# 02: 提取 CommandSandboxBackend seam

**What to build:** 在不改变现有命令执行语义的前提下，让 Runtime 可以注入经过能力探测、支持取消的命令 sandbox adapter，并让测试不依赖开发主机是否能够运行 bubblewrap，为后续严格链接禁令准备稳定 seam。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] bubblewrap adapter 保持现有 workspace 隔离、输出、非零退出、超时和取消行为。
- [x] 确定性的测试 adapter 与生产 adapter 满足同一 interface。
- [x] sandbox 不可用时仍在首次命令前明确失败，且不存在 host-shell fallback。
- [x] 现有本地工具测试继续通过。

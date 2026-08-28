# 03: 管理命令 sandbox 的 seccomp 资源生命周期

**What to build:** 应用反复创建并关闭 Thread 时，命令 sandbox 持有的 seccomp 能力资源会在不再使用后可靠释放，同时活跃命令仍能安全完成或取消，不发生提前关闭或文件描述符泄漏。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] `CommandSandboxBackend` adapter contract 提供幂等资源关闭语义。
- [x] 关闭空闲 Thread 会关闭其工具 registry 持有的 sandbox 资源。
- [x] 关闭活跃 Thread 时先完成取消与 lease 清理，再关闭 sandbox，不发生 use-after-close。
- [x] 生产 Bubblewrap 与确定性测试 adapter 均通过资源生命周期 contract 测试。

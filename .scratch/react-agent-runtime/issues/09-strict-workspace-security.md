# 09: 强制严格 workspace 与 sandbox 链接禁令

**What to build:** 每个 Turn 只在完整验证过的普通 workspace 中执行；持久 workspace 内的 symbolic link、hard link、嵌套 mount 和命令临时创建链接均被拒绝，而可信只读系统运行环境仍可工作。

**Blocked by:** 02: 提取 CommandSandboxBackend seam; 06: 实现相交 workspace 的并发互斥.

**Status:** complete

- [x] workspace 根、所有条目和文件工具路径层级均使用不跟随链接的检查。
- [x] symbolic link、regular-file hard link 和嵌套 mount/bind mount 被拒绝。
- [x] 完整扫描达到条目或时间预算时 fail closed 为 `WORKSPACE_VALIDATION_LIMIT`。
- [x] 生产 sandbox 阻止 workspace link 相关 syscall，能力不足时提前失败。
- [x] sandbox 可信只读系统目录中的必要链接不受 workspace 禁令影响。

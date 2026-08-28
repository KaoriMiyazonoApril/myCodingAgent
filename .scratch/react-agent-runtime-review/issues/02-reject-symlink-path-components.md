# 02: 拒绝 workspace 路径中的所有 symlink 父组件

**What to build:** 用户选择 workspace 或调用文件工具时，路径中的每个既存组件都通过不跟随链接的元数据检查；隐藏在父目录中的 symbolic link 与最终组件链接一样被稳定拒绝。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] Thread 创建拒绝任意父组件为 symbolic link 的 workspace 路径。
- [x] 文件工具组合与路径解析拒绝任意 symlink 父组件，不会先通过 `resolve` 跟随链接。
- [x] 合法普通目录仍规范化为稳定真实路径，不影响 workspace lease 的重叠判断。
- [x] Runtime 与本地工具 seam 都有父组件链接回归测试。

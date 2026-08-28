# 07: 同步本地工具的严格 symlink 规格

**What to build:** 本地工具规格准确说明 workspace 内任何 symbolic link 都会被拒绝，不再向调用方承诺实现已经禁止的内部链接跟随能力。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [x] 删除“workspace 内部 symlink 可用”和搜索会安全跟随文件 symlink 的过时表述。
- [x] 文件工具规格与 Runtime 严格 workspace link 禁令、实现和测试一致。
- [x] 相关 User Story 与测试说明不再暗示链接跟随能力。
- [ ] “当前实现”章节不再残留“链接目标位于 workspace/selected subtree 即可”的旧条件。

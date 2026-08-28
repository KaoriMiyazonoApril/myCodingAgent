# 08: 实现 read_file 的流式分页资源保证

**What to build:** `read_file` 在保持严格 UTF-8、NUL、大小、fingerprint 与总行数语义的同时逐块读取，只保留请求页面而不把允许范围内的整份文件及全部行同时载入内存。

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] `read_text_page` 不再通过整文件 `read_bytes()`、完整 decode 与 `splitlines()` 实现分页。
- [x] 流式路径仍验证文件大小、严格 UTF-8、NUL、regular-file 与 workspace 安全规则。
- [x] 返回页面、总行数、末尾换行、空文件和 content fingerprint 与既有公开语义一致。
- [x] 测试直接证明分页路径使用有界分块读取且不调用整文件读取 helper。
- [x] 本地工具规格与实现及验收证据同步。

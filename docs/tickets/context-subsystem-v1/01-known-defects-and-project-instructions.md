# 01: 修复 Context 边界并加载根 Project Instructions

**What to build:** 模型请求中的 stable/dynamic system section 始终有明确边界；workspace root 的
AGENTS.md 在每 Turn 有界加载并冻结，缺失、超大、parent/nested 和跨 Turn 修改行为可解释；symlink
loop 始终保持 IO_ERROR 语义。

**Blocked by:** None (can start immediately).

**Status:** completed

- [x] ContextPlan 到 OpenAI-compatible payload 的集成测试证明 system 文本边界稳定。
- [x] 仅 root AGENTS.md 被读取，缺失为空，parent/nested 不生效，超大输入确定性截断并记录 metadata/diagnostics。
- [x] 同一 Turn 使用固定 instructions snapshot，下一 Turn 重新读取修改后的文件。
- [x] symlink loop regression 在支持 symlink 的环境返回 IO_ERROR，bubblewrap 缺失只产生环境 skip。

# Context Architecture

Runtime 的 `Conversation` 是 Thread 的 canonical history：它保留完整的
system、user、assistant 和 tool 消息，供持久化、审计、UI 与重启恢复使用。它
不是某一次模型请求的可见 prompt，也不会在 context assembly 时被截断或替换。

```text
durable Thread / Conversation
          │ detached snapshot
          ▼
Context sources ── BaseSystemInstructions
                ├─ ProjectInstructions provider
                ├─ RuntimeContext (cwd/workspace/shell/capabilities)
                └─ TaskState (goal/progress/validation/checkpoints)
          │
          ▼
ContextManager
  ├─ HistorySelector (NoOp: preserve all)
  ├─ HistoryCompactor (NoOp: preserve all)
  ├─ ContextBudget (explicit CONTEXT_LIMIT)
  └─ ContextPlan
          │
          ▼
ContextRenderer ── provider-independent model messages ──► ModelInvoker / Model
                                                         ▲
                                                         │ tool schemas
                                                   ToolRegistry ownership
```

当前版本的 selector 与 compactor 都是 NoOp：不排序、不检索、不切片、不摘要，
因此 canonical history 始终完整。`ContextPlan` 保存 source sections、选中的与
compacted history、current input、估算值、budget 状态和决策元数据，便于解释模型
实际看到的内容。`ContextRenderer` 将稳定的 base/project 内容放在 system 消息前部，
再附加动态 runtime/task sections，并把 detached history/current input 还原为既有
`Message` 类型。

`ContextBudget` 是 assembly 的唯一容量检查者。它继续使用现有的保守 UTF-8 字节
估算和输出预留；超限显式抛出 `CONTEXT_LIMIT`，不会静默删除 canonical history。
工具 schema 只从 `ToolRegistry.definitions()` 传给 budget 与 ModelInvoker，不会复制
到 Conversation、instructions 或 TaskState。

重启恢复后，`ThreadRuntime` 先把未完成 Turn 收敛为可审计的终态，再用恢复出的
`Conversation.canonical_messages()` 创建下一次请求的 detached snapshot。因此恢复
后的 user、assistant、tool history 会完整进入 ContextManager；只有 runtime/task sections
按本次请求重新收集，稳定 base instructions 仍由 `BaseSystemInstructions` 负责。

`ProjectInstructions` 目前由静态 provider 提供 Runtime 附加指令；递归发现
`AGENTS.md`、真实 history selection 与 compaction 均留在后续实现，且应通过已有
abstraction 接入而不改变 durable/model-visible 边界。

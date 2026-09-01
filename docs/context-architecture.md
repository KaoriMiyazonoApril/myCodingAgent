# Context Architecture

## Durable history 与 model-visible context

Runtime 的 `Conversation` 是 Thread 的 canonical durable history。它保留完整的 system、user、
assistant 和 bounded tool messages，供持久化、审计、UI 与重启恢复使用。History selection、pressure
pruning 和 semantic compaction 只操作 deep detached snapshots，不删除、覆盖或重排 Conversation。

模型请求由 Turn-scoped `ContextManager` 组装。section 顺序固定为：base system、workspace root
`AGENTS.md`、RuntimeContext、synthetic CompactionSummary、TaskState、recent raw history/current
continuation。stable base/project 与 dynamic sections 保持独立 `TextBlock`，renderer 在 block 边界
显式加入两个换行，Provider 直接拼接 blocks 也不会产生文本粘连。

## Project Instructions lifecycle

生产 Runtime 只读取 `<opened workspace root>/AGENTS.md`。不搜索 parent、git root、nested 文件、cwd
hierarchy 或其他文件名。文件缺失产生空 instructions；读取/UTF-8 错误 fail-soft 并写 diagnostics。
读取上限为 64 KiB UTF-8，超限保留确定性 prefix 与包含 original/omitted bytes 的 marker。

每个 Turn 创建一个 ContextManager，并在首次 assembly 时冻结 ProjectInstructions；该 Turn 的后续
model/tool steps 复用同一 snapshot。下一 Turn 创建新 manager，因此能观察两个 Turns 之间的修改。

## Token estimation 与 budget

`TokenEstimator` 独立于 `ContextBudgetPolicy`。V1 heuristic 对 ASCII 约按 4 chars/token，对 CJK
约按 1.5 chars/token，对其他非 ASCII 约按 2 chars/token，并显式计算 request、message、role、
content block、tool call/result、tool schema 与 envelope overhead。它不再把 UTF-8 byte 直接当 token，
未来可替换为 Provider tokenizer。

```text
usable_input = context_window - output_reserve - safety_margin
soft_limit   = floor(usable_input * 0.80)
```

默认 safety margin 为 256 tokens。达到 soft limit 会尝试 reduction；达到 hard usable limit 必须尝试
reduction；只有 `estimated_input > usable_input` 才是不安全的 final overflow。ContextPlan 分别暴露
estimate、reserve、margin、usable、soft threshold、pressure 与 final fit。

## TaskState

TaskState 不保存 goal 或 constraints。当前目标仍来自 current user message、recent raw history 或
CompactionSummary。它只包含可选 TaskPlan 与 Harness-owned command evidence。TaskPlan 最多 20 个
`pending` / `in_progress` / `completed` steps，最多一个 `in_progress`；evidence 最多 100 条，只记录
tool、command、status、exit code、result ID 与 timestamp 等客观事实。step 与 evidence 字符串字段
均有集中、确定性的长度校验，非法更新会被拒绝而不会静默裁剪。

模型通过普通本地 `update_plan` tool 原子替换 plan；Harness 只做 schema/invariant 校验，不根据工具
名猜测步骤是否完成，也不把 command exit 0 推导为“整个项目正确”。

## ToolResult reduction

Layer 1 在 ToolResult 进入 Conversation 前对完整 provider-visible payload（content + metadata）执行
统一 64 KiB hard bound。小结果原样保留；超限 content 使用 `head + omission marker + tail`，嵌套
metadata 递归确定性缩减并优先保留 exit/status/error/path 等客观字段。metadata 记录
original/retained/omitted bytes、partial 与 strategy。Provider 的直接序列化入口也应用同一幂等 reducer，
因此大 stdout/stderr metadata 不能绕过边界。events 与 canonical Conversation 使用同一 bounded
model-visible result；V1 不新增 raw artifact store。

Layer 2 只在 pressure 下操作 detached history。旧、已闭合、超过阈值的 result 被替换为 marker，
但 tool message、call ID、error code 与 pairing 保留。open interaction 以及 transcript 尾部尚未被
下一模型响应消费的 interaction 被保护。prune 后立即重新估算。

## Atomic selection 与 rolling compaction

assistant 的一次 multi-tool call message 与所有对应 ToolResult messages 是不可拆 atomic unit；ordinary
message 是普通 unit；缺 result 的 interaction 标记为 open 并强制保留。`RecentRawTailSelector` 从新
到旧选择 raw units，默认目标为 usable input 的 20%，跨 boundary 的 unit 整体保留，并输出 compact
candidate region、canonical boundary 与 token metadata。

若 pruning 后仍有 pressure，`LLMHistoryCompactor` 通过低层 Provider `chat` seam 对整个 old region
生成一次 synthetic handoff。prompt 保留目标/约束、完成工作、技术判断、文件/修改/findings、
validation facts、open issues 与 next work，并删除冗余聊天、过时 reasoning 与 bulk output。
Compaction request 自身按 atomic units 有界，并纳入 Turn execution timeout。

`CompactionCheckpoint` 独立记录 summary、inclusive canonical coverage、canonical-prefix fingerprint、
version、timestamps 与 source metadata。coverage 必须落在 atomic unit 边界；下一次 compaction 只发送
previous summary 与 coverage 后新进入 old region 的 raw units。成功 checkpoint 由 ThreadStore 保存；
失败保留旧 checkpoint 和完整 canonical history。恢复时 coverage、atomic boundary 或 append-only prefix
fingerprint 无效的 checkpoint 被忽略并记录 `CHECKPOINT_INVALID`。

## Reduction orchestration

每个 model step 最多一次 prune 与一次 semantic compaction：

```text
assemble + estimate
  ├─ normal ───────────────────────────────────────────────► send
  └─ soft/hard pressure
       └─ prune old closed ToolResults once + re-estimate
            ├─ normal ─────────────────────────────────────► send
            └─ still pressured
                 └─ atomic recent-tail selection
                      └─ rolling semantic compaction once
                           └─ persist checkpoint + reassemble + estimate
                                ├─ estimated <= usable ────► send
                                └─ estimated > usable ─────► CONTEXT_LIMIT
```

初始 Turn preflight 先检查不可缩减的 base/project/runtime/current input/tool schemas，因此明显不可容纳的
请求仍能在创建 Provider transport 前失败。可缩减 durable history 延后到 Provider 解析后的 pipeline。
Compaction failure 产生明确的 `CONTEXT_COMPACTION_FAILED` Turn failure，不写坏旧 checkpoint。

`ContextPlan.decision_metadata` 记录 project diagnostics、各预算量、initial/prune/post-compaction
estimate、pressure、pruned count、selector boundary/tail tokens、checkpoint reuse/coverage、compaction
attempt/result 与 final fit。

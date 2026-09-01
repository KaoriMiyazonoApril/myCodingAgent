# Context Architecture

## Durable history 与 model-visible context

Runtime 的 `Conversation` 是 Thread 的 canonical durable history。它保留完整的 system、user、
assistant 和 bounded tool messages，供持久化、审计、UI 与重启恢复使用。History selection、pressure
pruning 和 semantic compaction 只操作 deep detached snapshots，不删除、覆盖或重排 Conversation。

模型请求由 Turn-scoped `ContextManager` 组装。V2 的顺序固定为：stable base/project instructions、
bounded available-Skills catalog、stable RuntimeContext、synthetic CompactionSummary、selected
chronological history/current continuation，以及 late loaded-Skill 与 TaskState projections。stable
epoch、dynamic epoch 和 late working tail 保持可观测的独立分区；renderer 在 block 边界显式加入
换行，Provider 直接拼接 blocks 也不会产生文本粘连。

Working tail 的消息 role 由 Turn 冻结的 Provider capability 决定，而不是由 Context Core
硬编码：显式验证过的 Provider 可以选择 `late_system`，未知或未确认的 Provider（包括当前
DeepSeek、Moonshot/Kimi 与 GLM presets）使用 `structured_user_tail`。两种形式都保持
stable prefix → chronological history → late working tail 的顺序；fallback 是一个有界的
`<agent_working_state>` user message，明确标注 Harness-maintained working state，且不是新的
user intent。该消息只存在于 detached model request，不进入 canonical Conversation、compaction
source 或 checkpoint fingerprint；current user message 仍只在 Turn 开始时追加一次。

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
CompactionSummary。它只包含可选 TaskPlan 与 Harness-owned mutation/validation/failure/artifact
evidence。TaskPlan 最多 20 个 `pending` / `in_progress` / `completed` / `blocked` steps，最多一个
`in_progress`；evidence 最多 100 条，只记录 tool、command、status、exit code、result ID 与 timestamp
等客观事实。step 与 evidence 的标量字符串字段均有集中、确定性的长度校验，非法更新会被拒绝；
provenance paths 则按输入顺序最多保留 16 项、单项 512 字符、总计 2,048 字符，超出部分确定性截断，
不会让路径 metadata 绕过 model-visible TaskState budget。

模型通过普通本地 `update_plan` tool 原子替换 plan；Harness 只做 schema/invariant 校验，不根据工具
名猜测步骤是否完成，也不把 command exit 0 推导为“整个项目正确”。

## ToolResult reduction

Layer 1 在 ToolResult 进入 Conversation 前对完整 provider-visible payload（content + metadata）执行
统一 64 KiB hard bound。小结果原样保留；超限 content 使用 `head + omission marker + tail`，嵌套
metadata 递归确定性缩减并优先保留 exit/status/error/path 等客观字段。metadata 记录
original/retained/omitted bytes、partial 与 strategy。Provider 的直接序列化入口也应用同一幂等 reducer，
因此大 stdout/stderr metadata 不能绕过边界。events 与 canonical Conversation 使用同一 bounded
model-visible result；validation/failure 等有语义的结果还写入 bounded `salient_evidence` metadata，
使诊断、来源工具、验证命令和路径在 Layer 2、compaction、TaskState 清空及后续 Turn 仍可见。
成功的纯 opaque bulk payload 不附加无意义的重复 handoff。V1 不新增 raw artifact store。

同步工具的异步注册 seam 使用独立的短生命周期 daemon worker，并以事件轮询把结果交回事件循环；它不把
用户工具任务注册到 Python 的 process-wide default executor，避免 Runtime 关闭时等待不可控的后台池。

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
previous summary 与 coverage 后新进入 old region 的 raw units。成功 checkpoint 由 ThreadStore 保存，内存
checkpoint 只在 durable transition 成功后保持；
失败保留旧 checkpoint 和完整 canonical history。恢复时 coverage、atomic boundary 或 append-only prefix
fingerprint 无效的 checkpoint 被忽略并记录 `CHECKPOINT_INVALID`。缺少 canonical fingerprint 的旧
checkpoint 仅作为迁移诊断保留，不能隐藏任何 canonical history；只有完整连续 prefix 且 coverage 不
超过实际发送给 compactor 的 atomic source 才能推进 watermark。

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

## V2 module boundaries

Context assembly is intentionally split into small, importable seams.  The old module names remain
compatibility facades so existing integrations do not need a flag day migration:

```text
agent/runtime/context.py             compatibility exports
agent/runtime/context_manager.py     turn-scoped orchestration and public facade
agent/runtime/context_plan.py        ContextSources/ContextPlan value objects
agent/runtime/context_renderer.py    model-visible section renderer
agent/runtime/context_reduction.py   retained-prefix reduction helper
agent/runtime/context_types.py       ContextSection and source contracts
agent/runtime/instructions.py        root AGENTS.md provider and bounded diagnostics
agent/runtime/runtime_environment.py stable runtime section (turn id stays internal)
agent/runtime/task_state.py          plan/evidence validation and deterministic view
agent/runtime/history/
  units.py                            canonical atomic history units and fingerprints
  selection.py                        recent raw-tail selection
  pruning.py                          bounded ToolResult pruning
  compaction.py                       rolling semantic compaction/checkpoints
agent/runtime/context_history.py     compatibility exports for history package
```

`ContextManager` owns orchestration while the three context modules above own plan data, rendering, and
reduction. The model-visible request is assembled in this order: short base instructions, root project
instructions, bounded available Skills catalog, stable runtime environment, validated compaction summary,
selected chronological history, explicitly mentioned Skill bodies, and the bounded `TaskStateView`.
Explicit Skill bodies are the only Skill text projected into that late tail. A model `skill(name)` call is
represented exactly once by its real assistant/tool pair and bounded ToolResult in chronological history;
its full body is never copied into the next late tail. `turn_id` and runtime telemetry are retained for
events and diagnostics but never enter a model-visible section.

## Skills and turn-local state

Skill discovery scans only direct child directories of the seven configured roots (workspace `.agents`,
`.claude`, `.opencode`, followed by the corresponding user roots).  A strict UTF-8 `SKILL.md` frontmatter
parser validates the directory-matching lowercase kebab-case name and bounded description.  Precedence is
deterministic, malformed files fail soft with diagnostics, and the model receives metadata only (bounded
to 8,000 characters) until it asks for a Skill.

The local `skill(name)` tool loads one known Skill idempotently. Explicit `$skill-name` mentions are
activated before the first provider request without a synthetic assistant/tool exchange and are projected
as a deterministic, aggregate, bounded late section. Loaded state and diagnostics are reset for every Turn,
while durable `skill_loaded` events and the read-only Skills popover expose metadata. A model-driven load
remains a real tool call/result in canonical history (including its bounded body), and is not projected again;
an explicit preflight load is projection-only, so no fabricated call is added to the thread snapshot.

## Thread settings and capabilities

Provider, model, temperature, output limit, approval mode, and optional thinking/budget settings are
thread-scoped.  The Host preserves internal safety limits when a partial settings update omits its
`limits` object.  `GET /api/threads/{thread_id}/capabilities` exposes conservative capability flags so
the Web editor disables unsupported thinking controls rather than guessing from provider names. The endpoint
accepts an optional provider/model candidate and always resolves that pair rather than reusing the current
Thread capability. The Web editor disables optional controls and Settings save while a candidate preview is
pending; a preview transport failure keeps save disabled rather than treating unknown capability as unsupported.
Host save validation normalizes unsupported fields before persistence. Thread snapshots include Skills metadata and
capability projections, never provider credentials. When Settings opens or its provider/model draft changes,
the Web editor marks the exact capability candidate as pending and disables saving until that preview resolves
or fails closed; an actual provider/model draft change also clears optional thinking fields. Explicit Skill
bodies are not persisted as synthetic messages; model-driven bodies remain in their real bounded ToolResult
history by design.

## Observability and verification

`ContextPlan.decision_metadata` is copied into a bounded Runtime diagnostic seam and exposed through
`ThreadRuntime.get_context_diagnostics()`/`ThreadSnapshot.context_diagnostics`; it is not discarded when a
model request is rendered. The projection records epoch sections, chronological and selected history,
TaskState estimate, loaded Skills, pruning, checkpoint validation/reuse/coverage, compaction, pressure and
final fit. It is intentionally non-model-visible. The integration regression
`tests/test_context_long_scenario.py` exercises two compactions, a validation failure with large output,
explicit and model Skill lifecycles, SQLite reload, and a scope-changing fresh Turn with a deterministic
fake provider. Skill discovery tests document the seven root paths, precedence, strict first-line `---`
parser, malformed/oversize/encoding/path cases, and the permitted canonical-readable symlink policy.

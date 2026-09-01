# Context 子系统 V1 完善与缺陷修复规格

Status: implemented

## Problem Statement

当前 Coding Agent 已明确区分 durable `Conversation` 与单次模型可见 Context，但 Context Policy
仍停留在占位阶段：HistorySelector/HistoryCompactor 为 NoOp，预算近似等于 UTF-8 字节数，当前
用户输入还被复制为 TaskState goal，ProjectInstructions 不能从 workspace 自动发现，工具输出也
可能长期无界进入请求。ContextRenderer 的 stable/dynamic block 在 OpenAI-compatible 序列化后还会
无分隔拼接，symlink loop 的异常分类也可能被 `Path.exists()` 错误降级为 NOT_FOUND。

结果是较大的中文任务、tool schema 或 command output 会过早触发 CONTEXT_LIMIT；长线程无法用
确定性裁剪和 rolling semantic handoff 延续；TaskState 与历史可能成为冲突 truth source；同时
canonical history、model-visible request、checkpoint 和 persistence 之间缺少足够可观察的契约。

## Solution

在保留 Agent Loop、Conversation、Thread/Turn lifecycle、Provider abstraction、tool abstraction 与
ThreadStore 边界的前提下，把 Context 建成分层、可替换、可解释的 reduction pipeline。每 Turn 从
workspace root 精确加载一次有界 AGENTS.md；通过独立 TokenEstimator 与 ContextBudgetPolicy 估算
完整请求并判断 normal/soft/hard pressure；优先对旧 ToolResult 做确定性 pruning，再用 atomic recent
tail selection 与一次 LLM semantic compaction 形成 rolling checkpoint。任何 selection、pruning 或
compaction 都只作用于 detached/model-visible copy，canonical Conversation 永不因此删除或覆盖。

TaskState 收窄为 model-maintained TaskPlan 与 Harness-owned command evidence。模型通过窄
`update_plan` 工具整体更新有界步骤；Harness 只记录工具实际返回的 command、exit code、状态、结果
标识和时间事实，不推导“项目正确”或“需求全部满足”。CompactionSummary 是带来源覆盖范围的
synthetic context，不伪装成历史 assistant message；checkpoint 独立持久化并在后续 pressure 下滚动
合并 previous summary 与新的 raw compact region。

## User Stories

1. As a model, I want stable and dynamic system sections to retain an explicit boundary after Provider serialization, so that instructions cannot run together.
2. As a maintainer, I want a test crossing ContextPlan, ContextRenderer and Provider payload encoding, so that block-level tests cannot miss serialization defects.
3. As a tool caller, I want a symlink loop reported as IO_ERROR, so that an inspection failure is not misrepresented as a missing file.
4. As a test operator, I want missing bubblewrap skips separated from product failures, so that environment limitations are reported truthfully.
5. As a workspace user, I want only the opened root AGENTS.md loaded, so that parent or nested projects cannot silently alter instructions.
6. As a workspace user, I want edits to root AGENTS.md visible on the next Turn, so that project guidance can evolve between Turns.
7. As a model, I want one Turn to use one immutable ProjectInstructions snapshot, so that its steps do not receive contradictory rules.
8. As an operator, I want oversized AGENTS.md deterministically truncated with diagnostics, so that a project file cannot prevent startup or silently disappear.
9. As a maintainer, I want ProjectInstructions discovery behind its provider seam, so that ContextManager does not own filesystem traversal.
10. As a user, I want English context estimated substantially below one token per byte, so that the model window is not wasted.
11. As a user, I want Chinese and mixed text conservatively estimated without byte-scale overcounting, so that multilingual tasks remain usable.
12. As a model, I want message roles, content blocks, tool calls, tool results, schemas and request framing included in estimates, so that apparent fits are not unsafe.
13. As a provider integrator, I want TokenEstimator replaceable, so that a provider tokenizer can be added later without rewriting policy.
14. As an operator, I want output reserve and safety margin explicit, so that safety is not hidden inside intentionally bad token estimates.
15. As a Context maintainer, I want normal, soft pressure and hard limit states centralized in one policy, so that magic thresholds do not drift.
16. As a user, I want pressure based on input plus reserved output, so that generation space remains protected.
17. As a model, I want a concise current/future TaskPlan, so that a multi-step Turn can track progress without copying old history.
18. As a model, I want pending, in_progress and completed statuses, so that plan meaning is narrow and predictable.
19. As a maintainer, I want invalid statuses, excess steps and multiple in_progress steps rejected, so that model bookkeeping cannot corrupt state.
20. As a user, I want trivial tasks to work without a plan, so that planning is optional rather than ceremony.
21. As a model, I want update_plan to replace the current plan atomically, so that stale steps do not grow without bound.
22. As an auditor, I want plan tool calls/results in canonical history while plan rendering remains detached, so that history remains truthful.
23. As a maintainer, I do not want TaskState to duplicate the current user goal, so that there is one source of task intent.
24. As a maintainer, I do not want copied constraints in TaskState, so that later user corrections cannot conflict with stale state.
25. As a model, I want objective command evidence, so that I can reason from actual command/exit facts.
26. As an auditor, I want Harness evidence to avoid claiming overall correctness, so that validation semantics remain honest.
27. As a model, I want every large ToolResult immediately bounded with head and tail, so that one command cannot monopolize context.
28. As a model, I want an omission marker with original and omitted sizes, so that partial output is unmistakable.
29. As a user, I want small ToolResults unchanged, so that normal precision is preserved.
30. As a maintainer, I want truncation metadata persisted with the bounded result, so that model-visible history can be audited.
31. As a model, I want old large ToolResults pruned before semantic compaction, so that cheap deterministic reduction happens first.
32. As a model, I want the latest unconsumed tool interaction preserved, so that current work remains actionable.
33. As a provider integrator, I want assistant tool calls and every matching result kept as one atomic unit, so that requests are protocol-valid.
34. As a model, I want recent raw history selected from newest to oldest under a centralized budget ratio, so that immediate details remain exact.
35. As a model, I want an atomic group retained even when it crosses the target boundary, so that pairing wins over a soft target.
36. As a model, I want an open/incomplete interaction always retained, so that continuation never loses pending calls.
37. As a debugger, I want selection metadata describing compact candidates, boundary and retained tokens, so that choices are explainable.
38. As a long-thread user, I want old history summarized as one semantic handoff, so that the agent remembers outcomes without raw noise.
39. As a user, I want summaries to preserve goal, constraints, completed work, decisions, files, findings, validation and open work, so that the agent does not repeat or contradict earlier work.
40. As a model, I want redundant chat, obsolete reasoning and bulk output removed from summaries, so that handoff remains useful.
41. As an auditor, I want CompactionSummary explicitly synthetic, so that it is never mistaken for a historical assistant statement.
42. As a persistence user, I want canonical messages retained unchanged after compaction, so that full history survives restart and audit.
43. As a long-thread user, I want a checkpoint recording the covered canonical position, so that old raw messages are not resent beside their summary.
44. As a long-thread user, I want later compaction to combine the previous summary with only newly old raw history, so that cost does not repeatedly start at message one.
45. As an operator, I want a failed compaction to leave the previous checkpoint untouched, so that errors cannot destroy the last valid handoff.
46. As a model, I want the compaction request itself budgeted, so that reduction cannot fail by constructing another oversized request.
47. As a maintainer, I want at most one deterministic prune and one semantic compaction per model step, so that reduction cannot loop forever.
48. As a user, I want CONTEXT_LIMIT only after safe reduction still cannot fit, so that long tasks fail only when genuinely irreducible.
49. As a model, I want stable source order before dynamic summary/plan/history, so that repeated requests keep a cache-friendly prefix.
50. As a debugger, I want ContextPlan to expose estimates, budgets, pressure, instructions, pruning, selection, checkpoints, compaction and final fit, so that Context Policy is not a black box.
51. As a restart user, I want a persisted checkpoint reused with restored canonical history, so that long threads do not regress after Host restart.
52. As a reviewer, I want one end-to-end long-thread test through multiple tools, pressure, prune, compaction and next request, so that the complete contract is proven.
53. As a reviewer, I want canonical transcript length and exact tool pairing preserved in that test, so that model efficiency never corrupts durable truth.
54. As a maintainer, I want README and architecture docs synchronized with actual persistence and Context behavior, so that documentation is authoritative.

## Implementation Decisions

- `Conversation` remains the only canonical message owner. All policies consume deep detached snapshots and return detached model-visible structures.
- Context section order is fixed: base system, ProjectInstructions, runtime/world information, CompactionSummary, TaskPlan/evidence, recent history and current continuation.
- Stable and dynamic content remain separate blocks for future cache-aware assembly, but the renderer owns an explicit two-newline boundary so concatenating Provider serialization remains semantically correct.
- Root instructions provider reads exactly `<workspace>/AGENTS.md`; it does not inspect parent directories, git roots, nested files, cwd variants or alternate names.
- Project instructions are loaded once while constructing a Turn-scoped ContextManager and then reused for every step of that Turn. The default hard read limit is 64 KiB UTF-8. Oversize text keeps a deterministic prefix plus a marker containing original/omitted byte counts. Absence is empty; other read/decode failures are diagnostics and empty instructions, not startup crashes.
- TokenEstimator is provider-independent and estimates the complete prepared request. V1 uses approximately four ASCII text characters per token, conservative per-character weighting for CJK/other non-ASCII, and explicit role/block/message/tool/schema/envelope overhead. It never uses UTF-8 byte count as the token count.
- ContextBudgetPolicy owns context window, output reserve, safety margin, usable input, 80% soft threshold and hard usable-input limit. Explicit positive output settings are honored; the default reserve remains bounded for small and large windows. Safety margin is a separate configurable value.
- Pressure labels are `normal`, `soft`, and `hard`; final status is `fits` or `overflow`. Decisions account for estimated input plus output reserve and safety margin against the context window.
- TaskState consists only of optional TaskPlan plus Harness-owned evidence. It has no goal or constraints fields.
- TaskPlan contains at most 20 PlanSteps. Status is exactly pending/in_progress/completed and at most one step is in_progress. Empty/no plan is legal. Step/evidence string fields have centralized validation limits. update_plan validates and atomically replaces the whole plan without interpreting step text.
- The update_plan capability is a normal locally implemented model tool, dispatched through existing tool abstraction and recorded through normal assistant/tool canonical history. Its execution updates only the active Turn TaskState.
- Command-capable tool completions contribute bounded objective evidence: tool name, command, exit/status facts, result identifier and timestamp when available. All command outcomes are evidence; Harness does not infer whether they prove the whole task correct.
- Layer-1 ToolResult reduction occurs before appending to Conversation. A unified reducer bounds the complete provider-visible payload (content plus recursively bounded metadata), preserves head and tail, emits clear partial-output/metadata markers, and records original size, retained size, omitted size, truncation and strategy metadata. The default threshold is centralized and test-overridable.
- Layer-2 pruning runs only under pressure on detached history. It replaces content of old, closed, large ToolResult blocks with deterministic pruned markers/metadata. It never removes tool messages or changes call IDs, so call/result pairing remains intact. The most recent tool interaction and any open interaction are protected.
- History is parsed into atomic units. A multi-tool assistant response plus all corresponding result messages is one unit; ordinary messages are units unless naturally grouped with the following tool interaction. Incomplete interactions are retained.
- RecentRawTailSelector walks atomic units from newest to oldest and targets 20% of usable input. A crossing unit is kept whole. It returns compact candidates, retained raw tail and structured decision metadata rather than a summary.
- Compaction is one semantic request over the previous synthetic checkpoint summary plus newly selected old raw units. Its prompt explicitly lists the required retention and deletion criteria from this spec.
- Compaction uses the existing low-level Provider abstraction through a narrow async compactor interface. It does not invoke provider-hosted execution, an agent framework or another Agent Loop.
- A CompactionCheckpoint contains version, synthetic summary, inclusive canonical coverage position, canonical-prefix fingerprint, creation/update metadata and source estimate. The coverage position must end on an atomic boundary and is defined against append-only canonical message order.
- Checkpoints are Thread-owned durable state and are serialized by ThreadStore with an explicit schema migration. Restored checkpoints are validated against restored canonical history and ignored with diagnostics if invalid.
- Rolling compaction never re-summarizes canonical history already covered by the previous valid checkpoint. A failed or invalid new summary leaves the prior checkpoint unchanged.
- One model step executes this bounded pipeline: assemble/estimate; if pressured prune old tool results once; re-estimate; if still pressured select once and compact once; atomically persist a successful checkpoint; reassemble/re-estimate; fail with CONTEXT_LIMIT only if still over hard limit. No retry loop is permitted.
- Compaction input has its own allowance derived from usable input. If previous summary plus candidate cannot fit, raw candidate is deterministically bounded on atomic-unit boundaries; if no safe compaction request can be formed, the step fails without checkpoint mutation.
- ContextPlan metadata includes estimated input, reserved output, safety margin, usable budget, pressure, project load/truncation diagnostics, prune counts and before/after estimates, selector boundary, retained tokens, checkpoint reuse/coverage, compaction attempt/result, post-compaction estimate and final fit.
- The initial Turn preflight remains provider-lazy when context is normal. If semantic compaction is required, provider resolution is authorized because the Provider is the compaction dependency; failure is surfaced as a Context reduction error without mutating canonical history.
- README persistence wording is corrected narrowly; Context architecture and Thread persistence docs are updated with the final implemented schema and recovery behavior.
- Implementation follows test-first vertical slices at the agreed seams, runs focused tests frequently and performs full Python/Web validation where touched. A specific Chinese commit is created and pushed; push failure is reported without changing remotes or history.

## Testing Decisions

- Good tests observe public behavior and durable/model-visible outputs, not private helper call order. Exact deterministic policy metadata is public diagnostic behavior and may be asserted where it explains a decision.
- `ContextManager.assemble/render` is the primary Context seam: instructions, budget states, reduction ordering, selection, checkpoint reuse, final fit and canonical immutability are observed here.
- Provider request serialization is the boundary seam for the system-section bug: a real ContextPlan is rendered and encoded into an OpenAI-compatible payload, then its final system text boundary is asserted.
- `ThreadRuntime` is the lifecycle seam: root AGENTS refresh between Turns, fixed snapshot within a Turn, update_plan behavior, tool-result hard bounds, long-thread reduction and current interaction protection are tested through scripted Providers.
- `ThreadStore` is the durable seam: checkpoint and bounded ToolResult metadata round-trip, schema migration/reopen and restored rolling compaction are verified through both in-memory and SQLite implementations.
- Focused TokenEstimator tests use independent known ranges for English, Chinese, mixed text, tool schemas and a large result. They assert estimates are plausible and not equal to UTF-8 bytes rather than duplicating the estimator formula.
- Focused ContextBudgetPolicy tests cover explicit/default output reserve, independent safety margin, 80% soft threshold, hard limit and invalid configurations.
- Focused TaskPlan tests cover create/replace, all statuses, invalid status, multiple in_progress, maximum size, optional plan and canonical-history independence.
- Focused ToolResult reducer tests cover unchanged small content, head/marker/tail output, exact metadata and deterministic repeated application.
- Focused selector tests cover ordinary recent tail, one/multiple tool calls, crossing boundaries, incomplete interactions and no orphan result/call.
- Focused compactor tests replace only the external model boundary with a deterministic fake; they cover summary insertion, canonical immutability, first/rolling checkpoint sources, failure rollback, request budgeting and post-summary overflow.
- The end-to-end scripted-provider regression performs user → assistant → multiple tools → soft pressure → old-result prune → selection/compaction → next request and verifies source order, synthetic summary, raw tail, no duplicated old raw history, legal pairing, full canonical history, TaskPlan and ContextPlan metadata.
- Existing Context, AgentLoop, Conversation, Provider serialization, local tools, ThreadRuntime and persistence regressions remain green. Full Python tests, compile/type checks and Web checks are run if touched.
- Production sandbox tests skipped solely because bubblewrap is unavailable remain skips and are reported separately; no expectation is weakened.

## Out of Scope

- Recursive or nested AGENTS.md, parent/git-root lookup, cwd-scoped instruction hierarchy, CLAUDE.md, GEMINI.md or alternate filenames.
- Embeddings, vector databases, RAG, semantic retrieval, relevance/importance scoring, keyword retrieval or an LLM relevance judge.
- A memory system, arbitrary model-maintained TaskState, duplicated constraints, automatic persistent ThreadGoal or multi-level summary trees.
- Per-tool LLM ToolResult summaries, a large raw artifact store or full raw execution provenance redesign.
- Exact tokenizer integration for every Provider; the seam is provided but V1 remains heuristic.
- Provider-hosted code/filesystem/shell execution, OpenAI Agents SDK or any high-level agent framework.
- Broad AgentLoop, Conversation, Provider, tool, persistence or UI replacement unrelated to the Context pipeline.
- Complex KV-cache APIs; V1 only preserves a stable prefix and deterministic source order.

## Further Notes

- Requirement grilling settled ambiguous values with recommended defaults: 64 KiB root-instruction limit, 20 plan steps, one in_progress, 80% soft threshold, 20% recent-tail target and one prune/one compact attempt per model step. All remain centralized and test-overridable.
- Baseline focused validation before implementation: 108 Context/OpenAI-compatible/local-tool tests pass. The symlink-loop regression currently passes on this filesystem, but code inspection confirms `Path.exists()` can classify resolution failures as absence on other platforms; the fix must preserve the IO_ERROR contract explicitly.
- The repository has no configured external issue tracker or `ready-for-agent` vocabulary document. This versioned spec and the accompanying repository tickets are the publication artifacts; no external issue is fabricated.
- Existing untracked `.playwright-cli/` and `output/` directories predate this work and must not be staged.

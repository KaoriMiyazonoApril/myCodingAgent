# Kernel Bug Fix、Lifecycle Hardening 与 Freeze Readiness 规格

Status: implemented; freeze readiness reviewed against the current code and tests

## Problem Statement

当前 Coding Agent Kernel 已形成清晰的 Thread → Turn → Step 生命周期、Provider abstraction、
ThreadStore、ContextManager 与本地工具边界，但代码审查和本轮现状核验确认：持久
ProcessSession 仍会被新 Turn 重绑事件 sink，Turn cancellation 会清理整个 Thread 的全部
Session，Turn terminal event 后仍可能出现同 Turn 的异步进程事件；pending approval 只存在于
事件流，刷新后无法恢复；生产 Provider 每 Turn 新建并长期持有一个 HTTP client；退出 Session、
dead tombstone 和 SQLite 事件保存均存在无界增长或 O(N²) 写放大。

这些问题会造成跨 Turn 事件归属错误、错误终止仍合法的持久进程、审批 UI 永久失去操作入口、
长期 Host 资源增长，以及长 Thread 持久化性能退化。Kernel 在这些 invariant 被修复并验证前不应
冻结。与此同时，本轮不得借 hardening 扩展 HistorySelector、HistoryCompactor、Project
Instructions discovery、TaskState workflow、System Prompt 或 Skill framework。

## Solution

以稳定 Session ownership 为统一根修复：每个 ProcessSession 在创建时冻结 owner Thread 与 owner
Turn；已有 Session 不再被后续 Turn 重绑；取消按 owner Turn 定向清理；Thread close 与 Host
shutdown 才清理 Thread 全部资源；所有 Turn terminal event 关闭该 Turn 的普通事件通道。退出进程
立即释放 OS/PTY/transport/callback 等重资源，仅以 TTL/capacity 有界的轻量完成结果和 tombstone
支持后续查询。

把 pending approval 纳入 canonical live Thread snapshot，使 HTTP reload 与 SSE reconnect 都能重建
approve/deny UI；Host restart 仍按现有持久化契约把未完成 Turn 收敛为
`FAILED/runtime_restarted`，不伪造可恢复 Future。生产 Provider 使用独立于 model/generation options
的 transport/client pool，并把真正的 client 创建延迟到模型调用需要发生时。ThreadStore 将状态
更新和新增 durable event 作为单次增量事务提交，避免反复删除和重建完整事件表。

普通文件继续使用同目录临时文件加 replacement atomicity；已有 hard-link target 继续原 inode
写入以保留 alias semantics，并在文档中明确它不具备相同的 crash-atomic replacement 保证。当前
canonical containment 的 check-then-use TOCTOU 限制被准确记录，本轮不以 Linux-only 大重构破坏
跨平台 seam。历史 specs 标注 current/superseded 关系，`pack.bat` 生成最小 source-review ZIP，
明确包含 backend/frontend tests 与必要配置并排除 generated artifacts。

## User Stories

1. As a Runtime maintainer, I want every ProcessSession to freeze its owner Thread and owner Turn at creation, so that lifecycle ownership cannot drift.
2. As a user, I want a Session created by Turn A to remain usable after Turn A completes, so that persistent process workflows continue across Turns.
3. As a user, I want Turn A Session output never attributed to Turn B, so that activity and audit history remain truthful.
4. As a Runtime maintainer, I want cross-Turn `write_stdin` access not to transfer Session ownership, so that access and ownership are distinct concepts.
5. As a user, I want cancelling Turn B to terminate only running Sessions owned by Turn B, so that older persistent Sessions survive.
6. As a user, I want closing a Thread to terminate all Sessions created by any Turn in that Thread, so that explicit closure releases the whole resource container.
7. As a Host operator, I want Host shutdown to await and reap every remaining process, PTY, reader, watchdog and cleanup task, so that shutdown leaves no resource behind.
8. As an event consumer, I want completed, failed, cancelled and limit-reached events to be terminal for their Turn, so that reducers never observe normal Turn events afterward.
9. As an event consumer, I want delayed process callbacks after a terminal event suppressed or represented outside the closed Turn channel, so that terminal ordering is enforceable.
10. As a debugger, I want Session results and lifecycle metadata to expose immutable owner identifiers, so that ownership can be inspected without private object access.
11. As a browser user, I want a pending approval included in the Thread snapshot, so that a reload restores the actionable approval card.
12. As a browser user, I want an approval ID, safe tool-call summary, policy reason, permission/profile data and timeout information in that snapshot, so that approve/deny needs no replayed historical event.
13. As a browser user, I want SSE reconnect after the approval event cursor to retain the approval UI, so that event cursors cannot hide current actionable state.
14. As a Runtime maintainer, I want approval resolution to validate both Thread and approval ID, so that stale or cross-Thread actions remain rejected.
15. As an operator, I want an interrupted approval after Host restart to become one auditable runtime-restarted failure, so that the system does not pretend an in-memory Future survived.
16. As a Host operator, I want repeated Turns against one Provider transport identity to reuse an HTTP connection pool, so that client count does not grow linearly.
17. As a user, I want model, temperature, reasoning effort and generation options to remain Turn-scoped request settings, so that changing them does not create needless transports.
18. As a Provider administrator, I want endpoint/credential/transport identity changes to replace or isolate the corresponding client safely, so that credentials are not mixed.
19. As a Runtime maintainer, I want context-limit, workspace-busy, workspace-invalid and pre-start cancellation paths not to create an HTTP client, so that rejected Turns allocate no model transport.
20. As a Host operator, I want model invocation failure and cancellation to leave pooled clients valid or close non-reusable clients reliably, so that error paths leak nothing.
21. As a long-running Thread user, I want naturally exited Sessions to release process handles, PTYs, pipes, transports, large live buffers and event callbacks immediately, so that completed processes become lightweight.
22. As a model, I want one bounded final poll result for a recently exited Session, so that final output remains recoverable without retaining the process object.
23. As a long-running Thread user, I want completed results and dead-session tombstones bounded by capacity and/or TTL, so that Session history cannot grow without limit.
24. As a model, I want evicted historical Session IDs to fail with a stable safe error, so that bounded retention does not produce undefined behavior.
25. As a persistence operator, I want each durable event inserted once in order, so that SQLite work grows linearly with event count.
26. As a persistence operator, I want Thread snapshot/state updated transactionally with a terminal or approval event, so that restart never observes a half transition.
27. As an SSE client, I want restart recovery to preserve event order, IDs, sequence watermark and cursor replay without duplicates, so that reconnect remains deterministic.
28. As an embedder, I want the ThreadStore abstraction to remain provider-independent and usable in memory, so that persistence optimization does not leak SQLite into Runtime.
29. As a workspace user, I want internal symlinks and hard links to remain legal while external symlink escapes, absolute paths and traversal are denied, so that hardening preserves current autonomous workspace semantics.
30. As a workspace user, I want writing one hard-link alias to update every alias of that inode, so that link semantics are not silently broken.
31. As a reviewer, I want docs to distinguish normal atomic replacement from in-place hard-link writes, so that crash guarantees are not overstated.
32. As a security reviewer, I want the pathname check-then-use TOCTOU limitation documented unless a small cross-platform-safe hardening is available, so that containment claims remain accurate.
33. As a future maintainer, I want historical specs explicitly marked superseded where they conflict with the current workspace architecture, so that obsolete link bans are not reintroduced.
34. As a context maintainer, I want canonical history preserved and context overflow explicit, so that this hardening never silently selects or compacts history.
35. As a future Context Policy author, I want tokenizer and KV-cache improvements recorded as follow-up observations, so that they do not expand the Kernel freeze scope.
36. As a reviewer, I want the source-review ZIP to contain backend/runtime source, frontend source, Python tests, frontend tests, docs/specs and required configuration, so that another Agent can reproduce the review.
37. As a reviewer, I want the ZIP to exclude dependencies, build output, caches, bytecode, coverage, IDE state and temporary artifacts, so that it stays minimal and safe.
38. As a maintainer, I want all P0/P1 fixes protected by behavior-level regression tests at existing public seams, so that implementation details may evolve without losing invariants.
39. As a coursework reviewer, I want AgentLoop to remain thin and Provider SDK types kept below the Provider abstraction, so that hardening preserves the coursework architecture.
40. As a project owner, I want one final independent freeze review after implementation and full validation, so that only genuine P0/P1 blockers can prevent a YES decision.

## Implementation Decisions

- Priority is fixed: Session ownership → cancellation isolation → terminal event closure → approval recovery → Provider lifecycle/preflight → Session retention → incremental event persistence → filesystem/docs → packaging.
- Thread remains the long-lived resource container; Turn remains one user task boundary; Step/model/tool execution remains below Turn. Persistent ProcessSession is Thread-resident but carries immutable creator Turn ownership.
- Session owner data includes `session_id`, `owner_thread_id` and `owner_turn_id`. Owner data is immutable and appears in safe public Session result/event metadata.
- A Turn may access an older Session by opaque ID within the same Thread, but access does not reassign ownership. Sessions are never shared across Threads.
- Per-Turn process routing must use a captured immutable routing context. Updating the current Turn context may affect only Sessions created afterward and must never mutate existing Session callbacks.
- Turn cancellation calls an owner-filtered Session cleanup operation. Thread close and Host shutdown call the all-Sessions cleanup operation. Naturally completed cached results are not “killed.”
- Terminal types include `turn_completed`, `turn_failed`, `turn_cancelled` and `turn_limit_reached`. Once one is appended, the emitter rejects or safely ignores later normal events for that Turn while preserving the terminal event itself as last.
- Process callbacks racing terminalization must be drained before terminal emission when they belong to active work, or gated after terminalization when they come from persistent background work. They must never be rebound to another Turn.
- Pending approval is a detached public value owned by the active Turn/ToolCoordinator and projected into `ThreadSnapshot`; it contains only data already safe for the approval event/UI. Futures, tasks and policy objects remain runtime-only.
- Live snapshot is the source of truth for an actionable approval. Durable restart does not restore the Future; existing runtime-restart recovery remains authoritative.
- Frontend hydration initializes approval state from the snapshot, then applies idempotent SSE changes. A snapshot recovery frame replaces stale actionable state rather than relying on replay of `approval_requested`.
- Provider-specific configuration remains below the `LLMProvider`/resolver boundary. The production composition owns a bounded client pool keyed by actual transport identity such as provider configuration, endpoint, credential identity and transport options.
- Model identity and model capabilities may select a lightweight per-invocation adapter/profile, but model/generation changes must not create a new HTTP connection pool. Credential rotation must not keep an unbounded chain of old live clients.
- Client construction is lazy enough that workspace validation, lease conflict, context-budget rejection and pre-start cancellation do not allocate a transport. Host shutdown closes every pooled client exactly once.
- ProcessManager separates active `ProcessSession` objects from lightweight completed results. Terminal process cleanup releases OS and asyncio resources immediately. Completed results and tombstones use explicit bounded retention; concrete limits should be small enough for coursework scale and large enough for ordinary follow-up polls.
- ThreadStore exposes an incremental semantic-transition seam. Runtime continues to submit provider-independent state; Local SQLite performs one state UPSERT plus idempotent INSERT of only new durable events in the same transaction. Full-state save remains available for initial import/test compatibility but is not used once per event.
- Event IDs remain unique, Thread sequence remains monotonic, durable ordering uses sequence with deterministic tie-breaking, and duplicate transition delivery is idempotent.
- Existing workspace-internal symlink/hard-link behavior is retained. Normal replacement writes and hard-link-preserving in-place writes have distinct documented durability guarantees.
- No broad Linux-only filesystem rewrite is required for freeze. If dirfd/openat-style hardening cannot be introduced locally without breaking portability or internal links, record the TOCTOU limitation and defer it.
- Current architecture documentation becomes the authority. Historical documents are retained but marked superseded where link-syscall rules conflict; no obsolete seccomp link ban is restored.
- Context token estimation, stable-prefix ordering, Project Instructions discovery, selection and compaction remain unchanged in code unless a truly local interface-only change is necessary. The default decision is documentation-only follow-up.
- `pack.bat` explicitly selects source, tests, docs and required root/web configuration, copies through an exclusion filter, and excludes its output archive. It must not zip the entire repository indiscriminately.
- Existing user modifications in the worktree are preserved. The streaming terminal-error fix already present in the worktree is validated and included only if it remains coherent with this implementation.
- Implementation uses test-first work at the seams below, performs a code review, writes a specific Chinese commit message, and pushes the configured branch. A push failure is reported without altering remotes or history.

## Testing Decisions

- Good tests observe public state, events, process liveness, HTTP responses, frontend UI state, SQLite rows and archive contents. They do not assert incidental helper call order except where SQL write amplification itself is the regression.
- `ThreadRuntime` is the highest lifecycle seam: multi-Turn ownership, cancellation isolation, Thread close, terminal ordering, approval snapshot and Host shutdown are tested here with deterministic providers and sandbox processes.
- `ProcessManager` is the focused resource seam: immutable owner metadata, owner-filtered cancellation, immediate heavy-resource cleanup, final polling, capacity/TTL eviction and tombstone bounds are tested directly.
- Host HTTP/SSE is the recovery seam: request approval, fetch Thread snapshot as if reloading, reconnect after the approval event cursor, approve and deny, and verify Turn continuation.
- React snapshot hydration and reducer tests verify approval reconstruction and resolution without requiring the original event replay. Existing event-client recovery tests are extended rather than replaced.
- Production runtime composition is the Provider lifecycle seam: many success Turns reuse transport; context-limit, workspace-busy, invalid workspace and cancellation allocate none; model errors do not grow clients; shutdown closes all clients.
- ThreadStore/Local SQLite is the persistence seam: instrument SQL or inspect trace callbacks to prove events are not repeatedly deleted/reinserted; restart verifies exact ordering, IDs, watermark, replay and absence of duplicates.
- WorkspaceFilesystem tests retain traversal, external absolute path, external symlink, internal symlink and internal hard-link coverage; an added hard-link write test asserts both aliases still share inode/content and documents the non-identical crash guarantee.
- Terminal invariant tests cover completed, failed, cancelled and limit-reached Turns, including delayed async process output. For each Turn, no later ordinary event with that Turn ID is accepted.
- Session stress tests create substantially more short-lived Sessions than retention capacity, intentionally skip polls, wait for natural exit, then assert active heavy objects are zero/bounded and lightweight caches/tombstones remain within configured limits.
- End-to-end Host smoke uses a deterministic/mock provider and real Runtime/HTTP/SSE/tool seams; no external credential or paid model call is required.
- Browser automation is used if the repository environment can run it, covering Thread open/create, submit, streaming, approval reload/reconnect, approve/deny, cancel and persistent-process visibility. Environment limitations are reported with the lower-level coverage that substitutes for them.
- Final validation includes full Python tests, Python compile checks, frontend tests, typecheck, lint, production build, Git diff check, Host smoke, approval smoke, persistent process smoke and actual `pack.bat` execution plus ZIP listing audit.
- Freeze review classifies every requested P0/P1 as `FIXED`, `NOT REPRODUCIBLE`, `ALREADY FIXED IN CURRENT CODE` or `BLOCKED`, with implementation and test evidence.

## Out of Scope

- History selection, sliding windows, relevance retrieval, semantic retrieval, summarization, lossy compaction and checkpoint compaction.
- Project instruction discovery/merging and recursive `AGENTS.md` loading.
- TaskState planning, workflow, autonomous checkpoint management, System Prompt redesign and any Skill framework.
- Provider-hosted files, shell/code execution, agent frameworks, agent SDKs or replacement of the repository-owned Agent loop.
- Cross-process/multi-user ProcessSession persistence, reconnecting PTYs after Host restart, or sharing Sessions between Threads.
- A full filesystem rewrite around Linux `openat2`, a new sandbox product, or removal of legal workspace-internal links.
- Complex tokenizer infrastructure, semantic Context Policy changes or broad KV-cache prefix reordering.
- UI redesign unrelated to restoring current actionable approval/process lifecycle state.

## Further Notes

- Baseline before implementation: 406 Python tests pass; 20 frontend tests pass; frontend typecheck and lint pass.
- Confirmed current-code root causes are the mutable ProcessManager event sink rebinding all live/exited Sessions, Thread-wide `cancel_active` called from Turn cancellation, unbounded exited/dead Session containers, missing pending approval in `ThreadSnapshot`, per-Turn production Provider accumulation, Provider construction before context/lease preflight completes, and full `thread_events` DELETE/reinsert in every SQLite save.
- The hard-link code already preserves inode aliases by writing in place; the fix is primarily documentation accuracy plus explicit regression evidence, not reintroducing a link ban.
- Current containment uses canonical path check followed by later pathname operations and therefore has a theoretical concurrent rename/symlink TOCTOU window. This is P2 and not a freeze blocker once accurately documented.
- Current ContextBudget intentionally uses a conservative UTF-8 byte upper bound, and current ContextRenderer places stable base/project sections before dynamic runtime/task content. Token efficiency and deeper stable-prefix work belong to the next Context Policy phase.
- No issue tracker or triage-label vocabulary is configured in this repository/session, so this versioned spec document is the publication artifact; no external issue was fabricated.

# 多轮 ReAct Coding Agent Runtime 规格

## Problem Statement

项目已经具备供应商无关的消息模型、OpenAI Compatible 模型连接层，以及能够读取、修改、
搜索文件和执行本地命令的工具子系统，但尚未具备真正的 ReAct Agent Runtime。用户目前
无法提交一条编程任务，让模型在多轮 reasoning、tool call 和 tool result 之间自主循环，
也无法在同一工作区继续追加后续对话。

课程演示需要展示一次完整的 coding-agent 闭环：理解用户请求、检查文件、修改代码、运行
验证、报告结果并提供本轮文件差异。Runtime 还需要允许多个不相交工作区并发执行，同时
避免相同或相交工作区之间发生写入竞争。未来 React 前端会通过 HTTP 与后端交换配置、
用户消息、状态和事件，因此核心领域模型必须可序列化且独立于具体 Web 框架，但本规格不
实现 HTTP 或前端。

Agent Loop 必须保持短小、直接和易于阅读。重试、预算、取消、Policy、审批、工具分派、
事件、文件版本跟踪、workspace lease 及完成状态等复杂规则不得堆积在主循环中，也不得
通过大量只做一次转发的浅模块制造表面抽象。

## Solution

实现一个以 `ThreadRuntime` 为最高外部 seam 的多轮 ReAct Runtime。一个 `Thread` 绑定
一个不可变 workspace 并保存对话历史；一条用户消息创建一个 `Turn`，一个 Turn 内运行
一次完整 ReAct 循环。当前 Turn 结束后才能提交下一条消息，每个 Turn 可以从 Thread
默认设置中选择或覆盖模型与生成参数，但 Turn 启动后配置不可变。

Runtime 组合现有 `LLMProvider` 与 `ToolRegistry` seam，并通过少量深模块隐藏各类运行规则：
`Conversation` 维护合法模型历史，`ModelInvoker` 封装模型调用和重试，
`ToolCoordinator` 顺序执行工具并处理 Policy，`RunController` 管理预算和取消，
`ChangeTracker` 跟踪本轮文件版本与差异，`WorkspaceLeaseManager` 防止相交工作区并发。
调用方通过 Runtime 获取结构化 Snapshot、阶段级事件和 Turn Summary，无需理解这些内部
实现。

第一版状态全部保存在内存中。核心对象设计为 JSON 可序列化数据，未来可由 REST 接收命令、
由 SSE 发送事件。HTTP adapter、React 前端、数据库和上下文压缩均留待后续实现。

## Implementation Status

- Ticket 01 的最小 tracer bullet 已完成：调用方可通过 `ThreadRuntime` 创建内存 Thread、
  提交一个 Turn，并让完整模型响应与现有工具注册表顺序循环，直至模型返回最终文本。
- 当前公开结果已覆盖完整 Thread 生命周期、版本化安全设置、阶段事件、运行预算、跨
  workspace 并发、Policy 与审批、文件 diff，以及严格 workspace 链接和挂载安全；
  保守的上下文容量预检与提交幂等仍由后续 ticket 实现。
- `PromptBuilder` 已提供供应商无关的默认 coding-agent 约束，并把 Runtime 附加指令置于
  默认约束之后，避免调用方定制时覆盖 workspace 路径、文件工具、错误处理和验证要求。
- Runtime 集成测试通过临时 workspace 中的真实 `read_file` 工具验证闭环；更完整的
  read/edit/test/diff 场景随 ChangeTracker 和运行控制阶段补充。
- Ticket 02 已提取公共 `CommandSandboxBackend`：生产 Bubblewrap 与确定性测试 adapter
  共享 capability probe、输出、超时和取消 contract。普通 Runtime 测试不再要求开发主机
  可运行 Bubblewrap，生产组合仍 fail-fast 且绝不回退到 host shell。Ticket 09 已在生产
  backend 上补充 link syscall 禁令及对应 capability probe。
- Ticket 03 已实现多 Turn 与设置冻结：`Conversation` 独占合法历史，Runtime 通过公开
  provider 配置 ID 和模型解析每个 Turn 的 `LLMProvider`，`ModelInvoker` 将冻结的
  temperature、max tokens 与 allowlisted thinking 设置应用于整个工具链。默认设置更新
  使用单调版本和 `SETTINGS_CONFLICT`；`TurnSettingsOverride` 以 `UNSET` 区分继承和显式
  `None`，因此可以只覆盖一个字段且不改写 Thread 默认值。每个 provider 实例暴露所选
  模型的 `ThinkingCapabilities`，Runtime 在请求前验证 thinking 开关、budget 和 keep，
  不支持的组合以 `UNSUPPORTED_MODEL_SETTING` 失败。API key、base URL 与任意
  `extra_body` 均不属于公开设置。
- Ticket 04 已实现版本化公开状态与阶段事件：`ThreadSnapshot` 现在包含脱敏后的完整公开
  对话、时间戳和最近一次 `TurnSummary`，两者均通过 `to_dict()` 产生可直接 JSON 编码的
  独立数据。公开消息保留用户/助手文本、工具调用与安全工具结果，但排除 system prompt、
  reasoning、credentials 和内部 traceback。Summary 已提供终止原因、累计 usage、计数与
  时间戳；修改文件和 diff 字段现由 Ticket 08 的 `ChangeTracker` 填充。
- 每个 Turn 通过 `TurnEventEmitter` 产生带 schema version、UUID、时间戳和单调 sequence
  的完整阶段事件，包括 Turn 生命周期、模型完整响应与工具请求/开始/完成。Thread 使用
  可配置的定长 ring buffer；写入只淘汰最旧事件，不会等待消费者。`get_events()` 通过
  event ID 游标读取，游标已淘汰时返回 `cursor_expired = true`，调用方据此重新读取
  Snapshot。reasoning 默认不进入任何公开状态或普通事件；仅当后端显式配置 `debug` 时，
  才紧随模型响应发出独立的 `model_reasoning` 事件。
- Ticket 05 已实现 Turn 运行控制：`ModelInvoker` 仅对 retryable `LLMError` 在三次总尝试
  内重试；`RunController` 统一累计模型迭代、工具调用、usage、执行 deadline 与连续相同
  失败指纹。公开 `AgentLimits` 提供 20 次迭代、50 次工具调用和 15 分钟执行时间的默认值，
  并对调用方输入设置硬上限。预算终止返回带具体 `stop_reason` 的
  `LIMIT_REACHED` Summary；连续三次相同失败是固定的运行保护规则，而不是调用方可放宽的
  设置，同时为同一批次内未执行的工具调用补齐结构化结果。
- `ThreadRuntime.cancel_turn()` 会取消当前模型或工具协程并返回 `CANCELLED` Summary；命令
  工具沿既有 `CommandSandboxBackend` contract 终止整个进程组。RunController 还提供审批
  deadline 暂停/恢复语义，供 Policy 审批流程组合；全局并发上限与 workspace lease 已由
  Ticket 06 接入。
- Ticket 06 已实现相交 workspace 并发互斥：`WorkspaceLeaseManager` 在 Turn 启动时对真实
  规范化路径立即申请 lease，相同、祖先和后代路径均以 `WORKSPACE_BUSY` 拒绝且不排队；
  不相交 workspace 可以真正并发。Runtime 默认允许四个活跃 Turn，并通过后端构造参数在
  1–32 的硬范围内调整容量。多个空闲 Thread 仍可绑定同一 workspace，只有活跃 Turn 持有
  lease；完成、失败、取消和预算终止都在统一 `finally` 路径释放 lease。
- Ticket 07 已实现 Policy 与外部审批：`ToolCoordinator` 统一顺序执行、预算检查、Policy
  决策、审批等待、工具结果补齐和生命周期事件，让 `AgentLoop` 只保留模型调用、完成判断与
  一次工具批次委派。默认 `AllowAllPolicy` 只表达允许，不宣称识别危险命令；`DENY` 返回
  `POLICY_DENIED`，`REQUIRE_APPROVAL` 把 Thread 切换为 `WAITING_APPROVAL` 并发出带独立 ID
  的 `approval_requested`。调用方通过 `resolve_approval()` 批准或拒绝；取消与独立审批超时
  均安全终止，审批等待通过 `RunController` 暂停执行 deadline 且始终继续持有 workspace
  lease。
- Ticket 08 已实现每 Turn 文件变更跟踪：`ChangeTracker` 在文件工具首次写入前保存 original，
  成功写入后更新 final，因此重复修改只在 Summary 产生一份 original-to-final unified diff；
  新文件以 `/dev/null` 表达，`file_changed` 事件在每次实际文件工具变化后发出。成功
  `read_file` 与文件写入会刷新已知内容指纹，外部变化在覆盖前返回 `FILE_CHANGED`，模型
  重新读取后可安全重试。只由文件工具修改的 Turn 报告 `diff_complete = true`；实际执行过
  `run_command` 的 Turn 保守报告 `false`，且不会猜测无法恢复 original 的命令改动文件。
- Ticket 09 已实现严格 workspace 安全：Thread 创建拒绝 symlink 根，每个 Turn 在模型调用前
  完整扫描普通条目并拒绝 symbolic link、regular-file hard link 和嵌套 mount/bind mount；
  扫描按条目数与单调时间双预算 fail closed 为 `WORKSPACE_VALIDATION_LIMIT`。文件工具路径
  逐组件使用 no-follow metadata，Turn 验证后新出现的 symlink 或 hard link 也返回
  `WORKSPACE_LINK`。生产 `BubblewrapSandboxBackend` 通过 libseccomp 导出 cBPF，阻止
  `symlink`、`symlinkat`、`link` 和 `linkat`，并在组合时运行真实 link probe；libseccomp、
  内核过滤或 link-blocking 能力不足均提前报告 sandbox unavailable，绝不降级。过滤只阻止
  新建链接，可信只读系统树中的既有链接仍可供普通 Linux 命令解析。

## User Stories

1. As a coding-agent user, I want to submit a programming request, so that the model can autonomously inspect, modify, and validate my project.
2. As a coding-agent user, I want the agent to continue calling tools until it can provide a final answer, so that I do not have to manually drive each step.
3. As a coding-agent user, I want to add a new message after the current turn finishes, so that I can refine the result without losing useful conversation history.
4. As a coding-agent user, I want a new turn rejected while the current turn is still active, so that two turns cannot corrupt one conversation history.
5. As a coding-agent user, I want every thread bound to one immutable workspace, so that paths in earlier messages retain their meaning.
6. As a coding-agent user, I want to open multiple threads for the same workspace, so that I can keep separate conversations about one project.
7. As a coding-agent user, I want turns in unrelated workspaces to run concurrently, so that independent tasks do not block each other.
8. As a coding-agent user, I want overlapping workspace turns rejected with `WORKSPACE_BUSY`, so that concurrent agents cannot overwrite the same files.
9. As a coding-agent user, I want to cancel an active turn, so that I can stop work that is unnecessary, incorrect, or taking too long.
10. As a coding-agent user, I want cancellation to terminate an active command process group, so that stopping the agent does not leave background work running.
11. As a coding-agent user, I want to choose a model before each turn, so that I can use different models for different tasks.
12. As a coding-agent user, I want a model choice to remain fixed during one turn, so that one tool-call chain has consistent provider semantics.
13. As a coding-agent user, I want to adjust temperature and maximum output tokens, so that I can tune model behavior from a future frontend.
14. As a coding-agent user, I want to enable supported thinking options explicitly, so that provider-specific reasoning behavior is controlled rather than assumed.
15. As a coding-agent user, I want thread settings to act as defaults for later turns, so that I do not have to repeat common configuration.
16. As a coding-agent user, I want one turn to override thread defaults, so that a temporary model or temperature change does not rewrite my defaults.
17. As a coding-agent user, I want settings changed during a running turn to affect only the next turn, so that active execution remains reproducible.
18. As a coding-agent user, I want stale settings updates rejected, so that an old browser page cannot silently overwrite newer choices.
19. As a frontend author, I want settings to expose a monotonically increasing version, so that I can perform optimistic update checks.
20. As a frontend author, I want only public model settings exposed, so that API keys, base URLs, and private provider configuration do not reach the browser.
21. As a runtime author, I want a turn to capture an immutable `TurnConfig`, so that every model request in the turn uses the same effective settings.
22. As a runtime author, I want provider configuration referenced by an in-memory ID, so that credentials are not copied into thread state or events.
23. As a model, I want the six existing local tools available, so that I can read, write, edit, discover, search, and validate code.
24. As a model, I want multiple tool calls from one response executed in their original order, so that dependent operations remain deterministic.
25. As a model, I want every tool call to receive a matching tool result, so that provider history remains protocol-correct even after an error.
26. As a model, I want recoverable tool errors returned as structured results, so that I can correct arguments or choose a different action.
27. As a model, I want invalid raw tool arguments preserved in history, so that my original call and the resulting error remain consistent.
28. As a model, I want non-zero command exit codes reported as failures, so that failed tests cannot be mistaken for successful validation.
29. As a model, I want to receive command stdout, stderr, timeout, and exit metadata, so that I can diagnose validation failures.
30. As a model, I want ordinary tool failures to continue the loop, so that one failed attempt does not terminate a recoverable task.
31. As a coding-agent user, I want the agent to stop when the assistant returns no tool calls, so that a final natural-language answer completes the turn.
32. As a coding-agent user, I want the final response to summarize completed work and validation, so that I can assess the outcome quickly.
33. As a maintainer, I want the Agent Loop to express only model-call, completion, tool-execution, and history-update steps, so that its control flow is easy to audit.
34. As a maintainer, I want retry behavior hidden behind the model invocation module, so that provider error policy does not clutter the Agent Loop.
35. As a maintainer, I want tool Policy and approval behavior hidden behind the tool coordination module, so that authorization can evolve independently.
36. As a maintainer, I want budget and cancellation checks hidden behind a run controller, so that termination rules have one source of truth.
37. As a maintainer, I want file change tracking hidden behind one module, so that diff and conflict semantics are consistent for every write tool.
38. As a maintainer, I want workspace overlap logic hidden behind a lease manager, so that every caller uses the same path rules.
39. As a runtime author, I want retryable LLM failures retried at most three times, so that transient provider failures can recover without an infinite loop.
40. As a runtime author, I want authentication, validation, and other non-retryable LLM failures to fail immediately, so that deterministic errors are not amplified.
41. As a runtime author, I want a maximum of 20 model iterations per turn by default, so that a confused agent cannot loop indefinitely.
42. As a runtime author, I want a maximum of 50 tool calls per turn by default, so that one response chain cannot exhaust local resources.
43. As a runtime author, I want a 15-minute execution budget per turn by default, so that abandoned work eventually terminates.
44. As a runtime author, I want a default global concurrency limit of four active turns, so that independent work remains bounded.
45. As a runtime author, I want identical tool name, normalized arguments, and error code repeated three times to terminate the turn, so that deterministic failure loops stop early.
46. As a coding-agent user, I want budget exhaustion reported as `LIMIT_REACHED`, so that an incomplete turn is not presented as success.
47. As a coding-agent user, I want repeated failures reported with a specific stop reason, so that I understand why the agent stopped.
48. As a coding-agent user, I want provider context limits detected before an oversized request where practical, so that I receive `CONTEXT_LIMIT` and can create a new thread.
49. As a coding-agent user, I want no silent history deletion or summarization in the first version, so that tool-call and tool-result relationships remain intact.
50. As a policy author, I want Policy decisions to be `ALLOW`, `DENY`, or `REQUIRE_APPROVAL`, so that future frontends can introduce interactive control without changing the loop.
51. As a policy author, I want the first default Policy to allow all valid tool calls, so that the coursework demo completes autonomously without pretending to classify dangerous commands.
52. As a coding-agent user, I want a denied tool call returned as `POLICY_DENIED`, so that the model can adapt without corrupting history.
53. As a frontend author, I want an approval-required turn to pause and emit an event, so that a future UI can resume or reject it explicitly.
54. As a frontend author, I want approval waiting excluded from the execution budget, so that human response time does not consume agent compute time.
55. As a frontend author, I want approval to have its own configurable timeout, so that abandoned requests do not remain pending forever.
56. As a coding-agent user, I want each turn to report its modified files and unified diffs, so that I can review what the agent changed.
57. As a coding-agent user, I want a file's first pre-write content compared with its final turn content, so that repeated edits produce one coherent diff.
58. As a coding-agent user, I want external file changes detected before a known file is overwritten, so that the agent does not silently erase newer work.
59. As a model, I want a `FILE_CHANGED` result after an optimistic version conflict, so that I can reread the file or abandon the edit.
60. As a coding-agent user, I want diff completeness stated explicitly, so that command-driven file mutations are not falsely presented as fully captured.
61. As a runtime author, I want source modifications encouraged through file tools, so that change tracking remains accurate and reviewable.
62. As a workspace owner, I want workspace symbolic links rejected, so that path aliases cannot bypass workspace assumptions.
63. As a workspace owner, I want regular files with multiple hard links rejected, so that path-disjoint workspaces cannot secretly share one inode.
64. As a workspace owner, I want nested mounts and bind mounts rejected, so that a workspace cannot expose another filesystem tree.
65. As a workspace owner, I want every traversed path component checked with non-following metadata operations, so that a link cannot be hidden in a parent directory.
66. As a workspace owner, I want workspace validation to fail closed when its resource budget is exceeded, so that an incomplete scan is never accepted as safe.
67. As a workspace owner, I want command execution prevented from creating workspace links, so that shell commands cannot bypass the file-tool rules.
68. As an application author, I want sandbox link-blocking capability probed before a turn starts, so that unsupported hosts fail before model execution.
69. As an application author, I want trusted read-only system links allowed in the sandbox runtime, so that ordinary Linux commands can still execute.
70. As a frontend author, I want a complete `ThreadSnapshot`, so that a refreshed view can reconstruct the current conversation and status.
71. As a frontend author, I want a structured `TurnSummary`, so that I can render the outcome without parsing model prose.
72. As a frontend author, I want stage-level `AgentEvent` objects, so that I can display model responses, tool activity, state changes, and file changes incrementally.
73. As a frontend author, I want event sequences to increase monotonically within a turn, so that concurrent timestamps cannot reorder the UI.
74. As a frontend author, I want public messages to contain user text, assistant text, tool calls, and safe tool results, so that a refresh retains the meaningful transcript.
75. As a frontend author, I want reasoning hidden by default, so that private model protocol data is not transmitted accidentally.
76. As a frontend author, I want debug reasoning emitted as a separate event when explicitly enabled, so that I may choose whether to display it without confusing it with the final answer.
77. As a security-conscious user, I want reasoning, API keys, provider secrets, and internal tracebacks excluded from ordinary snapshots and summaries, so that debug data does not become a public contract.
78. As a runtime author, I want an event buffer with bounded memory, so that a disconnected or slow frontend cannot exhaust the process.
79. As a frontend author, I want to recover from expired buffered events by requesting a fresh snapshot, so that event retention does not need to be permanent.
80. As a frontend author, I want duplicate turn submissions prevented, so that an HTTP retry cannot create the same task twice.
81. As a frontend author, I want a busy thread to reject another turn atomically, so that two browser tabs cannot start concurrent turns in one conversation.
82. As a frontend author, I want future HTTP commands and responses to use versioned JSON data, so that Python implementation types do not leak across the transport seam.
83. As a coursework reviewer, I want observable ReAct events and final tool history, so that I can see the model-tool loop rather than trust an opaque final answer.
84. As a coursework reviewer, I want normal, failure, cancellation, concurrency, and security scenarios covered by tests, so that the demonstration is reproducible.

## Implementation Decisions

- `Thread` is the long-lived conversation concept. It owns one immutable, normalized workspace reference, ordered conversation history, mutable default settings, a current status, an optional active Turn ID, and timestamps. Thread data is in-memory in the first version.
- `Turn` is one user message plus one complete ReAct execution. A Thread may have many sequential Turns. A new Turn may be submitted only while its Thread is `IDLE`; submission changes the state atomically so two callers cannot both start.
- Thread states are `IDLE`, `RUNNING`, `WAITING_APPROVAL`, and `CLOSED`. Turn states are `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, and `LIMIT_REACHED`. A completed, failed, cancelled, or limited Turn returns its Thread to `IDLE` unless the Thread is being closed.
- `ThreadRuntime` is the highest external seam and the primary test surface. Its small interface covers Thread creation, settings updates, Turn submission, cancellation, approval resolution, event consumption, snapshot retrieval, and Thread closure. Transport adapters call this interface rather than reaching into Agent Loop modules.
- The Agent Loop is deliberately narrow: request a model response, append the assistant message, finish if no tool calls exist, otherwise execute all calls in order and append all matching tool results. It does not implement retry, Policy, event serialization, file diffing, workspace locking, or provider-specific reasoning rules inline.
- `Conversation` owns ordered internal `Message` values and constructs each model request history. It preserves valid tool-call/tool-result pairings and delegates provider-specific reasoning encoding to existing model capabilities. Switching models between Turns retains text and tool history while allowing the selected provider adapter to filter reasoning according to its capabilities.
- `ModelInvoker` wraps the existing `LLMProvider` seam. It applies immutable Turn generation settings and retries only errors whose existing taxonomy marks `retryable`. The default is at most three attempts with short exponential backoff. Authentication, invalid request, configuration, and response-parse failures do not retry.
- `ToolCoordinator` wraps the existing `ToolRegistry` execution seam without moving tool capability behavior into the Runtime. It executes calls sequentially, consults Policy, manages an approval pause, emits lifecycle events, propagates cancellation, converts results to conversation blocks, and ensures every accepted call receives a corresponding result.
- Policy has three decisions: `ALLOW`, `DENY`, and `REQUIRE_APPROVAL`. The initial adapter allows all valid calls. `DENY` produces a safe `POLICY_DENIED` tool result. `REQUIRE_APPROVAL` changes the Thread to `WAITING_APPROVAL`, stops launching later calls, and awaits an external decision or an independent approval timeout.
- `RunController` is the single source of truth for one Turn's maximum iterations, maximum tool calls, execution deadline, cancellation, approval-clock suspension, and repeated-failure detection. `ThreadRuntime` owns the shared active-Turn capacity. Defaults are 20 model iterations, 50 tool calls, 15 minutes of execution time, three identical consecutive failures, and four active Turns globally.
- A repeated failure fingerprint consists of tool name, canonically normalized arguments, and error code. Only three consecutive identical fingerprints stop a Turn. Different failed operations do not count as one repeated sequence.
- Normal completion occurs only when the assistant response contains no tool calls. Budget exhaustion and repeated failure produce `LIMIT_REACHED` with an exact stop reason. Cancellation produces `CANCELLED`. Non-retryable model or Runtime failures produce `FAILED`. Ordinary tool errors remain in the loop.
- `ThreadSettings` contains the public defaults used by future Turns and has a monotonically increasing version. An update supplies its expected version; stale writes fail with `SETTINGS_CONFLICT`. Settings accepted during an active Turn affect only subsequent Turns.
- `TurnConfig` is an immutable snapshot created when a Turn starts. A field-level `TurnSettingsOverride` is merged with Thread defaults first; an internal `UNSET` sentinel means inherit while explicit `None` clears an optional value for that Turn. The snapshot contains provider configuration reference, public model name, temperature, maximum output tokens, supported thinking options, PromptBuilder output, reasoning visibility, and effective Agent limits. Values cannot change during the Turn.
- Frontend-controllable provider data is allowlisted. The public settings include provider configuration ID, public model, temperature, maximum output tokens, supported thinking options, and bounded Agent limits. API keys, base URLs, raw provider bodies, credentials, and backend hard ceilings are not public settings.
- Thinking request fields are fail-closed against the selected model's `ThinkingCapabilities` before the first model request. Provider defaults may be refined by exact-model `ModelProfile` overrides; unsupported thinking, budget, or keep values return `UNSUPPORTED_MODEL_SETTING` rather than passing an unchecked private body to the provider.
- The workspace reference is immutable after Thread creation. Selecting another folder requires another Thread so old message paths and diffs never change meaning.
- `WorkspaceLeaseManager` normalizes real workspace roots and treats equal paths or ancestor/descendant relationships as overlapping. Multiple Threads may refer to overlapping roots, but only one overlapping Turn may hold a lease. A conflict fails immediately with `WORKSPACE_BUSY`; there is no implicit queue. A lease is held during `RUNNING` and `WAITING_APPROVAL` and released on every terminal path.
- Before a Turn enters the loop, workspace validation rejects a symlink root, any symlink path entry, regular files whose hard-link count exceeds one, and nested mount or bind-mount points. Validation is complete within configured entry and time budgets; reaching either budget fails closed with `WORKSPACE_VALIDATION_LIMIT`.
- File operations inspect path components without following links. The earlier behavior that allowed internal file symlinks is intentionally replaced by a strict workspace prohibition.
- `CommandSandboxBackend` becomes a real seam because the strict production bubblewrap adapter and a deterministic test adapter both need to satisfy it. Its interface includes a capability probe and cancellable command execution. The production adapter must prevent workspace `symlink`, `symlinkat`, `link`, and `linkat` operations or report itself unavailable before the Turn starts.
- The strict link prohibition applies to the persistent workspace. Trusted read-only links in the sandbox system runtime remain allowed so standard Linux executables work. Hostile external processes replacing workspace entries during a Turn are not claimed to be fully preventable by the application.
- `ChangeTracker` records a file's original state before the first file-tool mutation in a Turn and its latest state after each successful mutation. A file modified repeatedly produces one original-to-final unified diff. New and deleted states are represented explicitly.
- The tracker records a content fingerprint when a file is read or first prepared for writing. A later write that observes a different disk fingerprint returns `FILE_CHANGED`; the model may reread and retry. The tracker updates its known fingerprint after its own successful write.
- Diff completeness is explicit. File-tool changes are tracked completely. Arbitrary source changes made inside `run_command` are not guaranteed to have a recoverable pre-command snapshot, so a Turn that may contain such changes reports `diff_complete = false`. The default prompt instructs the model to prefer file tools for source edits.
- `PromptBuilder` supplies a provider-independent default system prompt and permits Runtime-provided additional system instructions. It does not automatically read `AGENTS.md` in this version. Loop enforcement remains code-owned rather than encoded only in prose.
- The default prompt tells the model that it is a local coding agent, paths are workspace-relative, relevant files should be inspected before modification, source changes should prefer file tools, suitable validation should run before completion, tool errors must be handled honestly, and the final answer should summarize work and validation.
- Conversation history is not compressed, summarized, or silently truncated. A conservative history message/token budget rejects a new Turn with `CONTEXT_LIMIT` when the Thread cannot safely fit another request. The user must create a new Thread.
- `ThreadSnapshot` is a JSON-compatible public view containing Thread ID, normalized public workspace identifier, Thread status, public messages, active Turn ID, sanitized settings and version, timestamps, and the latest Turn summary where applicable. It never serializes internal Python objects directly.
- Public messages contain user and assistant text plus structured tool calls and safe tool results. System prompts, credentials, internal tracebacks, and raw reasoning are excluded from the ordinary message list.
- `TurnSummary` contains Turn ID, status, stop reason, final assistant text, modified files, file diffs, diff completeness, iteration/tool-call counters, accumulated usage, start/end timestamps, and an optional safe error summary. It is the final result of one Turn, not the mutable state of the whole Agent.
- Every `AgentEvent` has schema version, event ID, Thread ID, Turn ID, monotonically increasing per-Turn sequence, type, timestamp, and JSON-compatible payload. Stage-level types cover Turn lifecycle, complete model response, tool request/start/finish, approval request/resolution, file changes, settings updates, cancellation, completion, failure, and limit termination.
- Reasoning transmission is backend-controlled by `reasoning_visibility`. The default `hidden` value emits no reasoning. Explicit `debug` emits a separate complete reasoning event after a model response; reasoning does not enter ordinary messages, summaries, or logs. The frontend may decide whether to render a reasoning event it has received.
- Events are kept in a bounded in-memory ring buffer and must never block Agent execution because a consumer is slow or absent. Each event sequence allows gap detection; a consumer whose cursor expired retrieves a fresh Snapshot.
- The future transport mapping uses REST-like commands for Thread creation, versioned settings updates, Turn submission, cancellation, approval, Snapshot retrieval, and Thread closure, with SSE for events. An HTTP adapter is not part of this implementation and the core Runtime must not depend on a Web framework.
- Future Turn submission accepts an idempotency key and atomically requires the Thread to be `IDLE`. This behavior is represented in the core interface even before an HTTP adapter exists.
- All external data has an explicit schema version. Compatibility is defined by JSON field semantics, not by importing Python dataclasses into a frontend.
- The existing non-streaming model call remains sufficient for the first version. Agent events describe complete model stages; LLM token deltas and live command stdout/stderr are not required.

## Testing Decisions

- Tests assert observable behavior through interfaces. They do not assert private helper calls, internal module graphs, lock implementation details, retry-loop local variables, or the exact number of internal classes.
- The primary integration seam is `ThreadRuntime`. Most scenarios use its Thread creation, Turn submission, event consumption, cancellation, settings update, and Snapshot interfaces and assert the resulting messages, status, events, tool effects, and Turn Summary.
- The existing `LLMProvider` seam receives a scripted in-memory adapter in Runtime tests. Scripts return assistant text, valid and invalid tool calls, retryable errors, non-retryable errors, reasoning, and terminal responses without making network requests.
- The existing `ToolRegistry` remains the tool execution seam. Runtime integration tests use the real local file tools in temporary workspaces where practical, preserving current result and error contracts.
- `CommandSandboxBackend` receives contract tests shared by the deterministic test adapter and production bubblewrap adapter where the host supports it. Production capability tests verify that unavailable link-blocking support fails early rather than falling back.
- Workspace tests use temporary sibling, identical, parent, and child roots to demonstrate concurrent acquisition and overlap rejection. Tests use observable Turn results rather than inspecting locks.
- Conversation tests verify exact assistant tool-call and tool-result ordering across multiple iterations and multiple Turns, including raw invalid arguments and reasoning retention across provider changes.
- Model retry tests assert that retryable failures recover within the attempt budget and that non-retryable failures perform no unnecessary retry. Backoff timing is injected or disabled in tests rather than tested with wall-clock sleeps.
- Run-controller behavior is exercised through Runtime outcomes with small configured limits: normal completion, iteration limit, tool-call limit, deadline, repeated identical error, and cancellation.
- Change tracking tests assert original-to-final diffs across repeated file-tool edits, new files, optimistic `FILE_CHANGED` conflicts, and explicit incomplete diff status after command execution.
- Event tests assert schema version, IDs, monotonic sequence, safe payloads, status order, bounded-buffer gap behavior, Snapshot recovery, and default reasoning suppression. Debug tests verify reasoning appears only in its dedicated event.
- Settings tests assert immutable per-Turn snapshots, next-Turn application of mid-run updates, one-Turn overrides, version conflicts, model changes between Turns, hard server ceilings, and complete credential redaction.
- Security tests assert rejection of workspace symlinks, symlinked path components, hard-linked regular files, nested mounts where test infrastructure permits, validation-budget exhaustion, and command sandbox link-creation attempts.
- Existing provider tests are prior art for scripted provider responses and stable domain-type assertions. Existing local-tool tests are prior art for temporary workspace behavior, structured tool errors, command cancellation, and sandbox capability checks.
- The acceptance suite covers at least: no-tool completion; read/edit/test/diff; failed-test reporting; invalid argument recovery; independent workspace concurrency; overlapping workspace rejection; a second Turn on one Thread; model switching between Turns; command cancellation; budget and repeated-failure termination; optimistic file conflicts; strict link/mount rejection; ordered events consistent with Snapshot and Summary; and absence of credentials, tracebacks, and default-hidden reasoning from public data.
- Tests should survive refactoring inside deep modules. A test that must inspect past `ThreadRuntime`, `LLMProvider`, `ToolRegistry`, or `CommandSandboxBackend` seams requires justification.

## Out of Scope

- React UI, HTTP server, REST endpoint implementation, SSE server, WebSocket support, or frontend-generated types.
- Database persistence, process-restart recovery, distributed locks, multi-process Runtime coordination, or durable event storage.
- Mid-Turn user steering, concurrent Turns within one Thread, implicit Turn queues, or automatic cancellation of an older Turn.
- Automatic context compression, summarization, silent history deletion, or provider-specific tokenizers.
- Automatic loading or recursive precedence rules for `AGENTS.md` and other project instruction files.
- A production dangerous-command classifier. The default Policy allows valid calls; the Policy seam exists for future enforcement and approval.
- Accurate original-content capture for arbitrary file changes made by shell commands, formatters, build scripts, or external processes.
- Defense against a hostile external process that races workspace validation and replaces entries during execution.
- Git worktree creation, cross-workspace tasks, automatic merge resolution, persistent job sessions, or command reattachment.
- LLM token streaming, live command-output streaming, provider-hosted tools, hosted filesystems, and hosted code execution.
- Agent frameworks, orchestration frameworks, agent SDKs, or reuse of another coding-agent runtime.

## Further Notes

- The strict workspace-link requirement is the largest implementation risk. It requires a genuine sandbox capability, not only Python path validation, and may make package managers or build systems that create workspace symlinks incompatible. Capability failure must be explicit and early.
- The first version optimizes for a readable Agent Loop and strong observable contracts rather than maximum feature breadth. Complexity is concentrated behind deep module interfaces so future HTTP, persistence, approval policies, context compression, streaming, and richer sandbox adapters can be added without rewriting the loop.
- The current model and tool layers already provide the two principal lower-level seams: provider-independent `Message` history through `LLMProvider`, and structured local execution through `ToolRegistry`. The Runtime should extend these concepts rather than introduce parallel tool-call or message types.
- The specification intentionally distinguishes internal provider reasoning, public conversation messages, incremental Agent events, current Thread Snapshot, and terminal Turn Summary. Treating all five as one mutable "agent state" would make both the Runtime and future HTTP contract ambiguous.
- This specification is maintained under `docs/` at the user's explicit request and is not published to a GitHub Issue.

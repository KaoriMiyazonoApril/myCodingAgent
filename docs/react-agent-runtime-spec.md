# 多轮 ReAct Coding Agent Runtime 规格

> Status: historical/superseded for workspace security and lifecycle rules.
> Follow [Autonomous Web Workspace Refactor Spec](autonomous-web-workspace-refactor-spec.md)
> and [Kernel Freeze Lifecycle Hardening Spec](specs/kernel-freeze-lifecycle-hardening-spec.md)
> for current architecture. The old link-syscall/seccomp prohibition is not current:
> workspace-internal symlink and hard-link aliases remain legal under access-time canonical
> containment.

## Problem Statement

项目已经具备供应商无关的消息模型、OpenAI Compatible 模型连接层，以及能够读取、修改、
搜索文件和执行本地命令的工具子系统，但尚未具备真正的 ReAct Agent Runtime。用户目前
无法提交一条编程任务，让模型在多轮 reasoning、tool call 和 tool result 之间自主循环，
也无法在同一工作区继续追加后续对话。

课程演示需要展示一次完整的 coding-agent 闭环：理解用户请求、检查文件、修改代码、运行
验证、报告结果并提供本轮文件差异。Runtime 还需要允许多个不相交工作区并发执行，同时
避免相同或相交工作区之间发生写入竞争。独立 Agent Host/React 子系统现已通过 HTTP 与
后端交换配置、用户消息和状态，并通过 SSE 读取 Runtime 事件；核心领域模型仍保持可序列化
且独立于具体 Web 框架。Host/Web 的边界由 `docs/local-web-ui-host-spec.md` 约束。

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

Runtime 的默认嵌入模式仍可使用内存状态，但生产 Host 通过 ThreadStore 将
provider-independent 的 Thread、canonical Conversation、Turn summary、语义事件和幂等
记录保存到用户状态目录中的 SQLite。核心对象通过显式 JSON mapper 序列化，数据库不进入
workspace，也不保存 Provider secret 或进程运行时对象。独立 Host 通过 REST 接收命令、
由 SSE 转发 EventBuffer 事件；HTTP adapter 与 React 前端不进入 Runtime 模块。

## Implementation Status

- Thread persistence 已接入 ThreadStore seam：InMemoryThreadStore 保留测试和嵌入的
  无磁盘模式，LocalThreadStore 以版本化 SQLite 保存 canonical messages、ThreadSettings、
  Turn history、语义事件和幂等请求。Runtime 以 Thread-scoped sequence 恢复 EventBuffer，
  对重启时的 running/waiting_approval Turn 生成 FAILED/runtime_restarted，不恢复
  ProcessManager、PTY、approval future、stream assembler 或未完成消息。生产 Host 从持久
  Runtime 枚举历史 Thread；删除的 workspace 仍可读，但新 Turn 返回
  WORKSPACE_UNAVAILABLE。详细责任边界、schema 与兼容范围见
  docs/thread-persistence.md。

- Workstream B Phase 1 已补齐 `ApprovalMode`（`UNTRUSTED`、`ON_REQUEST`、`NEVER`）的
  Thread 默认、单 Turn 覆盖与冻结 `TurnConfig` 语义。命令策略在 Policy 层对模型提交的
  `run_command`/`exec_command` 做保守分类，并返回带稳定 reason code/message 的不可变
  `PolicyResult`；Sandbox 和工具实现不负责风险判断。Host 提供薄的审批 resolution
  command，前端只展示 Runtime 发出的原因并提交 approve/deny。

- Ticket 01 的最小 tracer bullet 已完成：调用方可通过 `ThreadRuntime` 创建内存 Thread、
  提交一个 Turn，并让完整模型响应与现有工具注册表顺序循环，直至模型返回最终文本。
- 当前公开结果已覆盖完整 Thread 生命周期、版本化安全设置、阶段事件、运行预算、跨
  workspace 并发、Policy 与审批、文件 diff，以及选择时根校验和访问时 canonical containment；
  同时具备保守的上下文容量预检与 Turn 提交幂等。
- `PromptBuilder` 已提供供应商无关的默认 coding-agent 约束，并把 Runtime 附加指令置于
  默认约束之后，避免调用方定制时覆盖 workspace 路径、文件工具、错误处理和验证要求。
- Runtime 集成测试通过临时 workspace 中的真实本地工具验证 read/edit/test/diff 完整闭环，
  并单独验证非零测试退出码作为结构化失败回传给模型后由最终答复诚实报告。
- Ticket 02 已提取公共 `CommandSandboxBackend`：生产 Bubblewrap 与确定性测试 adapter
  共享 capability probe、输出、超时和取消 contract。普通 Runtime 测试不再要求开发主机
  可运行 Bubblewrap，生产组合仍 fail-fast 且绝不回退到 host shell。历史 Ticket 09 曾提出
  link syscall 禁令；该安全规则已被 autonomous workspace 设计取代，不再约束当前 backend。
- Ticket 03 已实现多 Turn 与设置冻结：`Conversation` 独占合法历史，Runtime 通过公开
  provider 配置 ID 和模型解析每个 Turn 的 `LLMProvider`，`ModelInvoker` 将冻结的
  temperature、max tokens 与 allowlisted thinking 设置应用于整个工具链。默认设置更新
  使用单调版本和 `SETTINGS_CONFLICT`；`TurnSettingsOverride` 以 `UNSET` 区分继承和显式
  `None`，因此可以只覆盖一个字段且不改写 Thread 默认值。每个 provider 实例暴露所选
  模型的 `ThinkingCapabilities`，Runtime 在请求前验证 thinking 开关、budget 和 keep，
  不支持的组合以 `UNSUPPORTED_MODEL_SETTING` 失败。API key、base URL 与任意
  `extra_body` 均不属于公开设置。
- Web Host 可在 `create_thread(..., settings=...)` 中冻结创建时选择的公开模型设置；省略
  参数时仍使用 Runtime 默认设置并保持 version 0，因此既有调用方语义不变。
- Web Host 提交的 Turn 在同步状态写入前先建立 Turn emitter，并通过 worker thread 等待
  轻量 workspace 根校验。启动前的 context、lease、workspace、关闭或取消失败统一产生脱敏
  `turn_rejected`，且不写入 Conversation；已取得的 workspace lease 在所有拒绝路径释放。
- Ticket 04 已实现版本化公开状态与阶段事件：`ThreadSnapshot` 现在包含脱敏后的完整公开
  对话、时间戳和最近一次 `TurnSummary`，两者均通过 `to_dict()` 产生可直接 JSON 编码的
  独立数据。公开消息保留用户/助手文本、工具调用与安全工具结果，但排除 system prompt、
  reasoning、credentials 和内部 traceback。Summary 已提供终止原因、累计 usage、计数与
  时间戳；修改文件和 diff 字段现由 Ticket 08 的 `ChangeTracker` 填充。
- 每个 Turn 通过 `TurnEventEmitter` 产生带 schema version、UUID、时间戳和单调 sequence
  的完整阶段事件，包括 Turn 生命周期、模型完整响应与工具请求/开始/完成；默认设置更新
  通过同一缓冲区产生 Thread-scoped 事件。Thread 使用
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
  一次工具批次委派。默认 `CommandAwarePolicy` 只对命令做保守分类，文件工具在三种模式
  均允许；`DENY` 返回带 reason metadata 的 `POLICY_DENIED`，`REQUIRE_APPROVAL` 把 Thread
  切换为 `WAITING_APPROVAL` 并发出带独立 ID、原因代码和说明的 `approval_requested`。
  调用方通过 `resolve_approval()` 批准或拒绝；取消与独立审批超时均安全终止，审批等待通过
  `RunController` 暂停执行 deadline 且始终继续持有 workspace lease。显式注入
  `AllowAllPolicy` 仍可用于兼容或测试场景。
- Ticket 08 已实现每 Turn 文件变更跟踪：`ChangeTracker` 在文件工具首次写入前保存 original，
  成功写入后更新 final，因此重复修改只在 Summary 产生一份 original-to-final unified diff；
  新文件以 `/dev/null` 表达，`file_changed` 事件在每次实际文件工具变化后发出。成功
  `read_file` 与文件写入会刷新已知内容指纹，外部变化在覆盖前返回 `FILE_CHANGED`，模型
  重新读取后可安全重试。只由文件工具修改的 Turn 报告 `diff_complete = true`；实际执行过
  `run_command` 的 Turn 保守报告 `false`，且不会猜测无法恢复 original 的命令改动文件。
- Policy/output hardening 已补齐：`CommandAwarePolicy` 在既有 classifier 前以有界
  `shlex` token 分析解包 `env`、`python`/`python3 -m`、动态解释器和 `xargs`，未知或
  歧义 wrapper 保守归入需审批/拒绝路径；`read_file` 及 grep/glob/命令组合结果使用
  UTF-8 byte boundary，超长单行会带截断标记并报告实际返回字节、选中源字节和真实行范围。
- Workstream B Phase 2 将 `apply_patch` 纳入同一 ChangeTracker seam：一次结构化 patch 的
  多个路径先整体检查版本冲突，再按文档顺序记录 original/final 并发出 `file_changed`；
  patch parser 和 WorkspaceFilesystem 提供无部分修改的预检及提交失败逆序恢复。
- Workstream B Phase 3 在既有 sandbox invocation seam 上增加 per-Thread `ProcessManager`：
  `exec_command` 返回 running/exited 与 session cursor，`write_stdin` 只访问已创建 session，
  stdout/stderr 增量、PTY 合并标记和 command lifecycle 均沿 Turn event sink 发出。session
  会在 timeout、idle、Turn cancellation、Thread close 时终止并回收；Git workspace 的命令
  前后用轻量 porcelain/diff 记录可观察路径，无法观察时保持 `diff_complete = false`。
- Workstream B Phase 4 已接通 OpenAI-compatible async streaming：provider chunk 转换为本地
  delta 事件，`MessageAssembler` 按 index/id 组装 tool-call 并只在 MessageEnd 后构造
  canonical assistant message；EventBuffer 提供 wake-only async subscription，Host SSE
  优先实时订阅，前端 reducer 以 provisional state 展示尚未写入 Conversation 的 delta。
- Ticket 09 的 workspace 安全现由选择时根校验与每次访问的 canonical containment 组成：
  Turn 启动不再递归扫描工作区，内部 symbolic/hard link 不会被一概拒绝，外部 alias 返回
  `WORKSPACE_ESCAPE`，搜索遍历跳过工作区外目标并避免目录环。生产
  `BubblewrapSandboxBackend` 通过 execution profile 控制 workspace 挂载、网络与只读系统
  配置；它继续丢弃 capabilities，网络仅由已批准的 network profile 开启。
- Ticket 10 已实现上下文容量预检与 Turn 提交幂等：`ContextManager` 在首次和每次后续模型
  请求前，按完整消息、content block、工具 schema 与输出 token 预留执行 tokenizer-free
  保守估算；模型 capability 可声明精确 context window，未声明时使用后端保守默认值。首个
  请求超限以 `CONTEXT_LIMIT` 拒绝且不写入用户历史，工具结果使后续请求超限时则返回带相同
  error code 的失败 Summary，历史从不被静默删除或总结。`run_turn()` 接受有界
  `idempotency_key`；同 key、同 payload 的进行中重试等待原 Turn，完成后重试返回原 Summary，
  不同 payload 复用同 key 明确返回 `IDEMPOTENCY_CONFLICT`，不会创建第二个 Turn。
- Issue #3 Phase 2 已将模型可见 Context 与 durable canonical history 分离：
  `ContextManager` 从 detached history 与四类 Context source 生成可解释的
  `ContextPlan`，再由 `ContextRenderer` 交给 ModelInvoker；默认 selector/compactor
  为 NoOp，`ContextBudget` 在 assembly 中显式拒绝超限且不删除历史。详细 ownership
  与后续扩展边界见 `docs/context-architecture.md`。
- Ticket 11 已补齐 Thread 关闭生命周期与最终验收：`close_thread()` 对空闲 Thread 立即进入
  `CLOSED`，对活跃 Thread 发出 `thread_close_requested`、取消当前模型或工具操作，并在统一
  清理路径释放 workspace lease 后进入 `CLOSED`。关闭操作幂等，快照和历史仍可读取；新的
  Turn 与设置修改以 `THREAD_CLOSED` 拒绝。验收测试通过真实文件与命令工具覆盖正常
  read/edit/test/diff 链路及 failed-test reporting，而不是只断言伪造的内部调用。
- Review Ticket 01 已收紧同步工具取消语义：同步 executor 收到取消或执行 deadline 后先在
  worker thread 中运行至静止，`RunController` 再把已完成的真实 `ToolResult` 交给
  `ToolCoordinator` 记录历史、diff 与事件，随后按原请求终止 Turn。workspace lease 只在该
  协调完成后释放，因此相交 Turn 不会与失去所有权的后台写线程并发。
- Review Ticket 02 已将 filesystem security seam 收口到 access-time canonical containment；
  选择时只验证根目录，不递归检查后代。内部路径 alias 可用，canonical target 超出 workspace
  时 fail closed，且不再以 `st_nlink > 1` 或 symlink 本身拒绝整个 workspace。
- Review Ticket 03 已补齐命令 sandbox 的资源关闭链路：关闭空闲 Thread 时立即幂等关闭
  工具 registry；关闭活跃 Thread 时先完成 Turn 取消和 workspace lease 释放，再沿
  `ToolRegistry`、`CommandRunner`、`CommandSandboxBackend` 释放 seccomp descriptor，
  避免提前关闭与反复创建 Thread 时的文件描述符泄漏。
- Review Ticket 04 已让成功的默认设置更新产生可由既有 cursor 读取的 Thread-scoped
  `settings_updated` 事件；事件版本与最新 Snapshot 一致。`reasoning_visibility` 也进入
  不可变 `TurnConfig` 并在构造时校验，事件 emitter 只读取该 Turn 冻结的值。
- Review Ticket 05 已收口最终验收证据：真实 `OpenAICompatibleProvider` 的非法 raw tool
  arguments 会原样穿过 Runtime 历史、事件与下一次模型请求，同时产生匹配的
  `INVALID_ARGUMENTS` 结果并继续循环；测试还显式锁定设置版本 0、20/50/900 默认预算、
  四个活跃 Turn 的默认全局上限，以及 Event、Snapshot、Summary 和最终工具历史的一致性。
  原 Runtime tickets 05–10 与本组 review tickets 均已同步为完成状态。

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
62. As a workspace owner, I want every file-tool access checked against the effective canonical target, so that internal aliases work without allowing external escapes.
63. As a workspace owner, I want external symlink targets rejected while internal symlinks and hard-linked files remain usable, so that normal repositories are not invalidated by a whole-tree preflight.
64. As a workspace owner, I want recursive search to skip external aliases and link cycles, so that it remains contained and bounded by the requested operation.
65. As an application author, I want command execution to receive the minimum profile required by Policy, so that read-only, writable, and approved-network operations have matching sandbox capabilities.
66. As an application author, I want privileged commands denied when the sandbox cannot provide the capability, so that approval cannot imply unsupported authority.
67. As an application author, I want trusted read-only system links allowed in the sandbox runtime, so that ordinary Linux commands can still execute.
68. As an application author, I want approved network/package commands to receive an actual network-enabled profile, so that Policy and sandbox behavior agree.
69. As a runtime author, I want persistent command sessions to retain their workspace/profile and cleanly expose final exit state, so that long-running tools remain predictable.
70. As a frontend author, I want a complete `ThreadSnapshot`, so that a refreshed view can reconstruct the current conversation and status.
71. As a frontend author, I want a structured `TurnSummary`, so that I can render the outcome without parsing model prose.
72. As a frontend author, I want stage-level `AgentEvent` objects, so that I can display model responses, tool activity, settings updates, state changes, and file changes incrementally.
73. As a frontend author, I want event sequences to increase monotonically within their Turn or Thread lifecycle scope, so that concurrent timestamps cannot reorder the UI.
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

- `Thread` is the long-lived conversation concept. It owns one immutable, normalized workspace reference, ordered conversation history, mutable default settings, a current status, an optional active Turn ID, and timestamps. Runtime state is persisted through `ThreadStore` when supplied (the production Host uses SQLite); the default embedded mode remains in-memory.
- `Turn` is one user message plus one complete ReAct execution. A Thread may have many sequential Turns. A new Turn may be submitted only while its Thread is `IDLE`; submission changes the state atomically so two callers cannot both start.
- Thread states are `IDLE`, `RUNNING`, `WAITING_APPROVAL`, and `CLOSED`. Turn states are `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, and `LIMIT_REACHED`. A completed, failed, cancelled, or limited Turn returns its Thread to `IDLE` unless the Thread is being closed. `close_thread()` is idempotent: it closes an idle Thread and its tool resources immediately, or marks an active Thread as closing, requests cancellation, and lets the existing terminal cleanup path release leases before closing the tool registry and publishing `CLOSED`. Snapshots and retained events remain readable after closure; commands that would mutate the Thread fail with `THREAD_CLOSED`.
- `ThreadRuntime` is the highest external seam and the primary test surface. Its small interface covers Thread creation, settings updates, Turn submission, cancellation, approval resolution, event consumption, snapshot retrieval, and Thread closure. Transport adapters call this interface rather than reaching into Agent Loop modules.
- The Agent Loop is deliberately narrow: request a model response, append the assistant message, finish if no tool calls exist, otherwise execute all calls in order and append all matching tool results. It does not implement retry, Policy, event serialization, file diffing, workspace locking, or provider-specific reasoning rules inline.
- `Conversation` owns ordered internal `Message` values and constructs each model request history. It preserves valid tool-call/tool-result pairings and delegates provider-specific reasoning encoding to existing model capabilities. Switching models between Turns retains text and tool history while allowing the selected provider adapter to filter reasoning according to its capabilities.
- `ModelInvoker` wraps the existing `LLMProvider` seam. It applies immutable Turn generation settings and retries only errors whose existing taxonomy marks `retryable`. The default is at most three attempts with short exponential backoff. Authentication, invalid request, configuration, and response-parse failures do not retry.
- `ToolCoordinator` wraps the existing `ToolRegistry` execution seam without moving tool capability behavior into the Runtime. It executes calls sequentially, consults Policy, manages an approval pause, emits lifecycle events, propagates cancellation, converts results to conversation blocks, and ensures every accepted call receives a corresponding result.
- Synchronous registry executors run in worker threads that cannot be forcibly stopped by coroutine cancellation. Their async dispatch therefore defers cancellation propagation until the worker is quiescent and marks the reconciled result internally. `RunController` returns only that marked result to `ToolCoordinator`, which records its actual history and file effects before a cancellation or deadline checkpoint terminates the Turn. The workspace lease remains held throughout this reconciliation.
- Policy has three decisions: `ALLOW`, `DENY`, and `REQUIRE_APPROVAL`. The initial adapter allows all valid calls. `DENY` produces a safe `POLICY_DENIED` tool result. `REQUIRE_APPROVAL` changes the Thread to `WAITING_APPROVAL`, stops launching later calls, and awaits an external decision or an independent approval timeout.
- `RunController` is the single source of truth for one Turn's maximum iterations, maximum tool calls, execution deadline, cancellation, approval-clock suspension, and repeated-failure detection. `ThreadRuntime` owns the shared active-Turn capacity. Defaults are 20 model iterations, 50 tool calls, 15 minutes of execution time, three identical consecutive failures, and four active Turns globally.
- A repeated failure fingerprint consists of tool name, canonically normalized arguments, and error code. Only three consecutive identical fingerprints stop a Turn. Different failed operations do not count as one repeated sequence.
- Normal completion occurs only when the assistant response contains no tool calls. Budget exhaustion and repeated failure produce `LIMIT_REACHED` with an exact stop reason. Cancellation produces `CANCELLED`. Non-retryable model or Runtime failures produce `FAILED`. Ordinary tool errors remain in the loop.
- `ThreadSettings` contains the public defaults used by future Turns and has a monotonically increasing version. An update supplies its expected version; stale writes fail with `SETTINGS_CONFLICT`. Every accepted update appends a Thread-scoped `settings_updated` event carrying the new version and selected provider/model identity; its `turn_id` is null because the update is not owned by an active Turn. Settings accepted during an active Turn affect only subsequent Turns.
- Thread creation may supply one initial `ModelSettings` value, which is converted directly to version-zero `ThreadSettings`; callers that omit it continue to receive the Runtime defaults.
- `TurnConfig` is an immutable snapshot created when a Turn starts. A field-level `TurnSettingsOverride` is merged with Thread defaults first; an internal `UNSET` sentinel means inherit while explicit `None` clears an optional value for that Turn. The snapshot contains provider configuration reference, public model name, temperature, maximum output tokens, supported thinking options, PromptBuilder output, reasoning visibility, and effective Agent limits. Values cannot change during the Turn.
- Frontend-controllable provider data is allowlisted. The public settings include provider configuration ID, public model, temperature, maximum output tokens, supported thinking options, and bounded Agent limits. API keys, base URLs, raw provider bodies, credentials, and backend hard ceilings are not public settings.
- Thinking request fields are fail-closed against the selected model's `ThinkingCapabilities` before the first model request. Provider defaults may be refined by exact-model `ModelProfile` overrides; unsupported thinking, budget, or keep values return `UNSUPPORTED_MODEL_SETTING` rather than passing an unchecked private body to the provider.
- The workspace reference is immutable after Thread creation. Selecting another folder requires another Thread so old message paths and diffs never change meaning.
- `WorkspaceLeaseManager` normalizes real workspace roots and treats equal paths or ancestor/descendant relationships as overlapping. Multiple Threads may refer to overlapping roots, but only one overlapping Turn may hold a lease. A conflict fails immediately with `WORKSPACE_BUSY`; there is no implicit queue. A lease is held during `RUNNING` and `WAITING_APPROVAL` and released on every terminal path.
- Before a Turn enters the loop, workspace validation checks only that the selected root still exists, is a readable directory, resolves canonically, and remains within the Host allowlist. It does not recursively scan the workspace or reject internal symlinks and hard-linked files. `WorkspaceLeaseManager` overlap/concurrency semantics remain unchanged.
- File operations reject absolute paths and lexical traversal, resolve existing targets (or the nearest existing parent for new targets), and check effective canonical containment at access time. Internal symlinks are allowed when their effective target remains inside the workspace; external escapes are denied with `WORKSPACE_ESCAPE`. Recursive search follows the same rule and avoids external aliases and cycles.
- `CommandSandboxBackend` remains the production/test seam for profile-aware command execution. The production bubblewrap adapter preserves process isolation, capability dropping, constrained mounts, ephemeral home/tmp, and process-group cleanup while selecting read-only, writable, or network-enabled mounts from the `ExecutionProfile`.
- A hard-linked regular file does not invalidate the workspace. Atomic writes remain the default; when an existing hard-linked target is intentionally edited, the filesystem updates its inode in place so aliases observe the same content.
- `ChangeTracker` records a file's original state before the first file-tool mutation in a Turn and its latest state after each successful mutation. A file modified repeatedly produces one original-to-final unified diff. New and deleted states are represented explicitly.
- The tracker records a content fingerprint when a file is read or first prepared for writing. A later write that observes a different disk fingerprint returns `FILE_CHANGED`; the model may reread and retry. The tracker updates its known fingerprint after its own successful write.
- Diff completeness is explicit. File-tool changes are tracked completely. Arbitrary source changes made inside `run_command` are not guaranteed to have a recoverable pre-command snapshot, so a Turn that may contain such changes reports `diff_complete = false`. The default prompt instructs the model to prefer file tools for source edits.
- `PromptBuilder` supplies a provider-independent default system prompt and permits Runtime-provided additional system instructions. It does not automatically read `AGENTS.md` in this version. Loop enforcement remains code-owned rather than encoded only in prose.
- The default prompt tells the model that it is a local coding agent, paths are workspace-relative, relevant files should be inspected before modification, source changes should prefer file tools, suitable validation should run before completion, tool errors must be handled honestly, and the final answer should summarize work and validation.
- Conversation history is not compressed, summarized, or silently truncated. `ContextBudget` estimates a provider-independent upper bound from serialized UTF-8 request bytes plus framing allowance and reserves the configured maximum output tokens. `ProviderCapabilities.context_window_tokens` overrides the Runtime's conservative 32,000-token fallback. An oversized first request is rejected with `CONTEXT_LIMIT` before history mutation; an oversized later tool-chain request terminates with a safe `CONTEXT_LIMIT` Summary. The user must create a new Thread.
- `ThreadSnapshot` is a JSON-compatible public view containing Thread ID, normalized public workspace identifier, Thread status, public messages, active Turn ID, sanitized settings and version, timestamps, and the latest Turn summary where applicable. It never serializes internal Python objects directly.
- Public messages contain user and assistant text plus structured tool calls and safe tool results. System prompts, credentials, internal tracebacks, and raw reasoning are excluded from the ordinary message list.
- `TurnSummary` contains Turn ID, status, stop reason, final assistant text, modified files, file diffs, diff completeness, iteration/tool-call counters, accumulated usage, start/end timestamps, and an optional safe error summary. It is the final result of one Turn, not the mutable state of the whole Agent.
- Every `AgentEvent` has schema version, event ID, Thread ID, nullable Turn ID, a monotonically increasing Thread-scoped sequence, type, timestamp, and JSON-compatible payload. The shared bounded EventBuffer and event-ID cursor preserve total append order, while durable semantic events are mirrored through `ThreadStore`; model streaming deltas remain transient. Runtime reserves sequence ranges in bounded checkpoints (so a crash may leave gaps but never reuses an observed transient sequence), rather than committing once per delta. Stage-level types cover Turn lifecycle, complete model response, tool request/start/finish, approval request/resolution, file changes, settings updates, cancellation, completion, failure, and limit termination.
- Reasoning transmission is backend-controlled by `reasoning_visibility`. The default `hidden` value emits no reasoning. Explicit `debug` emits a separate complete reasoning event after a model response; reasoning does not enter ordinary messages, summaries, or logs. The frontend may decide whether to render a reasoning event it has received.
- Live events are kept in a bounded in-memory ring buffer and must never block Agent execution because a consumer is slow or absent. Durable semantic events are also restored from `ThreadStore`; the next Thread sequence continues after restart. A consumer whose live cursor expired retrieves a fresh Snapshot.
- The separate Host transport maps Thread creation, versioned settings updates, Turn submission, cancellation, Snapshot retrieval, and Thread closure to HTTP commands and forwards Runtime events with SSE. Approval remains excluded from Web V1. The core Runtime still has no dependency on the Web framework.
- Turn submission accepts a non-empty, at-most-200-character idempotency key and atomically requires the Thread to be `IDLE` for a new key. A matching retry joins the in-flight result or returns a detached completed Summary; reuse with different user text or settings override fails with `IDEMPOTENCY_CONFLICT`. The Host adapter consumes this behavior without moving it into transport code.
- All external data has an explicit schema version. Compatibility is defined by JSON field semantics, not by importing Python dataclasses into a frontend.
- OpenAI-compatible model calls use the streaming seam when the provider exposes it. Legacy
  chat-only providers remain compatible through the complete-response fallback; provisional
  model deltas never enter Conversation and are cleared on stream failure/cancellation.

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
- Event tests assert schema version, IDs, scoped monotonic sequence, safe payloads, status order, settings-version consistency, bounded-buffer gap behavior, Snapshot recovery, and default reasoning suppression. Debug tests verify reasoning appears only in its dedicated event.
- Settings tests assert immutable per-Turn snapshots, next-Turn application of mid-run updates, one-Turn overrides, version conflicts, model changes between Turns, hard server ceilings, and complete credential redaction.
- Security tests assert access-time rejection of absolute/traversal paths and external symlink escapes, allow internal symlinks and hard-linked files, and cover nested mounts where test infrastructure permits. They also verify profile-aware command sandbox behavior.
- Existing provider tests are prior art for scripted provider responses and stable domain-type assertions. Existing local-tool tests are prior art for temporary workspace behavior, structured tool errors, command cancellation, and sandbox capability checks.
- The acceptance suite covers at least: no-tool completion; read/edit/test/diff; failed-test reporting; invalid raw provider-argument preservation and structured recovery; explicit default budgets and four-Turn capacity; independent workspace concurrency; overlapping workspace rejection; a second Turn on one Thread; model switching between Turns; command cancellation; budget and repeated-failure termination; optimistic file conflicts; effective-target link containment; ordered events consistent with final tool history, Snapshot, and Summary; and absence of credentials, tracebacks, and default-hidden reasoning from public data.
- Tests should survive refactoring inside deep modules. A test that must inspect past `ThreadRuntime`, `LLMProvider`, `ToolRegistry`, or `CommandSandboxBackend` seams requires justification.

## Out of Scope

- React UI, HTTP routes, and SSE framing remain outside the Runtime module and are specified in `docs/local-web-ui-host-spec.md`; WebSocket and frontend-generated Runtime types remain out of scope.
- Distributed locks and multi-process Runtime coordination remain out of scope. Thread, canonical
  Conversation, Turn summary, semantic event, settings, and idempotency persistence plus local
  process-restart recovery are provided by `ThreadStore`; live process/session reattachment remains
  out of scope.
- Mid-Turn user steering, concurrent Turns within one Thread, implicit Turn queues, or automatic cancellation of an older Turn.
- Automatic context compression, summarization, silent history deletion, or provider-specific tokenizers.
- Automatic loading or recursive precedence rules for `AGENTS.md` and other project instruction files.
- A production dangerous-command classifier. The default Policy allows valid calls; the Policy seam exists for future enforcement and approval.
- Accurate original-content capture for arbitrary file changes made by shell commands, formatters, build scripts, or external processes.
- Defense against a hostile external process that races workspace validation and replaces entries during execution.
- Git worktree creation, cross-workspace tasks, automatic merge resolution, persistent job sessions, or command reattachment.
- Provider-hosted tools, hosted filesystems, and hosted code execution remain out of scope.
- Agent frameworks, orchestration frameworks, agent SDKs, or reuse of another coding-agent runtime.

## Further Notes

- The strict workspace-link requirement is the largest implementation risk. It requires a genuine sandbox capability, not only Python path validation, and may make package managers or build systems that create workspace symlinks incompatible. Capability failure must be explicit and early.
- The first version optimizes for a readable Agent Loop and strong observable contracts rather than maximum feature breadth. Complexity is concentrated behind deep module interfaces so future HTTP, persistence, approval policies, context compression, streaming, and richer sandbox adapters can be added without rewriting the loop.
- The current model and tool layers already provide the two principal lower-level seams: provider-independent `Message` history through `LLMProvider`, and structured local execution through `ToolRegistry`. The Runtime should extend these concepts rather than introduce parallel tool-call or message types.
- The specification intentionally distinguishes internal provider reasoning, public conversation messages, incremental Agent events, current Thread Snapshot, and terminal Turn Summary. Treating all five as one mutable "agent state" would make both the Runtime and future HTTP contract ambiguous.
- This specification is maintained under `docs/` at the user's explicit request and is not published to a GitHub Issue.

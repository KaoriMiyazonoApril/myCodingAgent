# 本地 Web UI 与 Agent Host 规格

## Problem Statement

项目已经具备以 `ThreadRuntime` 为最高外部 seam 的内存态 Coding Agent Runtime，能够创建
Thread、运行阻塞至终态的 Turn、读取 Snapshot 与事件、取消 Turn、更新设置、关闭 Thread，
并通过本地工具、Policy、Conversation 和模型连接层完成 ReAct 闭环。当前缺少面向普通用户的
本地应用入口：用户只能通过 Python 代码组合这些模块，无法在浏览器中选择 Host 文件系统里的
workspace、配置模型厂家、创建 Thread、提交任务，或持续观察工具执行与文件变化。

浏览器和 Agent 的执行环境也可能不同。典型使用方式是在 WSL2 中运行 Host，而用户从 Windows
浏览器访问 localhost。浏览器看到的文件系统和路径不应成为 Agent workspace 的事实来源；模型
密钥也不应交给 React 直接访问第三方服务。项目需要一个本地 Agent Host，把浏览器命令转换为
现有 Runtime 调用，把 Runtime 事件通过 SSE 传给前端，并在同一进程中管理后台 Turn task、
Provider 配置、受限 workspace 浏览和生产静态资源。

该能力必须保持现有依赖方向和课程约束。Web UI 不得绕过 `ThreadRuntime` 依赖 AgentLoop、
ToolCoordinator、具体 Provider 或具体工具实现；Host 不得复制 Agent 状态或重新实现 Agent
推理、工具执行与 Conversation 规则。第一版仍是可信本机单用户、单进程、内存态 Thread，
重点是打通可观察、可取消、可恢复的本地纵向链路，而不是扩展成远程多用户平台或完整 IDE。

## Solution

提供 `agent web` 本地应用入口。Linux 或 WSL2 中启动 Host 后，用户通过浏览器访问
`http://127.0.0.1:<port>`。Host 使用 FastAPI 与 Uvicorn 提供稳定 JSON command 接口、SSE
事件、静态 React 资源、server-side workspace picker，以及 WSL 下可选的 native Windows
folder picker。开发模式由独立 Vite server 代理 `/api`；生产模式由 Python Host 直接托管构建
后的前端。

首次启动允许 Runtime 尚未配置。用户在 Web 设置中为 DeepSeek、Moonshot/Kimi 或 GLM 填写
API key，Host 将凭据原子地保存在本机用户配置目录，只向浏览器返回脱敏状态。保存后，前端让
Host 使用受信 Provider preset 的固定 endpoint 获取 Provider 返回的模型 ID；发现失败时明确
展示错误并允许手工输入模型。用户选择默认 Provider 和模型后，Host 才惰性组合单例
`ThreadRuntime`。

每个 Thread 绑定 Host 选择的 workspace 和创建时的模型设置。Turn submission 由薄的
`TurnTaskManager` 接受并放入后台 task，HTTP 立即返回 accepted；Runtime Snapshot 仍是 Agent
状态事实来源，TaskManager 只暴露 submission lifecycle。Runtime 的同步 workspace validation
异步下放到 worker thread，启动前失败产生脱敏 `turn_rejected` Runtime 事件。运行进度继续使用
现有 `AgentEvent` 与 `EventBuffer`，Host 仅把 event ID 映射为 SSE cursor，并在 cursor 淘汰时
发送 Snapshot recovery。

React UI 使用桌面优先三栏布局：左侧 workspace 与 Thread，中央 Conversation 和 Composer，
右侧 Activity、工具状态、文件变化和简化 diff。完整模型响应一次性显示，不引入 token delta。
运行时 Send 旁提供真正调用 Runtime cancellation 的 Stop。Workstream B Phase 1 已确定
`ApprovalMode` 与 command-aware Policy，因此 Host 仅增加一个薄的 approval resolution command，
前端展示 Runtime 提供的原因并允许 approve/deny；风险判断仍不进入 Web。

## User Stories

1. As a Coding Agent user, I want to start the application with one command, so that I do not have to assemble Runtime objects manually.
2. As a Linux user, I want the Host to listen on localhost, so that I can use the Agent through my local browser.
3. As a WSL2 user, I want to open the Host from a Windows browser, so that I can operate a WSL workspace without a Linux desktop browser.
4. As a WSL2 user, I want Agent execution to remain inside WSL, so that commands and paths use the actual Host environment.
5. As a security-conscious user, I want the Host bound only to loopback, so that the application is not exposed to the local network.
6. As a first-time user, I want the Web UI to start before a Provider is configured, so that I can complete setup in the browser.
7. As a user, I want to choose DeepSeek, Moonshot/Kimi, or GLM, so that I can use an existing supported Provider preset.
8. As a user, I want to enter an API key in a password field, so that I can configure model access without editing source code.
9. As a user, I want my Provider configuration to survive a Host restart, so that I do not have to enter the key every time.
10. As a user, I want the UI to show only a masked credential state after save, so that the full key is not repeatedly exposed.
11. As a user, I want to clear or replace a stored key, so that I can rotate or revoke credentials.
12. As a user, I want model discovery to run after saving a key, so that I can select an ID the Provider reports for my account.
13. As a user, I want authentication and connection failures shown separately, so that I can correct the relevant configuration.
14. As a user, I want to enter a model ID manually when discovery is unavailable, so that an incomplete models endpoint does not block use.
15. As a user, I want one default Provider and model remembered, so that new Threads start with my current preference.
16. As a user, I want changing the global default to affect only new Threads, so that existing conversations remain reproducible.
17. As a user, I want a Provider key replacement to affect future Turns, so that active model requests are not mutated mid-flight.
18. As a user, I want the workspace picker to display Host directories, so that browser filesystem semantics never determine Agent access.
19. As a user, I want workspace browsing limited to configured roots, so that the picker cannot enumerate the entire Host filesystem.
20. As a user, I want to navigate one directory level at a time, so that selecting a project remains simple and predictable.
21. As a user, I want hidden directories available in the picker, so that dot-prefixed project directories remain selectable.
22. As a user, I want symlinks excluded from navigation, so that directory selection does not weaken workspace safety rules.
23. As a user, I want clear errors for a missing, inaccessible, or out-of-root directory, so that an invalid workspace is diagnosable.
24. As a user, I want to create a Thread for a selected workspace, so that conversation paths retain one stable meaning.
25. As a user, I want to choose Provider and model when creating a Thread, so that its initial settings do not depend on an obsolete Runtime default.
26. As a user, I want multiple Threads listed in the sidebar, so that I can switch between independent conversations.
27. As a user, I want closed Threads retained as read-only until Host shutdown, so that I can inspect their results.
28. As a user, I want to close a Thread explicitly, so that its local tool resources are released without pretending its history was deleted.
29. As a user, I want to submit a natural-language task, so that the existing Agent can inspect, modify, and validate my workspace.
30. As a user, I want Turn submission to return immediately, so that a long Agent run does not hold the POST response open.
31. As a user, I want the UI to show a starting state during workspace preflight, so that an accepted task is not mistaken for an idle Thread.
32. As a user, I want duplicate submission rejected while a task is starting or running, so that one Thread never executes concurrent Turns.
33. As a user, I want a starting Turn to be cancellable, so that workspace validation does not make Stop temporarily meaningless.
34. As a user, I want Stop to call Runtime cancellation after the Turn starts, so that model calls, commands, and tool work are actually interrupted.
35. As a user, I want cancellation progress and terminal status displayed, so that closing the event stream is never mistaken for cancelling work.
36. As a user, I want complete model responses displayed when available, so that V1 works without changing the model abstraction for token streaming.
37. As a user, I want requested tools displayed before execution, so that I can understand the Agent plan.
38. As a user, I want running tools visibly distinguished, so that I know which operation is active.
39. As a user, I want successful and failed tool results displayed with structured metadata, so that commands and file operations are auditable.
40. As a user, I want tool failures shown rather than swallowed, so that I can understand why the Agent changed course or stopped.
41. As a user, I want modified files and available diffs displayed, so that I can assess the resulting code changes.
42. As a user, I want incomplete diff tracking labelled honestly after command execution, so that the UI does not claim complete coverage.
43. As a user, I want Turn usage, iteration count, tool-call count, timestamps, and stop reason displayed, so that I can inspect execution metadata.
44. As a user, I want Runtime failure cards in the Conversation, so that a failed Turn remains part of the visible history.
45. As a user, I want Host connection failures shown persistently, so that a disconnected application never appears idle and healthy.
46. As a user, I want SSE to reconnect automatically, so that transient browser or network interruptions do not lose live progress.
47. As a user, I want reconnection to resume after the last event ID, so that retained events are not replayed as new actions.
48. As a user, I want Snapshot recovery when the event cursor expires, so that a slow or refreshed browser can rebuild the current UI.
49. As a user, I want repeated events handled idempotently, so that recovery races do not duplicate messages or tool cards.
50. As a user, I want a refreshed page to recover Thread messages, settings, latest Turn, activity, and submission state, so that refresh is safe.
51. As a user, I want model settings updated with optimistic version checks, so that stale browser state cannot overwrite newer Thread settings.
52. As a user, I want settings changed during a Turn to apply only later, so that active execution remains frozen.
53. As a frontend maintainer, I want stable JSON DTOs and error codes, so that UI behavior does not depend on Python reprs or exception text.
54. As a frontend maintainer, I want one Runtime event format, so that transport recovery does not coordinate competing Agent cursors.
55. As a frontend maintainer, I want Host submission state separated from Agent Snapshot state, so that transport concerns do not become duplicate Agent truth.
56. As a Runtime maintainer, I want Web calls to stop at `ThreadRuntime`, so that the Agent Loop remains independent of HTTP and React.
57. As a Runtime maintainer, I want startup rejection emitted by Runtime, so that accepted background work has an observable terminal outcome.
58. As a Runtime maintainer, I want workspace validation moved off the event loop without changing validation rules, so that Host responsiveness does not weaken safety.
59. As a model-layer maintainer, I want Provider credentials resolved through an opaque configuration ID, so that keys never enter Thread settings or events.
60. As a Host maintainer, I want fixed Provider endpoints for V1, so that model discovery cannot become an arbitrary server-side request primitive.
61. As a Host maintainer, I want graceful shutdown to cancel and await tasks, so that local commands and Runtime resources do not leak.
62. As a developer, I want Vite to proxy `/api` in development, so that I can run frontend and Host separately without broad CORS.
63. As a developer, I want production assets served by the Host, so that users run one process after building the frontend.
64. As a developer, I want missing production assets to fail fast with an actionable command, so that a blank page is not mistaken for a running application.
65. As a developer, I want Host, SSE, Provider, Runtime integration, reducer, and production build tests, so that the full local path remains reviewable.
66. As a course reviewer, I want the Host to reuse the project's own Runtime, tools, events, and model client, so that the project remains an independently implemented Coding Agent.
67. As a course reviewer, I want architecture and usage documentation synchronized with implementation, so that the design can be assessed without reverse engineering the code.

## Implementation Decisions

### Scope and dependency direction

- V1 supports Linux Host and WSL2 Host accessed from a Windows browser. A native Windows Host is not an acceptance target.
- The dependency direction is Web UI → Agent Host → `ThreadRuntime` → Agent Core. The Web UI knows only transport DTOs for Provider, Workspace, Thread, Turn, Message, AgentEvent, ToolCall, FileChange, Settings, submission state, and errors.
- Host behavior stops at the `ThreadRuntime` interface. AgentLoop, ToolCoordinator, Conversation policy, concrete tools, and Provider request execution remain behind Runtime and model seams.
- Workstream B Phase 1 已形成最小审批接线：Host 暴露 Runtime 的 approval resolution command，
  前端只展示 `approval_requested` 的稳定 reason 与 approve/deny 操作；Policy 决策和风险判断
  仍完全属于 Runtime，Web 不自行复算。
- Thread and event persistence remain in memory. Provider credentials and default Provider/model selection are the only durable local application settings in V1.

### Deep modules and seams

- The Host application factory is the highest test seam for HTTP commands, DTOs, static serving, lifecycle, and error mapping. Tests inject Runtime composition and Provider discovery behavior through application dependencies.
- `ThreadRuntime` remains the highest Agent seam. Runtime integration tests exercise public Snapshot, EventBatch, cancellation, settings, and close behavior rather than internal loop collaborators.
- Provider configuration is hidden behind one deep store interface that owns versioned loading, atomic saving, permission enforcement, masking, default selection, and credential clearing.
- Provider model discovery is hidden behind one catalog interface with production and fake adapters. It returns sanitized model IDs and typed failures; callers do not handle SDK response objects.
- `TurnTaskManager` is a deep Host module with a small start, cancel, inspect, and shutdown interface. It owns task mappings and cleanup without becoming the source of Agent status.
- The SSE adapter owns polling, framing, heartbeat, cursor recovery, and disconnect cleanup behind one streaming interface. Runtime and routes do not contain React-specific event reduction.
- Workspace browsing is hidden behind a root policy interface that normalizes configured roots, checks containment without following symlinks, lists one level, and returns transport-safe entries.
- Native Windows selection is hidden behind an injected `NativePickerAdapter`. It detects WSL/interop capability, launches a fixed UTF-8 PowerShell dialog through argv, translates the selected Windows path with system `wslpath`, and exposes idempotent close/shutdown. The PowerShell process emits its Windows PID before opening the dialog, allowing shutdown to terminate both that process and a WSL `/init` launcher instead of leaving a detached native dialog. It never authorizes a workspace; the Host calls the same `WorkspaceBrowser.validate()` used by Thread creation.

### Provider configuration and model discovery

- V1 exposes exactly three logical Provider presets: DeepSeek, Moonshot/Kimi, and GLM. Kimi and Moonshot are one logical configuration rather than separate accounts.
- Each Provider has at most one stored credential. Multi-key rotation, multiple accounts for one Provider, custom base URLs, custom headers, and arbitrary OpenAI-compatible endpoints are excluded.
- Provider IDs are stable opaque configuration references. Runtime settings contain only the ID and public model name; API keys, base URLs, raw headers, and SDK clients never enter Snapshot, Event, Summary, logs, or frontend state returned by the Host.
- Credentials are stored in a versioned Host configuration document under the Linux/WSL user configuration directory. Writes are atomic, files use owner-only permissions, and the workspace is never a credential location.
- Provider reads return configured state and optional masked suffix, never the full key. The frontend clears the plaintext form value after a successful save.
- Saving a key and discovering models are separate commands. The UI automatically starts discovery after save. A failed discovery leaves the saved key available for correction, replacement, or clearing.
- Discovery uses only the fixed base URL from the existing Provider preset and the Host-stored key. Responses are reduced to non-empty model IDs, deduplicated and sorted.
- A discovery result means “reported by the Provider for this credential,” not verified tool-calling compatibility. V1 does not send a paid chat or tool probe.
- Model discovery results are cached in process memory for five minutes and are not durable truth. A manual model ID is always accepted when non-empty; incompatibility is reported by the existing model/Runtime error path when used.
- Host settings store one `default_provider_id` and one `selected_model` per configured Provider. Changing these defaults affects new Threads only.
- The Host may start with no configured Provider. Runtime composition is lazy; Thread creation before a Provider and model are selected fails with `CONFIGURATION_REQUIRED` while health, static UI, Provider, and workspace endpoints remain available.

### Minimal Runtime changes

- Thread creation gains an optional initial `ModelSettings` value. Existing callers that omit it retain the current default behavior. Web creation supplies the selected Provider and model directly, avoiding an artificial immediate settings update.
- Workspace validation in `run_turn` runs in a worker thread and is awaited asynchronously. Validation rules, budgets, lease acquisition, history mutation ordering, and fail-closed behavior remain unchanged.
- A Turn ID and emitter are available before asynchronous preflight. Provider resolution, context checks, workspace lease/validation, or cancellation before `turn_started` produce a sanitized `turn_rejected` event and then preserve the existing exception contract.
- Cancellation during preflight releases acquired resources and produces an observable rejection/cancellation code. Cancellation after `turn_started` continues through `RunController` and the existing terminal Summary/event behavior.
- No changes are made to model token streaming, AgentLoop control flow, tool execution ordering, Conversation history, Policy decisions, diff generation, or EventBuffer retention semantics.

### Host lifecycle and Thread catalog

- The Host owns an in-memory catalog of Thread IDs it created because Runtime has no enumeration interface. For every ID, Runtime Snapshot remains the source of Thread status, messages, settings, and latest Turn.
- Closed Threads remain in the catalog and are returned as read-only until process shutdown. Closing is explicit and idempotent; it is not represented as deletion or persistence.
- `TurnTaskManager` registers a submission before scheduling `run_turn`, rejects a second submission for that Thread, creates the background task, consumes its result/exception, and removes the mapping on every terminal path.
- Turn POST returns HTTP 202 with accepted status. While preflight is pending, Thread view adds a separate Host `submission.status = starting`; this is transport lifecycle rather than Agent state.
- Pending cancellation cancels the scheduled task. Once Runtime reports an active Turn, cancellation delegates to `cancel_turn`. Closing an active Thread follows Runtime close/cancel semantics and awaits task cleanup.
- Server shutdown stops accepting mutating commands, cancels pending and active Turns, awaits TaskManager cleanup, closes every Thread and Provider resource, and has a ten-second grace period. A timeout is logged and causes a non-zero exit.

### Workspace browsing

- `agent web` accepts repeatable workspace roots; the default is the process working directory. Browser navigation and Thread creation must remain within at least one normalized configured root.
- `GET /api/native-picker/capability` reports whether WSL interop and the required executables are available. `POST /api/native-picker/select` waits asynchronously for one native Windows folder dialog, translates its result with `wslpath`, and validates the resulting Host POSIX path against configured roots before returning it.
- The native adapter is a selection transport only: it does not perform root containment, symlink, accessibility, or directory authorization. A selected path is always passed through `WorkspaceBrowser.validate()`; the existing browser endpoint remains available for `/home/...`, native Linux, unavailable interop, cancellation, and failures.
- The picker uses Host-native POSIX paths after translation. A WSL Host may therefore expose `/mnt/c/...`; no browser-side Windows-to-WSL path conversion exists. Concurrent native requests return `NATIVE_PICKER_BUSY`, and cancellation returns a normal `cancelled` result.
- A request lists one level, directory-first and name-sorted, with at most 500 entries and an explicit `truncated` flag.
- Hidden directories are included. Symlinks and non-directories are excluded, and metadata checks do not follow links.
- Missing, inaccessible, and root-escape requests receive distinct stable errors. Picker authorization does not replace Runtime workspace validation; creating and starting a Thread still use existing strict workspace rules.

### HTTP commands and DTOs

- Health exposes process readiness and whether Runtime configuration is required without exposing credentials.
- Provider commands list preset/configuration state, save a credential and selected model, clear a credential, discover models, and update the default Provider/model.
- Workspace browsing is a read-only Host command scoped to configured roots.
- Native workspace selection is exposed as a capability read (`GET /api/native-picker/capability`) and a selection command (`POST /api/native-picker/select`). Capability, interop, process, malformed-result, translation, and busy failures use stable JSON error codes; cancellation is not an error. Windows paths are never returned by the transport.
- Thread commands list Thread views, create a Thread, get one hydratable Thread view, submit a Turn, cancel, close, and version-update settings.
- Event transport is the sole SSE endpoint. Approval and destructive Thread deletion endpoints do not exist in V1.
- Thread view is a stable transport DTO containing Runtime Snapshot, current Runtime event cursor, and optional Host submission lifecycle. It never serializes internal dataclasses or task objects directly.
- Every JSON response is strict and versionable. Enums are strings, timestamps use the Runtime's UTC representation, paths are Host POSIX strings, and arbitrary Python reprs are rejected.
- Errors use an envelope with a stable code, safe message, and JSON details. Parameter, path, and settings failures use 400; missing resources use 404; configuration prerequisites and lifecycle conflicts use 409; upstream Provider availability uses 502; unknown errors use 500 with a generic message.
- Provider authentication failure uses a dedicated stable code without implying authentication to the local Host. Duplicate submission, idle cancellation, closed mutation, workspace conflict, stale settings, and missing configuration have distinct codes.
- Closing an already closed Thread succeeds idempotently. Cursor expiry is recovered in-stream and is not an HTTP error.

### Turn task acceptance and responsiveness

- Turn submission never awaits the terminal Turn result. It schedules `run_turn` and returns accepted as soon as the task is registered.
- Workspace validation reaches an actual asynchronous suspension promptly because its scan runs in a worker. A large workspace therefore cannot block the Host event loop while preflight runs.
- The TaskManager's submission record covers the interval before Runtime becomes active, allowing refresh, duplicate detection, and Stop during preflight.
- Expected Runtime rejection is represented by `turn_rejected`. Infrastructure exceptions are converted to safe Host errors and remain visible through the Thread view; Host errors do not masquerade as Runtime AgentEvents or create a competing replay cursor.

### SSE event transport and recovery

- Browser-to-Host commands use HTTP; Host-to-browser progress uses SSE. WebSocket is not introduced.
- Runtime `AgentEvent` envelopes and payloads pass through without domain renaming. SSE `event` is the Runtime event type, `id` is `event_id`, and `data` is strict JSON.
- The adapter polls every 100 milliseconds while a Thread is active or a submission is starting, every one second while idle or closed, and sends a comment heartbeat every 15 seconds.
- An explicit `after_event_id` query parameter takes precedence over the `Last-Event-ID` header. Both refer to Runtime event IDs rather than sequence values.
- A normal connection emits retained events strictly in EventBuffer append order. Browser reducers deduplicate by event ID and stable tool-call IDs.
- A hydratable Thread read captures the latest event cursor and Snapshot without yielding control between those reads, then returns both. The client builds canonical UI from Snapshot and connects after the cursor.
- When Runtime reports an expired cursor, the adapter emits a `snapshot` recovery event containing a fresh Snapshot and cursor. Its SSE ID advances to the new Runtime cursor, or explicitly clears an obsolete ID when no event exists. Streaming then continues after that cursor.
- Snapshot recovery rebuilds Conversation from public messages, tool cards from structured message blocks, latest result and diffs from TurnSummary, settings from ThreadSnapshot, and pending transport state from Thread view. Approval recovery is not required in this version.
- Closing an SSE connection never cancels a Turn. Stop always uses the cancellation command.

### React Web UI

- The frontend uses React, TypeScript, Vite, React hooks, ordinary CSS/CSS variables, ESLint, Vitest, and React Testing Library. Tailwind, shadcn, Redux, and Zustand are not used.
- The primary desktop layout has workspace and Thread navigation on the left, Conversation and Composer in the center, and Activity on the right. It is an execution-oriented Coding Agent UI rather than a plain chat clone.
- Conversation renders user and assistant messages, tool request/running/result cards, file-change cards, Turn errors, Runtime rejection, and Host connection/configuration errors. Approval Card is absent.
- Activity renders current status, submission/Turn metadata, tool calls, changed files, diff completeness, and simplified unified diffs. Tool output and diffs use bounded scrollable monospace regions.
- Composer supports multiline text, submit, pending/running disable rules, and Stop. It never interprets Policy or treats SSE disconnect as cancellation.
- Provider settings use a password input with save, clear, masked configured state, automatic model discovery, explicit refresh, manual model entry, and default selection.
- Host errors are never swallowed. The UI distinguishes disconnected Host, invalid workspace, missing Thread, Provider authentication/availability, rejected Turn, failed Turn, failed tool, failed settings update, and failed cancellation.
- The SSE client reconnects automatically with bounded backoff, keeps current Snapshot-derived UI during disconnection, and refetches the hydratable Thread view when recovery cannot continue.
- Opening the existing Project dialog queries native capability and, when available, starts the native picker from that user action. A selected validated WSL path becomes the current project; cancel or any unavailable/failure response falls back to the Host filesystem browser, with a retry action for recoverable native failures. Duplicate native requests are disabled in the dialog while the Host also enforces single-flight.
- At widths below 1024 pixels, Thread navigation becomes a toggleable sidebar and Activity becomes a drawer or tab. Conversation and Composer remain available. V1 is desktop-first rather than a separate mobile product.
- Interactive controls support keyboard use and visible focus. Status meaning uses text/icon in addition to color, and failures are announced in persistent visible UI.

### Development, production, and CLI

- Python packaging registers an `agent` console entry with a `web` subcommand. The CLI accepts port, repeatable workspace roots, and a development flag.
- The server always binds `127.0.0.1`. V1 exposes no arbitrary bind-address option and does not open a browser automatically.
- The default port is 3080. Startup prints the actual local URL and configured workspace roots.
- Development mode starts only the Host interface and permits the exact Vite origin at `127.0.0.1:5173`; Vite proxies `/api` to the Host. Broad CORS is not enabled.
- Production mode serves the frontend build at `/`, assets beneath their build paths, and SPA fallback for non-API navigation. `/api` failures always remain JSON/SSE and never fall back to HTML.
- Production mode fails fast when the frontend entry asset is absent and prints the npm install/build commands needed to create it.
- The repository uses npm and commits its lock file. Python package metadata and the existing requirements workflow remain synchronized; no additional environment manager is introduced.

### Security and privacy

- The threat model is a trusted local single user on Linux/WSL. The application does not claim protection from an attacker who already controls that OS account.
- Loopback-only binding, same-origin production, exact development CORS, JSON mutation requests, fixed Provider endpoints, workspace root restrictions, secret masking, and safe error mapping are mandatory controls.
- API keys are never placed in browser persistent storage, URL parameters, events, logs, Snapshot, Summary, Thread settings, model messages, or tool results.
- Provider model discovery cannot accept arbitrary URLs. Workspace browsing cannot read file contents or broaden tool filesystem access.

## Testing Decisions

- Good tests assert externally observable behavior through the highest stable seam. Host tests call the application interface and assert status, DTO, headers, SSE frames, persisted safe state, and Runtime-visible effects; they do not assert route helper calls, task local variables, React hook internals, or AgentLoop collaborators.
- The Host application factory is the primary HTTP test seam. Runtime and Provider catalog dependencies are injected so API tests remain deterministic and do not use real API keys, network calls, paid requests, or production bubblewrap.
- One Host integration path uses the real `ThreadRuntime`, a scripted in-memory LLM adapter, real local file tools where practical, and the existing deterministic command sandbox adapter. It proves create → submit → event → tool/file change → terminal Snapshot end to end.
- Runtime tests extend existing prior art for Thread creation, busy rejection, event ordering, cursor expiry, cancellation, settings versions, close semantics, workspace validation, diffs, and public-data redaction.
- Runtime tests cover optional initial settings, asynchronous validation responsiveness, lease cleanup after preflight cancellation, and sanitized `turn_rejected` events for every pre-start failure class.
- Provider store tests cover first load, schema version, atomic replacement, owner-only permissions, masking, replacement, clearing, default selection, malformed-file failure, and absence of secrets from repr/log/error surfaces.
- Provider catalog tests cover successful ID reduction, deduplication, sorting, five-minute cache behavior, authentication failure, network failure, malformed payload, empty list, and manual-model fallback. Production URLs are asserted against preset-owned values rather than caller input.
- Workspace browser tests cover default and multiple roots, one-level sorting, hidden directories, truncation, symlink exclusion, nonexistent paths, permissions, sibling-prefix escape, `..` escape, and WSL-style POSIX paths.
- Thread API tests cover list, create with initial settings, not found, hydratable view, close idempotency, closed mutation, settings update, stale version, configuration required, and invalid workspace.
- Turn API tests cover accepted response latency, starting state, duplicate conflict, Runtime transition, terminal cleanup, preflight cancellation, active cancellation, expected rejection, unexpected task failure, close during Turn, and shutdown cleanup.
- SSE adapter tests cover event names, IDs, JSON data, append ordering, initial cursor, query/header precedence, disconnect/reconnect, heartbeat, active/idle polling selection, cursor expiry, snapshot recovery, empty-buffer cursor reset, terminal events, and no cancellation on disconnect.
- Frontend reducer tests cover Snapshot hydration, event-ID deduplication, tool lifecycle merging, terminal Summary, rejection, cursor recovery, reconnect without UI loss, and file diff completeness.
- Frontend interaction tests cover Provider setup, model discovery success/failure/manual fallback, workspace selection, Thread creation/switch/close, send, starting, running, Stop, settings conflict, visible Host errors, responsive navigation, and keyboard focus.
- Validation runs frontend lint, TypeScript checking, focused Vitest tests, production build, focused Python Host/Runtime tests, and the full existing pytest suite. The final acceptance run uses `npm ci` semantics against the committed lock file.
- Existing provider, local-tool, and Runtime suites are regression requirements. No test may depend on a live Provider account or native Windows Host.

## Out of Scope

- Native Windows Host execution, Windows command sandboxing, Windows process-tree cancellation, or Windows workspace validation.
- Approval endpoints, Approval Card, dangerous-command classification, or a new production ToolPolicy.
- Token-level assistant streaming, live command stdout/stderr streaming, or changes to the existing non-streaming model interface.
- WebSocket, Electron, Tauri, PTY, xterm.js, Monaco IDE, drag-and-drop filesystem access, or browser File System API workspace selection.
- Thread, Turn, event, conversation, diff, or task persistence across Host restarts; database-backed sessions; command reattachment beyond the in-memory per-Thread `ProcessManager` lifecycle.
- Authentication, TLS, remote access, multi-user operation, cloud deployment, distributed Host instances, or cross-process coordination.
- Custom Provider base URLs, OpenRouter, Ollama, LM Studio, arbitrary OpenAI-compatible endpoints, multiple accounts per Provider, multiple API keys, key rotation, OAuth, or system keyring integration.
- Provider pricing/capability registries, paid compatibility probes, automatic tool-calling verification, or a Cherry Studio-scale Provider catalog.
- Context compression, session branching, concurrent Turns in one Thread, Turn queues, mid-Turn steering, multi-agent UI, subagent UI, or plugin UI.
- Complete detection of files changed by arbitrary commands, Git-based change tracking, automatic commit/reset/checkout, or a full diff editor.
- Mobile-specific product design, frontend global state frameworks, CRDT, offline mode, or service workers.

## Further Notes

- The existing Runtime was deliberately designed for a future REST/SSE adapter. Snapshot, EventBatch, event IDs, safe public messages, settings versions, cancellation, close semantics, and TurnSummary should be reused directly rather than mirrored into a second Host domain model.
- The interval before `turn_started` is handled without changing the AgentLoop seam: workspace validation runs in a worker, every pre-start failure emits a sanitized `turn_rejected`, and the Host retains a safe transport error when an unexpected background task failure occurs.
- The largest recovery risk is creating a second replay log for TaskManager state. V1 instead keeps Runtime EventBuffer as the only Agent-event cursor and exposes submission lifecycle in the hydratable Thread transport view.
- Provider configuration borrows the useful separation seen in mature local clients—preset-owned connection facts, Host-owned sensitive configuration, and model IDs discovered from the Provider—without adopting their databases, large registries, multi-key systems, or protocol breadth.
- The first implementation should proceed as tracer bullets: establish Provider setup and one safe Host page, then workspace/Thread creation, background Turn, SSE recovery, execution UI, and finally production lifecycle. Each slice must remain demoable and keep the full existing test suite green.
- After this specification, work is split into dependency-declared tickets. Each completed ticket is validated, documented where its behavior changes design, committed with a specific Chinese message, and pushed without including unrelated workspace changes.

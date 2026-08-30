# Conversation-first Coding Agent Web UI refinement

## Problem Statement

当前 Web UI 已经具备完整的桌面 App Shell、Project 与 Model 顶层选择、Conversation 导航、固定 Composer，以及可折叠的 Activity 区域，但打开已有 Conversation 后，视觉中心仍被 Thread UUID、Project 路径、settings version、连接状态和 Runtime 操作占据。用户感知到的是“附带聊天框的 Runtime Dashboard”，而不是“可以自主修改代码的对话式 Agent”。

本轮需要在不改变 `Web → Host → ThreadRuntime → Agent Core` 依赖方向、不修改 state ownership、不重构 AgentLoop 的前提下，将普通对话 chrome 简化为 ChatGPT / DeepSeek 风格的 conversation-first 体验，同时保留 Coding Agent 所需的高密度 Tool、Changes、Diff、running、cancel 和 recovery 信息。

## Solution

将左侧区域进一步收敛为 Conversation history，将中央区域重组为“简洁 Conversation header → Conversation feed → 固定 Composer”，并把 Tool execution 与 File changes 首先呈现在 Conversation 内。右侧 Context Panel 继续作为可折叠的辅助监督视图，通过 Activity / Changes tabs、状态提示和计数提供更深入的信息，而不成为理解 Agent 行为的必经入口。

所有用户界面文案、状态、按钮、提示与无障碍标签使用中文。Provider 名称、模型 ID、文件路径、工具原始标识、代码、Diff、终端输出和错误代码作为技术数据保持原值；工具在普通界面显示中文友好名称，并在展开详情中保留原始标识。

Conversation 标题由前端根据第一条用户消息确定性生成，不新增 Host/API title 字段。无消息时使用“新对话”作为 fallback；有消息后清理连续空白并截断为适合导航和标题栏的短文本。标题可从 Thread Snapshot 重建，因此刷新后稳定，但本轮不实现独立 Session/title persistence。

## User Stories

1. As a Coding Agent user, I want the active Conversation to be the visual focus, so that I can concentrate on the coding task rather than Runtime records.
2. As a Coding Agent user, I want all user-facing controls and status text in Chinese, so that the product language is consistent.
3. As a Coding Agent user, I want technical identifiers and code artifacts preserved verbatim, so that localization never corrupts commands, paths, model IDs, or diffs.
4. As a new user, I want to see a clear “打开项目” action when no Project is selected, so that I know how to begin.
5. As a user with a selected Project, I want a prominent empty-Conversation prompt, so that I immediately know what I can ask the Agent to do.
6. As a user with an empty Conversation, I want starter prompts such as explaining the Project, finding a bug, running tests, and implementing a feature, so that I can begin without inventing phrasing.
7. As a user, I want starter prompts to fill the Composer without auto-submitting, so that I remain in control of code execution.
8. As a user, I want one stable Composer rather than duplicate empty-state and footer inputs, so that the input model stays predictable.
9. As a user, I want the Composer fixed at the bottom of the Conversation pane, so that it remains reachable while the feed scrolls.
10. As a user, I want clear Chinese send and stop actions, so that submission and cancellation are unambiguous.
11. As a user, I want the Composer to communicate when the Agent is working, so that I understand why another message cannot be sent.
12. As a user, I want to stop a running Turn from the main Composer area, so that cancellation is never hidden in Runtime controls.
13. As a user, I want the sidebar primary action labelled “新对话”, so that navigation follows familiar chat-product terminology.
14. As a user, I want the sidebar to be presented as Conversation history, so that it does not duplicate Project or Runtime settings.
15. As a user, I want the Project summary and full path removed from the sidebar, so that Project context is not repeated across three surfaces.
16. As a user, I want the Project selector to remain in the top bar, so that Project continues to be a global context.
17. As a user, I want a Conversation title derived from my first message, so that history entries are meaningful.
18. As a user, I want an empty Conversation to use a short “新对话” fallback, so that an internal UUID is never the primary title.
19. As a user, I want long titles truncated without horizontal overflow, so that navigation remains usable at narrow desktop widths.
20. As a user, I want the title derivation to be local and deterministic, so that it requires no extra model request or backend persistence.
21. As a user, I want the normal Conversation header to contain only the title and necessary transient status, so that the interface feels conversational.
22. As a user, I want idle and healthy connection metadata hidden, so that normal operation is visually quiet.
23. As a user, I want running, reconnecting, error, and closed states visible in Chinese, so that exceptional or actionable states remain clear.
24. As a user, I want UUID, settings version, full Project path, and provider details moved into Conversation details, so that technical metadata remains accessible without dominating the page.
25. As a user, I want manual Refresh moved into the overflow menu, so that automatic SSE/Snapshot recovery remains the normal path.
26. As a user, I want Conversation settings and close actions available from one overflow menu, so that secondary controls remain discoverable.
27. As a user, I want the top-bar Model selector to remain the default for new Conversations, so that this refinement does not silently mutate existing Thread settings.
28. As a user, I want the current Conversation model visible in its settings/details, so that I can inspect or change it deliberately.
29. As a user, I want Tool execution embedded in the Conversation feed, so that I can understand what the Agent is doing without opening a side panel.
30. As a user, I want compact tool rows with Chinese action names, status, and relevant target, so that common work is scannable.
31. As a user, I want running tools to have a clear active treatment, so that ongoing work is distinguishable from completed work.
32. As a user, I want successful tools to be visually quiet but explicit, so that completed work does not overwhelm the conversation.
33. As a user, I want failed tools to show a clear error state and expandable detail, so that I can diagnose failures.
34. As a user, I want tool arguments, results, metadata, and original tool IDs available on expansion, so that dense technical detail remains accessible.
35. As a user, I want File changes shown inside the Conversation after Agent work, so that modifications are easy to discover.
36. As a user, I want each changed file to show its path and change type, so that I can quickly assess scope.
37. As a user, I want to expand a file Diff in place, so that review stays connected to the Conversation.
38. As a user, I want code, terminal output, and Diff typography to remain dense and monospace, so that technical artifacts stay readable.
39. As a user, I want ordinary titles, buttons, prompts, and messages to use normal application typography, so that the UI does not feel like a debug console.
40. As a user, I want the Context Panel collapsed by default, so that Conversation receives the available width.
41. As a user, I want lightweight Activity and Changes indicators on the collapsed rail, so that important work remains discoverable.
42. As a user, I want separate Activity and Changes tabs after expanding the Context Panel, so that execution supervision and file review have distinct purposes.
43. As a user, I want the Activity tab to summarize Turn state and tool execution, so that I can supervise longer work.
44. As a user, I want the Changes tab to list modified files and expose their Diffs, so that the side panel supports focused review.
45. As a user, I want the panel to remain manually collapsible during and after a Turn, so that it never permanently consumes Conversation width.
46. As a user, I want Conversation switching to retain each Thread's Snapshot-derived feed, status, tools, and changes, so that work remains isolated.
47. As a user, I want a closed Conversation clearly marked and its Composer disabled, so that I do not attempt to continue an immutable Thread.
48. As a user, I want reconnecting and connection-lost states shown only when relevant, so that transport details appear only when actionable.
49. As a user, I want refresh and SSE recovery to reconstruct the same Conversation title and Agent work, so that browser recovery feels continuous.
50. As a keyboard user, I want menus, dialogs, tabs, starter prompts, Composer, and disclosure controls to have Chinese accessible names and visible focus, so that the workflow is operable without a pointer.
51. As a desktop user, I want the document locked to the viewport with independent sidebar, feed, and context scrolling, so that the Composer and top bar never disappear.
52. As a 1440×900 user, I want the full Conversation workspace and optional Context Panel visible without document scrolling, so that the desktop layout feels native.
53. As a 1280×800 user, I want the Conversation to remain dominant and controls unclipped, so that a common laptop viewport is comfortable.
54. As a 1024×768 user, I want a narrower sidebar and collapsed Context Panel without horizontal overflow, so that the UI remains usable on compact desktops.
55. As a maintainer, I want this refinement implemented in the React presentation layer, so that Host, ThreadRuntime, and Agent Core responsibilities remain unchanged.
56. As a maintainer, I want ThreadRuntime Snapshots and Agent events to remain the state source of truth, so that the UI does not invent a second agent state model.
57. As a maintainer, I want existing Provider, Workspace, thread settings, close, cancellation, SSE, and recovery APIs preserved, so that this refinement introduces no protocol migration.
58. As a maintainer, I want existing frontend integration and event-reducer tests updated rather than deleted, so that behavior remains regression protected.
59. As a maintainer, I want real-browser validation of empty, running, tool, error, changes, diff, close, switch, cancel, settings, model, and recovery states, so that tests do not substitute for UX verification.
60. As a coursework reviewer, I want the AgentLoop and Runtime architecture untouched, so that the project continues to implement its own required Agent mechanisms.

## Implementation Decisions

- Preserve the dependency direction `Web → Host → ThreadRuntime → Agent Core`; this feature is frontend-only unless browser validation proves an already-approved interaction impossible without a minimal API addition.
- Keep Thread, `thread_id`, Snapshot, EventBuffer, SSE cursor recovery, cancellation, thread settings, thread close, Provider API, model discovery, Workspace API, background Turn, and AgentLoop contracts unchanged.
- Use “对话”“新对话”“对话记录” as primary user-facing vocabulary. “Thread” and UUID remain internal details.
- Generate a frontend-only Conversation title from the first user text block by collapsing whitespace and truncating it to a stable display length. Use “新对话” when no user message exists.
- Do not translate or summarize Conversation titles with an LLM. Chinese input remains Chinese; other-language input remains in its source language.
- Remove the Project summary from the sidebar and the Project path from the normal Conversation header. The top-bar Project selector remains the canonical global Project surface.
- Reduce the normal Conversation header to title, transient status when meaningful, and one overflow control.
- Move Conversation settings, Refresh state, Conversation details, and Close conversation into the overflow surface. Conversation details may expose Thread UUID, full Project path, settings version, Provider, and model.
- Keep the top-bar Model selector as the default for newly created Conversations. Changing an existing Conversation model continues through the existing thread-settings API in Conversation settings.
- Hide healthy `connected` state. Show Chinese reconnecting, disconnected, recovered, running, cancelling, failed, and closed feedback only when meaningful.
- Keep one Composer in a fixed, non-shrinking footer. Empty-state starter prompts populate it without submitting.
- During a running Turn, show a prominent “停止” action beside the Composer and disable new submission while preserving cancellation.
- Keep Tool execution and File changes in the Conversation feed as the primary Agent-work representation. Use compact rows and expandable technical details.
- The current event projection does not provide reliable persisted cross-Turn timestamps for every tool artifact. Do not fabricate chronology. Present the available tool activity as an Agent-work cluster associated with the current Snapshot/latest work, while preserving message order and exact technical contents.
- Map known tool names to Chinese user-facing actions such as “读取文件”“编辑文件”“运行命令”“搜索文件”; retain the raw tool name in expanded details and use a safe generic “工具调用” fallback.
- Keep the Context Panel secondary and collapsed by default, including at Turn start. The collapsed rail may show running and change-count indicators.
- Split the expanded Context Panel into Activity and Changes tabs. Activity summarizes execution; Changes lists files and Diffs. User choice controls expansion.
- Maintain full-height App Shell and independent scrolling for Conversation history, Conversation feed, and Context Panel. Prevent long paths, titles, code, and technical output from creating document-level horizontal scrolling.
- Use normal application typography for conversation chrome and reserve monospace/uppercase styling for code artifacts, terminal output, model IDs, paths, compact technical labels, and transient execution indicators.
- Localize all static user-facing strings and accessibility labels into Chinese. Preserve technical data values exactly.
- Do not introduce Redux, Zustand, MobX, a UI framework, a design-system dependency, plugin architecture, slot architecture, Cordis, or DSH runtime concepts.

## Testing Decisions

- The highest frontend integration seam remains the rendered App with mocked Host HTTP responses. Tests assert external behavior through roles, labels, visible Chinese text, disabled states, requests, and reconstructed Snapshot content rather than component internals.
- Existing App integration tests are updated to cover Chinese navigation, Project selection, new Conversation creation, deterministic titles, simplified header, overflow actions, empty-state prompts, fixed Composer semantics, Activity/Changes tabs, close state, and cancellation.
- Existing event reducer tests remain the seam for Snapshot hydration, event deduplication, tool lifecycle merging, errors, file changes, cancellation, and terminal summaries. Add assertions only where presentation needs a new safe projection; do not test styling implementation details.
- Existing EventSource client tests remain the seam for SSE cursor recovery, reconnect backoff, and in-stream Snapshot recovery.
- 按用户在实施开始前给出的最新指令，本轮不执行第三步 Playwright 审核；最终报告不得把真实浏览器路径或多视口验收误报为已完成。
- 原定真实浏览器状态与响应式检查保留为后续验收清单，不作为本轮实现完成的伪造证据。
- Completion requires frontend tests, TypeScript, ESLint, production build, Python regression tests, and `git diff --check`.
- Review compares each numbered user story with implementation、测试与代码证据。A separate Luna Max audit reports complete, partial, or missing coverage and identifies any architectural drift，并明确标记因未执行 Playwright 而无法确认的视觉验收项。

## Out of Scope

- Session, Conversation-title, or database persistence beyond reconstructing display state from the existing in-memory Thread Snapshot.
- Approval UI or approval-policy changes.
- Sandbox redesign, native Windows Host, remote deployment, multi-user, multi-agent, worktree, or token streaming.
- New Agent Loop, Runtime, event protocol, provider backend architecture, DSH plugin/Cordis/slot architecture, or provider-hosted execution.
- Exact persisted cross-Turn visual chronology when the current Snapshot/event projection does not encode sufficient ordering metadata.
- Rebranding the product or replacing the existing dark theme and App Shell.

## Further Notes

- ChatGPT / DeepSeek Web defines the conversation-first product shape: simple history, quiet header, Conversation focus, stable Composer, and contextual connection feedback.
- DSH is used only as a reference for Coding Agent-specific Tool, Changes, Diff, Activity, Project, and running-state presentation. No DSH internals are adopted.
- The previous browser audit found that the production environment cannot run the default command sandbox because the local kernel rejects the required seccomp configuration. Real lifecycle UI validation therefore uses an audit-only deterministic Host built from the repository's existing Host, ThreadRuntime, SSE, local-tool, and test-sandbox seams; this does not change product architecture.
- The DSH endpoint at `http://127.0.0.1:3081` was unavailable during the prior audit, so no unobserved DSH behavior will be claimed as evidence.
- 本轮实施遵照用户最新指令跳过 Playwright 审核，因此不会生成或声称新的真实浏览器证据。

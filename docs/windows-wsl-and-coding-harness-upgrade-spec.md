# Host 项目选择与 Coding Harness 内核升级规格

> 2026-08-31 更新：原 Workstream A（Windows/WSL 原生目录选择器）已撤销并由
> [autonomous-web-workspace-refactor-spec.md](autonomous-web-workspace-refactor-spec.md)
> 取代。Web 端项目选择现在只使用 Host filesystem browser；原生 picker、PowerShell
> dialog、Windows path translation 和对应 transport API 均不再属于系统架构。
>
> 本文保留后续 Workstream 的历史记录，不是当前安全规范。凡与新规格冲突的旧描述（包括
> 递归拒绝内部 symlink/hard-link 或旧的 link syscall 限制）均不具规范效力；当前实现遵循
> access-time canonical containment，并允许 workspace-internal alias。见
> [current-architecture.md](current-architecture.md)。

## 1. 目的与交付顺序

本规格保留 Coding Agent 内核按 Policy、`apply_patch`、Stateful Shell、Streaming 四个 Phase
的升级约束。项目选择架构由上方链接的新规格定义。

内核实施顺序固定为 Phase 1 → 2 → 3 → 4。
每个阶段必须先完成 focused tests、相关 regression、diff 检查和中文 commit，再进入下一阶段。
所有完成项最后统一执行全量 Python/frontend regression、架构审查、`git diff --check` 和 push。

本规格不授权修改用户已有的 `.gitignore`、`agent.zip`、`web.zip`、`.playwright-cli/` 或
`output/` 改动。实现和提交必须精确暂存本任务文件。

## 2. 现有架构事实

- 依赖方向是 Web → Host → `ThreadRuntime` → Agent Core。
- `WorkspaceBrowser` 规范化 configured roots，通过词法 containment、逐组件 symlink 拒绝、
  directory/accessibility 检查实现 Host workspace 浏览和选择授权。
- `ThreadHost.create_thread()` 在调用 Runtime 前再次调用 `WorkspaceBrowser.validate()`；
  `ThreadRuntime.create_thread()` 和 Turn preflight 仍有各自既有 workspace safety 校验。
- Host 使用 FastAPI lifespan 统一关闭 `TurnTaskManager`、Threads、Runtime 工具和 Provider。
- Open Project 当前打开 React `WorkspaceDialog`，通过 `GET /api/workspaces` 浏览 Host 文件系统。
- Host JSON 错误使用 `{error: {code, message, details}}`，不把异常消息或 stack trace直接返回。
- `ToolCoordinator` 已拥有 policy/approval/tool execution 生命周期；`ThreadRuntime` 已有
  `resolve_approval()`，但默认 policy 是 `AllowAllPolicy`，Host/UI 尚未接入审批 resolution。
- `WorkspaceFilesystem` 是本地工具 workspace path、no-follow safety、文本读写和原子单文件写入
  的唯一入口；不得绕过。
- `ChangeTracker` 当前对 `write_file`/`edit_file` 假设一次调用一个 path，command diff 标记为
  incomplete。
- `run_command` 当前是 Bubblewrap 内的一次性非交互命令，支持 timeout/cancellation，但没有
  session、stdin、PTY 或增量输出。
- Provider-independent delta types 已存在；OpenAI-compatible `stream()` 尚未实现；AgentLoop 使用
  `chat()` 完整响应；`EventBuffer` 只支持即时 `read()`；Host SSE 通过轮询读取。

## 3. 已约定的测试 seam

TDD 测试只跨以下现有或必要扩展的公共 seam 观察行为：

- Host application factory 与 HTTP JSON/SSE transport；
- `WorkspaceBrowser.validate()` 最终 workspace authorization；
- `ThreadSettings` → `TurnSettingsOverride` → frozen `TurnConfig`；
- `ToolPolicy`/`ToolCoordinator` decision、approval、execution interface；
- `ToolRegistry` 对 parsed `ToolCallBlock` 的 dispatch interface；
- `WorkspaceFilesystem` 与 patch application interface；
- per-thread `ProcessManager` 的 exec/write/close interface；
- `LLMProvider.stream()` 的 local event interface和 completed canonical response assembly；
- `EventBuffer.read()` replay/cursor recovery 与 async subscription interface；
- 现有 React Open Project、approval/activity 和 event reducer 用户流程。

平台测试必须注入 fake adapter，不得弹真实 GUI。测试断言公共结果、事件、文件和进程状态，
不锁定私有 helper 调用或 subprocess 内部实现。

---

# Coding Harness 内核四阶段升级

## B0. 不变的责任边界

- AgentLoop 只保留 model ↔ tools 控制流；仅 Phase 4 为从 `chat()` 切换到 streaming 做最小改动。
- ToolCoordinator 负责 policy、approval 和 execution coordination。
- ToolRegistry 负责 schema validation 和 dispatch。
- Policy 判断 allow/deny/require approval，不进入工具实现。
- Sandbox 保持实际资源边界，不与 Policy 合并，不做无关重构。
- ProcessManager 只管进程/session 生命周期，不做 command risk classification。
- Event system 是 model、command、file、approval、turn 实时状态唯一出口。
- Conversation 只保存 completed canonical messages；stream chunks 永不直接写 history。

## Phase 1 — Command-aware Policy / Approval Policy

### P1.1 Settings 与冻结语义

新增 `ApprovalMode`：`UNTRUSTED`、`ON_REQUEST`、`NEVER`。默认值为 `ON_REQUEST`。它必须是
`ModelSettings`/`ThreadSettings` 的持久 Thread 设置字段，可由 `TurnSettingsOverride` 单 Turn 覆盖，
并进入 immutable `TurnConfig`。Turn 开始后修改 Thread settings 不影响 active Turn。

Host Thread create/settings DTO 和 Snapshot 序列化必须携带该字段；新增薄的 approval resolution
Host command，调用既有 `ThreadRuntime.resolve_approval()`。Frontend 只展示 runtime 产生的 reason
和 approve/deny 操作，不自行判断 command 风险。

不得把 mutable approval mode 放进共享 `ToolPolicy`。每 Turn policy decision 必须使用 frozen
`TurnConfig.approval_mode`；可通过 per-turn immutable policy、显式 decision context 或等价小接口实现。

### P1.2 Structured policy result

保留 `PolicyDecision` enum，新增 immutable structured result：`decision`、`reason_code`、`message`。
ToolCoordinator 只接受该结构；approval event 包含该 runtime result，denied ToolResult 也包含稳定
reason metadata。不得由 frontend 复算原因。

### P1.3 ExecPolicy classification 与 matrix

第一版只分析简单命令，fail closed 处理无法可靠分析的 shell。至少分类：

- `safe_read_only`：如 `pwd`、`ls`、`find`、`rg`、`grep`、`cat`、`head`、`tail`、`git status/diff/log/show`；
- `test_build`：常见 test、lint、typecheck、build 命令；
- `ordinary_sandboxed`：不含 shell control syntax 的简单项目命令；
- `destructive`：`rm`、危险 `git reset/clean/restore/checkout --` 等；
- `network`：`curl`、`wget`、`ssh`、`scp` 等；
- `package_install`：`pip/pipx/npm/yarn/pnpm/apt/... install/add`；
- `privileged`：`sudo`、`su`、`chmod`、`chown`、`mount` 等；
- `interactive`：interactive shell/REPL 或 `tty=true`；
- `complex_shell`：`bash -c`、`sh -c`、`eval`、pipe、`&&`、`||`、`;`、`$()`、backticks 等；
- `unknown`：malformed 或无法保守识别的输入。

不得实现或引入完整 Bash parser。不得因内部 Bubblewrap 最终使用 `bash -c` 而把所有 command 误判
为 complex；只分类模型提交的 command string。最低 policy matrix：

| Classification | UNTRUSTED | ON_REQUEST | NEVER |
|---|---|---|---|
| safe/read-only | ALLOW | ALLOW | ALLOW |
| test/build | REQUIRE_APPROVAL | ALLOW | ALLOW |
| ordinary sandboxed | REQUIRE_APPROVAL | ALLOW | ALLOW |
| destructive/network/package/privileged/interactive/complex/unknown | REQUIRE_APPROVAL | REQUIRE_APPROVAL | DENY |

`read_file`、`glob`、`grep`、workspace 内 `write_file`/`edit_file` 和后续 `apply_patch` 在三种模式均
ALLOW；workspace safety 继续由 WorkspaceFilesystem/ChangeTracker 保证。

Phase 1 focused tests 必须覆盖分类、matrix、复杂 shell、dangerous commands、普通项目命令、
structured reason、approval event/resolve、NEVER denial、Turn config frozen semantics 和 Host/UI 接线。
阶段完成后提交中文 commit。

## Phase 2 — Structured `apply_patch`

### P2.1 Tool interface

注册正式 `apply_patch` filesystem mutation tool，参数是一个非空 `patch` string。不得通过
`run_command`/`patch` binary 实现。支持一个 patch 中任意组合：

```text
*** Begin Patch
*** Add File: relative/path
+new content
*** Update File: relative/path
@@
 context
-old
+new
*** Delete File: relative/path
*** End Patch
```

支持多个 update hunk。Parser 必须拒绝：缺 begin/end、未知 section、重复/冲突 operation、非法 hunk
line、Add 非 `+` body、Delete 携带 mutation body、空 path、absolute path 和 `..` path。Hunk 按原文件
内容和顺序做 deterministic exact match；zero match、ambiguous match 或上下文不符返回稳定
`PATCH_HUNK_MISMATCH`，不写任何文件。

### P2.2 Safety 与 atomic-ish commit

流程固定为 parse all → validate all paths/operations → read/prepare all final contents → commit。所有 path
必须通过 `WorkspaceFilesystem` 的现有 lexical/no-follow/containment/text safety，不复制第二套 filesystem
sandbox。Add 要求目标不存在；Update/Delete 要求现有 UTF-8 regular file。symlink、hardlink、absolute、
workspace escape 保持现有拒绝语义。

在 commit 前所有逻辑 failure 必须发生；commit 遇到 I/O failure 时必须 rollback 已提交 path 到 before
state。整个 patch 对调用者表现为 all success 或 no partial modification。保留既有 file mode（适用时），
删除与回滚不得跟随 link。

### P2.3 ChangeTracker 0..N paths

将 ChangeTracker 从 one call → one path 升级为 one call → 0..N affected paths。`write_file`/`edit_file`
行为保持。`apply_patch` execution 前 snapshot 全部 declared path，成功后 snapshot 全部 path，按稳定 patch
顺序 emit `file_changed`，Turn summary 产生 original→final diff。失败/rollback 不产生虚假 change event。

ToolResult metadata 至少含稳定 ordered `affected_paths`、added/updated/deleted counts。Policy 仍在
ToolCoordinator，不进入 patch tool。

Phase 2 focused tests：single update、multi-file、add、delete、multiple hunks、malformed、hunk mismatch、
absolute/`../`/symlink escape、inaccessible/non-directory、logical failure no write、injected commit failure
rollback、ChangeTracker event/summary order。阶段完成后提交中文 commit。

## Phase 3 — Stateful Shell / Long-running Process

### P3.1 Tool surface

正式 model-facing surface 使用 `exec_command` 与 `write_stdin`。迁移现有 `run_command` tests/docs/prompt；
除非兼容性确有现存调用要求，不长期同时暴露两个重复 command tools。

`exec_command` 参数至少：`command`、workspace-relative `cwd="."`、`yield_time_ms`、`timeout_ms`、
`tty=false`。initial yield 内退出则返回 `status="exited"`、exit code 和当前 stdout/stderr；仍运行则返回
`status="running"`、opaque `session_id` 和当前输出。

`write_stdin` 参数至少：`session_id`、`chars=""`、`yield_time_ms`。空 chars 只等待/拉取新增输出；非空
原样写 stdin；支持 `\u0003` Ctrl-C。每次结果只返回调用 cursor 以来的增量及当前 running/exited 状态。
TTY 合并输出时必须在 interface/metadata 中明确，不伪造独立 stderr。

### P3.2 ProcessManager lifecycle

每个 Thread 的 local tool composition 拥有一个 `ProcessManager`；session 不进入 AgentLoop/Conversation
state ownership。默认最大 4 个 active sessions/thread、15 分钟无 interaction idle timeout、每流 100 KiB
bounded head/tail 或等价明确预算。所有 output reader 必须持续 drain，慢 UI 不得阻塞 child。

Session 在 process exit 后保留最后 unread output，读取终态后回收；timeout 终止 process group；start failure、
unknown session、session limit、stdin closed 使用稳定 error code。thread close/Host shutdown 必须 terminate、
kill fallback、await/reap child 和取消 reader/watchdog task，close 幂等且不遗留进程。

Turn cancellation 终止该 Turn 创建且仍 active 的 sessions；正常 Turn 完成后 session 可继续作为 per-thread
session，直到 exit/idle/thread close。该权限与生命周期必须由测试固定。

### P3.3 Sandbox、PTY 与 approval

继续复用现有 Bubblewrap command construction、workspace mount、network isolation 和 process-group safety；
不得回退 host shell。扩展既有 sandbox adapter 以支持 session/stdio/PTY，不新建绕开 Bubblewrap 的生产
execution path。

`tty=true` 或启动 shell/REPL classification 必须 REQUIRE_APPROVAL（NEVER 为 DENY）。批准语义是“在
现有 sandbox 内开启一个 interactive session”；该 session 后续 `write_stdin` 在这次已批准 session 权限内
运行，不重新解析 stdin command。非 interactive `write_stdin` 也只访问已由 ExecPolicy 决策的 session。

### P3.4 Unified events 与 command change tracking

通过 ToolCoordinator 向 tool execution 提供 turn-scoped event sink/context；具体 process module只报告
生命周期/output，不创建第二套 event bus。至少 emit：`command_started`、`command_output_delta`
（stream + text + session_id）、`command_completed`。增量必须在 command 运行时出现，而不是仅在 ToolResult。

Git workspace 下，在 command 前后用轻量 Git porcelain/diff 或等价方式改善 changed-path/diff tracking；
不得全量 snapshot 整个 workspace。对于仍运行、非 Git 或无法完整观察的情况明确
`diff_complete=false`，不得猜测完整性。

Phase 3 focused tests：fast、running、incremental stdout/stderr、stdin、empty poll、PTY、Ctrl-C、timeout、
Turn cancellation、thread close、Host shutdown、idle cleanup、session limit、bounded output、sandbox escape、
interactive approval/NEVER、command events ordering、Git change tracking。阶段完成后提交中文 commit。

## Phase 4 — End-to-end Streaming

### P4.1 Provider 与 canonical assembly

实现 OpenAI-compatible `stream()`，通过 SDK async stream 转换成本地 `TextDeltaEvent`、
`ReasoningDeltaEvent`、`ToolCallDeltaEvent`、`MessageEndEvent`、`ErrorEvent`。处理 provider 支持的 reasoning
字段、finish reason、usage 和 typed/sanitized error。

新增 `MessageAssembler` deep module，按 tool-call index，随后用 id 校验/合并 fragmented id、name 和
arguments。arguments 只在 message end 后解析 JSON；malformed JSON 复用现有 `arguments_error` 语义。
text/reasoning/tool delta 先实时 emit；只有正常 MessageEnd 后才构建一个 canonical assistant Message 并
调用 `Conversation.append_assistant()`。stream error/cancellation 不追加半条 message或半个 ToolCall。

ModelInvoker 继续应用 frozen TurnConfig、context budget 和 retry rules。若 stream 已产生任何 provisional
delta，失败后不得透明 retry 并重复 UI 内容；只允许在首个 delta 前按既有 retry policy 重试。

AgentLoop 只做最小控制流改动：stream/assemble completed response → append canonical assistant → tool calls
则 ToolCoordinator → 下一轮，否则完成。不得把 chunk parsing、Policy 或 process lifecycle放进 Loop。

### P4.2 EventBuffer subscription 与 Host SSE

保留 `EventBuffer.read(after_event_id)` 用于 replay、cursor recovery 和 Snapshot。增加 async real-time
subscription/wait interface。实现必须 bounded/non-blocking：append 不等待 subscriber；slow/disconnected
subscriber 不阻塞 Agent；cursor 落后继续通过现有 expiry+Snapshot恢复；subscription cancellation 清理等待
对象/queue。

Host `EventStreamAdapter` 改用 real-time subscription，保留 heartbeat、Last-Event-ID、query cursor precedence、
disconnect 和 Snapshot recovery 行为，不创建 websocket/第二事件通道。

统一事件至少覆盖：model text/reasoning/tool-call delta；command start/output/completed；apply_patch/file
change；approval requested/resolved；turn completed/failed/cancelled。Frontend reducer 用 provisional streaming
state 渲染 delta，canonical Snapshot/recovery 仍来自 completed Conversation。reasoning visibility 继续遵循
runtime setting。

Phase 4 focused tests：provider text/reasoning/tool fragments、Unicode、fragmented JSON、multiple calls by
index/id、finish/usage、malformed arguments、provider error、pre/post-delta retry、cancellation no partial history、
subscription wakeup/slow/disconnect/bounds/cursor recovery、SSE immediate delivery、frontend reducer/render。
阶段完成后提交中文 commit。

## B5. 跨阶段 E2E 与最终回归

使用 fake provider 做完整 public-flow 测试：

1. streaming assistant fragmented `apply_patch` tool call；
2. patch files + ordered file events；
3. streaming `exec_command` long-running call；
4. command output deltas + `write_stdin`/poll + terminal ToolResult；
5. streaming final assistant；
6. tool call/result pairing、event ordering、canonical Conversation、Turn summary valid。

另测 dangerous command → `approval_requested` → Host approve → execute，以及 dangerous + NEVER →
`POLICY_DENIED`。覆盖 cancellation、timeouts 和 no leaked process。

每个 Phase 的 commit message 必须是准确中文。最终运行：

```bash
rtk .venv/bin/python -m pytest -q
cd web && rtk npm run lint
cd web && rtk npm run typecheck
cd web && rtk npm test -- --run
cd web && rtk npm run build
rtk git diff --check
```

根据实际环境调整虚拟环境命令，但不得跳过 full Python regression、frontend lint/typecheck/test/build。

## B6. 最终架构审查清单

以下任一情况均为必须修正的 blocker：

- Policy logic 进入 AgentLoop；
- Approval logic 进入具体 tool；
- Sandbox 与 Policy 合并；
- ProcessManager 承担 Policy；
- frontend 判断 command 风险；
- stream chunk 直接写 Conversation；
- command stdout 绕开统一 EventBuffer/SSE；
- `apply_patch` 绕开 WorkspaceFilesystem；
- interactive process 没有 idle/thread/Host shutdown 生命周期；
- Host workspace path 未经 canonical containment 检查便进入 Runtime；
- 为上述能力引入 Agent framework/SDK、Electron/Tauri、provider-hosted execution 或第二套 Runtime。

## 4. 明确不在范围

不实现 native Windows Host、远程部署、多用户、多 Agent、独立 session/PTY 持久化、完整 Bash parser、
新的 sandbox 产品、Provider-hosted filesystem/command、Electron/Tauri、Browser File System Access 作为 Agent
filesystem、UI redesign 或 Agent framework/SDK。

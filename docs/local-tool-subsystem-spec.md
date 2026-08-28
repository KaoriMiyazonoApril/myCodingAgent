# 本地工具子系统 MVP 规格

## Problem Statement

该 coding agent 已具备供应商无关的消息、模型请求和模型可见工具定义，但还不能在本地
工作区中实际读取、修改、搜索文件或运行命令。因此，模型即使生成了已解析的工具调用，
Agent 仍缺少一个一致、可测试且不依赖模型供应商的本地能力层来执行它们。

用户需要六项基础能力：读取文件、写入文件、精确编辑、按 glob 查找文件、按正则搜索
内容，以及执行一次非交互式 shell 命令。所有文件和命令可见的持久化可写目录必须受
配置的工作区根目录约束；常见操作失败必须成为结构化结果，而不是未控制的异常或会话
副作用。

## Solution

实现一个独立的本地工具子系统。应用或未来 runtime 负责持有工作区配置、构造共享的
文件系统和进程执行模块，并把它们注入六个能力工具；纯粹的工具注册表只负责注册、按
名称查找、列出模型定义和分派已接受的工具调用。

该子系统复用现有的模型可见工具定义和对话工具调用模型。工具执行返回新的执行域
`ToolResult`；它不改变会话历史，也不携带对话消息绑定信息。未来 runtime 可将其转换
为既有的会话工具结果 block。错误代码是执行结果是否失败的唯一事实源。

## User Stories

1. As an agent runtime author, I want to construct local tools with one configured workspace root, so that filesystem and initial command directories have a consistent scope.
2. As an agent runtime author, I want the registry to remain independent of workspace configuration, so that composition policy stays outside the registry module.
3. As a model, I want to receive definitions for all six tools, so that I can select available local capabilities through the existing model request interface.
4. As a model, I want to call `read` with a path and optional pagination, so that I can inspect a text file without receiving its entire contents.
5. As a model, I want read output to include one-based line numbers, so that I can identify an exact location for a later edit.
6. As a model, I want read metadata to report requested and returned lines, total lines, range, and truncation, so that I can page deterministically.
7. As a model, I want an out-of-range read offset to succeed with an empty page, so that probing past EOF is not treated as a runtime failure.
8. As a model, I want to write UTF-8 text to a new file and have missing parent directories created, so that I can create source files and configuration files.
9. As a model, I want writes to replace an existing regular text file atomically, so that a failed write does not leave a partial file.
10. As a model, I want writes to preserve an existing file's mode where practical, so that executable source scripts do not unexpectedly lose their executable bit.
11. As a model, I want to edit a file by exact old-string replacement, so that a change is precise and reviewable.
12. As a model, I want a non-global edit to reject zero or multiple old-string matches, so that ambiguous or missing edits never partially modify a file.
13. As a model, I want `replace_all` to replace every exact occurrence, so that intentional repeated transformations are possible.
14. As a model, I want to search files with a relative glob pattern, so that I can discover relevant regular files without host-specific paths.
15. As a model, I want glob results to be stable, workspace-relative POSIX paths, so that follow-up calls can reuse them across operating systems.
16. As a model, I want to search file contents with a Python regular expression and optional include glob, so that I can locate source text with paths, line numbers, and matching lines.
17. As a model, I want invalid search regular expressions reported with a stable error code, so that I can repair the call instead of inferring an implementation exception.
18. As a model, I want ordinary searches to skip dependency and generated directories, so that result budgets remain useful.
19. As a model, I want an explicitly selected ignored directory to be searchable, so that default noise reduction does not block an intentional investigation.
20. As a model, I want to run one non-interactive shell command from a workspace-relative initial directory, so that I can validate or inspect local work.
21. As a model, I want command stdout and stderr captured separately with duration, exit code, timeout, and truncation metadata, so that I can distinguish command behavior from tool behavior.
22. As a model, I want a non-zero command exit code reported as `COMMAND_FAILED` while retaining stdout, stderr, and exit metadata, so that failing tests cannot be mistaken for success and remain inspectable.
23. As a model, I want timed-out command output retained where possible, so that I can diagnose the partial execution.
24. As an agent-loop author, I want stable error codes for invalid arguments, path violations, text-file failures, edits, regexes, and process failures, so that recovery logic can act without parsing prose.
25. As a maintainer, I want tools to return results rather than append messages or request approvals, so that capability execution remains separate from policy, UI, and conversation orchestration.
26. As a maintainer, I want temporary-directory tests at the tool registry and tool execution seams, so that results do not depend on a developer machine's paths or files.
27. As an async runtime author, I want a non-blocking dispatch entry point, so that local execution does not freeze the Agent Loop or UI event loop.
28. As a model, I want commands to access persistent files only in the configured workspace, so that a generated command cannot inspect or alter unrelated host files.
29. As an async runtime author, I want cancellation to terminate the command process group, so that stopping the Agent cannot leave commands running in the background.
30. As an application author, I want command-sandbox capability checked during local-tool composition, so that an unsupported host fails before the first model command.
31. As a model, I want a selected search path to remain a hard boundary even when it contains symlinks, so that a subtree search cannot read a sibling file.
32. As a runtime author, I want file reads and searches to have byte, file-count, line-length, duration, and regex-time limits, so that model-controlled inputs cannot exhaust memory or monopolize a worker.
33. As an application author, I want closing a tool registry to release its command-sandbox resources exactly once, so that repeatedly creating and closing Threads does not leak capability descriptors.

## Implementation Decisions

- The tool subsystem extends the existing tool-domain definition model rather than creating another model-facing schema type. Existing parsed tool-call and conversation result blocks retain their current responsibilities.
- A new execution-domain result carries model-readable content, JSON-compatible metadata, and an optional stable error code. `ok` and `is_error` are derived exclusively from the presence of that code; no independently mutable error boolean exists.
- `ToolResult.to_message_block` binds an execution result to a tool call. The model adapter serializes `ok`, content, metadata, and error code together as a JSON tool-message envelope.
- The common executable-tool interface accepts an existing, already accepted tool call and returns an execution result. Individual tools contain capability behavior only: they do not perform approval, permission-policy, UI, agent-loop, or conversation-history work.
- The registry is a deep module with a small interface: registration, lookup, definition enumeration, consistent execution dispatch, and idempotent closure. It owns no workspace configuration, file system, process service, policy, or runtime state; composition may attach one opaque cleanup callback for services captured by registered executors.
- The registry offers synchronous `execute` for CLI/tests and `execute_async` for Agent Loop/UI callers. Ordinary synchronous tools use a worker thread; cancellation waits for that worker to become quiescent and marks its returned result for Runtime reconciliation before the Turn may release shared state. Tools with an async executor, notably `run_command`, remain natively async and cancellable.
- Runtime composition is explicit: it constructs shared filesystem and process modules from the configured workspace, injects those modules into tools, then registers tools. No global workspace configuration is introduced.
- A filesystem module centralizes relative-path normalization, absolute-path rejection, workspace containment, final symlink containment, UTF-8 validation, regular-file validation, atomic text replacement, and controlled traversal. It accepts `/` path separators at the tool interface and returns workspace-relative POSIX paths.
- Absolute paths and any lexical or resolved path escaping the workspace are rejected. An internal symlink is usable only when its resolved target remains inside the configured workspace. During `glob` and `grep`, a file symlink is followed only when its resolved target also remains below the caller-selected `path`; directory symlinks are not traversed.
- Text files are strict UTF-8 regular files without NUL bytes and have a 10 MiB direct-read resource limit. Direct reads of directories, missing files, oversized files, binary or non-UTF-8 files, and unsafe paths become structured operational errors. Search traversal skips non-text files and continues with remaining files. New write content containing NUL is rejected as non-text.
- Existing-file writes use a same-directory temporary file plus atomic replacement. Existing mode and executable permission are preserved where practical; ACLs, extended attributes, fsync durability protocols, and broader metadata preservation are not implemented.
- `read` uses one-based offsets and a default limit of 200. The limit must be an integer from 1 through 2,000, expressed in both the model schema and local validation. It streams lines and retains only the requested page while counting total lines, so memory is bounded by the page plus the 10 MiB file limit. Out-of-range offsets return an empty success page. Read metadata contains `requested_offset`, `requested_limit`, `returned_lines`, `total_lines`, `start_line`, `end_line`, and `truncated`; an empty page uses `start_line = null` and `end_line = null`.
- `edit` performs exact, case-sensitive string replacement. `old_string` must be nonempty without trimming; whitespace-only strings are valid. `new_string` may be empty. A non-global edit requires exactly one match, while global edit replaces all matches. Failed matching never writes the file.
- `glob` and `grep` use a single platform-neutral Python traversal implementation rather than selecting between ripgrep and a fallback. They return only regular files, use stable workspace-relative POSIX ordering, retain at most 200 matches, stop as soon as a 201st match proves truncation, scan at most 10,000 files, and expose truncation.
- Default traversal excludes `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `build`, and `dist`. The exclusion is overridden only when the caller explicitly chooses such a directory as `path`; a matching-looking pattern does not override it.
- `glob` treats its pattern as a relative pathlib-style glob below the selected path. `grep` treats `pattern` as a timeout-capable regular expression and optional `include` as a glob against workspace-relative POSIX paths. Invalid regexes produce `INVALID_REGEX`; a match exceeding 50 ms produces `REGEX_TIMEOUT`. `grep` additionally limits each file to 5 MiB, cumulative selected content to 50 MiB, each line to 100,000 characters, and the search to 5 seconds. Reaching a non-fatal scan boundary sets `truncated = true`.
- The process module resolves `cwd` through the filesystem module. Its public `CommandSandboxBackend` seam owns capability probing, cancellable command execution, and idempotent resource closure. The default `BubblewrapSandboxBackend` requires Linux or WSL2 plus `bwrap`, and runs `bash --noprofile --norc -c` in a new namespace with only `/usr`, process/device support, an ephemeral `/tmp`, and the workspace mounted read-write. Host environment variables, host paths, capabilities, and network namespaces are not inherited. Runtime composition executes a minimal command through the real sandbox configuration, not merely `which bwrap`, and raises `CommandSandboxUnavailableError` immediately if installation, user namespaces, mounts, or container policy make the backend unusable; it never silently falls back to a host shell. Explicit backend injection exists for controlled application composition and deterministic contract tests. Closing the composed registry delegates through `CommandRunner.close()` and releases Bubblewrap's retained seccomp descriptor exactly once; later execution fails closed.
- Command timeout defaults to 60,000 ms and is constrained to 1 through 300,000 ms. The process module captures stdout and stderr independently, preserving bounded head and tail portions of each 100 KiB stream. It reports duration, exit code, timeout, and truncation. Exit zero is success, non-zero is `COMMAND_FAILED`, and timeout is `TIMEOUT`.
- `CommandRunner` uses `asyncio.create_subprocess_exec()` and async stream readers. Timeout and coroutine cancellation terminate the owned process group and bound every cleanup wait; if the asyncio process waiter still cannot settle after SIGKILL because a detached descendant retains a pipe, that waiter and the pending readers are cancelled. This prevents pipe-lock deadlocks and keeps timeout/cancellation latency bounded. Production Bubblewrap additionally owns the isolated process namespace; the deterministic test adapter does not claim to contain deliberately detached host descendants.
- Error codes include at least invalid arguments, unknown tool, workspace escape, not found, not a file, not text, file too large, invalid regex, regex timeout, missing edit match, ambiguous edit, I/O failure, process start failure, command failure, timeout, and unexpected internal failure. Expected operational failures are converted into execution results at the registry/tool seam; unexpected exceptions are logged with traceback while only a safe message reaches the model.
- Every model-visible tool schema is an object with `additionalProperties: false`. Required fields, defaults, numeric ranges, and `edit.old_string` minimum length agree with local validation.
- `ToolDefinition.validate_arguments` is the runtime source of truth for the supported JSON Schema subset. It validates types, required/unknown fields, string lengths and numeric ranges, and applies defaults before dispatch; unsupported or missing property schema types fail closed instead of skipping validation. Capability functions retain only semantic checks such as regex compilation and workspace containment.

## Testing Decisions

- Tests assert observable tool results, created or modified files, command output, and schemas; they do not assert private helper calls, internal traversal mechanics, or subprocess implementation details.
- The main execution seam is the registry executing an existing parsed tool call and returning an execution result. This reuses the project’s existing tool-call domain model and avoids introducing a parallel call representation.
- Filesystem tests use temporary workspaces and cover normal read, read pagination and missing files; write create, overwrite and parent creation; edit success, zero-match immutability, multiple-match immutability, global replacement, and empty old-string rejection.
- Search tests use temporary workspace trees and cover regular-file glob results, workspace-relative path normalization, selected-subtree symlink containment, regex line matches, line numbers, include filtering, default ignores, explicit ignored-directory paths, early result limits, file/byte bounds, invalid regex errors, and regex timeouts.
- Process tests cover stdout/stderr separation, zero and non-zero exit codes, workspace-relative cwd, timeout behavior, bounded cleanup when a detached descendant retains a pipe, partial output retention where deterministic, output truncation, command-start failures, and idempotent sandbox resource closure.
- Cross-cutting tests cover absolute paths, traversal attempts, external symlink targets, non-text files, strict validation, all six registry definitions, duplicate registration behavior, unknown tools, and the absence of uncaught expected operational exceptions.
- Existing pytest tests for the OpenAI-compatible provider demonstrate the repository’s style: direct construction of local dataclasses, dependency injection where an external client would otherwise be required, and assertions on stable local domain types. The new tests follow that style.

## Out of Scope

- Approval workflows, user interaction, general permission policy, and command authorization beyond the mandatory workspace sandbox.
- Agent-loop control, conversation-history mutation, UI communication, and conversion of execution results into conversation result blocks.
- Persistent command sessions, stdin streaming, job management, process reattachment, cancellation tokens beyond coroutine cancellation, and production command sandbox backends other than bubblewrap.
- `apply_patch`, git-specific, browser, web, MCP, or subagent tools.
- Arbitrary shell-command translation between operating systems.
- Ripgrep integration, pluggable search backends, parallel tool execution, deferred-tool exposure, tool search, and registry production features beyond the MVP.
- Full ACL, extended-attribute, ownership, or durability management during writes.

## Further Notes

- Project command execution requires Linux or WSL2 with bubblewrap installed and usable. `run_command` uses its namespace with `/workspace` as the sole persistent writable mount; `/tmp` is ephemeral, other host paths are absent, and the network is unshared. This is capability isolation, not an approval or intent policy.
- Model-facing paths should remain workspace-relative POSIX paths even when the runtime executes on Windows. The runtime does not translate arbitrary command syntax between shells.
- The retained result limit for glob and grep is 200; traversal stops on the 201st match rather than accumulating the full repository result set. Direct text files are limited to 10 MiB, and grep has the additional scan limits documented above. Each command stdout and stderr capture budget is 100 KiB. These are resource limits, not approval or permission decisions, and all non-error truncation is exposed to the model.
- This specification is local by explicit request and has not been published as a GitHub Issue.

## 当前实现

当前 MVP 位于 `agent/tools/`，并保持模型连接层与执行能力层解耦：

- `types.py` 定义既有的模型可见 `ToolDefinition`，以及执行域的 `ToolResult`。`error_code`
  为 `None` 表示成功；`ok` 与 `is_error` 均由该字段推导，不存在可独立修改的错误布尔值。
- `filesystem.py` 的 `WorkspaceFilesystem` 是工作区路径、文本文件和原子写入的唯一
  入口；共享的逐组件 no-follow 检查同时保护 workspace 根与每次工具路径解析，父目录中的
  symbolic link 不会先被 `resolve` 跟随。`ToolOperationError` 将预期的本地操作失败编码为
  稳定错误码。
- `process.py` 的 `CommandSandboxBackend` 统一能力探测、异步输出采集、超时与进程组取消
  及幂等资源关闭生命周期；生产 `BubblewrapSandboxBackend` 负责构造隔离命令并在关闭时
  释放 seccomp descriptor，测试 adapter 通过同一
  contract 提供确定性执行。`CommandRunner` 通过受验证的初始目录运行一次 shell 命令，并分别在
  metadata 中返回 stdout、stderr、持续时间、退出码、`command_succeeded`、sandbox、
  超时和截断状态。非零退出返回 `COMMAND_FAILED`，超时返回 `TIMEOUT`。
- `registry.py` 的 `ToolRegistry` 提供注册、查找、定义列举及对既有
  `ToolCallBlock` 的统一分派；它不保存任何工作区配置。同步工具在 worker thread 中执行，
  取消或 deadline 到达时会先等待 worker 静止，再把实际结果交还 Runtime 记录，避免后台
  文件修改越过 workspace lease 生命周期。其 `close()` 幂等调用组合层注入的资源清理回调，
  关闭后所有执行请求均 fail closed。
- `local.py` 显式组合共享的文件系统与进程服务，并注册
  `read_file`、`write_file`、`edit_file`、`glob`、`grep`、`run_command` 六个工具。
  组合函数可显式接收一个 `CommandSandboxBackend`；未提供时只使用生产 Bubblewrap，
  capability probe 失败即终止组合，不会回退到 host shell。

所有工具定义均使用关闭的对象 schema（`additionalProperties: false`）。文件查询与搜索
结果均使用工作区相对的 POSIX 路径；搜索中的符号链接目标必须同时位于工作区和调用方
选定子树内。`glob` 和 `grep` 最多保留 200 个结果并在确认截断后提前停止，`grep` 还对
文件数量、单文件/总字节数、行长、总时长和正则匹配时长设置资源边界。`read_file` 流式
保留请求页，并拒绝超过 10 MiB 的文本文件。命令的每个输出流最多保留 100 KiB 的头尾内容。

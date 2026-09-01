# My Coding Agent

本项目是一个独立实现的本地 Coding Agent。Agent loop、Conversation、工具调度、终止逻辑和
本地文件/命令工具均由本仓库实现；Web UI 通过 Host/API 层复用 `ThreadRuntime`，不直接依赖
AgentLoop、Provider SDK 或具体工具。

## Development

后端与前端开发服务分开启动。Host 固定监听 `127.0.0.1:3080`，Vite 固定监听
`127.0.0.1:5173` 并代理 `/api`：

```bash
# terminal 1: backend
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m agent web --dev --workspace-root "$PWD"

# terminal 2: frontend
cd web
npm install
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`。开发模式不读取 `web/dist`，且 CORS 只允许约定的 Vite
origin。

## Production

先构建前端，再启动单一 Host 进程：

```bash
cd web
npm install
npm run build
cd ..

.venv/bin/pip install -e .
agent web --workspace-root "$PWD"
```

默认地址为 `http://127.0.0.1:3080`。`agent web` 会同时提供 API、SSE 和 `web/dist` 静态资源；
若 production build 缺失，会直接退出并打印构建命令。

可重复指定允许浏览和创建 Thread 的 Host workspace root：

```bash
agent web \
  --port 3080 \
  --workspace-root /home/user/projects \
  --workspace-root /mnt/c/code
```

未指定 root 时从 Host 的 `/` 开始浏览；启动命令所在目录不再隐式成为授权边界。
Workspace Dialog 通过 Host browser 列出、导航并显式选择目录，选择后 Host 创建带有
`workspace_id`、规范化路径和显示名称的内存 Workspace 记录。目录中的内部 symlink
按实际规范化目标处理，Agent 工具仍在每次访问时执行 workspace containment 校验。

## Provider Setup

首次打开页面后，在 Provider settings 中配置 DeepSeek、Moonshot/Kimi 或 GLM 的 API key，
发现或手动填写模型，并选为默认 Provider。密钥由 Host 写入
`~/.config/my-coding-agent/providers.json`（文件权限 `0600`）；响应、Thread、Snapshot 和事件均
不会返回密钥。Provider endpoint 是 Host 内置固定值，浏览器不能提交任意 base URL。

## Linux / WSL

本版本支持 Linux Host，以及在 WSL2 中运行 Host、由 Windows 浏览器访问的方式：

```bash
# inside WSL
agent web --workspace-root /home/user/projects
```

Windows 浏览器通常可直接访问 `http://localhost:3080`。Host 在 WSL 中时，Agent 操作的是
Linux/WSL 文件系统、shell 和进程环境；例如 Windows 盘可通过 `/mnt/c` 直接在 Host browser
中浏览和选择。浏览器不会自行解释或转换 `C:\...` 路径，也不会打开 Windows 原生文件夹
窗口。

## Validation

```bash
.venv/bin/python -m pytest -q

cd web
npm run lint
npm run typecheck
npm test -- --run
npm run build
```

## Architecture

```text
React Web UI
    │ HTTP commands + SSE events
    ▼
Agent Host
    ├── stable JSON API
    ├── TurnTaskManager
    ├── EventStreamAdapter
    ├── WorkspaceBrowser + Host Workspace records
    └── production static files
    │
    ▼
ThreadRuntime
    │
    ▼
Agent loop ── Conversation ── local Tools / LLM client
```

依赖方向始终为 `Web → Host → ThreadRuntime → Agent Core`。当前 Runtime 已通过本地 SQLite
持久化 Thread、canonical Conversation、Turn/event/idempotency 状态与 Context compaction
checkpoint。V1 通过既有 Runtime approval state machine 提供 Web approval UI；仍不包含
WebSocket、认证、远程多用户服务或 Electron/Tauri；既有
SSE/streaming 与 PTY 能力保持在现有 Runtime/ProcessManager seam 内。

## Context V2

Context 组装、历史选择/裁剪/压缩、项目指令、运行时环境和任务状态位于独立模块；旧的
`agent.runtime.context` 与 `agent.runtime.context_history` 入口仅保留兼容导出。每个 Turn 都会在
发送模型前固定 root `AGENTS.md`、有界 Skills catalog、运行时环境、压缩摘要、选中的原始历史、
显式 `$skill` late projection 与确定性 `TaskStateView`。模型调用 `skill(name)` 只通过真实的
assistant/tool ToolResult 进入 chronological history，不在下一请求重复投影全文。Task plan 通过 `update_plan` 原子替换，命令
执行产生受限的 mutation/validation/failure/artifact evidence；模型可通过本地 `skill(name)` 工具
按需加载本地 `SKILL.md`。显式 `$skill-name` 会在首个模型请求前加载且不伪造 tool call；模型实际
调用产生的 bounded ToolResult 仍按 canonical history 规则保留。

Web 工作区的对话设置支持 provider/model、temperature、output limit、approval mode 与 capability
驱动的 thinking budget；候选 provider/model 会先由 Host 有界预览并在保存时规范化。对话标题旁的
Skills 面板消费实时事件状态，只显示当前 Turn 已加载和可用 Skill 的元数据。
预算、ToolResult hard bound、atomic history、rolling compaction 与 checkpoint 约束详见
[`docs/context-architecture.md`](docs/context-architecture.md)。

Context V2 的 working tail 由当前 Provider capability 选择表示方式：DeepSeek、Moonshot/Kimi
和 GLM 默认使用有界的 `structured_user_tail`（`<agent_working_state>`），只有显式验证过的
Provider 才使用 `late_system`。两者都位于 chronological history 之后，working state 不会
写入 canonical Conversation，也不会覆盖最新的真实用户指令。

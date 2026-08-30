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

未指定 root 时使用启动命令所在目录。Workspace picker 只浏览这些 root 内的目录，不跟随
symlink，也不替代 Runtime 原有 workspace/filesystem safety 校验。

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
Linux/WSL 文件系统、shell 和进程环境；例如 Windows 盘需通过 `/mnt/c/...` 显式作为 Host
workspace root。打开项目时，若 WSL 的 Windows interop、`powershell.exe` 和 `wslpath` 可用，
Host 会优先打开原生 Windows 文件夹选择器；选择结果通过系统 `wslpath` 翻译为 WSL 路径，
再由同一个 WorkspaceBrowser 校验 configured roots。Cancel、原生选择器不可用或失败时，
对话框会保留 Host filesystem 浏览器作为 fallback。浏览器不会自行解释或转换 `C:\...` 路径。

原生选择器的 capability 与 selection 只属于 Host transport（分别为
`GET /api/native-picker/capability` 和 `POST /api/native-picker/select`）；Windows 路径不会
进入 Runtime、Thread DTO、Snapshot、事件或日志。生产 Host 通过 argv 启动固定受信的
PowerShell dialog script 和系统 `wslpath`，不使用 shell 拼接用户路径。

Native Windows Host 不在当前 V1 的支持与完成声明中。

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
    ├── WorkspaceBrowser
    ├── NativeWindowsFolderPicker (WSL optional)
    └── production static files
    │
    ▼
ThreadRuntime
    │
    ▼
Agent loop ── Conversation ── local Tools / LLM client
```

依赖方向始终为 `Web → Host → ThreadRuntime → Agent Core`。V1 不包含 approval UI、WebSocket、
token streaming、认证、远程多用户服务、PTY、Electron/Tauri 或持久化 Thread 数据库。

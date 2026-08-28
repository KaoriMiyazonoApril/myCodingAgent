# 08: 完成 Production CLI、静态托管与最终验收

**What to build:** 用户完成一次前端 build 后，可以用 `agent web` 在 Linux/WSL 启动单一进程，
从 Windows 或 Linux 浏览器访问完整应用；开发模式、静态 fallback、关闭清理、文档和全套测试
共同形成可交付的课程演示。

**Blocked by:** 07: 完善 Activity、取消、错误与文件变化.

**Status:** ready-for-agent

- [ ] Python package metadata 注册 `agent web`，CLI 支持默认 3080、repeatable workspace roots 和 `--dev`。
- [ ] Host 始终绑定 `127.0.0.1`，不提供远程 bind option，也不自动打开浏览器。
- [ ] 开发模式不要求 production assets，并仅允许约定 Vite origin；Vite `/api` proxy 可用。
- [ ] Production Host 托管 index、assets 和非 API SPA fallback；所有 `/api` 失败保持 JSON/SSE。
- [ ] 缺少 production index 时 fail fast，并输出准确的 npm install/build 操作。
- [ ] 前端 package、npm lock、lint、typecheck、test 和 build scripts 可从干净依赖安装运行。
- [ ] Server shutdown 停止新 mutation，取消 pending/active tasks，等待 TaskManager，关闭全部 Thread/Provider resources，并执行十秒 grace policy。
- [ ] Shutdown 超时产生清晰日志和非零结果，不把未清理状态报告为 graceful success。
- [ ] README 说明 backend/frontend development、production build、`agent web`、workspace roots、Provider setup、Linux 与 WSL/Windows-browser 语义。
- [ ] 架构文档与实际 Host、Runtime seam、API、SSE、security 和 out-of-scope 保持同步。
- [ ] Host/API/SSE/Provider/Runtime focused suites、完整 pytest、frontend lint、typecheck、Vitest 与 production build 全部通过。
- [ ] 最终 code review 同时检查仓库标准与规格符合性，所有高置信问题在交付前解决。
- [ ] Git 提交只包含本功能相关源码、文档、测试和 lock 文件，不包含 secret、临时配置、build cache 或既有无关修改。
- [ ] Linux Host 与 WSL Host + Windows browser 使用方式有可复现验收记录，native Windows Host 明确不在完成声明中。

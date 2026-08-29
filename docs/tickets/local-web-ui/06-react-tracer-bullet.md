# 06: 完成三栏 Coding Agent React 体验

**What to build:** 将前几票的最小页面整合为清晰的三栏 Coding Agent UI：左侧 workspace 与
Thread，中间 Conversation 与 Composer，右侧 Activity。用户可以从 Provider setup 一路走到
创建 Thread、发送任务并观察工具执行，窄窗口仍保留核心操作。

**Blocked by:** 01: 打通本地 Provider 配置纵向链路; 02: 打通受限 Workspace 选择纵向链路; 03: 打通 Thread 创建与设置纵向链路; 04: 打通非阻塞 Turn 提交纵向链路; 05: 打通 SSE 事件与 Snapshot 恢复纵向链路.

**Status:** completed

- [x] UI 使用 React hooks 与普通 CSS/CSS variables，不引入 Tailwind、shadcn、Redux 或 Zustand。
- [x] 左栏统一呈现当前 workspace、workspace picker、New Thread、Thread list、closed 状态和切换操作。
- [x] 中栏统一呈现 user/assistant/tool/result/error cards，底部 Composer 在 idle、starting、running、closed 状态下行为正确。
- [x] 右栏提供当前 status、submission/Turn metadata、tool lifecycle 和 changed-file 摘要的稳定位置。
- [x] Provider 设置支持 key password field、configured mask、save/replace/clear、discover refresh、manual model 和 default selection。
- [x] Host 断线、配置缺失、无效 workspace、Thread not found、settings conflict、Turn rejection 和工具失败均有非静默 UI。
- [x] 低于 1024px 时左栏成为可切换 sidebar，Activity 成为 drawer/tab，Conversation 与 Composer 保持可用。
- [x] 所有交互支持键盘和可见 focus，status 同时使用文字或图标而非只靠颜色。
- [x] 工具内容采用可展开、受高度限制的等宽区域，长输出不破坏页面布局。
- [x] 前端 domain types 与 transport DTO 明确分层，不导入或复刻 Python class repr。
- [x] ESLint、TypeScript 和 focused component tests 覆盖 setup-to-turn tracer bullet、responsive controls 和错误可见性。
- [x] 现有 Host 与 Runtime tests 保持 green，UI 不直接依赖 AgentLoop、ToolCoordinator、Provider SDK 或具体工具。

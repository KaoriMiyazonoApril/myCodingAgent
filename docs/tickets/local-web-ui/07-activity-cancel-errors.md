# 07: 完善 Activity、取消、错误与文件变化

**What to build:** 用户可以在 Activity 中审计完整 Turn 元数据、工具成功或失败、修改文件和
简化 diff，能可靠取消 active Turn，并能区分 Host、Provider、Runtime、Thread 和工具层错误。

**Blocked by:** 05: 打通 SSE 事件与 Snapshot 恢复纵向链路; 06: 完成三栏 Coding Agent React 体验.

**Status:** ready-for-agent

- [ ] Activity 合并 event 与 latest TurnSummary，显示 iterations、tool calls、usage、timestamps、stop reason 和 terminal status。
- [ ] tool cards 展示 arguments、running、success/error、result content、metadata 和 error code，事件乱序或重复不会破坏状态。
- [ ] changed files 来自现有 file_changed/TurnSummary；available unified diff 可滚动展示，`diff_complete = false` 有明确提示。
- [ ] Active Stop 委派 Runtime 并最终展示 cancel requested 与 cancelled；关闭 SSE 不触发 Stop。
- [ ] close during Turn 使用 Runtime close/cancel 语义，task 清理后 Thread 成为 CLOSED 只读。
- [ ] 稳定 error envelope 覆盖 invalid argument、not found、conflict、Provider auth、Provider unavailable 和 internal error。
- [ ] 前端区分并持久显示 Host disconnected、Turn rejected、Turn failed、tool failed、cancel failed、settings failed 和 invalid workspace。
- [ ] 未知异常不暴露 traceback、credential、base URL、内部文件路径或任意对象 repr。
- [ ] Thread refresh 可以从 Snapshot messages 与 latest Summary 重建 Activity，不依赖完整历史 event 永久保留。
- [ ] Host integration tests 使用真实 ThreadRuntime、scripted LLM 和测试 sandbox 跑通 read/edit/test/diff 或等价真实本地工具链路。
- [ ] Tests 覆盖 active cancellation、close during Turn、错误映射、diff completeness、refresh recovery 和 secret redaction。
- [ ] 不新增 approval、Git diff 扫描、token streaming 或 live command-output transport。


# 05: 打通 SSE 事件与 Snapshot 恢复纵向链路

**What to build:** 用户提交 Turn 后可以实时看到现有 Runtime 事件；浏览器断线后从 event ID
继续，cursor 被淘汰时使用 Snapshot 重建 Conversation 与执行状态。关闭 SSE 不会取消 Agent。

**Blocked by:** 04: 打通非阻塞 Turn 提交纵向链路.

**Status:** ready-for-agent

- [ ] SSE frame 的 event、id 和 data 分别使用 Runtime event type、event_id 和严格 JSON AgentEvent。
- [ ] Adapter 在 active/starting 时按 100ms、idle/closed 时按 1s 读取 EventBuffer，并每 15s 发送 comment heartbeat。
- [ ] query `after_event_id` 优先于 `Last-Event-ID`，两者都使用 Runtime event ID 而非 sequence。
- [ ] 正常读取保持 EventBuffer append order，不创建第二套 Agent event replay log。
- [ ] cursor expiry 产生 snapshot recovery frame，携带 fresh Thread view 和新 cursor，并在空 buffer 时清除旧 SSE ID。
- [ ] SSE disconnect 只清理 stream generator，不调用 cancel、close 或修改 Runtime 状态。
- [ ] React event client 以 Snapshot 为 canonical hydration、按 event_id 幂等，并用稳定 tool-call ID 合并 lifecycle。
- [ ] Conversation 可以实时显示完整 model response 和基本 tool requested/started/finished cards，不要求 token delta 或 live stdout。
- [ ] 客户端自动 reconnect，连接失败期间保留已有 UI；无法恢复时重新读取 hydratable Thread view。
- [ ] reducer 能从 public messages 恢复 user、assistant、tool call/result，并从 latest Summary 恢复 terminal outcome。
- [ ] Adapter tests 覆盖 ordering、ID、query/header、heartbeat、disconnect/reconnect、expiry、empty reset 和 terminal event。
- [ ] Frontend tests 覆盖 Snapshot hydration、duplicate event、tool merge、recovery、reconnect 和无 UI 数据丢失。


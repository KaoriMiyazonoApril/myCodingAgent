# Thread 持久化与重启恢复

## 责任边界

ThreadStore 是 Runtime 唯一的持久化 seam。InMemoryThreadStore 用于测试和
不需要磁盘的嵌入；生产 Host 使用标准库 sqlite3 实现的 LocalThreadStore。
ThreadRuntime 只提交 provider-independent 的 ThreadState，不直接拼接 SQL，也不
保存 Provider、工具或进程运行时对象。

生产数据库位于用户状态目录，而不是选中的 workspace：

- 设置了 XDG_STATE_HOME 时使用
  $XDG_STATE_HOME/my-coding-agent/threads.sqlite3；
- 否则使用 ~/.local/state/my-coding-agent/threads.sqlite3。

create_app(..., state_dir=...)、build_web_app(..., state_dir=...) 可为部署和测试
指定状态目录；也可用 database_path=... 指定 SQLite 文件。目录权限为 owner-only
（0700），数据库文件为 owner-only（0600）。
Thread 只保存 provider configuration ID 和模型名，不保存 API key、endpoint、SDK
client、Future、task、lock、PTY、subprocess 或 pickle 数据。

## Schema 与序列化

SQLite 使用 PRAGMA user_version，当前 STORE_SCHEMA_VERSION 为 1。Thread 的快照
保存在 threads，事件和幂等请求分别保存在 thread_events 与 thread_idempotency；
单次 save_thread 在一个事务中替换三部分，因此恢复时不会观察到半个状态。未知的新
schema 会显式失败，未来迁移集中在 Store 初始化处。

thread_store.py 中的显式 JSON mapper 负责枚举、时间戳、设置版本、消息及所有
canonical block、Turn summary、Agent event 和 idempotency override。Runtime 的
canonical Conversation 包括 system、user、assistant、tool 消息及 reasoning block；
Host 的 public snapshot 仍按既有规则隐藏 system prompt 和 reasoning。

EventBuffer 仍是实时订阅与有限 replay 的唯一运行时机制。语义事件在 emitter/buffer
边界镜像到 Store；模型 token/delta 事件不写 SQLite。事件 sequence 属于 Thread，
恢复后从已保存的最大 sequence 继续分配，event ID 不会与旧记录冲突。

## 重启语义

Runtime 构造时从 Store 枚举并恢复 Thread。可用 workspace 会重建新的工具 registry；
ProcessManager/session、PTY、pending approval future、in-flight model request、stream
assembler 和未完成 assistant/tool arguments 永不恢复。只有已经进入 canonical
Conversation 的完整消息会进入数据库。

如果加载到 running 或 waiting_approval 的 Thread，Runtime 会把对应未完成 Turn
一次性转为 FAILED，stop_reason=runtime_restarted，追加 terminal turn_failed
事件并将 Thread 置为 idle。Interrupted tool call 不会自动重试。带幂等 key 的未完成
请求标记为 interrupted；重试同一 key 会得到 IDEMPOTENCY_INTERRUPTED，不同请求仍
得到 IDEMPOTENCY_CONFLICT。已完成请求可在重启后按原 key 返回保存的 summary，且不
重新调用 Provider 或工具。

workspace 删除或不可访问不影响 Thread 列表、Snapshot、canonical messages、settings、
Turn history 或 events。新 Turn 在 Provider/tool 执行前返回 WORKSPACE_UNAVAILABLE，
不会创建替代目录。Host 用保存的 canonical path 生成可读的历史 workspace 展示记录；
其可用性不成为列出历史的前置条件。

Host 进程 shutdown 会取消并等待当前 task，关闭 ephemeral tool/provider 资源，但不会
把每个仍可恢复的 Thread 自动标成 closed。只有显式 close_thread() 才产生永久的
CLOSED lifecycle 状态。

## 兼容性边界

这是单用户、单进程的本地 SQLite 存储，不提供跨进程协调、分布式锁、云同步或旧 PTY
重连。数据库文件必须由 LocalThreadStore 的版本化初始化与显式 mapper 读取；不承诺
兼容未声明 schema 版本或手工修改的 JSON。新增字段应保持默认值并通过 schema migration
递增 PRAGMA user_version，而不是依赖 pickle 或 Provider SDK 的隐式序列化。

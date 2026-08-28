# 03: 打通 Thread 创建与设置纵向链路

**What to build:** 配置过 Provider 的用户可以从已选 workspace 创建 Thread，创建时冻结所选
Provider 与模型作为初始设置，在侧栏切换和关闭 Thread，并从 Host 获取可恢复的 Runtime
Snapshot。未配置、越界、关闭或 stale settings 操作都得到稳定反馈。

**Blocked by:** 01: 打通本地 Provider 配置纵向链路; 02: 打通受限 Workspace 选择纵向链路.

**Status:** completed

- [x] Runtime Thread creation 接受可选初始 ModelSettings；省略参数的所有现有调用保持原行为与版本 0。
- [x] Host 在第一次有效 Provider/model 选择后惰性组合单例 Runtime，setup 状态不会构造伪 Provider。
- [x] Thread creation 显式传入当前 Provider、model 和安全默认设置，不使用 create-then-patch 绕行。
- [x] 未配置 Provider/model 时创建返回 `CONFIGURATION_REQUIRED`，Provider、workspace 和健康命令仍可用。
- [x] Host 维护只含已创建 Thread ID 的内存 catalog；Snapshot、messages、settings 和 status 始终从 Runtime 读取。
- [x] Thread list、create、hydratable get、versioned settings update 和 idempotent close 使用稳定版本化 DTO。
- [x] Thread view 同时返回 Runtime Snapshot、当前 event cursor 和独立的可空 submission transport 字段。
- [x] CLOSED Thread 保留在列表并成为只读；重复 close 成功，发送和设置修改得到明确 conflict。
- [x] Runtime KeyError、stale settings、workspace safety 和配置错误映射为稳定 HTTP status/code，不解析异常字符串。
- [x] React 侧栏支持创建、切换、刷新恢复和关闭 Thread；顶部显示 workspace、Provider、model 和设置版本。
- [x] Host application tests 覆盖成功链路、初始设置、未配置、not found、stale version、close 与 closed mutation。
- [x] Runtime 回归测试证明可选 initial settings 没有改变现有 Thread/Turn 语义。

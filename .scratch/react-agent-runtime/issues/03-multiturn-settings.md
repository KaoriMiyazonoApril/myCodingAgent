# 03: 支持多轮 Thread 与版本化模型设置

**What to build:** 用户在一个 Turn 完成后可以继续向同一 Thread 提交消息，并能修改后续 Turn 的默认模型、temperature、max tokens 和 thinking 设置，或只覆盖一轮；每个 Turn 使用启动时冻结的配置。

**Blocked by:** 01: 实现最小单 Turn ReAct 闭环.

**Status:** complete

- [x] Thread 完成一个 Turn 后回到可接受下一条消息的状态，并保留合法对话历史。
- [x] `ThreadSettings` 使用版本检查拒绝过期更新。
- [x] `TurnConfig` 在启动时冻结，运行中的设置修改只影响下一 Turn。
- [x] 两个 Turn 之间可以切换 provider 配置与模型，并按新模型能力处理旧 reasoning。
- [x] API key、base URL 和原始 provider 参数不进入公开设置。

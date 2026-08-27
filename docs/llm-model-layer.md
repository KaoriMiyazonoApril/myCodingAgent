# LLM 模型连接层

## 范围

本模块只负责本地 coding agent 与模型 API 的连接和协议转换。它不包含
Agent Loop、文件或 Shell 工具、权限控制、UI、上下文压缩或自动重试。

当前实现涉及以下边界明确的目录：

- `agent/core/messages.py`：Agent 内部统一消息和 content block 表示；
- `agent/tools/types.py`：模型可见的工具定义 `ToolDefinition`；
- `agent/model/types.py`：LLM 调用请求、响应、usage、配置和流事件；
- `agent/model/provider.py`：SDK 无关的 `LLMProvider` 抽象接口；
- `agent/model/openai_compatible.py`：唯一的 Chat Completions 适配器；
- `agent/model/presets.py`：DeepSeek、Kimi/Moonshot、GLM 的便捷端点预设；
- `agent/model/errors.py`：LLM 调用层的稳定异常类型。

调用方向如下：

```text
Agent / UI -> core.Message / ContentBlock + model.LLMRequest
           -> OpenAICompatibleProvider
           -> OpenAI AsyncOpenAI Chat Completions API
           -> DeepSeek / Kimi(Moonshot) / GLM
```

OpenAI Python SDK 只在 `OpenAICompatibleProvider` 内用作 HTTP 客户端。SDK 的
请求和响应对象不会进入 Agent/UI 或对话历史。

## 统一类型

对话历史始终为 `list[agent.core.messages.Message]`。每个 `Message` 有 `system`、`user`、
`assistant` 或 `tool` role，且 `content` 是 block 列表：

- `TextBlock`：普通文本；
- `ReasoningBlock`：兼容供应商可能返回的 reasoning/thinking 文本；
- `ToolCallBlock`：工具调用 ID、名称、已解析的 `dict` 参数，以及可恢复的参数解析错误；
- `ToolResultBlock`：绑定调用 ID 的本地工具结果，保留 content、metadata 与 error code。

`Message`、`Role` 和各 content block 是 Agent 领域类型，而非 Model-specific
类型；它们可被未来 Conversation、Runtime、Tool Dispatcher 或 UI/API adapter 使用。
`agent.core` 不依赖 Model Layer 或 OpenAI SDK。`ToolDefinition` 同样属于工具领域，
由 `agent.tools.types` 定义，当前只描述工具的元数据与 JSON Schema，不包含工具执行。

`LLMRequest` 目前只抽象共同子集：messages、tools、temperature、max_tokens 与
`extra_body`。调用 `chat()` 或 `stream()` 决定传输模式，避免请求数据同时包含两种
模式的开关。`extra_body` 是经过刻意保留的厂商私有参数逃生口，
不会污染核心类型。`Usage` 的三个 token 字段都允许为 `None`。

## 编码与解析

`OpenAICompatibleProvider` 在请求前把本地消息编码为 Chat Completions JSON：

- `TextBlock` 合并成 `content` 字符串；
- assistant 的 `ToolCallBlock` 编码为 `tool_calls[].function.arguments` JSON 字符串；
- 一个 `ToolResultBlock` 编码成一个 role 为 `tool`、带 `tool_call_id` 的消息；
- `ToolDefinition` 编码为 OpenAI 的 `{type: "function", function: {...}}` 格式。

普通对话中的 `ReasoningBlock` 只保留在本地历史中。对于带工具调用的 thinking turn，
适配器依据 `ProviderCapabilities` 将 reasoning 以供应商声明的字段（当前三家均为
`reasoning_content`）发回请求，避免交错思考在工具结果轮次中断。供应商私有的 thinking
启用参数仍由调用方通过 `extra_body` 指定。

解析响应时，文本变成 `TextBlock`，`reasoning_content` 或字符串形式的
`thinking` 变成 `ReasoningBlock`。工具调用参数用 `json.loads` 解析为 `dict`。空参数
变成 `{}`；无效 JSON、非对象 JSON 或非字符串参数不会废弃整条模型响应，而是产生带
`arguments_error` 的 `ToolCallBlock`。Registry 随后将其转成 `INVALID_ARGUMENTS`
工具结果，让模型可在下一轮自行修复。缺失调用 ID 或函数名因无法绑定工具结果，仍抛出
`LLMResponseParseError`。`content=None` 加 `tool_calls` 是有效响应。

执行域 `ToolResult.to_message_block()` 明确完成结果绑定。适配器把 block 编码成 JSON
envelope：`content`、`metadata`、`error_code` 会一起进入 tool message；`is_error` 只由
`error_code` 推导。因此分页、截断、exit code 等控制信息不会只停留在本地对象中。

## 供应商切换

所有供应商均实例化同一个 `OpenAICompatibleProvider`。`create_provider_config`
提供当前官方默认端点：DeepSeek `https://api.deepseek.com`、Kimi/Moonshot
`https://api.moonshot.cn/v1`、GLM `https://open.bigmodel.cn/api/paas/v4`；调用者
仍可用 `base_url` 覆盖。Preset 同时携带显式 `ProviderCapabilities`：DeepSeek、Kimi 和
GLM 的 thinking tool-call turn 都需保留 `reasoning_content`；DeepSeek 还要求 tool-call
assistant message 提供非 null content。API key 不会出现在 `ProviderConfig` 的默认 repr
中，示例也只从环境变量读取 key。

能力声明依据供应商的 thinking + tool-call 协议文档：
[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、
[Kimi K2.6 Tool Use Compatibility](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)、
[GLM 思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)。

`ProviderConfig` 和预设选择的所有配置错误都统一抛出
`LLMConfigurationError`。`Message` 在创建时校验 role 与 block 的组合：system/user
只能包含文本，assistant 可包含文本、reasoning 和 tool call，tool 只能包含 tool
result；非法组合抛出 core 层的 `MessageValidationError`，不会在编码时被静默忽略。

## Streaming 状态

已定义 `TextDeltaEvent`、`ReasoningDeltaEvent`、`ToolCallDeltaEvent`、
`MessageEndEvent` 和 `ErrorEvent`，并在 `LLMProvider` 中预留 `stream()` 接口。
第一阶段尚未将 SDK stream chunks 转换为这些事件；调用 `stream()` 会得到
`LLMStreamingNotImplementedError`，不会泄露 SDK stream 对象。

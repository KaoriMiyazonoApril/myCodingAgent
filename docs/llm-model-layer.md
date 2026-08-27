# LLM 模型连接层

## 范围

本模块只负责本地 coding agent 与模型 API 的连接和协议转换。它不包含
Agent Loop、文件或 Shell 工具、权限控制、UI、上下文压缩或自动重试。

目录为 `agent/model/`：

- `types.py`：agent 使用的统一数据结构；
- `provider.py`：SDK 无关的 `LLMProvider` 抽象接口；
- `openai_compatible.py`：唯一的 Chat Completions 适配器；
- `presets.py`：DeepSeek、Kimi/Moonshot、GLM 的便捷端点预设；
- `errors.py`：项目自己的稳定异常类型。

调用方向如下：

```text
Agent / UI -> LLMRequest、Message、ContentBlock
           -> OpenAICompatibleProvider
           -> OpenAI AsyncOpenAI Chat Completions API
           -> DeepSeek / Kimi(Moonshot) / GLM
```

OpenAI Python SDK 只在 `OpenAICompatibleProvider` 内用作 HTTP 客户端。SDK 的
请求和响应对象不会进入 Agent/UI 或对话历史。

## 统一类型

对话历史始终为 `list[Message]`。每个 `Message` 有 `system`、`user`、
`assistant` 或 `tool` role，且 `content` 是 block 列表：

- `TextBlock`：普通文本；
- `ReasoningBlock`：兼容供应商可能返回的 reasoning/thinking 文本；
- `ToolCallBlock`：工具调用 ID、名称和已解析的 `dict` 参数；
- `ToolResultBlock`：未来本地工具执行的结果。

`LLMRequest` 目前只抽象共同子集：messages、tools、stream、temperature、
max_tokens 与 `extra_body`。`extra_body` 是经过刻意保留的厂商私有参数逃生口，
不会污染核心类型。`Usage` 的三个 token 字段都允许为 `None`。

## 编码与解析

`OpenAICompatibleProvider` 在请求前把本地消息编码为 Chat Completions JSON：

- `TextBlock` 合并成 `content` 字符串；
- assistant 的 `ToolCallBlock` 编码为 `tool_calls[].function.arguments` JSON 字符串；
- 一个 `ToolResultBlock` 编码成一个 role 为 `tool`、带 `tool_call_id` 的消息；
- `ToolDefinition` 编码为 OpenAI 的 `{type: "function", function: {...}}` 格式。

`ReasoningBlock` 保留在本地历史中，但不发回请求。reasoning/thinking 不是三家
共同的 Chat Completions 请求字段；供应商私有开关应使用 `extra_body`。

解析响应时，文本变成 `TextBlock`，`reasoning_content` 或字符串形式的
`thinking` 变成 `ReasoningBlock`。工具调用参数用 `json.loads` 解析为 `dict`：
空参数变成 `{}`，无效 JSON、非对象 JSON、缺失调用 ID 或函数名都会抛出清晰的
项目异常 `LLMToolArgumentsParseError`/`LLMResponseParseError`，而不是 SDK 异常。
`content=None` 加 `tool_calls` 是有效的工具调用响应。

## 供应商切换

所有供应商均实例化同一个 `OpenAICompatibleProvider`。`create_provider_config`
提供当前官方默认端点：DeepSeek `https://api.deepseek.com`、Kimi/Moonshot
`https://api.moonshot.cn/v1`、GLM `https://open.bigmodel.cn/api/paas/v4`；调用者
仍可用 `base_url` 覆盖。API key 不会出现在 `ProviderConfig` 的默认 repr 中，示例
也只从环境变量读取 key。

## Streaming 状态

已定义 `TextDeltaEvent`、`ReasoningDeltaEvent`、`ToolCallDeltaEvent`、
`MessageEndEvent` 和 `ErrorEvent`，并在 `LLMProvider` 中预留 `stream()` 接口。
第一阶段尚未将 SDK stream chunks 转换为这些事件；调用 `stream()` 会得到
`LLMStreamingNotImplementedError`，不会泄露 SDK stream 对象。

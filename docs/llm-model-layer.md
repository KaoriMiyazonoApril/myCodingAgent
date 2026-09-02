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
- `ToolCallBlock`：工具调用 ID、名称、原始参数字符串、可选的已解析 `dict` 参数，以及
  可恢复的参数解析错误；
- `ToolResultBlock`：绑定调用 ID 的本地工具结果，保留 content、metadata 与 error code。

`Message`、`Role` 和各 content block 是 Agent 领域类型，而非 Model-specific
类型；它们可被未来 Conversation、Runtime、Tool Dispatcher 或 UI/API adapter 使用。
`agent.core` 不依赖 Model Layer 或 OpenAI SDK。`ToolDefinition` 同样属于工具领域，
由 `agent.tools.types` 定义，当前只描述工具的元数据与 JSON Schema，不包含工具执行。

`LLMRequest` 目前只抽象共同子集：messages、tools、temperature、max_tokens、统一的
`ThinkingRequest` 与 `extra_body`。调用 `chat()` 或 `stream()` 决定传输模式，避免请求数据同时包含两种
模式的开关。Provider adapter 负责把统一 Thinking 意图映射成真实厂商字段；`extra_body` 是经过刻意保留的厂商私有参数逃生口，
不会污染核心类型。`Usage` 的三个 token 字段都允许为 `None`。

## 编码与解析

`OpenAICompatibleProvider` 在请求前把本地消息编码为 Chat Completions JSON：

- `TextBlock` 合并成 `content` 字符串；
- assistant 的 `ToolCallBlock` 优先把 `raw_arguments` 原样编码为
  `tool_calls[].function.arguments`；仅程序内部构造且没有原始值的调用才序列化
  `arguments`；
- 一个 `ToolResultBlock` 编码成一个 role 为 `tool`、带 `tool_call_id` 的消息；
- `ToolDefinition` 编码为 OpenAI 的 `{type: "function", function: {...}}` 格式。

reasoning 历史由 `ProviderCapabilities.reasoning_retention` 的三态策略控制：`NEVER`
从不回放，`TOOL_CHAIN_ONLY` 只回放含 tool call 的 assistant turn，`ALWAYS` 回放所有
含 reasoning 的 assistant turn。适配器使用 `reasoning_input_field` 指定的字段发送保留
内容，并依次从 `reasoning_output_fields` 声明的响应字段中解析 reasoning；因此网关若使用
`reasoning_details` 等非默认字段，只需配置 capability，无需修改适配器。供应商私有的
thinking 启用参数在该底层边界仍通过 `extra_body` 指定。Agent Runtime 不向前端公开这个
任意参数逃生口，而是从 allowlisted `ThinkingSettings` 生成受限的 `thinking` 对象。
`ProviderCapabilities.thinking` 使用 `ThinkingCapabilities` 声明所选模型是否支持开关、
默认状态、`budget_tokens`、强度选项以及允许的 `keep` 值；`ModelInvoker` 在第一次请求前逐项校验，未知或
不支持的组合 fail closed 为 `UNSUPPORTED_MODEL_SETTING`。`ThinkingSettings` 到
`ThinkingRequest` 是 1:1 映射：`ModelInvoker` 不合成默认 thinking 请求，也不为任何
Provider 发明 thinking 预算——模型"默认开启思考"由其文档化默认行为体现（不发送参数即
保持开启），三种已收录 Provider 的官方 API 均不存在 `budget_tokens`。Capability 还可通过
`context_window_tokens` 声明所选模型的上下文容量；Runtime 用它在每次请求前执行保守
容量检查，但该字段不改变底层 API payload。

`ProviderCapabilities.working_tail_mode` 控制 Context V2 的 detached working tail，取值为
`late_system` 或 `structured_user_tail`。后者是保守默认，当前 DeepSeek、Moonshot/Kimi 和
GLM presets 均显式采用它；`late_system` 只对已验证 Provider opt-in。两种模式都由最终请求
renderer 在 chronological history 之后追加，fallback user message 使用
`<agent_working_state>` delimiter，并不会写入 canonical Conversation。

解析响应时，文本变成 `TextBlock`，能力声明匹配到的字符串 reasoning 字段变成
`ReasoningBlock`。工具调用参数用 `json.loads` 解析为 `dict`。空参数
变成 `{}`；无效 JSON、非对象 JSON 或非字符串参数不会废弃整条模型响应，而是产生
`arguments=None`、带 `arguments_error` 的 `ToolCallBlock`。无论解析成功与否，原始
`function.arguments` 都保存在 `raw_arguments`，下一轮历史回放不会把非法参数改写成
`{}`。Registry 只执行已解析的 `arguments`，解析失败会变成 `INVALID_ARGUMENTS`
工具结果，让模型可在下一轮自行修复。缺失调用 ID 或函数名因无法绑定工具结果，仍抛出
`LLMResponseParseError`。`content=None` 加 `tool_calls` 是有效响应。

执行域 `ToolResult.to_message_block()` 明确完成结果绑定。适配器把 block 编码成 JSON
envelope：`ok`、`content`、`metadata`、`error_code` 会一起进入 tool message；`ok` 与
`is_error` 都只由 `error_code` 推导，避免并列状态互相矛盾。因此分页、截断、exit code
等控制信息不会只停留在本地对象中。

## 供应商切换

所有供应商均实例化同一个 `OpenAICompatibleProvider`。`create_provider_config`
提供当前官方默认端点：DeepSeek `https://api.deepseek.com`、Kimi/Moonshot
`https://api.moonshot.ai/v1`、GLM `https://open.bigmodel.cn/api/paas/v4`；调用者
仍可用 `base_url` 覆盖。每个 `ProviderProfile` 提供默认 capabilities，并可通过精确模型
名映射到 `ModelProfile` 的逐字段覆盖；未覆盖字段继承 provider 默认值。对于可空字段，
内部 `UNSET` 表示继承，而显式 `None` 表示禁用，因此 model profile 可以清除 provider
默认的 reasoning 输入字段。Thinking 能力与 context window 同样可由精确模型 profile
覆盖，Runtime 据此校验当前选择，而不是按 provider 名称猜测。只有已由当前官方资料确认的
精确模型才声明容量；没有显式容量声明时，Runtime 使用自身的保守默认值。调用方也可在
创建配置时显式传入完整 capabilities。

当前 DeepSeek 预设面向 `deepseek-v4-flash` 与 `deepseek-v4-pro` 等当前模型，不再保留已
退役的 `deepseek-chat` / `deepseek-reasoner` 特例；其 reasoning 使用
`TOOL_CHAIN_ONLY`，且 tool-call assistant message 需要非 null content。Kimi/Moonshot
预设使用 `ALWAYS`，与 preserved thinking 默认保留完整历史的行为对齐，并声明其
`thinking.keep` allowlist；DeepSeek 与 GLM 声明支持 thinking 开关；DeepSeek V4
精确 profile 区分两个字段：`model_max_output_tokens=384000`（官方 pricing 页硬上限，
仅用于 clamp）与 `default_request_max_tokens=131072`（Harness 内部请求策略——官方在
未显式给出 `max_tokens` 时使用较低默认上限，思考型长任务会在正文写出前耗尽预算，故策略性
提高单次请求上限避免截断）。两者均不声称支持协议未列出的 budget/keep 字段；精确模型
profile 只增加已确认的 Thinking 默认值、开关、强度和输出上限，不会把未知能力继承到未知
模型。Runtime 调用方只使用安全的
`ThinkingSettings`。GLM 预设继续使用 `TOOL_CHAIN_ONLY`。API key 不会出现在
`ProviderConfig` 的默认 repr 中，
示例也只从环境变量读取 key。

### Issue #6 capability/profile authority

模型显示信息和 Runtime capability 由同一组 `ProviderProfile.model_profiles` 提供，Host
只把 Provider 返回的 ID 与这些精确 profile 合并，不在 Web 或 Runtime 复制一份模型注册表。
截至 2026-09-02，内置的已确认 profile 如下：

| Provider | 精确模型 | Context Window | 官方最大输出（capability） | 内部默认请求上限（policy） | Thinking |
| --- | --- | ---: | ---: | ---: | --- |
| DeepSeek | `deepseek-v4-flash`, `deepseek-v4-pro` | 1M | 384K | 131072 | 默认开启，可切换 `low/high/max`，默认 `high` |
| Moonshot/Kimi | `kimi-k3` | 1M | 未收录（不发送 `max_tokens`） | 未收录 | 始终开启，`low/high/max`，默认 `max` |
| Moonshot/Kimi | `kimi-k2.7-code`, `kimi-k2.7-code-highspeed` | 256K | 未收录 | 未收录 | 始终开启，不声明强度选项 |
| Moonshot/Kimi | `kimi-k2.6` | 256K | 未收录 | 未收录 | 默认开启，可关闭，不声明强度选项 |
| GLM | `glm-5.3` | 1M | 未收录（不发送 `max_tokens`） | 未收录 | 始终开启，`low/high/max`，默认 `max` |
| GLM | `glm-5.2` | 1M | 未收录 | 未收录 | 默认开启，可关闭，`none/minimal/low/medium/high/xhigh/max`，默认 `max` |

未命中精确 profile 的 Provider-reported ID 仍可手动使用，但 capability 和 Context Window
均按未知处理；该保守结果不会猜测 Thinking、上下文容量、输出上限或工具兼容性。`ModelInvoker`
只把已验证的 `ThinkingSettings` 转成统一 `ThinkingRequest`，Provider adapter 再映射到
供应商协议：DeepSeek V4 与 GLM 使用
`thinking.type` 并在有强度时发送顶层 `reasoning_effort`，Kimi K3 使用顶层
`reasoning_effort`，可关闭的 Kimi 使用 `thinking.type`，始终开启的 Kimi 不发送关闭参数。
这些映射由 `ThinkingParameterStyle` allowlist 实现，不能由前端提交任意 `extra_body`。
DeepSeek 的 effort 在 adapter 边界按官方档位归一化（`medium`/`xhigh` → `high`）；
generic 兜底分支仍保留 `budget_tokens`/`keep`/`intensity` 嵌套逃生口，供声明支持扩展思考
的端点使用，但内置三个 Provider 永远不会触发该路径。

### 输出上限与请求策略分离

`ModelProfile.max_output_tokens` 已废除，不再存在"capability 与 policy 共用一个字段"
的情况：

- `model_max_output_tokens`：模型官方硬上限（Model Capability）。只用于 clamp
  `resolved_output_limit`，永远不会成为未配置时的请求默认值；未核验则为 `None`。
- `default_request_max_tokens`：Harness 内部请求默认上限（Request Policy）。线程没有
  显式覆盖时才使用；`None` 表示不发送 `max_tokens`、交由 Provider 默认。
- 未知模型对两个字段都不做猜测。

每次 Turn 只调用一次统一 resolver
`resolve_output_limit(explicit_max_tokens, capabilities)`（位于
`agent/runtime/model_invoker.py`）：显式线程覆盖优先，其次 harness 默认，最后为
`None`（省略请求字段）；结果按 `model_max_output_tokens` clamp。同一个
`resolved_output_limit` 同时供给 `ContextBudget`（输出 reserve）与
`ModelInvoker`（`LLMRequest.max_tokens`），因此 ContextBudget 与 Provider 实际发送的
值永远一致；`OpenAICompatibleProvider` 不再在请求发送前静默回填任何输出上限。
ContextBudget 的 reserve 规则：有 resolved 值时
`max(1, min(resolved, context_window // 4))`；没有时沿用保守默认
`max(1, min(4096, context_window // 4))`。输出能力与策略都不会经 capability 投影暴露
给 Host/UI。

能力声明依据供应商的 thinking + tool-call 协议文档：
[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、
[DeepSeek V4 Release Notes](https://api-docs.deepseek.com/news/news260424/)、
[Kimi Model List](https://platform.kimi.ai/docs/models)、
[Kimi Thinking Models](https://platform.kimi.ai/docs/guide/use-thinking-models)、
[GLM 深度思考](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)。

`ProviderConfig` 和预设选择的所有配置错误都统一抛出
`LLMConfigurationError`。`Message` 在创建时校验 role 与 block 的组合：system/user
只能包含文本，assistant 可包含文本、reasoning 和 tool call，tool 只能包含 tool
result；非法组合抛出 core 层的 `MessageValidationError`，不会在编码时被静默忽略。

所有 `LLMError` 都公开 `status_code`、`retryable` 与 `provider`。HTTP 400/401 默认不可
重试，429、5xx、timeout 和 connection error 可重试；适配器仍保留 authentication、
rate-limit、connection 与 request 的稳定异常子类，使未来 Agent Loop 无需解析错误文本。

## Streaming 状态

`OpenAICompatibleProvider.stream()` 将 SDK async stream 转换为
`TextDeltaEvent`、`ReasoningDeltaEvent`、`ToolCallDeltaEvent`、`MessageEndEvent` 和
`ErrorEvent`，不会把 SDK chunk 暴露到 Runtime。`MessageAssembler` 按 tool-call index
累积碎片，并只在正常 `MessageEndEvent` 后解析 JSON、构造 canonical assistant message；
模型流在首个 delta 后失败不会透明重试。尚未实现 streaming 的旧 Provider 继续使用
`chat()` 完整响应兼容入口，不合成额外的 UI delta。

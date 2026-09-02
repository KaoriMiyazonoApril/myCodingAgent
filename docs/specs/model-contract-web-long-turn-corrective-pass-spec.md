# Model Contract Consistency + Web Long-Turn UX Corrective Pass

## 状态与基线

- 基线：`main` / `origin/main` 均为 `e4433f1`，开始时工作树干净。
- `.venv/bin/python -c "import agent.runtime.thread_runtime"` 通过。
- `.venv/bin/python -m pytest -q`：531 passed。
- Web：lint、typecheck、46 个 Vitest、production build 均通过。
- 直接执行 `.venv/bin/pytest -q` 因仓库根目录未进入该脚本的 `sys.path`，collection 中 7 个测试模块无法导入 `tests.sandbox_support`；完成态必须让约定测试入口可 collection。

## 目标与非目标

本轮仅完成 Model Contract migration 与长任务 Web presentation/state 修正。不得重构 Agent Loop、Context V2、history/compaction、TaskState、tool-result compression、Skills、persistence 或 approval Runtime state machine。`finish_reason == "length"` 必须继续产生非成功终止；canonical Conversation 必须继续完整保存 assistant text、reasoning 与 tool calls。

## Model Contract

统一链路为：

```text
ThinkingSettings(enabled, intensity; internal-only budget_tokens/keep when supported)
  -> ThinkingRequest
  -> exact-model ProviderCapabilities + ThinkingParameterStyle
  -> provider wire payload
```

- 核心类型必须真实定义并可导入：`ThinkingRequest`、`ThinkingCapabilities`、`ThinkingParameterStyle`（若适配器仍需）、`ThinkingSettings.intensity`、`ProviderCapabilities.model_max_output_tokens`、`ProviderCapabilities.default_request_max_tokens`、`LLMRequest.thinking`。
- Runtime 不按 provider name 注入参数；未知 model 不继承/猜测 optional thinking、intensity、context 或 output 能力。
- 普通 Web 不暴露 `budget_tokens` / `keep`。
- DeepSeek V4 Flash/Pro：exact model；thinking 默认开启、可切换；intensity `low/high/max`，兼容输入映射遵循官方；1M context；384K 为 hard maximum；Harness 默认请求 131072；不支持也不发送 `thinking.budget_tokens`。
- Kimi：exact model。K3 始终 thinking，仅 top-level `reasoning_effort=low/high/max`；1M context；`max_completion_tokens` 官方默认 131072、最大 1048576。K2.7 Code/Highspeed 始终 thinking、Preserved Thinking 固定 all，不支持 intensity；K2.6 可切换 thinking，可选 keep all；两者 256K context。不得发送不受对应模型支持的字段。
- GLM：只对官方已确认的 exact model 声明能力。GLM-5.2 为 1M context、128K hard maximum，thinking toggle 与 `reasoning_effort` 按官方映射；未确认的 `glm-5.3` 按 unknown model 处理，不猜测。
- serialization tests 只证明本地 wire contract，报告必须写 `NOT LIVE VERIFIED`（无真实 credential）。

参考：

- DeepSeek Thinking / Chat / pricing：<https://api-docs.deepseek.com/guides/thinking_mode/>、<https://api-docs.deepseek.com/api/create-chat-completion/>、<https://api-docs.deepseek.com/quick_start/pricing/>
- Kimi model/parameter/chat：<https://platform.kimi.ai/docs/models>、<https://platform.kimi.ai/docs/api/models-overview>、<https://platform.kimi.ai/docs/api/chat>
- GLM-5.2 / parameters：<https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2>、<https://docs.bigmodel.cn/cn/guide/start/concept-param>

## Output Policy B

唯一 resolver：

```text
candidate = explicit thread override if present else default_request_max_tokens
resolved = None                         if candidate is None
resolved = min(candidate, model max)    if model max is known
resolved = candidate                    otherwise
```

`model_max_output_tokens` 只作 hard clamp，绝不成为隐式请求默认。唯一 `resolved_output_limit` 同时传给 ContextBudget 和 Provider request；adapter 不得补 Runtime 不知道的 output limit。测试覆盖 explicit、default、clamp、all None、unknown model 与两端同源。

## Public/UI Message 与 Reasoning

- `assistant` message 含任一 `tool_call` 时，其中 `text` 是 tool-step narration，不进入 ordinary conversation messages；允许作为最多两行、截断、非持久的运行反馈。
- `assistant` message 不含 tool call 时才是 final/user-facing message，进入主对话。
- 分类只看结构，不看文本关键词；实时 `model_response` 与 snapshot hydration 使用同一规则。
- canonical history 不删除任何 TextBlock/ReasoningBlock/ToolCallBlock。
- reasoning visibility 只允许 `hidden | debug`。normal Web reducer 即使收到 stale `model_reasoning_delta` 或 preview 也不得保存/渲染 raw reasoning；debug 后端可输出受控诊断事件。
- `model_activity` 为 transient、phase-change-only：首个 reasoning delta -> `thinking`；首个 text/tool delta -> `generating`；message end -> `idle`。payload 不含 reasoning text，不逐 token，不使用后端 timer。
- Web 在 `thinking` 时用 `performance.now()` 本地计时；phase 变更后冻结 `Thinking · Xs`。

## Current Turn Activity 与 Compact UI

- `messages` 是 durable Thread history；`tools` 是 current/latest Turn activity，两者生命周期分离。
- `turn_started` 必须清空旧 tools；snapshot hydration 只能恢复 latest/current Turn 的 tool slice，不得把全 Thread 工具历史平铺到运行区。
- 成功项默认 bounded：显示最近 N 项并把更早成功项折叠为摘要；running、waiting approval、error/rejected 始终可见。
- 参数/result/metadata 置于 `<details>`：成功默认折叠，失败默认展开；大内容 `max-height` + internal scroll。

## React Identity、Snapshot 与 Scroll

- `ActiveThreadView` key 只由 `thread_id` 决定，禁止 `updated_at`、`accepted_at` 等 metadata。
- 正常 SSE/approval 路径用 reducer state + authoritative snapshot metadata 的最小 reconciliation，不从零 hydrate event-derived UI。
- 仅 reconnect/cursor-expired recovery 与无 EventSource fallback 可完整 snapshot hydrate。
- scroll container：用户在底部附近时新内容跟随；用户向上滚时不强拉；approval resolve 与 snapshot reconcile 保持视觉位置；Thread 切换可采用底部默认。禁止 window scroll、每 render scroll 与 setTimeout workaround。

## Web 设置与 Provider 状态

- 普通设置只显示 Provider、searchable Model + Custom ID、Model Information、只读 Context Window、Thinking toggle、受支持时 Intensity、Approval Mode。
- 不显示 temperature、max output、thinking budget/keep、可编辑 context、top-p/top-k、penalty、seed；Web 不写 provider-specific if/else。
- credential save 快速返回“已配置”，异步 discovery。状态明确为未配置、已配置、已验证（真实 discovery 成功）、验证失败；credential existence 不得显示“已连接”。

## Regression / Browser Acceptance

- Python 覆盖 contract、resolver、Context/Provider 同源、三家 exact-model serialization、unknown fail-closed、hidden/debug、phase event 与 canonical continuity。
- Vitest/RTL 覆盖 intermediate/final classification、turn-scoped tools、compact rendering、normal reasoning defense、local thinking timer、approval 不 remount/不跳顶、真实 snapshot recovery（messages/approval/terminal/skills）。
- 真实浏览器验收由用户本人执行；代理不得运行或声称已经通过 Playwright。交付一份可重复的人工验收清单，覆盖 15–30 tools、巨大 details、approval 下部位置、thinking timer/no raw reasoning、多次 text+tool narration 与最终 text-only response。

## 完成条件

```bash
.venv/bin/python -c "import agent.runtime.thread_runtime"
.venv/bin/python -m pytest -q
cd web
npm run lint
npm run typecheck
npm test -- --run
npm run build
```

另需 `git diff --check` 通过、独立 `luna max` 审核明确无问题、主代理最终复核；之后用中文 commit message 提交并 push。真实浏览器结果明确标记为“待用户验收”；不得把 serialization unit test 声称为 live API success。

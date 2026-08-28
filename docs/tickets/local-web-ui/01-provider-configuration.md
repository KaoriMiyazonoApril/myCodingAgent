# 01: 打通本地 Provider 配置纵向链路

**What to build:** 用户可以启动未配置 Runtime 的本地 Host，在最小 React 设置界面中为
DeepSeek、Moonshot/Kimi 或 GLM 保存、替换和清除 API key，自动获取 Provider 返回的模型，
手工填写回退模型，并选择新 Thread 使用的默认 Provider 与模型。凭据只由 Host 持久化，
浏览器只能看到脱敏状态。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] Host application factory 可以在没有 Provider 和 Runtime 的 setup 状态启动，健康信息明确报告 `configuration_required`。
- [ ] React/Vite 开发入口可以连接 Host 并展示三个固定 Provider preset，不引入全局状态框架。
- [ ] 用户可以保存一个 Provider 的 key 与 selected model；配置使用版本化文档、原子替换和 owner-only 文件权限。
- [ ] Provider 查询只返回 configured 状态和掩码，不返回 API key、base URL、SDK 对象或内部 repr。
- [ ] 用户可以替换或清除 key，清除后对应 Provider 不再可用于新 Thread。
- [ ] 保存 key 后前端自动触发模型发现；结果经过非空过滤、去重、排序和五分钟进程内缓存。
- [ ] 模型发现只访问 preset 固定 endpoint，不接受浏览器提供的 URL，并区分认证失败、上游不可用、非法响应和空列表。
- [ ] 发现失败时用户仍可输入非空模型 ID；UI 文案明确列表只是 Provider 返回的模型而非 tool-calling 验证。
- [ ] 用户可以持久化 default Provider 和各 Provider 的 selected model，刷新页面后恢复选择。
- [ ] plaintext key 在成功保存后从前端表单状态清除，错误、日志和响应均不回显 secret。
- [ ] Host 与前端 focused tests 覆盖配置落盘、权限、掩码、清除、默认值、发现成功及所有失败分支，不调用真实 Provider。
- [ ] 现有模型与 Runtime 测试继续通过，设计文档在实现偏离规格时同步更新。


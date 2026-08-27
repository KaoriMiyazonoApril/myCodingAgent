# AGENTS.md

## Project Overview

This repository is a university coursework project for independently designing and implementing a coding agent.

The goal is to build a simplified coding agent similar in concept to Claude Code, Codex, OpenCode, or DeepSeek Harness.

The agent should interact with large language models and autonomously perform programming tasks by:

- reading files,
- writing or modifying files,
- executing local commands,
- inspecting command results,
- iterating with the language model,
- and completing user-provided coding tasks.

The important agent mechanisms must be implemented directly in this repository.

---

# 1. Core Coursework Constraints

## 1.1 No existing agent products

Do NOT build this project by wrapping, embedding, or adding a UI around an existing agent product.

Forbidden examples include, but are not limited to:

- Claude Code
- Codex CLI
- OpenCode
- DeepSeek Harness
- other complete coding-agent products

They may be studied for architectural inspiration, but their agent runtime must not be reused as the implementation of this project.

---

## 1.2 No agent frameworks or agent SDKs

Do NOT use any agent framework, agent orchestration framework, or high-level agent SDK.

Forbidden examples include, but are not limited to:

- LangChain
- LlamaIndex
- OpenAI Agents SDK
- Claude Agent SDK
- AutoGen
- CrewAI

Do not introduce another dependency that provides equivalent high-level agent functionality under a different name.

In particular, a dependency must not replace the project's own implementation of:

- agent loops,
- conversation orchestration,
- tool dispatch,
- context management,
- autonomous task execution,
- tool execution lifecycle,
- termination logic.

If a proposed dependency may implement one of these coursework-required mechanisms, do not add it automatically. Prefer implementing the mechanism directly in this repository.

---

# 2. Allowed LLM Integration

Model-vendor API client libraries are allowed.

Examples of acceptable low-level API clients include:

- OpenAI Python SDK
- official model-vendor API clients
- ordinary HTTP clients such as httpx or requests

Using the OpenAI Python SDK as a client for an OpenAI-compatible API is allowed.

The SDK must only serve as a low-level API communication layer.

It must NOT provide the agent runtime.

---

# 3. No Provider-Hosted Code or File Execution

Do NOT rely on API-provider-hosted code execution, filesystem access, or managed agent tools.

Forbidden examples include:

- OpenAI Code Interpreter
- OpenAI hosted computer/code execution
- OpenAI Files API as the agent's filesystem mechanism
- provider-hosted shell execution
- provider-hosted file reading or writing
- provider-managed coding environments

All coding-agent tools must operate locally through code implemented in this repository.

Examples of local tools that may be implemented later include:

- `read_file`
- `write_file`
- `edit_file`
- `list_files`
- `search_files`
- `run_command`

Their definitions, validation, execution, result collection, and error handling must be implemented locally.

---

# 4. Git Workflow

After completing a coherent implementation task:

1. inspect the changes,
2. run the relevant tests or validation commands,
3. ensure no secrets, temporary files, or unrelated generated files are included,
4. create a Git commit,
5. push the commit to the configured remote repository.

Do not leave completed and validated work only as uncommitted local changes.

## 4.1 Commit Message Requirements

Every Git commit message must be written in Chinese.

The commit message must clearly and accurately describe what the commit actually implements, changes, fixes, or refactors.

A reader should be able to understand the main purpose of the commit from the commit message alone without having to inspect the diff.

Prefer concise but specific commit messages.

Good examples:

- `实现 OpenAI Compatible 模型连接层`
- `新增 Agent 内部消息与 ContentBlock 数据结构`
- `实现 DeepSeek、Kimi、GLM Provider 配置切换`
- `实现工具调用参数解析与校验`
- `实现本地文件读取与写入工具`
- `实现 Agent 主循环与最大迭代终止条件`
- `修复工具调用 arguments 非法 JSON 时的异常处理`
- `补充 LLM Provider 单元测试`
- `更新模型连接层设计文档`

Avoid vague commit messages such as:

- `更新代码`
- `修改`
- `修复问题`
- `优化`
- `update`
- `fix`
- `changes`

If a task contains several closely related changes that together implement one coherent feature, summarize the feature in the commit message rather than listing every modified file.

## 4.2 Push Requirements

After committing completed and validated work, push the commit to the configured remote repository.

If pushing fails because the remote is unavailable, authentication is missing, or no remote is configured:

- keep the local commit,
- clearly report the reason,
- do not silently treat the push as successful.

Do not modify Git remotes, credentials, or repository history merely to make a push succeed.

## 4.3 Git Safety

Do not use destructive Git operations such as:

- `git reset --hard`
- `git clean -fd`
- `git push --force`

unless explicitly requested by the user.

---

# 5. Design Documentation

Project architecture, module design, important data structures, protocols, and major implementation decisions must be documented under the `docs/` directory.

`AGENTS.md` should contain project-wide development constraints and workflow rules, not detailed architecture documentation.

When implementing a new subsystem or making a change that affects the existing design, update the relevant documentation in `docs/` as part of the same task.

Documentation must stay synchronized with the implementation.

Examples of changes that normally require documentation updates include:

- adding or removing a major module,
- changing module responsibilities,
- changing important interfaces or internal data structures,
- changing the LLM provider abstraction,
- changing conversation or context management,
- changing tool definitions or the tool execution model,
- changing the agent loop or termination behavior,
- introducing a significant architectural decision.

Do not postpone documentation updates until the end of the project when the implementation has already changed.

Before modifying an existing subsystem, read its relevant documentation in `docs/` when available.

After modifying the subsystem, verify whether the documentation is still accurate and update it when necessary.

Documentation updates should be included in the same Git commit as the corresponding implementation whenever practical.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";

function appThread(
  threadId: string,
  thinking: { enabled: boolean; budget_tokens: number | null; keep: null } | null = null,
) {
  return {
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: threadId,
      workspace: "/workspace",
      status: "idle" as const,
      active_turn_id: null,
      completed_turns: 0,
      settings: {
        provider_config_id: "deepseek",
        model: "deepseek-chat",
        temperature: null,
        max_tokens: null,
        thinking,
        limits: {
          max_iterations: 20,
          max_tool_calls: 50,
          max_execution_seconds: 900,
        },
        approval_mode: "on_request" as const,
        version: 0,
      },
      messages: [],
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:00Z",
      latest_turn: null,
      skills: {
        schema_version: 1,
        available: [
          {
            name: "repo-guide",
            description: "Repository workflow guidance",
            source: "workspace .agents",
            source_path: "/workspace/.agents/skills/repo-guide/SKILL.md",
            directory: "/workspace/.agents/skills/repo-guide",
          },
        ],
        loaded: [],
        diagnostics: [],
      },
    },
    event_cursor: null,
    submission: null,
    workspace: {
      workspace_id: "workspace-1",
      path: "/workspace",
      canonical_path: "/workspace",
      display_name: "workspace",
    },
    capabilities: {
      thinking_supported: true,
      supports_thinking_budget: true,
      supported_keep_values: ["none", "all"],
    },
  };
}

class AppFakeEventSource {
  static instances: AppFakeEventSource[] = [];
  readonly listeners = new Map<
    string,
    Array<(event: MessageEvent<string>) => void>
  >();
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string) {
    AppFakeEventSource.instances.push(this);
  }

  addEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {}

  emit(type: string, payload: Record<string, unknown>) {
    const message = new MessageEvent<string>(type, {
      data: JSON.stringify(payload),
    });
    this.listeners.get(type)?.forEach((listener) => listener(message));
  }
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () =>
        String(input) === "/api/threads"
          ? { schema_version: 1, threads: [] }
        : String(input) === "/api/workspaces/select"
          ? {
              schema_version: 1,
              workspace: {
                workspace_id: "workspace-1",
                path: "/workspace",
                canonical_path: "/workspace",
                display_name: "workspace",
              },
            }
          : String(input).startsWith("/api/workspaces")
          ? {
              schema_version: 1,
              path: "/workspace",
              parent: null,
              roots: ["/workspace"],
              entries: [],
              truncated: false,
            }
          : {
              schema_version: 1,
              default_provider_id: null,
              providers: [
                {
                  provider_id: "deepseek",
                  display_name: "DeepSeek",
                  configured: false,
                  credential_mask: null,
                  selected_model: null,
                  is_default: false,
                },
                {
                  provider_id: "moonshot",
                  display_name: "Moonshot / Kimi",
                  configured: false,
                  credential_mask: null,
                  selected_model: null,
                  is_default: false,
                },
                {
                  provider_id: "glm",
                  display_name: "GLM",
                  configured: false,
                  credential_mask: null,
                  selected_model: null,
                  is_default: false,
                },
              ],
            },
    })),
  );
});

test("starts with a compact empty state when no model is configured", async () => {
  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "选择一个项目开始" }),
  ).toBeInTheDocument();
  expect(screen.getAllByText("尚未配置模型").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "设置" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  expect(
    await screen.findByRole("heading", { name: "模型与服务商" }),
  ).toBeInTheDocument();
  expect(screen.getByText("DeepSeek")).toBeInTheDocument();
  expect(screen.getByText("Moonshot / Kimi")).toBeInTheDocument();
  expect(screen.getByText("GLM")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "连接 DeepSeek" })).toBeInTheDocument();
});

test("saves a key, discovers models, and selects a default", async () => {
  const requests: Array<{ method: string; path: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ method, path });
      if (path === "/api/threads") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [] }),
        };
      }
      if (path === "/api/workspaces/select" && method === "POST") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            workspace: {
              workspace_id: "workspace-project",
              path: "/home/student/project",
              canonical_path: "/home/student/project",
              display_name: "project",
            },
          }),
        } as Response;
      }
      if (path.startsWith("/api/workspaces")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            path: "/workspace",
            parent: null,
            roots: ["/workspace"],
            entries: [],
            truncated: false,
          }),
        };
      }
      if (path.endsWith("/models/discover")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            provider_id: "deepseek",
            models: ["deepseek-chat", "deepseek-reasoner"],
            cached: false,
          }),
        };
      }
      if (method === "PUT") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            provider: {
              provider_id: "deepseek",
              display_name: "DeepSeek",
              configured: true,
              credential_mask: "••••cret",
              selected_model: null,
              is_default: false,
            },
          }),
        };
      }
      if (method === "PATCH") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            provider: {
              provider_id: "deepseek",
              display_name: "DeepSeek",
              configured: true,
              credential_mask: "••••cret",
              selected_model: "deepseek-reasoner",
              is_default: true,
            },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          schema_version: 1,
          default_provider_id: null,
          providers: [
            {
              provider_id: "deepseek",
              display_name: "DeepSeek",
              configured: false,
              credential_mask: null,
              selected_model: null,
              is_default: false,
            },
          ],
        }),
      };
    }),
  );
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "连接 DeepSeek" }),
  );
  const key = screen.getByLabelText("访问密钥");
  fireEvent.change(key, { target: { value: "sk-api-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并发现模型" }));

  expect(
    await screen.findByRole("option", { name: "deepseek-reasoner" }),
  ).toBeInTheDocument();
  expect(key).toHaveValue("");
  expect(screen.getByText("凭据已保存 · ••••cret")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("服务商模型"), {
    target: { value: "deepseek-reasoner" },
  });
  fireEvent.click(screen.getByRole("button", { name: "设为默认" }));

  expect((await screen.findAllByText("默认")).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "刷新模型" }));
  await waitFor(() =>
    expect(
      requests.filter(({ path }) => path.endsWith("/models/discover")),
    ).toHaveLength(2),
  );
  expect(requests).toEqual(
    expect.arrayContaining([
      { method: "PUT", path: "/api/providers/deepseek" },
      {
        method: "POST",
        path: "/api/providers/deepseek/models/discover",
      },
      { method: "PATCH", path: "/api/provider-default" },
    ]),
  );
});

test("shows provider request failures inline", async () => {
  const fetchMock = vi.mocked(fetch);
  fetchMock.mockImplementation(async (input, init) => {
    if (String(input) === "/api/threads") {
      return {
        ok: true,
        json: async () => ({ schema_version: 1, threads: [] }),
      } as Response;
    }
    if (String(input).startsWith("/api/workspaces")) {
      return {
        ok: true,
        json: async () => ({
          schema_version: 1,
          path: "/workspace",
          parent: null,
          roots: ["/workspace"],
          entries: [],
          truncated: false,
        }),
      } as Response;
    }
    if ((init?.method ?? "GET") === "PUT") {
      return {
        ok: false,
        status: 400,
        json: async () => ({
          error: {
            code: "PROVIDER_AUTHENTICATION_FAILED",
            message: "Provider rejected the configured credential",
            details: {},
          },
        }),
      } as Response;
    }
    return {
      ok: true,
      json: async () => ({
        schema_version: 1,
        default_provider_id: null,
        providers: [
          {
            provider_id: "deepseek",
            display_name: "DeepSeek",
            configured: false,
            credential_mask: null,
            selected_model: null,
            is_default: false,
          },
        ],
      }),
    } as Response;
  });

  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "连接 DeepSeek" }),
  );
  fireEvent.change(screen.getByLabelText("访问密钥"), {
    target: { value: "rejected-key" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存并发现模型" }));

  expect(
    await screen.findByRole("alert"),
  ).toHaveTextContent("Provider rejected the configured credential");
});

test("navigates Host workspaces and selects the current directory", async () => {
  const workspaceRequests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/threads") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [] }),
        } as Response;
      }
      if (path === "/api/workspaces/select" && init?.method === "POST") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            workspace: {
              workspace_id: "workspace-project",
              path: "/home/student/project",
              canonical_path: "/home/student/project",
              display_name: "project",
            },
          }),
        } as Response;
      }
      if (path.startsWith("/api/workspaces")) {
        workspaceRequests.push(path);
        const child = path.includes("%2Fhome%2Fstudent%2Fproject");
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            path: child ? "/home/student/project" : "/home/student",
            parent: child ? "/home/student" : "/home",
            roots: ["/home/student", "/mnt/c/code"],
            entries: child
              ? [{ name: "src", path: "/home/student/project/src", type: "directory" }]
              : [
                  {
                    name: "project",
                    path: "/home/student/project",
                    type: "directory",
                  },
                ],
            truncated: false,
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          schema_version: 1,
          default_provider_id: null,
          providers: [],
        }),
      } as Response;
    }),
  );

  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "选择一个项目开始" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "选择项目" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: /打开项目/ }));
  expect(await screen.findByRole("heading", { name: "打开项目" })).toBeInTheDocument();
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "打开 project" }));
  await waitFor(() => expect(workspaceRequests.length).toBeGreaterThan(1));
  expect(await screen.findByText("/home/student/project")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "使用此项目" }));
  expect(
    await screen.findByRole("button", { name: "当前项目：project" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("/home/student/project")).not.toBeInTheDocument();
  expect(workspaceRequests).toContain(
    "/api/workspaces?path=%2Fhome%2Fstudent%2Fproject",
  );
});

test("keeps workspace errors visible with a reload action", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/threads") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [] }),
        } as Response;
      }
      if (String(input).startsWith("/api/workspaces")) {
        return {
          ok: false,
          status: 403,
          json: async () => ({
            error: {
              code: "PERMISSION_DENIED",
              message: "Workspace path is not accessible",
              details: {},
            },
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          schema_version: 1,
          default_provider_id: null,
          providers: [],
        }),
      } as Response;
    }),
  );

  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "选择项目" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: /打开项目/ }));
  expect(await screen.findByText("项目不可用")).toBeInTheDocument();
  expect(screen.getByText(/Workspace path is not accessible/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新加载根目录" })).toBeInTheDocument();
});

test("returns focus to the project selector after closing its dialog", async () => {
  render(<App />);

  const opener = screen.getByRole("button", { name: "选择项目" });
  fireEvent.click(opener);
  fireEvent.click(await screen.findByRole("menuitem", { name: /打开项目/ }));

  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveFocus();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭项目对话框" }));

  await waitFor(() => expect(document.activeElement).toBe(opener));
});

test("creates switches refreshes and closes Host threads", async () => {
  const requests: Array<{ path: string; method: string; body?: string }> = [];
  let settingsUpdates = 0;
  const threadView = (id: string, workspace: string, status = "idle") => ({
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: id,
      workspace,
      status,
      active_turn_id: null,
      completed_turns: 0,
      settings: {
        provider_config_id: "deepseek",
        model: "deepseek-chat",
        temperature: null,
        max_tokens: null,
        thinking: null,
        limits: {
          max_iterations: 20,
          max_tool_calls: 50,
          max_execution_seconds: 900,
        },
        version: 0,
      },
      messages:
        id === "thread-existing"
          ? [
              {
                role: "user",
                content: [
                  {
                    type: "text",
                    text: "修复浏览器恢复时的重复事件",
                  },
                ],
              },
              {
                role: "assistant",
                content: [
                  {
                    type: "text",
                    text: "我先检查当前实现。",
                  },
                  {
                    type: "tool_call",
                    id: "call-read",
                    name: "read_file",
                    arguments: { path: "src/example.py" },
                  },
                ],
              },
              {
                role: "tool",
                content: [
                  {
                    type: "tool_result",
                    tool_call_id: "call-read",
                    ok: true,
                    content: "print('example')",
                    error_code: null,
                  },
                ],
              },
            ]
          : [],
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:00Z",
      latest_turn:
        id === "thread-existing"
          ? {
              status: "completed",
              stop_reason: "completed",
              iterations: 2,
              tool_calls: 1,
              usage: { input_tokens: 8, output_tokens: 3, total_tokens: 11 },
              modified_files: ["src/example.py"],
              file_diffs: [
                {
                  path: "src/example.py",
                  change_type: "modified",
                  diff: "--- a/src/example.py\n+++ b/src/example.py\n",
                },
              ],
              diff_complete: false,
              started_at: "2026-08-29T00:00:00Z",
              ended_at: "2026-08-29T00:00:01Z",
              error: null,
            }
          : null,
    },
    event_cursor: null,
    submission: null,
    workspace: {
      workspace_id: `workspace-${id}`,
      path: workspace,
      canonical_path: workspace,
      display_name: workspace.split("/").at(-1) || "/",
    },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ path, method, body: init?.body as string | undefined });
      if (path === "/api/providers") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            default_provider_id: "deepseek",
            providers: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                configured: true,
                credential_mask: "••••test",
                selected_model: "deepseek-chat",
                is_default: true,
              },
            ],
          }),
        } as Response;
      }
      if (path === "/api/workspaces/select" && method === "POST") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            workspace: {
              workspace_id: "workspace-project",
              path: "/home/student/project",
              canonical_path: "/home/student/project",
              display_name: "project",
            },
          }),
        } as Response;
      }
      if (path.startsWith("/api/workspaces")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            path: "/home/student/project",
            parent: null,
            roots: ["/home/student/project"],
            entries: [],
            truncated: false,
          }),
        } as Response;
      }
      if (path === "/api/threads" && method === "GET") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            threads: [threadView("thread-existing", "/home/student/old")],
          }),
        } as Response;
      }
      if (path === "/api/threads" && method === "POST") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread: threadView("thread-new", "/home/student/project"),
          }),
        } as Response;
      }
      if (path === "/api/threads/thread-new") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread: threadView("thread-new", "/home/student/project"),
          }),
        } as Response;
      }
      if (path === "/api/threads/thread-new/settings" && method === "PATCH") {
        settingsUpdates += 1;
        if (settingsUpdates > 1) {
          return {
            ok: false,
            status: 409,
            json: async () => ({
              error: {
                code: "SETTINGS_CONFLICT",
                message: "Thread settings changed",
                details: {},
              },
            }),
          } as Response;
        }
        const updated = threadView("thread-new", "/home/student/project");
        updated.snapshot.settings.model = "deepseek-reasoner";
        updated.snapshot.settings.version = 1;
        updated.snapshot.updated_at = "2026-08-29T00:00:02Z";
        return {
          ok: true,
          json: async () => ({ schema_version: 1, thread: updated }),
        } as Response;
      }
      if (path.endsWith("/close")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread: threadView("thread-new", "/home/student/project", "closed"),
          }),
        } as Response;
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );

  render(<App />);
  expect(
    await screen.findByRole("button", {
      name: /修复浏览器恢复时的重复事件/,
    }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "当前项目：old" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("thread-existing")).not.toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "修复浏览器恢复时的重复事件" }),
  ).toBeInTheDocument();
  const conversation = screen.getByRole("region", { name: "编码助手对话" });
  expect(conversation).not.toHaveTextContent("/home/student/old");
  expect(conversation).not.toHaveTextContent("settings v0");
  const workProcess = screen.getByLabelText("编码助手工作过程");
  expect(workProcess).toHaveTextContent("读取文件");
  expect(workProcess).toHaveTextContent("src/example.py");
  fireEvent.click(within(workProcess).getByText("技术详情"));
  expect(workProcess).toHaveTextContent("read_file");
  fireEvent.click(screen.getByRole("button", { name: "对话选项" }));
  expect(screen.getByRole("menuitem", { name: "对话详情" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "对话详情" }));
  const details = screen.getByLabelText("对话详情");
  expect(details).toHaveTextContent("thread-existing");
  expect(details).toHaveTextContent("/home/student/old");
  expect(details).toHaveTextContent("设置版本0");
  fireEvent.click(screen.getByRole("button", { name: "关闭对话详情" }));
  expect(
    await screen.findByRole("navigation", { name: "对话记录" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("region", { name: "编码助手对话" }),
  ).toBeInTheDocument();
  const contextPanel = screen.getByRole("complementary", { name: "动态与修改" });
  expect(contextPanel).not.toHaveTextContent("迭代次数2");
  expect(screen.getByRole("button", { name: "展开动态与修改" })).toHaveTextContent(
    "修改 1",
  );
  fireEvent.click(screen.getByRole("button", { name: "展开动态与修改" }));
  expect(await screen.findByRole("tab", { name: "动态" })).toHaveAttribute(
    "aria-selected",
    "false",
  );
  expect(screen.getByRole("tab", { name: "修改 1" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(contextPanel).toHaveTextContent("src/example.py");
  expect(contextPanel).toHaveTextContent("差异可能不完整");
  expect(
    screen.getByLabelText("对话文件修改"),
  ).toHaveTextContent("src/example.py");
  fireEvent.click(screen.getByRole("tab", { name: "动态" }));
  expect(contextPanel).toHaveTextContent("迭代次数2");
  expect(contextPanel).toHaveTextContent("输入令牌8");
  fireEvent.click(screen.getByRole("button", { name: "收起动态与修改" }));
  expect(screen.getByRole("button", { name: "显示对话记录" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  fireEvent.click(screen.getByRole("button", { name: "显示对话记录" }));
  expect(screen.getByRole("button", { name: "显示对话记录" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "显示对话" }));
  expect(
    await screen.findByRole("heading", { name: "修复浏览器恢复时的重复事件" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "当前项目：old" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: /打开项目/ }));
  fireEvent.click(await screen.findByRole("button", { name: "使用此项目" }));
  await screen.findByRole("button", { name: "当前项目：project" });
  fireEvent.click(screen.getByRole("button", { name: "新对话" }));

  expect(
    await screen.findByRole("heading", { name: "新对话" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "想让编码助手做什么？" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "解释这个项目" }));
  expect(screen.getByLabelText("询问编码助手")).toHaveValue("解释这个项目");
  expect(screen.queryByText("/home/student/project")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "对话选项" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "对话设置" }));
  fireEvent.change(screen.getByLabelText("对话模型"), {
    target: { value: "deepseek-reasoner" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存对话设置" }));
  await waitFor(() =>
    expect(screen.queryByLabelText("对话设置")).not.toBeInTheDocument(),
  );
  fireEvent.click(await screen.findByRole("button", { name: "对话选项" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "对话设置" }));
  fireEvent.change(screen.getByLabelText("对话模型"), {
    target: { value: "stale-model" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存对话设置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "更新对话设置失败 · Thread settings changed",
  );
  fireEvent.click(screen.getByRole("button", { name: "对话选项" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "刷新状态" }));
  await waitFor(() =>
    expect(requests.some(({ path }) => path === "/api/threads/thread-new")).toBe(true),
  );
  fireEvent.click(screen.getByRole("button", { name: "对话选项" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "关闭对话" }));
  expect(await screen.findByText("此对话已关闭")).toBeInTheDocument();
  expect(screen.getByLabelText("询问编码助手")).toBeDisabled();
  expect(requests).toEqual(
    expect.arrayContaining([
      {
        path: "/api/threads",
        method: "POST",
        body: JSON.stringify({
          workspace_id: "workspace-project",
          provider_config_id: "deepseek",
          model: "deepseek-chat",
        }),
      },
      {
        path: "/api/threads/thread-new/settings",
        method: "PATCH",
        body: JSON.stringify({
          expected_version: 0,
          provider_config_id: "deepseek",
          model: "deepseek-reasoner",
          temperature: null,
          max_tokens: null,
          thinking: null,
          limits: {
            max_iterations: 20,
            max_tool_calls: 50,
            max_execution_seconds: 900,
          },
        }),
      },
      { path: "/api/threads/thread-new", method: "GET", body: undefined },
      {
        path: "/api/threads/thread-new/close",
        method: "POST",
        body: undefined,
      },
    ]),
  );
});

test("submits multiline work and stops through Host commands", async () => {
  const requests: Array<{ path: string; method: string; body?: string }> = [];
  const thread = {
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: "thread-1",
      workspace: "/workspace",
      status: "idle" as const,
      active_turn_id: null,
      completed_turns: 0,
      settings: {
        provider_config_id: "deepseek",
        model: "deepseek-chat",
        temperature: null,
        max_tokens: null,
        thinking: null,
        limits: {
          max_iterations: 20,
          max_tool_calls: 50,
          max_execution_seconds: 900,
        },
        version: 0,
      },
      messages: [],
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:00Z",
      latest_turn: {
        status: "completed",
        stop_reason: "completed",
        iterations: 1,
        tool_calls: 0,
        usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
        modified_files: [],
        file_diffs: [],
        diff_complete: true,
        started_at: "2026-08-28T00:00:00Z",
        ended_at: "2026-08-28T00:00:01Z",
        error: null,
      },
    },
    event_cursor: null,
    submission: null,
    workspace: {
      workspace_id: "workspace-1",
      path: "/workspace",
      canonical_path: "/workspace",
      display_name: "workspace",
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ path, method, body: init?.body as string | undefined });
      if (path === "/api/providers") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            default_provider_id: "deepseek",
            providers: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                configured: true,
                credential_mask: "••••test",
                selected_model: "deepseek-chat",
                is_default: true,
              },
            ],
          }),
        } as Response;
      }
      if (path.startsWith("/api/workspaces")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            path: "/workspace",
            parent: null,
            roots: ["/workspace"],
            entries: [],
            truncated: false,
          }),
        } as Response;
      }
      if (path === "/api/threads" && method === "GET") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [thread] }),
        } as Response;
      }
      if (path.endsWith("/turns")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread_id: "thread-1",
            submission: {
              thread_id: "thread-1",
              status: "starting",
              accepted_at: "2026-08-29T00:00:01Z",
            },
          }),
        } as Response;
      }
      if (path.endsWith("/cancel")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread_id: "thread-1",
            submission: {
              thread_id: "thread-1",
              status: "cancelling",
              accepted_at: "2026-08-29T00:00:01Z",
            },
          }),
        } as Response;
      }
      if (path === "/api/threads/thread-1") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread: {
              ...thread,
              submission: {
                thread_id: "thread-1",
                status: "starting",
                accepted_at: "2026-08-29T00:00:01Z",
              },
            },
          }),
        } as Response;
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );

  render(<App />);
  const composer = await screen.findByLabelText("询问编码助手");
  const threadReadsBeforeSubmit = requests.filter(
    ({ path, method }) =>
      path === "/api/threads/thread-1" && method === "GET",
  ).length;
  fireEvent.change(composer, { target: { value: "First line\nSecond line" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));

  expect(await screen.findByText("正在启动编码助手…")).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "First line Second line" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "编码助手对话" })).toHaveTextContent(
    "First line Second line",
  );
  await waitFor(() =>
    expect(
      requests.filter(
        ({ path, method }) =>
          path === "/api/threads/thread-1" && method === "GET",
      ).length,
    ).toBeGreaterThan(threadReadsBeforeSubmit),
  );
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(await screen.findByText("正在停止…")).toBeInTheDocument();
  expect(requests).toEqual(
    expect.arrayContaining([
      {
        path: "/api/threads/thread-1/turns",
        method: "POST",
        body: JSON.stringify({ message: "First line\nSecond line" }),
      },
      {
        path: "/api/threads/thread-1/cancel",
        method: "POST",
        body: undefined,
      },
    ]),
  );
});

test("shows Skills metadata and capability-gated thread settings", async () => {
  const requests: Array<{ path: string; method: string; body?: string }> = [];
  const thread = {
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: "thread-skills",
      workspace: "/workspace",
      status: "idle",
      active_turn_id: null,
      completed_turns: 0,
      settings: {
        provider_config_id: "deepseek",
        model: "deepseek-chat",
        temperature: null,
        max_tokens: null,
        thinking: null,
        limits: {
          max_iterations: 20,
          max_tool_calls: 50,
          max_execution_seconds: 900,
        },
        approval_mode: "on_request",
        version: 0,
      },
      messages: [],
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:00Z",
      latest_turn: null,
      skills: {
        schema_version: 1,
        available: [
          {
            name: "repo-guide",
            description: "Repository workflow guidance",
            source: "workspace .agents",
            source_path: "/workspace/.agents/skills/repo-guide/SKILL.md",
            directory: "/workspace/.agents/skills/repo-guide",
          },
        ],
        loaded: [],
        diagnostics: [],
      },
    },
    event_cursor: null,
    submission: null,
    workspace: {
      workspace_id: "workspace-1",
      path: "/workspace",
      canonical_path: "/workspace",
      display_name: "workspace",
    },
    capabilities: {
      thinking_supported: true,
      supports_thinking_budget: true,
      supported_keep_values: ["none", "all"],
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ path, method, body: init?.body as string | undefined });
      if (path === "/api/providers") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            default_provider_id: "deepseek",
            providers: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                configured: true,
                credential_mask: "••••test",
                selected_model: "deepseek-chat",
                is_default: true,
              },
              {
                provider_id: "moonshot",
                display_name: "Moonshot / Kimi",
                configured: true,
                credential_mask: "••••test",
                selected_model: "moonshot-v1",
                is_default: false,
              },
            ],
          }),
        } as Response;
      }
      if (path === "/api/threads" && method === "GET") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [thread] }),
        } as Response;
      }
      if (path.startsWith("/api/workspaces")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            path: "/workspace",
            parent: null,
            roots: ["/workspace"],
            entries: [],
            truncated: false,
          }),
        } as Response;
      }
      if (path.startsWith("/api/threads/thread-skills/capabilities")) {
        const candidate = new URL(path, "http://test.local");
        const provider = candidate.searchParams.get("provider_config_id");
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread_id: "thread-skills",
            capabilities:
              provider === "moonshot"
                ? {
                    thinking_supported: false,
                    supports_thinking_budget: false,
                    supported_keep_values: [],
                  }
                : thread.capabilities,
          }),
        } as Response;
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );

  render(<App />);
  expect(await screen.findByRole("button", { name: "Skills" })).toHaveTextContent(
    "Loaded 0 / Available 1",
  );
  fireEvent.click(screen.getByRole("button", { name: "Skills" }));
  const skills = await screen.findByRole("dialog", { name: "Skills" });
  expect(skills).toHaveTextContent("repo-guide");
  expect(skills).toHaveTextContent("Repository workflow guidance");
  fireEvent.click(screen.getByRole("button", { name: "对话选项" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "对话设置" }));
  await waitFor(() => expect(screen.getByLabelText("Thinking")).not.toBeDisabled());
  fireEvent.click(screen.getByLabelText("Thinking"));
  expect(screen.getByLabelText("Thinking budget")).toBeInTheDocument();
  expect(screen.queryByLabelText("Thinking history")).not.toBeInTheDocument();
  expect(
    requests.some(({ path }) => path.startsWith("/api/threads/thread-skills/capabilities?")),
  ).toBe(true);

  // A draft switch must use the candidate capability preview immediately and
  // must not retain the previous provider's thinking controls.
  fireEvent.change(screen.getByLabelText("模型服务商"), {
    target: { value: "moonshot" },
  });
  await waitFor(() => expect(screen.getByLabelText("Thinking")).toBeDisabled());
  expect(screen.queryByLabelText("Thinking budget")).not.toBeInTheDocument();
  await waitFor(() =>
    expect(
      requests.some(({ path }) =>
        path.includes("provider_config_id=moonshot") && path.includes("model=moonshot-v1"),
      ),
    ).toBe(true),
  );
  fireEvent.change(screen.getByLabelText("模型服务商"), {
    target: { value: "deepseek" },
  });
  await waitFor(() => expect(screen.getByLabelText("Thinking")).not.toBeDisabled());
});

test("opening and saving unchanged Settings preserves enabled Thinking", async () => {
  const thread = appThread("thread-thinking", {
    enabled: true,
    budget_tokens: 1_024,
    keep: null,
  });
  const requests: Array<{ path: string; method: string; body?: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ path, method, body: init?.body as string | undefined });
      if (path === "/api/providers") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            default_provider_id: "deepseek",
            providers: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                configured: true,
                credential_mask: "••••test",
                selected_model: "deepseek-chat",
                is_default: true,
              },
            ],
          }),
        } as Response;
      }
      if (path === "/api/threads") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [thread] }),
        } as Response;
      }
      if (path.startsWith(`/api/threads/${thread.snapshot.thread_id}/capabilities`)) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            thread_id: thread.snapshot.thread_id,
            capabilities: thread.capabilities,
          }),
        } as Response;
      }
      if (path === `/api/threads/${thread.snapshot.thread_id}/settings` && method === "PATCH") {
        return {
          ok: true,
          json: async () => ({ thread }),
        } as Response;
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );

  render(<App />);
  await screen.findByRole("button", { name: /Skills/ });
  fireEvent.click(screen.getByRole("button", { name: "对话选项" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "对话设置" }));
  await waitFor(() => expect(screen.getByLabelText("Thinking")).not.toBeDisabled());
  expect(screen.getByLabelText("Thinking")).toBeChecked();
  expect(screen.getByLabelText("Thinking budget")).toHaveValue(1_024);

  fireEvent.click(screen.getByRole("button", { name: "保存对话设置" }));
  await waitFor(() =>
    expect(
      requests.some(
        ({ path, method }) =>
          path === `/api/threads/${thread.snapshot.thread_id}/settings` &&
          method === "PATCH",
      ),
    ).toBe(true),
  );
  const patch = requests.find(
    ({ path, method }) =>
      path === `/api/threads/${thread.snapshot.thread_id}/settings` && method === "PATCH",
  );
  expect(JSON.parse(patch?.body ?? "{}").thinking).toEqual({
    enabled: true,
    budget_tokens: 1_024,
    keep: null,
  });
});

test("updates the Skills button from a live skill_loaded event", async () => {
  const previousEventSource = globalThis.EventSource;
  AppFakeEventSource.instances = [];
  vi.stubGlobal("EventSource", AppFakeEventSource);
  const thread = appThread("thread-live-skills");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/providers") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            default_provider_id: "deepseek",
            providers: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                configured: true,
                credential_mask: "••••test",
                selected_model: "deepseek-chat",
                is_default: true,
              },
            ],
          }),
        } as Response;
      }
      if (path === "/api/threads") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [thread] }),
        } as Response;
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );

  try {
    render(<App />);
    const skillsButton = await screen.findByRole("button", { name: /Skills/ });
    expect(skillsButton).toHaveTextContent("Loaded 0 / Available 1");
    const source = await waitFor(() => {
      const current = AppFakeEventSource.instances[0];
      if (current === undefined) {
        throw new Error("EventSource was not created");
      }
      return current;
    });
    source.onopen?.(new Event("open"));
    source.emit("skill_loaded", {
      schema_version: 1,
      event_id: "skill-loaded-live",
      thread_id: thread.snapshot.thread_id,
      turn_id: "turn-live",
      sequence: 1,
      type: "skill_loaded",
      timestamp: "2026-08-29T00:00:01Z",
      payload: {
        name: "repo-guide",
        description: "Repository workflow guidance",
        source: "workspace .agents",
        source_path: "/workspace/.agents/skills/repo-guide/SKILL.md",
        directory: "/workspace/.agents/skills/repo-guide",
      },
    });
    await waitFor(() =>
      expect(skillsButton).toHaveTextContent("Loaded 1 / Available 1"),
    );
  } finally {
    vi.stubGlobal("EventSource", previousEventSource);
  }
});

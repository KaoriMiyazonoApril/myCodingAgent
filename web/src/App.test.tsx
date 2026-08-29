import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () =>
        String(input) === "/api/threads"
          ? { schema_version: 1, threads: [] }
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

test("starts in provider setup mode without hiding the host state", async () => {
  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "Connect a model provider" }),
  ).toBeInTheDocument();
  expect(screen.getByText("DeepSeek")).toBeInTheDocument();
  expect(screen.getByText("Moonshot / Kimi")).toBeInTheDocument();
  expect(screen.getByText("GLM")).toBeInTheDocument();
  expect(
    screen.getByText(/Provider setup required before creating a thread\./),
  ).toBeInTheDocument();
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

  fireEvent.click(
    await screen.findByRole("button", { name: "Configure DeepSeek" }),
  );
  const key = screen.getByLabelText("API key");
  fireEvent.change(key, { target: { value: "sk-api-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and discover" }));

  expect(
    await screen.findByRole("option", { name: "deepseek-reasoner" }),
  ).toBeInTheDocument();
  expect(key).toHaveValue("");
  expect(screen.getByText("Key saved · ••••cret")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Provider model"), {
    target: { value: "deepseek-reasoner" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Use as default" }));

  expect((await screen.findAllByText("Default provider")).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "Refresh models" }));
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
  fireEvent.click(
    await screen.findByRole("button", { name: "Configure DeepSeek" }),
  );
  fireEvent.change(screen.getByLabelText("API key"), {
    target: { value: "rejected-key" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save and discover" }));

  expect(
    await screen.findByRole("alert"),
  ).toHaveTextContent("Provider rejected the configured credential");
});

test("navigates Host workspaces and selects the current directory", async () => {
  const workspaceRequests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/threads") {
        return {
          ok: true,
          json: async () => ({ schema_version: 1, threads: [] }),
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
    await screen.findByRole("heading", { name: "Choose a workspace" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open project" }));
  expect(await screen.findByText("/home/student/project")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Use this workspace" }));
  expect(screen.getByText("Selected · /home/student/project")).toBeInTheDocument();
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
              code: "WORKSPACE_NOT_ACCESSIBLE",
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

  expect(await screen.findByText("Workspace unavailable")).toBeInTheDocument();
  expect(screen.getByText("Workspace path is not accessible")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reload roots" })).toBeInTheDocument();
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
      messages: [],
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
    await screen.findByRole("navigation", { name: "Workspace and threads" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("region", { name: "Agent conversation" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("complementary", { name: "Activity" })).toBeInTheDocument();
  expect(screen.getByRole("complementary", { name: "Activity" })).toHaveTextContent(
    "Iterations2",
  );
  expect(screen.getByRole("complementary", { name: "Activity" })).toHaveTextContent(
    "Input tokens8",
  );
  expect(screen.getByRole("complementary", { name: "Activity" })).toHaveTextContent(
    "Stop reasoncompleted",
  );
  expect(screen.getByRole("complementary", { name: "Activity" })).toHaveTextContent(
    "src/example.py",
  );
  expect(
    screen.getByLabelText("Conversation file changes"),
  ).toHaveTextContent("src/example.py");
  expect(screen.getByRole("complementary", { name: "Activity" })).toHaveTextContent(
    "Diff may be incomplete",
  );
  expect(screen.getByRole("button", { name: "Show navigation" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  fireEvent.click(screen.getByRole("button", { name: "Show navigation" }));
  expect(screen.getByRole("button", { name: "Show navigation" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "Show conversation" }));
  expect(
    await screen.findByRole("heading", { name: "thread-existing" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Use this workspace" }));
  fireEvent.click(screen.getByRole("button", { name: "New thread" }));

  expect(
    await screen.findByRole("heading", { name: "thread-new" }),
  ).toBeInTheDocument();
  expect(screen.getAllByText("/home/student/project").length).toBeGreaterThan(0);
  expect(screen.getByText("DeepSeek · deepseek-chat · settings v0")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Thread settings" }));
  fireEvent.change(screen.getByLabelText("Thread model"), {
    target: { value: "deepseek-reasoner" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save thread settings" }));
  expect(
    await screen.findByText("DeepSeek · deepseek-reasoner · settings v1"),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Thread settings" }));
  fireEvent.change(screen.getByLabelText("Thread model"), {
    target: { value: "stale-model" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save thread settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Settings update failed · Thread settings changed",
  );
  fireEvent.click(screen.getByRole("button", { name: "Refresh active thread" }));
  await waitFor(() =>
    expect(requests.some(({ path }) => path === "/api/threads/thread-new")).toBe(true),
  );
  fireEvent.click(screen.getByRole("button", { name: "Close active thread" }));
  expect(
    await screen.findByText(
      (_, element) =>
        element?.classList.contains("thread-status") === true &&
        element.textContent === "Status · closed",
    ),
  ).toBeInTheDocument();
  expect(requests).toEqual(
    expect.arrayContaining([
      {
        path: "/api/threads",
        method: "POST",
        body: JSON.stringify({
          workspace: "/home/student/project",
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
  const composer = await screen.findByLabelText("Ask Agent");
  const threadReadsBeforeSubmit = requests.filter(
    ({ path, method }) =>
      path === "/api/threads/thread-1" && method === "GET",
  ).length;
  fireEvent.change(composer, { target: { value: "First line\nSecond line" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByText("Starting…")).toBeInTheDocument();
  await waitFor(() =>
    expect(
      requests.filter(
        ({ path, method }) =>
          path === "/api/threads/thread-1" && method === "GET",
      ).length,
    ).toBeGreaterThan(threadReadsBeforeSubmit),
  );
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  expect(await screen.findByText("Cancelling…")).toBeInTheDocument();
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

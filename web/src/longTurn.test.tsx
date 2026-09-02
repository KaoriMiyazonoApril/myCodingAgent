import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ThreadView } from "./api";
import { App, ModelActivityPill } from "./App";
import { PROVISIONAL_TEXT_MAX_CHARS } from "./events";

type FixtureOptions = {
  id?: string;
  status?: ThreadView["snapshot"]["status"];
  activeTurnId?: string | null;
  messages?: Array<Record<string, unknown>>;
  latestTurn?: Record<string, unknown> | null;
  pendingApproval?: ThreadView["snapshot"]["pending_approval"];
  submission?: ThreadView["submission"];
  skills?: ThreadView["snapshot"]["skills"];
};

function threadView(options: FixtureOptions = {}): ThreadView {
  const id = options.id ?? "thread-long";
  return {
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: id,
      workspace: "/workspace",
      status: options.status ?? "idle",
      active_turn_id: options.activeTurnId ?? null,
      completed_turns: 1,
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
      messages: options.messages ?? [],
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:00Z",
      latest_turn: options.latestTurn ?? null,
      ...(options.pendingApproval === undefined
        ? {}
        : { pending_approval: options.pendingApproval }),
      skills: options.skills ?? {
        schema_version: 1,
        available: [],
        loaded: [],
        diagnostics: [],
      },
    },
    event_cursor: "cursor-long",
    submission: options.submission ?? null,
    workspace: {
      workspace_id: "workspace-1",
      path: "/workspace",
      canonical_path: "/workspace",
      display_name: "workspace",
    },
  };
}

function event(
  type: string,
  turnId: string,
  payload: Record<string, unknown>,
  eventId = `${type}-${turnId}`,
) {
  return {
    schema_version: 1,
    event_id: eventId,
    thread_id: "thread-long",
    turn_id: turnId,
    sequence: 1,
    type,
    timestamp: "2026-08-29T00:00:01Z",
    payload,
  };
}

class LongTurnEventSource {
  static instances: LongTurnEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<
    string,
    Array<(event: MessageEvent<string>) => void>
  >();
  closed = false;

  constructor(readonly url: string) {
    LongTurnEventSource.instances.push(this);
  }

  addEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, payload: Record<string, unknown>) {
    const message = new MessageEvent<string>(type, {
      data: JSON.stringify(payload),
    });
    this.listeners.get(type)?.forEach((listener) => listener(message));
  }
}

function response(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as Response;
}

async function renderFixture(
  initial: ThreadView,
  getCurrent: () => ThreadView = () => initial,
) {
  LongTurnEventSource.instances = [];
  vi.stubGlobal("EventSource", LongTurnEventSource);
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/providers") {
      return response({
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
            catalog: {
              status: "ready",
              models: ["deepseek-chat"],
              cached: true,
              error_code: null,
            },
          },
        ],
      });
    }
    if (path === "/api/threads" && (init?.method ?? "GET") === "GET") {
      return response({ schema_version: 1, threads: [initial] });
    }
    if (path === `/api/threads/${initial.snapshot.thread_id}`) {
      return response({ schema_version: 1, thread: getCurrent() });
    }
    if (path.includes("/approvals/") && init?.method === "POST") {
      return response({
        schema_version: 1,
        thread_id: initial.snapshot.thread_id,
        approval_id: "approval-1",
        approved: true,
      });
    }
    if (path.includes("/capabilities")) {
      return response({
        schema_version: 1,
        thread_id: initial.snapshot.thread_id,
        capabilities: {
          thinking_supported: false,
        },
      });
    }
    throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  await screen.findByRole("button", { name: "技能" });
  const source = await waitFor(() => {
    const current = LongTurnEventSource.instances.find((item) => !item.closed);
    if (current === undefined) {
      throw new Error("EventSource was not created");
    }
    return current;
  });
  source.onopen?.(new Event("open"));
  return { fetchMock, source };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("A: keeps intermediate assistant narration out and admits final text-only replies", async () => {
  const initial = threadView({
    activeTurnId: "turn-a",
    status: "running",
    submission: {
      thread_id: "thread-long",
      status: "running",
      accepted_at: "2026-08-29T00:00:00Z",
    },
    messages: [
      { role: "user", content: [{ type: "text", text: "Inspect" }] },
      {
        role: "assistant",
        content: [
          { type: "text", text: "private tool narration" },
          { type: "tool_call", id: "call-a", name: "read_file", arguments: { path: "README.md" } },
        ],
      },
      {
        role: "tool",
        content: [
          {
            type: "tool_result",
            tool_call_id: "call-a",
            ok: true,
            content: "read",
            error_code: null,
          },
        ],
      },
    ],
  });
  const { source } = await renderFixture(initial);

  expect(screen.queryByText("private tool narration")).not.toBeInTheDocument();
  await act(async () => {
    source.emit(
      "model_response",
      event("model_response", "turn-a", {
        message: { role: "assistant", content: [{ type: "text", text: "final answer" }] },
        reasoning_preview: { text: "secret preview" },
      }),
    );
  });
  expect(screen.getByText("final answer")).toBeInTheDocument();
  expect(screen.queryByText("secret preview")).not.toBeInTheDocument();
});

test("A: bounds transient narration and hides it once a tool call is structured", async () => {
  const initial = threadView({
    activeTurnId: "turn-progress",
    status: "running",
    submission: {
      thread_id: "thread-long",
      status: "running",
      accepted_at: "2026-08-29T00:00:00Z",
    },
  });
  const { source } = await renderFixture(initial);
  const narration = "n".repeat(PROVISIONAL_TEXT_MAX_CHARS);

  await act(async () => {
    source.emit(
      "model_text_delta",
      event("model_text_delta", "turn-progress", {
        text: `${narration}${"overflow".repeat(80)}`,
      }),
    );
  });
  expect(screen.getByText(`${narration}…`)).toBeInTheDocument();

  await act(async () => {
    source.emit(
      "model_tool_call_delta",
      event("model_tool_call_delta", "turn-progress", {
        index: 0,
        id: "call-progress",
        name: "read_file",
        arguments_delta: '{"path":"README.md"}',
      }),
    );
  });
  expect(screen.queryByText(`${narration}…`)).not.toBeInTheDocument();
  expect(screen.getByText("read_file")).toBeInTheDocument();
});

test("B/C: scopes tools to the current Turn and bounds long success history", async () => {
  const calls = Array.from({ length: 18 }, (_, index) => ({
    type: "tool_call",
    id: `call-success-${index}`,
    name: "read_file",
    arguments: { path: `file-${index}.txt` },
  }));
  const results = calls.map((call) => ({
    type: "tool_result",
    tool_call_id: call.id,
    ok: true,
    content: `result-${call.id}`,
    error_code: null,
  }));
  const initial = threadView({
    activeTurnId: "turn-tools",
    status: "running",
    submission: {
      thread_id: "thread-long",
      status: "running",
      accepted_at: "2026-08-29T00:00:00Z",
    },
    messages: [
      { role: "user", content: [{ type: "text", text: "Long task" }] },
      { role: "assistant", content: [{ type: "text", text: "step narration" }, ...calls] },
      { role: "tool", content: results },
    ],
  });
  const { source } = await renderFixture(initial);

  await act(async () => {
    source.emit(
      "tool_started",
      event("tool_started", "turn-tools", {
        tool_call_id: "call-running",
        name: "run_command",
      }),
    );
    source.emit(
      "tool_finished",
      event("tool_finished", "turn-tools", {
        name: "run_command",
        result: {
          tool_call_id: "call-error",
          ok: false,
          content: "command failed",
          error_code: "COMMAND_FAILED",
        },
      }),
    );
  });

  const work = screen.getByLabelText("编码助手工作过程");
  expect(work).toHaveTextContent("已折叠 6 项较早的已完成工具");
  expect(work.querySelectorAll(".tool-card.success")).toHaveLength(12);
  expect(work.querySelector(".tool-card.running")).toBeInTheDocument();
  expect(work.querySelector(".tool-card.error")).toBeInTheDocument();
  expect(work.querySelectorAll("pre").length).toBeGreaterThan(0);

  await act(async () => {
    source.emit(
      "turn_started",
      event("turn_started", "turn-next", { user_message: "next turn" }, "next-turn"),
    );
  });
  expect(screen.queryByLabelText("编码助手工作过程")).not.toBeInTheDocument();
});

test("D: ignores raw reasoning deltas and uses a local performance timer that freezes", async () => {
  let now = 10_000;
  vi.useFakeTimers();
  vi.spyOn(performance, "now").mockImplementation(() => now);
  const { rerender } = render(
    <ModelActivityPill
      activity={{
        phase: "thinking",
        since: "2026-08-29T00:00:00Z",
        finished: false,
      }}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("思考中… 0.0s");

  await act(async () => {
    vi.advanceTimersByTime(0);
  });
  now += 2_500;
  await act(async () => {
    vi.advanceTimersByTime(100);
  });
  expect(screen.getByRole("status")).toHaveTextContent("思考中… 2.5s");

  await act(async () => {
    rerender(
      <ModelActivityPill
        activity={{
          phase: "generating",
          since: "2026-08-29T00:00:02.5Z",
          finished: false,
        }}
      />,
    );
    vi.advanceTimersByTime(0);
  });
  expect(screen.getByRole("status")).toHaveTextContent("思考 · 2.5s");
  expect(screen.getByRole("status")).toHaveTextContent("生成中… 0.0s");

  now += 500;
  await act(async () => {
    vi.advanceTimersByTime(100);
  });
  expect(screen.getByRole("status")).toHaveTextContent("思考 · 2.5s");
  expect(screen.getByRole("status")).toHaveTextContent("生成中… 0.5s");

  now += 500;
  await act(async () => {
    rerender(
      <ModelActivityPill
        activity={{
          phase: "idle",
          since: "2026-08-29T00:00:00Z",
          finished: true,
          ended_at: "2026-08-29T00:00:03Z",
        }}
      />,
    );
  });
  await act(async () => {
    vi.advanceTimersByTime(500);
  });
  const frozen = screen.getByRole("status").textContent;
  expect(frozen).toMatch(/思考 · 2\.5s/);
  expect(frozen).toMatch(/已完成 · 1\.0s/);
  now += 10_000;
  await act(async () => {
    vi.advanceTimersByTime(1_000);
  });
  expect(screen.getByRole("status").textContent).toBe(frozen);
});

test("E: approval reconciliation preserves the live DOM and manual scroll position", async () => {
  const initial = threadView({
    activeTurnId: "turn-approval",
    status: "waiting_approval",
    submission: {
      thread_id: "thread-long",
      status: "running",
      accepted_at: "2026-08-29T00:00:00Z",
    },
    messages: [
      { role: "user", content: [{ type: "text", text: "Approve this" }] },
      { role: "assistant", content: [{ type: "text", text: "partial answer" }] },
    ],
    pendingApproval: {
      approval_id: "approval-1",
      tool_call: {
        id: "call-approval",
        name: "run_command",
        arguments: { command: "echo ok" },
      },
      reason_code: "COMMAND_REQUIRES_APPROVAL",
      message: "command requires approval",
    },
  });
  const refreshed = threadView({
    id: initial.snapshot.thread_id,
    status: "idle",
    activeTurnId: null,
    submission: null,
    messages: initial.snapshot.messages,
    pendingApproval: null,
    latestTurn: { status: "completed", final_text: "done" },
    skills: initial.snapshot.skills,
  });
  let current = initial;
  const { source } = await renderFixture(initial, () => current);
  const scroll = document.querySelector<HTMLDivElement>(".thread-scroll");
  expect(scroll).not.toBeNull();
  Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1_000 });
  Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
  scroll!.scrollTop = 120;
  fireEvent.scroll(scroll!);

  const before = scroll;
  current = refreshed;
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "批准" }));
  });
  await waitFor(() =>
    expect(screen.queryByRole("alert", { name: "等待确认" })).not.toBeInTheDocument(),
  );
  expect(screen.getByText("partial answer")).toBeInTheDocument();
  expect(document.querySelector(".thread-scroll")).toBe(before);
  expect(scroll!.scrollTop).toBe(120);
  expect(source.closed).toBe(true);
});

test("F: full snapshot hydration recovers final messages, approval, terminal, and skills", async () => {
  const initial = threadView({
    activeTurnId: "turn-recovery",
    status: "waiting_approval",
    submission: null,
    messages: [
      { role: "user", content: [{ type: "text", text: "recover" }] },
      {
        role: "assistant",
        content: [
          { type: "text", text: "tool narration" },
          { type: "tool_call", id: "call-recovery", name: "read_file", arguments: { path: "README.md" } },
        ],
      },
      {
        role: "tool",
        content: [
          {
            type: "tool_result",
            tool_call_id: "call-recovery",
            ok: true,
            content: "snapshot result",
            error_code: null,
          },
        ],
      },
      { role: "assistant", content: [{ type: "text", text: "recovered final" }] },
    ],
    latestTurn: { status: "completed", stop_reason: "completed", tool_calls: 1 },
    pendingApproval: {
      approval_id: "approval-1",
      tool_call: { id: "call-pending", name: "run_command", arguments: { command: "echo ok" } },
      reason_code: "COMMAND_REQUIRES_APPROVAL",
      message: "approval restored",
    },
    skills: {
      schema_version: 1,
      available: [
        {
          name: "repo-guide",
          description: "Repository guide",
          source: "workspace",
          source_path: "/workspace/.agents/repo-guide/SKILL.md",
          directory: "/workspace/.agents/repo-guide",
        },
      ],
      loaded: [],
      diagnostics: [],
    },
  });
  await renderFixture(initial);

  expect(screen.getByText("recovered final")).toBeInTheDocument();
  expect(screen.queryByText("tool narration")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "技能" })).toHaveTextContent("技能 0 / 1");
  expect(screen.getByRole("alert", { name: "等待确认" })).toHaveTextContent(
    "approval restored",
  );
  expect(screen.getByLabelText("编码助手工作过程")).toHaveTextContent("读取文件");
  fireEvent.click(screen.getByRole("button", { name: "展开运行详情与修改" }));
  expect(screen.getByRole("complementary", { name: "运行详情与修改" })).toHaveTextContent(
    "任务 · 已完成",
  );
});

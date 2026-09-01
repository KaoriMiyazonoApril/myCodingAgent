import { expect, test } from "vitest";

import type { ThreadView } from "./api";
import {
  applyAgentEvent,
  eventRequiresSnapshotRefresh,
  hydrateThread,
  type AgentEvent,
} from "./events";

function view(): ThreadView {
  return {
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: "thread-1",
      workspace: "/workspace",
      status: "idle",
      active_turn_id: null,
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
        version: 0,
      },
      messages: [
        {
          schema_version: 1,
          role: "user",
          content: [{ type: "text", text: "Inspect the project" }],
        },
        {
          schema_version: 1,
          role: "assistant",
          content: [
            { type: "text", text: "I will inspect it." },
            {
              type: "tool_call",
              id: "call-1",
              name: "read_file",
              arguments: { path: "README.md" },
            },
          ],
        },
        {
          schema_version: 1,
          role: "tool",
          content: [
            {
              type: "tool_result",
              tool_call_id: "call-1",
              ok: true,
              content: "contents",
              error_code: null,
            },
          ],
        },
      ],
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:01Z",
      latest_turn: { status: "completed", final_text: "Done" },
    },
    event_cursor: "cursor-1",
    submission: null,
  };
}

function event(type: string, payload: Record<string, unknown>): AgentEvent {
  return {
    schema_version: 1,
    event_id: `${type}-id`,
    thread_id: "thread-1",
    turn_id: "turn-1",
    sequence: 1,
    type,
    timestamp: "2026-08-29T00:00:00Z",
    payload,
  };
}

test("hydrates messages tools and terminal outcome from a Snapshot", () => {
  const state = hydrateThread(view());

  expect(state.messages.map(({ role, text }) => ({ role, text }))).toEqual([
    { role: "user", text: "Inspect the project" },
    { role: "assistant", text: "I will inspect it." },
  ]);
  expect(state.tools).toEqual([
    {
      id: "call-1",
      name: "read_file",
      arguments: { path: "README.md" },
      status: "success",
      result: "contents",
      error_code: null,
    },
  ]);
  expect(state.terminal).toEqual({ status: "completed", final_text: "Done" });
});

test("hydrates a pending approval from a snapshot without replaying its event", () => {
  const state = hydrateThread({
    ...view(),
    snapshot: {
      ...view().snapshot,
      status: "waiting_approval",
      active_turn_id: "turn-approval",
      pending_approval: {
        approval_id: "approval-snapshot",
        tool_call: { id: "call-snapshot", name: "run_command" },
        timeout_seconds: 300,
        decision: "require_approval",
        execution_profile: "workspace_write_network",
        reason_code: "NETWORK_COMMAND",
        message: "command requires approval",
      },
    },
  });

  expect(state.approval).toEqual({
    approval_id: "approval-snapshot",
    tool_call: { id: "call-snapshot", name: "run_command" },
    timeout_seconds: 300,
    decision: "require_approval",
    execution_profile: "workspace_write_network",
    reason_code: "NETWORK_COMMAND",
    message: "command requires approval",
  });
});

test("deduplicates event IDs and merges a tool lifecycle by call ID", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  const requested = event("tool_requested", {
    tool_call: { id: "call-2", name: "run_command", arguments: { command: "pytest" } },
  });
  state = applyAgentEvent(state, requested);
  const duplicate = applyAgentEvent(state, requested);
  expect(duplicate).toBe(state);
  state = applyAgentEvent(
    duplicate,
    event("tool_started", { tool_call_id: "call-2", name: "run_command" }),
  );
  state = applyAgentEvent(
    state,
    event("tool_finished", {
      name: "run_command",
      result: {
        tool_call_id: "call-2",
        ok: false,
        content: "1 failed",
        error_code: "COMMAND_FAILED",
      },
    }),
  );

  expect(state.seen_event_ids).toHaveLength(3);
  expect(state.tools).toEqual([
    {
      id: "call-2",
      name: "run_command",
      arguments: { command: "pytest" },
      status: "error",
      result: "1 failed",
      error_code: "COMMAND_FAILED",
    },
  ]);
});

test("applies live messages and safe rejection errors", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  state = applyAgentEvent(
    state,
    event("turn_started", { user_message: "Fix the tests" }),
  );
  state = applyAgentEvent(
    state,
    event("model_response", {
      message: {
        role: "assistant",
        content: [{ type: "text", text: "Working on it" }],
      },
    }),
  );
  state = applyAgentEvent(
    state,
    event("turn_rejected", {
      error: { code: "UNSAFE_WORKSPACE", message: "Turn could not start" },
    }),
  );

  expect(state.messages.map(({ text }) => text)).toEqual([
    "Fix the tests",
    "Working on it",
  ]);
  expect(state.error).toEqual({
    code: "UNSAFE_WORKSPACE",
    message: "Turn could not start",
  });
  expect(state.terminal).toEqual({
    status: "rejected",
    error: { code: "UNSAFE_WORKSPACE", message: "Turn could not start" },
  });
  expect(eventRequiresSnapshotRefresh("turn_rejected")).toBe(true);
});

test("updates loaded Skills from live events and resets them at the next Turn", () => {
  const initial = view();
  initial.snapshot.skills = {
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
  };
  let state = hydrateThread(initial);
  expect(state.skills.loaded).toHaveLength(0);
  state = applyAgentEvent(
    state,
    event("skill_loaded", {
      name: "repo-guide",
      description: "Repository workflow guidance",
      source: "workspace .agents",
      source_path: "/workspace/.agents/skills/repo-guide/SKILL.md",
      directory: "/workspace/.agents/skills/repo-guide",
    }),
  );
  expect(state.skills.loaded.map(({ name }) => name)).toEqual(["repo-guide"]);
  state = applyAgentEvent(
    state,
    event("skill_loaded", {
      name: "repo-guide",
      description: "Repository workflow guidance",
      source: "workspace .agents",
      source_path: "/workspace/.agents/skills/repo-guide/SKILL.md",
    }),
  );
  expect(state.skills.loaded).toHaveLength(1);
  state = applyAgentEvent(
    state,
    event("skill_activation_failed", {
      name: "missing-skill",
      error_code: "SKILL_NOT_FOUND",
    }),
  );
  expect(state.skills.diagnostics).toEqual([
    { code: "SKILL_NOT_FOUND", name: "missing-skill" },
  ]);
  state = applyAgentEvent(state, event("turn_started", { user_message: "Next" }));
  expect(state.skills.available.map(({ name }) => name)).toEqual(["repo-guide"]);
  expect(state.skills.loaded).toEqual([]);
  expect(state.skills.diagnostics).toEqual([
    { code: "SKILL_NOT_FOUND", name: "missing-skill" },
  ]);
});

test("hydrates a safe Host background failure as a terminal error", () => {
  const state = hydrateThread({
    ...view(),
    host_error: {
      code: "TURN_TASK_FAILED",
      message: "Agent Turn task failed",
    },
  });

  expect(state.error).toEqual({
    code: "TURN_TASK_FAILED",
    message: "Agent Turn task failed",
  });
  expect(state.terminal?.status).toBe("rejected");
});

test("preserves terminal tools and reconstructs cancellation and file activity", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  state = applyAgentEvent(
    state,
    event("tool_finished", {
      name: "edit_file",
      result: {
        tool_call_id: "call-edit",
        ok: true,
        content: "edited src/app.py",
        metadata: { path: "src/app.py", replacements: 1 },
        error_code: null,
      },
    }),
  );
  state = applyAgentEvent(
    state,
    event("tool_started", { tool_call_id: "call-edit", name: "edit_file" }),
  );
  state = applyAgentEvent(
    state,
    event("file_changed", {
      path: "src/app.py",
      change_type: "modified",
      diff: "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
    }),
  );
  state = applyAgentEvent(state, event("turn_cancel_requested", {}));
  state = applyAgentEvent(
    state,
    event("turn_cancelled", {
      summary: {
        status: "cancelled",
        stop_reason: "cancelled",
        iterations: 2,
        tool_calls: 1,
        usage: { input_tokens: 8, output_tokens: 3, total_tokens: 11 },
        modified_files: ["src/app.py"],
        file_diffs: [
          {
            path: "src/app.py",
            change_type: "modified",
            diff: "--- a/src/app.py\n+++ b/src/app.py\n",
          },
        ],
        diff_complete: false,
        started_at: "2026-08-29T00:00:00Z",
        ended_at: "2026-08-29T00:00:03Z",
      },
    }),
  );

  expect(state.tools[0]).toMatchObject({
    status: "success",
    metadata: { path: "src/app.py", replacements: 1 },
  });
  expect(state.files).toEqual([
    expect.objectContaining({ path: "src/app.py", change_type: "modified" }),
  ]);
  expect(state.cancel_requested).toBe(false);
  expect(state.terminal).toMatchObject({
    status: "cancelled",
    iterations: 2,
    tool_calls: 1,
    diff_complete: false,
  });
});

test("keeps policy reason in the approval state until runtime resolves it", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  state = applyAgentEvent(
    state,
    event("approval_requested", {
      approval_id: "approval-1",
      execution_profile: "workspace_write",
      reason_code: "DESTRUCTIVE_COMMAND",
      message: "command requires approval",
      tool_call: { id: "call-rm", name: "exec_command", arguments: { command: "rm -rf build" } },
    }),
  );
  expect(state.approval).toEqual({
    approval_id: "approval-1",
    execution_profile: "workspace_write",
    reason_code: "DESTRUCTIVE_COMMAND",
    message: "command requires approval",
    tool_call: { id: "call-rm", name: "exec_command", arguments: { command: "rm -rf build" } },
  });
  state = applyAgentEvent(state, event("approval_resolved", {
    approval_id: "approval-1",
    resolution: "approved",
  }));
  expect(state.approval).toBeNull();
});

test("does not let a stale approval resolution clear a newer approval", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  state = applyAgentEvent(
    state,
    event("approval_requested", {
      approval_id: "approval-new",
      reason_code: "COMMAND_REQUIRES_APPROVAL",
      message: "command requires approval",
      tool_call: {
        id: "call-new",
        name: "run_command",
        arguments: { command: "echo safe" },
      },
    }),
  );
  state = applyAgentEvent(
    state,
    event("approval_resolved", {
      approval_id: "approval-old",
      resolution: "timeout",
    }),
  );
  expect(state.approval?.approval_id).toBe("approval-new");

  state = applyAgentEvent(
    state,
    {
      ...event("approval_resolved", {
        approval_id: "approval-new",
        resolution: "timeout",
      }),
      event_id: "approval-new-resolved",
    },
  );
  expect(state.approval).toBeNull();
});

test("ignores an approval resolution without a valid matching id", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  state = applyAgentEvent(
    state,
    event("approval_requested", {
      approval_id: "approval-active",
      reason_code: "COMMAND_REQUIRES_APPROVAL",
      message: "command requires approval",
      tool_call: {
        id: "call-active",
        name: "run_command",
        arguments: { command: "echo safe" },
      },
    }),
  );

  state = applyAgentEvent(
    state,
    event("approval_resolved", { resolution: "timeout" }),
  );
  expect(state.approval?.approval_id).toBe("approval-active");

  state = applyAgentEvent(
    state,
    event("approval_resolved", { approval_id: "" }),
  );
  expect(state.approval?.approval_id).toBe("approval-active");
});

test("hydrates a pending approval from the Thread snapshot", () => {
  const pending = {
    approval_id: "snapshot-approval",
    tool_call: {
      id: "snapshot-call",
      name: "write_file",
      arguments: { path: "src/app.py", content: "..." },
    },
    timeout_seconds: 30,
    reason_code: "WORKSPACE_WRITE",
    message: "writing a workspace file requires approval",
    execution_profile: "workspace_write",
  };
  const hydrated = hydrateThread({
    ...view(),
    snapshot: {
      ...view().snapshot,
      status: "waiting_approval",
      active_turn_id: "turn-snapshot",
      messages: [],
      latest_turn: null,
      pending_approval: pending,
    },
  });
  expect(hydrated.approval).toEqual(pending);
});

test("renders streaming deltas provisionally and clears them on canonical response", () => {
  let state = hydrateThread({
    ...view(),
    snapshot: { ...view().snapshot, messages: [], latest_turn: null },
  });
  state = applyAgentEvent(state, event("turn_started", { user_message: "Stream" }));
  state = applyAgentEvent(state, event("model_text_delta", { text: "生" }));
  state = applyAgentEvent(state, {
    ...event("model_tool_call_delta", {
      index: 0,
      id: "call",
      name: "read_file",
      arguments_delta: '{"path":',
    }),
    event_id: "delta-2",
  });
  expect(state.messages).toEqual([
    { id: "turn_started-id", role: "user", text: "Stream" },
  ]);
  expect(state.provisional).toEqual({
    turn_id: "turn-1",
    text: "生",
    reasoning: "",
    tool_calls: [
      { index: 0, id: "call", name: "read_file", arguments: '{"path":' },
    ],
    message_end: false,
  });

  state = applyAgentEvent(
    state,
    event("model_message_end", { finish_reason: "tool_calls" }),
  );
  expect(state.provisional?.message_end).toBe(true);
  state = applyAgentEvent(
    state,
    event("model_response", {
      message: {
        role: "assistant",
        content: [{ type: "text", text: "生" }],
      },
    }),
  );
  expect(state.provisional).toBeNull();
  expect(state.messages.at(-1)).toEqual({
    id: "model_response-id-text",
    role: "assistant",
    text: "生",
  });
});

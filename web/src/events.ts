import type { ThreadView } from "./api";

export type AgentEvent = {
  schema_version: number;
  event_id: string;
  thread_id: string;
  turn_id: string | null;
  sequence: number;
  type: string;
  timestamp: string;
  payload: Record<string, unknown>;
};

export type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
};

export type ToolActivity = {
  id: string;
  name: string;
  arguments: unknown;
  status: "requested" | "running" | "success" | "error";
  result: string | null;
  error_code: string | null;
  metadata?: Record<string, unknown>;
};

export type FileChange = {
  path: string;
  change_type: string;
  diff: string;
};

export type EventState = {
  messages: ConversationMessage[];
  tools: ToolActivity[];
  terminal: Record<string, unknown> | null;
  error: { code: string; message: string } | null;
  files: FileChange[];
  cancel_requested: boolean;
  seen_event_ids: string[];
};

export function hydrateThread(view: ThreadView): EventState {
  const state: EventState = {
    messages: [],
    tools: [],
    terminal: view.snapshot.latest_turn,
    error: null,
    files: [],
    cancel_requested: false,
    seen_event_ids: [],
  };
  view.snapshot.messages.forEach((message, index) => {
    hydrateMessage(state, message, `snapshot-${index}`);
  });
  const latestError = view.snapshot.latest_turn?.error;
  if (isRecord(latestError)) {
    state.error = safeError(latestError);
  }
  if (isRecord(view.host_error)) {
    state.error = safeError(view.host_error);
    state.terminal = { status: "rejected", error: state.error };
  }
  hydrateSummaryFiles(state, view.snapshot.latest_turn);
  return state;
}

export function applyAgentEvent(state: EventState, event: AgentEvent): EventState {
  if (state.seen_event_ids.includes(event.event_id)) {
    return state;
  }
  const next: EventState = {
    messages: [...state.messages],
    tools: state.tools.map((tool) => ({ ...tool })),
    terminal: state.terminal,
    error: state.error,
    files: state.files.map((file) => ({ ...file })),
    cancel_requested: state.cancel_requested,
    seen_event_ids: [...state.seen_event_ids, event.event_id],
  };

  if (event.type === "turn_started") {
    next.terminal = null;
    next.error = null;
    next.files = [];
    next.cancel_requested = false;
    const text = event.payload.user_message;
    if (typeof text === "string") {
      next.messages.push({ id: event.event_id, role: "user", text });
    }
  } else if (event.type === "model_response") {
    const message = event.payload.message;
    if (isRecord(message)) {
      hydrateMessage(next, message, event.event_id);
    }
  } else if (event.type === "tool_requested") {
    const call = event.payload.tool_call;
    if (isRecord(call) && typeof call.id === "string") {
      mergeTool(next, {
        id: call.id,
        name: typeof call.name === "string" ? call.name : "tool",
        arguments: call.arguments,
        status: "requested",
        result: null,
        error_code: null,
      });
    }
  } else if (event.type === "tool_started") {
    const id = event.payload.tool_call_id;
    if (typeof id === "string") {
      mergeTool(next, {
        id,
        name: typeof event.payload.name === "string" ? event.payload.name : "tool",
        arguments: null,
        status: "running",
        result: null,
        error_code: null,
      });
    }
  } else if (event.type === "tool_finished") {
    const result = event.payload.result;
    if (isRecord(result) && typeof result.tool_call_id === "string") {
      mergeTool(next, {
        id: result.tool_call_id,
        name: typeof event.payload.name === "string" ? event.payload.name : "tool",
        arguments: null,
        status: result.ok === true ? "success" : "error",
        result: typeof result.content === "string" ? result.content : null,
        error_code:
          typeof result.error_code === "string" ? result.error_code : null,
        ...(isRecord(result.metadata) ? { metadata: result.metadata } : {}),
      });
    }
  } else if (event.type === "file_changed") {
    const change = safeFileChange(event.payload);
    if (change !== null) {
      mergeFile(next, change);
    }
  } else if (event.type === "turn_cancel_requested") {
    next.cancel_requested = true;
  } else if (
    event.type === "turn_completed" ||
    event.type === "turn_cancelled" ||
    event.type === "turn_failed" ||
    event.type === "turn_limit_reached"
  ) {
    const summary = event.payload.summary;
    if (isRecord(summary)) {
      next.terminal = summary;
      next.cancel_requested = false;
      hydrateSummaryFiles(next, summary);
      if (isRecord(summary.error)) {
        next.error = safeError(summary.error);
      }
    }
  } else if (event.type === "turn_rejected") {
    const error = event.payload.error;
    next.error = isRecord(error)
      ? safeError(error)
      : { code: "TURN_REJECTED", message: "Turn could not start" };
    next.terminal = { status: "rejected", error: next.error };
    next.cancel_requested = false;
  }
  return next;
}

function hydrateMessage(
  state: EventState,
  message: Record<string, unknown>,
  idPrefix: string,
) {
  const role = message.role;
  const content = message.content;
  if (!Array.isArray(content)) {
    return;
  }
  if (role === "user" || role === "assistant") {
    const text = content
      .filter(isRecord)
      .filter((block) => block.type === "text" && typeof block.text === "string")
      .map((block) => block.text as string)
      .join("");
    if (text) {
      state.messages.push({ id: `${idPrefix}-text`, role, text });
    }
  }
  content.filter(isRecord).forEach((block) => {
    if (block.type === "tool_call" && typeof block.id === "string") {
      mergeTool(state, {
        id: block.id,
        name: typeof block.name === "string" ? block.name : "tool",
        arguments: block.arguments,
        status: "requested",
        result: null,
        error_code: null,
      });
    }
    if (block.type === "tool_result" && typeof block.tool_call_id === "string") {
      mergeTool(state, {
        id: block.tool_call_id,
        name: "tool",
        arguments: null,
        status: block.ok === true ? "success" : "error",
        result: typeof block.content === "string" ? block.content : null,
        error_code:
          typeof block.error_code === "string" ? block.error_code : null,
        ...(isRecord(block.metadata) ? { metadata: block.metadata } : {}),
      });
    }
  });
}

function mergeTool(state: EventState, incoming: ToolActivity) {
  const index = state.tools.findIndex((tool) => tool.id === incoming.id);
  if (index === -1) {
    state.tools.push(incoming);
    return;
  }
  const existing = state.tools[index]!;
  const status =
    TOOL_STATUS_RANK[incoming.status] < TOOL_STATUS_RANK[existing.status]
      ? existing.status
      : incoming.status;
  const metadata = incoming.metadata ?? existing.metadata;
  state.tools[index] = {
    ...existing,
    ...incoming,
    name: incoming.name === "tool" ? existing.name : incoming.name,
    arguments: incoming.arguments ?? existing.arguments,
    result: incoming.result ?? existing.result,
    error_code: incoming.error_code ?? existing.error_code,
    status,
    ...(metadata === undefined ? {} : { metadata }),
  };
}

const TOOL_STATUS_RANK: Record<ToolActivity["status"], number> = {
  requested: 0,
  running: 1,
  success: 2,
  error: 2,
};

function hydrateSummaryFiles(
  state: EventState,
  summary: Record<string, unknown> | null,
) {
  if (!isRecord(summary) || !Array.isArray(summary.file_diffs)) {
    return;
  }
  summary.file_diffs.filter(isRecord).forEach((value) => {
    const change = safeFileChange(value);
    if (change !== null) {
      mergeFile(state, change);
    }
  });
}

function safeFileChange(value: Record<string, unknown>): FileChange | null {
  if (typeof value.path !== "string") {
    return null;
  }
  return {
    path: value.path,
    change_type:
      typeof value.change_type === "string" ? value.change_type : "modified",
    diff: typeof value.diff === "string" ? value.diff : "",
  };
}

function mergeFile(state: EventState, incoming: FileChange) {
  const index = state.files.findIndex((file) => file.path === incoming.path);
  if (index === -1) {
    state.files.push(incoming);
  } else {
    state.files[index] = incoming;
  }
}

function safeError(value: Record<string, unknown>) {
  return {
    code: typeof value.code === "string" ? value.code : "RUNTIME_ERROR",
    message: typeof value.message === "string" ? value.message : "Turn failed",
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

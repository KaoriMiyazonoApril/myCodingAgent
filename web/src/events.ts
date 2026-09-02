import type { SkillMetadata, ThreadSkills, ThreadView } from "./api";

const SNAPSHOT_REFRESH_EVENT_TYPES = new Set([
  "turn_completed",
  "turn_cancelled",
  "turn_failed",
  "turn_limit_reached",
  "turn_rejected",
]);

export function eventRequiresSnapshotRefresh(type: string): boolean {
  return SNAPSHOT_REFRESH_EVENT_TYPES.has(type);
}

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

export type ReasoningPreview = {
  text: string;
  truncated?: boolean;
  total_chars?: number;
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

export type ProvisionalToolCall = {
  index: number;
  id: string | null;
  name: string | null;
  arguments: string;
};

export type ProvisionalAssistant = {
  turn_id: string | null;
  text: string;
  /** Whether the transient narration hit the client-side display cap. */
  text_truncated: boolean;
  tool_calls: ProvisionalToolCall[];
  message_end: boolean;
};

// A streamed model narration is progress feedback, not durable conversation
// content. Keep it small even if a provider emits a very long text delta.
export const PROVISIONAL_TEXT_MAX_CHARS = 320;

// Phase-change-only model activity (normal Web never receives raw reasoning).
// The current public vocabulary is thinking/generating/idle.  writing/acting
// are accepted as legacy aliases because older Hosts may still emit them.
export type ActivityPhase =
  | "thinking"
  | "generating"
  | "idle"
  | "writing"
  | "acting";

export type ActivityState = {
  phase: ActivityPhase;
  /** ISO timestamp when the phase began (the announcing event's timestamp). */
  since: string;
  /** True once the model call ended ("idle"); UI freezes the duration. */
  finished: boolean;
  /** ISO timestamp of the terminating "idle" event, when finished. */
  ended_at?: string;
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
  error: { code: string; message: string; detail?: string } | null;
  files: FileChange[];
  cancel_requested: boolean;
  approval: ApprovalRequest | null;
  provisional: ProvisionalAssistant | null;
  activity: ActivityState | null;
  /** Current Turn boundary used to reject stale SSE deltas and tool events. */
  current_turn_id: string | null;
  seen_event_ids: string[];
  skills: ThreadSkills;
};

export type ApprovalRequest = {
  approval_id: string;
  tool_call: Record<string, unknown> | null;
  timeout_seconds?: number;
  decision?: string;
  execution_profile?: string;
  reason_code: string;
  message: string;
};

/**
 * Reconcile only approval metadata after an approval command is accepted.
 *
 * The authoritative Thread read is still needed to handle races, but it must
 * not rebuild the durable message/tool projection while the Turn is running.
 * Reconnect/cursor recovery continues to use hydrateThread instead.
 */
export function reconcileApprovalSnapshot(
  state: EventState,
  view: ThreadView,
): EventState {
  const pending = safeApproval(view.snapshot.pending_approval);
  const currentTurn =
    typeof view.snapshot.active_turn_id === "string"
      ? view.snapshot.active_turn_id
      : view.snapshot.status === "idle" || view.snapshot.status === "closed"
        ? null
        : state.current_turn_id;
  const next: EventState = {
    ...state,
    tools: state.tools.map((tool) => ({ ...tool })),
    approval: pending,
    current_turn_id: currentTurn,
  };
  const call = pending?.tool_call;
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
  return next;
}

export function hydrateThread(view: ThreadView): EventState {
  const state: EventState = {
    messages: [],
    tools: [],
    terminal: view.snapshot.latest_turn,
    error: null,
    files: [],
    cancel_requested: false,
    approval: null,
    provisional: null,
    activity: null,
    current_turn_id:
      typeof view.snapshot.active_turn_id === "string"
        ? view.snapshot.active_turn_id
        : null,
    seen_event_ids: [],
    skills: normalizeSkills(view.snapshot.skills),
  };
  view.snapshot.messages.forEach((message, index) => {
    hydrateMessage(state, message, `snapshot-${index}`);
  });
  // A Snapshot contains the entire canonical conversation, but the activity
  // rail is intentionally scoped to the latest/current Turn.  Message roles
  // are the only stable boundary available on the existing wire contract: the
  // latest user message starts the latest Turn, followed by its assistant and
  // tool messages.  Do not flatten tool artifacts from older Turns here.
  state.tools = [];
  const latestTurnStart = latestTurnMessageStart(view.snapshot.messages);
  if (latestTurnStart !== -1) {
    view.snapshot.messages.slice(latestTurnStart).forEach((message) => {
      hydrateToolBlocks(state, message);
    });
  }
  const latestError = view.snapshot.latest_turn?.error;
  if (isRecord(latestError)) {
    state.error = safeError(latestError);
  }
  if (isRecord(view.host_error)) {
    state.error = safeError(view.host_error);
    state.terminal = { status: "rejected", error: state.error };
  }
  const pending = safeApproval(view.snapshot.pending_approval);
  if (pending !== null) {
    state.approval = pending;
    // The pending call is part of the current Turn even when a Snapshot was
    // taken before its durable tool result.  Keep one compact requested row so
    // the approval card and activity list describe the same operation.
    const call = pending.tool_call;
    if (isRecord(call) && typeof call.id === "string") {
      mergeTool(state, {
        id: call.id,
        name: typeof call.name === "string" ? call.name : "tool",
        arguments: call.arguments,
        status: "requested",
        result: null,
        error_code: null,
      });
    }
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
    approval: state.approval,
    provisional:
      state.provisional === null
        ? null
        : {
            ...state.provisional,
            tool_calls: state.provisional.tool_calls.map((call) => ({ ...call })),
          },
    activity: state.activity,
    current_turn_id: state.current_turn_id,
    seen_event_ids: [...state.seen_event_ids, event.event_id],
    skills: {
      schema_version: state.skills.schema_version,
      available: [...state.skills.available],
      loaded: [...state.skills.loaded],
      diagnostics: state.skills.diagnostics.map((item) => ({ ...item })),
    },
  };

  if (event.type === "turn_started") {
    next.current_turn_id = event.turn_id;
    next.terminal = null;
    next.error = null;
    next.files = [];
    next.cancel_requested = false;
    next.approval = null;
    // Tool activity belongs to one Turn, never to the whole Thread.  The
    // next Turn starts with an empty working set even though canonical
    // messages remain durable below.
    next.tools = [];
    // Loaded Skills are turn-local.  Keep the complete available catalog but
    // never carry a prior Turn's bodies/metadata into the next execution.
    next.skills.available = [
      ...next.skills.available,
      ...next.skills.loaded.filter(
        (loaded) => !next.skills.available.some((available) => available.name === loaded.name),
      ).map((loaded) => {
        const catalog = { ...loaded };
        delete catalog.activation_source;
        delete catalog.placement;
        return catalog;
      }),
    ];
    next.skills.loaded = [];
    next.provisional = {
      turn_id: event.turn_id,
      text: "",
      text_truncated: false,
      tool_calls: [],
      message_end: false,
    };
    next.activity = null;
    const text = event.payload.user_message;
    if (typeof text === "string") {
      next.messages.push({ id: event.event_id, role: "user", text });
    }
  } else if (event.type === "model_activity") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const phase = event.payload.phase;
    const normalizedPhase = normalizeActivityPhase(phase);
    if (normalizedPhase !== null && normalizedPhase !== "idle") {
      // Phase-change-only semantics: a stray duplicate of the live phase must
      // not restart the duration timer.
      if (next.activity?.phase !== normalizedPhase || next.activity.finished) {
        next.activity = {
          phase: normalizedPhase,
          since: event.timestamp,
          finished: false,
        };
      }
    } else if (normalizedPhase === "idle" && next.activity !== null) {
      next.activity = {
        ...next.activity,
        phase: "idle",
        finished: true,
        ended_at: event.timestamp,
      };
    }
  } else if (event.type === "model_response") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    next.provisional = null;
    const message = event.payload.message;
    if (isRecord(message)) {
      // Deliberately ignore reasoning_preview in the normal Web projection.
      // A stale/overly-permissive Host must not turn diagnostic text into a
      // user-visible message.
      hydrateMessage(next, message, event.event_id);
    }
  } else if (event.type === "model_text_delta") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const provisional = ensureProvisional(next, event.turn_id);
    if (typeof event.payload.text === "string") {
      const remaining = PROVISIONAL_TEXT_MAX_CHARS - provisional.text.length;
      if (remaining <= 0) {
        provisional.text_truncated = true;
      } else if (event.payload.text.length > remaining) {
        provisional.text += event.payload.text.slice(0, remaining);
        provisional.text_truncated = true;
      } else {
        provisional.text += event.payload.text;
      }
    }
  } else if (event.type === "model_reasoning_delta") {
    // Raw reasoning is never retained by the normal Web reducer.  The
    // phase-only model_activity event is the complete public signal.
    return next;
  } else if (event.type === "model_tool_call_delta") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const provisional = ensureProvisional(next, event.turn_id);
    const index = event.payload.index;
    if (typeof index === "number" && Number.isInteger(index) && index >= 0) {
      let call = provisional.tool_calls.find((item) => item.index === index);
      if (call === undefined) {
        call = { index, id: null, name: null, arguments: "" };
        provisional.tool_calls.push(call);
      }
      if (typeof event.payload.id === "string" && event.payload.id) {
        call.id = event.payload.id;
      }
      if (typeof event.payload.name === "string" && event.payload.name) {
        call.name = event.payload.name;
      }
      if (typeof event.payload.arguments_delta === "string") {
        call.arguments += event.payload.arguments_delta;
      }
    }
  } else if (event.type === "model_message_end") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    ensureProvisional(next, event.turn_id).message_end = true;
    // Hosts normally announce this transition immediately before the message
    // end event. Keep the reducer correct when an older/replayed Host only
    // sends the durable boundary event.
    if (next.activity !== null && !next.activity.finished) {
      next.activity = {
        ...next.activity,
        phase: "idle",
        finished: true,
        ended_at: event.timestamp,
      };
    }
  } else if (event.type === "model_error") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    next.error = {
      code:
        typeof event.payload.error_code === "string"
          ? event.payload.error_code
          : "MODEL_STREAM_ERROR",
      message:
        typeof event.payload.message === "string"
          ? event.payload.message
          : "模型流式响应失败",
    };
    if (next.activity !== null && !next.activity.finished) {
      next.activity = {
        ...next.activity,
        phase: "idle",
        finished: true,
        ended_at: event.timestamp,
      };
    }
  } else if (event.type === "tool_requested") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
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
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
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
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
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
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const change = safeFileChange(event.payload);
    if (change !== null) {
      mergeFile(next, change);
    }
  } else if (event.type === "skill_loaded") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const skill = safeSkill(event.payload);
    if (skill !== null && !next.skills.loaded.some((item) => item.name === skill.name)) {
      next.skills.loaded.push(skill);
      next.skills.available = next.skills.available.filter(
        (item) => item.name !== skill.name,
      );
    }
  } else if (event.type === "skill_activation_failed") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const name = event.payload.name;
    if (typeof name === "string" && name.trim()) {
      next.skills.diagnostics.push({
        code:
          typeof event.payload.error_code === "string"
            ? event.payload.error_code
            : "SKILL_NOT_FOUND",
        name,
      });
    }
  } else if (event.type === "turn_cancel_requested") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    next.cancel_requested = true;
  } else if (event.type === "approval_requested") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const approvalId = event.payload.approval_id;
    if (typeof approvalId === "string") {
      next.approval = {
        approval_id: approvalId,
        tool_call: isRecord(event.payload.tool_call)
          ? event.payload.tool_call
          : null,
        ...(typeof event.payload.timeout_seconds === "number"
          ? { timeout_seconds: event.payload.timeout_seconds }
          : {}),
        ...(typeof event.payload.decision === "string"
          ? { decision: event.payload.decision }
          : {}),
        ...(typeof event.payload.execution_profile === "string"
          ? { execution_profile: event.payload.execution_profile }
          : {}),
        reason_code:
          typeof event.payload.reason_code === "string"
            ? event.payload.reason_code
            : "APPROVAL_REQUIRED",
        message:
          typeof event.payload.message === "string"
            ? event.payload.message
            : "该命令需要确认",
      };
      const call = next.approval.tool_call;
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
    }
  } else if (event.type === "approval_resolved") {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const approvalId = event.payload.approval_id;
    // Resolution events can race with a replacement approval or arrive from
    // a replayed/late SSE connection.  Only the active matching ID may clear
    // the actionable card.
    if (
      typeof approvalId === "string" &&
      approvalId.length > 0 &&
      typeof next.approval?.approval_id === "string" &&
      next.approval.approval_id.length > 0 &&
      next.approval.approval_id === approvalId
    ) {
      next.approval = null;
    }
  } else if (
    event.type === "turn_completed" ||
    event.type === "turn_cancelled" ||
    event.type === "turn_failed" ||
    event.type === "turn_limit_reached"
  ) {
    if (!eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const summary = event.payload.summary;
    next.terminal = isRecord(summary) ? summary : null;
    next.provisional = null;
    next.cancel_requested = false;
    next.approval = null;
    next.current_turn_id = null;
    if (next.activity !== null && !next.activity.finished) {
      next.activity = {
        ...next.activity,
        phase: "idle",
        finished: true,
        ended_at: event.timestamp,
      };
    }
    if (isRecord(summary)) {
      hydrateSummaryFiles(next, summary);
      if (isRecord(summary.error)) {
        next.error = safeError(summary.error);
      }
    }
  } else if (event.type === "turn_rejected") {
    // A preflight rejection has no preceding turn_started event, so it is
    // accepted while idle. Once another Turn is active, reject a stale
    // rejection just like any other turn-scoped event.
    if (next.current_turn_id !== null && !eventBelongsToCurrentTurn(next, event)) {
      return next;
    }
    const error = event.payload.error;
    next.error = isRecord(error)
      ? safeError(error)
      : { code: "TURN_REJECTED", message: "Turn could not start" };
    next.terminal = { status: "rejected", error: next.error };
    next.provisional = null;
    next.cancel_requested = false;
    next.approval = null;
    next.current_turn_id = null;
  }
  return next;
}

function ensureProvisional(
  state: EventState,
  turnId: string | null,
): ProvisionalAssistant {
  if (state.provisional === null) {
    state.provisional = {
      turn_id: turnId,
      text: "",
      text_truncated: false,
      tool_calls: [],
      message_end: false,
    };
  }
  return state.provisional;
}

function eventBelongsToCurrentTurn(state: EventState, event: AgentEvent): boolean {
  return (
    state.current_turn_id !== null &&
    event.turn_id !== null &&
    state.current_turn_id === event.turn_id
  );
}

function safeApproval(value: unknown): ApprovalRequest | null {
  if (!isRecord(value) || typeof value.approval_id !== "string" || !value.approval_id) {
    return null;
  }
  return {
    approval_id: value.approval_id,
    tool_call: isRecord(value.tool_call) ? value.tool_call : null,
    ...(typeof value.timeout_seconds === "number"
      ? { timeout_seconds: value.timeout_seconds }
      : {}),
    ...(typeof value.decision === "string" ? { decision: value.decision } : {}),
    ...(typeof value.execution_profile === "string"
      ? { execution_profile: value.execution_profile }
      : {}),
    reason_code:
      typeof value.reason_code === "string"
        ? value.reason_code
        : "APPROVAL_REQUIRED",
    message:
      typeof value.message === "string" ? value.message : "该命令需要确认",
  };
}

function normalizeActivityPhase(value: unknown): ActivityPhase | null {
  if (value === "thinking") {
    return "thinking";
  }
  if (value === "generating") {
    return "generating";
  }
  if (value === "writing" || value === "acting") {
    return "generating";
  }
  if (value === "idle") {
    return "idle";
  }
  return null;
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
    const hasToolCall = content.some(
      (block) => isRecord(block) && block.type === "tool_call",
    );
    const text = content
      .filter(isRecord)
      .filter((block) => block.type === "text" && typeof block.text === "string")
      .map((block) => block.text as string)
      .join("");
    // An assistant message containing a ToolCall is an intermediate model
    // step. Its text is narration for that step, not an ordinary answer.
    if (text && (role === "user" || !hasToolCall)) {
      state.messages.push({
        id: `${idPrefix}-text`,
        role,
        text,
      });
    }
  }
  hydrateToolBlocks(state, message);
}

function hydrateToolBlocks(
  state: EventState,
  message: Record<string, unknown>,
) {
  const content = message.content;
  if (!Array.isArray(content)) {
    return;
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

function latestTurnMessageStart(messages: Array<Record<string, unknown>>): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === "user") {
      return index;
    }
  }
  return -1;
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
  const error: { code: string; message: string; detail?: string } = {
    code: typeof value.code === "string" ? value.code : "RUNTIME_ERROR",
    message: typeof value.message === "string" ? value.message : "Turn failed",
  };
  if (typeof value.detail === "string" && value.detail) {
    error.detail = value.detail;
  }
  return error;
}

function normalizeSkills(value: unknown): ThreadSkills {
  if (!isRecord(value)) {
    return { schema_version: 1, available: [], loaded: [], diagnostics: [] };
  }
  const available = Array.isArray(value.available)
    ? value.available.map(safeSkill).filter((item): item is SkillMetadata => item !== null)
    : [];
  const loaded = Array.isArray(value.loaded)
    ? value.loaded.map(safeSkill).filter((item): item is SkillMetadata => item !== null)
    : [];
  const diagnostics = Array.isArray(value.diagnostics)
    ? value.diagnostics.filter(isRecord).map((item) => ({ ...item }))
    : [];
  return {
    schema_version: typeof value.schema_version === "number" ? value.schema_version : 1,
    available,
    loaded,
    diagnostics,
  };
}

function safeSkill(value: unknown): SkillMetadata | null {
  if (!isRecord(value)) {
    return null;
  }
  if (
    typeof value.name !== "string" ||
    typeof value.description !== "string" ||
    typeof value.source !== "string" ||
    typeof value.source_path !== "string"
  ) {
    return null;
  }
  return {
    name: value.name,
    description: value.description,
    source: value.source,
    source_path: value.source_path,
    directory:
      typeof value.directory === "string" ? value.directory : value.source_path,
    ...(value.activation_source === "explicit" || value.activation_source === "tool"
      ? { activation_source: value.activation_source }
      : {}),
    ...(value.placement === "working_tail" || value.placement === "tool_history"
      ? { placement: value.placement }
      : {}),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

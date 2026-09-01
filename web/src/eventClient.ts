import type { ThreadView } from "./api";
import type { AgentEvent } from "./events";

type EventSourceLike = {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
};

type EventClientCallbacks = {
  onEvent: (event: AgentEvent) => void;
  onSnapshot: (thread: ThreadView) => void;
  onConnection: (state: "connected" | "disconnected") => void;
  recover: () => Promise<ThreadView>;
  onError: (message: string) => void;
};

type EventClientOptions = {
  eventSourceFactory?: (url: string) => EventSourceLike;
  setTimer?: (callback: () => void, delay: number) => number;
  clearTimer?: (timer: number) => void;
};

const EVENT_TYPES = [
  "turn_started",
  "model_text_delta",
  "model_reasoning_delta",
  "model_tool_call_delta",
  "model_message_end",
  "model_error",
  "model_response",
  "tool_requested",
  "tool_started",
  "tool_finished",
  "approval_requested",
  "approval_resolved",
  "command_started",
  "command_output_delta",
  "command_completed",
  "file_changed",
  "settings_updated",
  "skill_loaded",
  "skill_activation_failed",
  "turn_cancel_requested",
  "thread_close_requested",
  "turn_completed",
  "turn_cancelled",
  "turn_failed",
  "turn_limit_reached",
  "turn_rejected",
  "snapshot",
];

export class ThreadEventClient {
  private source: EventSourceLike | null = null;
  private timer: number | null = null;
  private stopped = false;
  private generation = 0;
  private attempts = 0;
  private cursor: string | null;
  private readonly factory: (url: string) => EventSourceLike;
  private readonly setTimer: (callback: () => void, delay: number) => number;
  private readonly clearTimer: (timer: number) => void;

  constructor(
    private readonly threadId: string,
    initialCursor: string | null,
    private readonly callbacks: EventClientCallbacks,
    options: EventClientOptions = {},
  ) {
    this.cursor = initialCursor;
    this.factory =
      options.eventSourceFactory ??
      ((url) => new EventSource(url) as unknown as EventSourceLike);
    this.setTimer = options.setTimer ?? window.setTimeout.bind(window);
    this.clearTimer = options.clearTimer ?? window.clearTimeout.bind(window);
  }

  start() {
    this.stopped = false;
    this.open();
  }

  stop() {
    this.stopped = true;
    this.generation += 1;
    this.source?.close();
    this.source = null;
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
  }

  private open() {
    if (this.stopped) {
      return;
    }
    const query =
      this.cursor === null ? "" : `?after_event_id=${encodeURIComponent(this.cursor)}`;
    const source = this.factory(`/api/threads/${this.threadId}/events${query}`);
    const generation = ++this.generation;
    this.source = source;
    source.onopen = () => {
      if (this.stopped || this.source !== source || generation !== this.generation) {
        return;
      }
      this.attempts = 0;
      this.callbacks.onConnection("connected");
    };
    EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (message) =>
        this.receive(type, message, source, generation),
      );
    });
    source.onerror = () => {
      if (
        this.stopped ||
        this.source !== source ||
        generation !== this.generation
      ) {
        return;
      }
      source.close();
      // Invalidate this EventSource immediately. The browser may still flush
      // callbacks queued by a closed source while Snapshot recovery and the
      // reconnect timer are in flight; those callbacks must not mutate the
      // newer Thread generation.
      this.source = null;
      const recoveryGeneration = ++this.generation;
      this.callbacks.onConnection("disconnected");
      void this.recoverThenReconnect(recoveryGeneration);
    };
  }

  private async recoverThenReconnect(recoveryGeneration: number) {
    try {
      const thread = await this.callbacks.recover();
      if (!this.recoveryIsCurrent(recoveryGeneration)) {
        return;
      }
      this.cursor = thread.event_cursor;
      this.callbacks.onSnapshot(thread);
    } catch {
      if (!this.recoveryIsCurrent(recoveryGeneration)) {
        return;
      }
      this.callbacks.onError("Unable to recover Thread snapshot");
    }
    if (!this.recoveryIsCurrent(recoveryGeneration)) {
      return;
    }
    const delay = Math.min(500 * 2 ** this.attempts, 8_000);
    this.attempts += 1;
    this.timer = this.setTimer(() => {
      this.timer = null;
      if (this.recoveryIsCurrent(recoveryGeneration)) {
        this.open();
      }
    }, delay);
  }

  private recoveryIsCurrent(recoveryGeneration: number) {
    return (
      !this.stopped &&
      this.source === null &&
      recoveryGeneration === this.generation
    );
  }

  private receive(
    type: string,
    message: MessageEvent<string>,
    source: EventSourceLike,
    generation: number,
  ) {
    if (
      this.stopped ||
      this.source !== source ||
      generation !== this.generation
    ) {
      return;
    }
    try {
      const payload = JSON.parse(message.data) as Record<string, unknown>;
      if (type === "snapshot") {
        const thread = payload.thread as ThreadView;
        this.cursor = typeof payload.cursor === "string" ? payload.cursor : null;
        this.callbacks.onSnapshot(thread);
        return;
      }
      const event = payload as AgentEvent;
      this.cursor = event.event_id || message.lastEventId || this.cursor;
      this.callbacks.onEvent(event);
    } catch {
      this.callbacks.onError("Received an invalid Agent event");
    }
  }
}

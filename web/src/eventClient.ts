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
  "file_changed",
  "settings_updated",
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
    this.source = source;
    source.onopen = () => {
      this.attempts = 0;
      this.callbacks.onConnection("connected");
    };
    EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (message) => this.receive(type, message));
    });
    source.onerror = () => {
      if (this.stopped) {
        return;
      }
      source.close();
      this.callbacks.onConnection("disconnected");
      void this.callbacks
        .recover()
        .then((thread) => {
          if (!this.stopped) {
            this.cursor = thread.event_cursor;
            this.callbacks.onSnapshot(thread);
          }
        })
        .catch(() => this.callbacks.onError("Unable to recover Thread snapshot"));
      const delay = Math.min(500 * 2 ** this.attempts, 8_000);
      this.attempts += 1;
      this.timer = this.setTimer(() => this.open(), delay);
    };
  }

  private receive(type: string, message: MessageEvent<string>) {
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

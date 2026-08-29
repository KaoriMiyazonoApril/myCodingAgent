import { expect, test, vi } from "vitest";

import type { ThreadView } from "./api";
import { ThreadEventClient } from "./eventClient";

class FakeEventSource {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data: object, lastEventId = "") {
    this.listeners.get(type)?.({
      data: JSON.stringify(data),
      lastEventId,
    } as MessageEvent<string>);
  }
}

function recoveredView(cursor: string | null): ThreadView {
  return {
    schema_version: 1,
    snapshot: {
      schema_version: 1,
      thread_id: "thread-1",
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
        version: 0,
      },
      messages: [],
      created_at: "",
      updated_at: "",
      latest_turn: null,
    },
    event_cursor: cursor,
    submission: null,
  };
}

test("connects after the hydration cursor and forwards events", () => {
  const sources: FakeEventSource[] = [];
  const onEvent = vi.fn();
  const client = new ThreadEventClient(
    "thread-1",
    "cursor 1",
    {
      onEvent,
      onSnapshot: vi.fn(),
      onConnection: vi.fn(),
      recover: async () => recoveredView("recovered"),
      onError: vi.fn(),
    },
    {
      eventSourceFactory: (url) => {
        const source = new FakeEventSource(url);
        sources.push(source);
        return source;
      },
    },
  );

  client.start();
  sources[0]!.emit("turn_started", {
    event_id: "event-2",
    type: "turn_started",
    payload: { user_message: "hello" },
  });
  client.stop();

  expect(sources[0]!.url).toBe(
    "/api/threads/thread-1/events?after_event_id=cursor%201",
  );
  expect(onEvent).toHaveBeenCalledWith(
    expect.objectContaining({ event_id: "event-2", type: "turn_started" }),
  );
  expect(sources[0]!.closed).toBe(true);
});

test("recovers Snapshot state and reconnects with bounded backoff", async () => {
  const sources: FakeEventSource[] = [];
  const timers: Array<{ callback: () => void; delay: number }> = [];
  const onSnapshot = vi.fn();
  const onConnection = vi.fn();
  const client = new ThreadEventClient(
    "thread-1",
    null,
    {
      onEvent: vi.fn(),
      onSnapshot,
      onConnection,
      recover: async () => recoveredView("cursor-recovered"),
      onError: vi.fn(),
    },
    {
      eventSourceFactory: (url) => {
        const source = new FakeEventSource(url);
        sources.push(source);
        return source;
      },
      setTimer: (callback, delay) => {
        timers.push({ callback, delay });
        return timers.length;
      },
      clearTimer: vi.fn(),
    },
  );

  client.start();
  sources[0]!.onerror?.(new Event("error"));
  await Promise.resolve();
  await Promise.resolve();
  timers[0]!.callback();

  expect(onConnection).toHaveBeenCalledWith("disconnected");
  expect(onSnapshot).toHaveBeenCalledWith(
    expect.objectContaining({ event_cursor: "cursor-recovered" }),
  );
  expect(timers[0]!.delay).toBe(500);
  expect(sources[1]!.url).toBe(
    "/api/threads/thread-1/events?after_event_id=cursor-recovered",
  );
  client.stop();
});

test("applies an in-stream snapshot recovery without discarding it", () => {
  const sources: FakeEventSource[] = [];
  const onSnapshot = vi.fn();
  const client = new ThreadEventClient(
    "thread-1",
    "expired",
    {
      onEvent: vi.fn(),
      onSnapshot,
      onConnection: vi.fn(),
      recover: async () => recoveredView(null),
      onError: vi.fn(),
    },
    {
      eventSourceFactory: (url) => {
        const source = new FakeEventSource(url);
        sources.push(source);
        return source;
      },
    },
  );

  client.start();
  const recovered = recoveredView(null);
  sources[0]!.emit("snapshot", { thread: recovered, cursor: null });
  client.stop();

  expect(onSnapshot).toHaveBeenCalledWith(recovered);
});

import { useCallback, useEffect, useState } from "react";

import {
  cancelTurn,
  closeThread,
  createThread,
  clearProviderCredential,
  discoverModels,
  getProviders,
  getThread,
  getThreads,
  getWorkspaces,
  saveProvider,
  selectProviderDefault,
  startTurn,
  updateThreadSettings,
  type ProviderView,
  type ThreadView,
  type WorkspaceListing,
} from "./api";
import { ThreadEventClient } from "./eventClient";
import { applyAgentEvent, hydrateThread } from "./events";
import "./styles.css";

const TERMINAL_EVENT_TYPES = new Set([
  "turn_completed",
  "turn_cancelled",
  "turn_failed",
  "turn_limit_reached",
]);

export function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<string | null>(null);
  const [showProviderSettings, setShowProviderSettings] = useState(false);

  useEffect(() => {
    let active = true;
    void getProviders()
      .then((response) => {
        if (active) {
          setProviders(response.providers);
          setError(null);
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : "Host is unavailable");
        }
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const providerReady = providers.some(
    (provider) =>
      provider.is_default && provider.configured && provider.selected_model !== null,
  );
  const showSetup = !providerReady || showProviderSettings || editing !== null;

  return (
    <main className={providerReady ? "app-shell" : "setup-shell"}>
      <header className="product-bar">
        <span className="product-mark" aria-hidden="true">
          &gt;_
        </span>
        <div>
          <p className="eyebrow">LOCAL RUNTIME</p>
          <p className="product-name">Agent</p>
        </div>
        <div className="product-actions">
          {providerReady ? (
            <button
              type="button"
              className="quiet-button provider-settings-toggle"
              aria-expanded={showSetup}
              onClick={() => {
                setEditing(null);
                setShowProviderSettings((current) => !current);
              }}
            >
              Provider settings
            </button>
          ) : null}
          <span className="host-badge">127.0.0.1</span>
        </div>
      </header>

      {showSetup ? (
      <section
        className={providerReady ? "provider-drawer" : "setup-panel"}
        aria-labelledby="provider-heading"
      >
        <div className="setup-copy">
          <p className="step-label">STEP 01 · MODEL ACCESS</p>
          <h1 id="provider-heading">Connect a model provider</h1>
          <p className="lede">
            Provider setup required before creating a thread. Credentials stay in
            this Host and are never returned to the browser.
          </p>
        </div>

        {loading ? <p className="status-line">Loading Host configuration…</p> : null}
        {error ? (
          <div className="error-banner" role="alert">
            <strong>Host connection failed</strong>
            <span>{error}</span>
          </div>
        ) : null}

        <div className="provider-grid" aria-busy={loading}>
          {providers.map((provider) => (
            <article className="provider-card" key={provider.provider_id}>
              <div>
                <p className="provider-name">{provider.display_name}</p>
                <p className="provider-state">
                  {provider.configured
                    ? `Connected · ${provider.credential_mask ?? "key saved"}`
                    : "Not configured"}
                </p>
                {provider.is_default ? (
                  <p className="default-label">Default provider</p>
                ) : null}
              </div>
              <button
                type="button"
                aria-label={`Configure ${provider.display_name}`}
                onClick={() => setEditing(provider.provider_id)}
              >
                Configure
              </button>
            </article>
          ))}
        </div>

        {editing ? (
          <ProviderEditor
            provider={providers.find((item) => item.provider_id === editing) ?? null}
            onClose={() => setEditing(null)}
            onChange={(next) => {
              setProviders((current) =>
                current.map((item) => {
                  if (item.provider_id === next.provider_id) {
                    return next;
                  }
                  return next.is_default ? { ...item, is_default: false } : item;
                }),
              );
            }}
          />
        ) : null}
      </section>
      ) : null}
      <ThreadPanel providers={providers} providerReady={providerReady} />
    </main>
  );
}

function WorkspacePicker({ onSelect }: { onSelect: (path: string) => void }) {
  const [listing, setListing] = useState<WorkspaceListing | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    try {
      setListing(await getWorkspaces(path));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Workspace request failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    void getWorkspaces()
      .then((response) => {
        if (active) {
          setListing(response);
          setError(null);
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(
            reason instanceof Error ? reason.message : "Workspace request failed",
          );
        }
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <section className="workspace-panel" aria-labelledby="workspace-heading">
      <div className="setup-copy">
        <p className="step-label">EXECUTION ROOT</p>
        <h2 id="workspace-heading">Choose a workspace</h2>
        <p className="field-help">Linux or WSL Host directories only.</p>
      </div>

      {error ? (
        <div className="error-banner" role="alert">
          <strong>Workspace unavailable</strong>
          <span>{error}</span>
          <button type="button" className="quiet-button" onClick={() => void load()}>
            Reload roots
          </button>
        </div>
      ) : null}
      {loading && listing === null ? (
        <p className="status-line">Loading Host directories…</p>
      ) : null}

      {listing ? (
        <div className="workspace-browser" aria-busy={loading}>
          <div className="workspace-roots" aria-label="Workspace roots">
            {listing.roots.map((root) => (
              <button key={root} type="button" onClick={() => void load(root)}>
                {root}
              </button>
            ))}
          </div>
          <div className="path-bar">
            <code>{listing.path}</code>
            <button type="button" onClick={() => void load(listing.path)}>
              Reload
            </button>
          </div>
          {listing.parent ? (
            <button
              type="button"
              className="directory-row"
              aria-label="Open parent directory"
              onClick={() => void load(listing.parent ?? undefined)}
            >
              <span aria-hidden="true">↰</span>
              <span>..</span>
            </button>
          ) : null}
          <div className="directory-list">
            {listing.entries.map((entry) => (
              <button
                type="button"
                className="directory-row"
                key={entry.path}
                aria-label={`Open ${entry.name}`}
                onClick={() => void load(entry.path)}
              >
                <span aria-hidden="true">▸</span>
                <span>{entry.name}</span>
              </button>
            ))}
          </div>
          {listing.truncated ? (
            <p className="field-help">Showing the first 500 directories.</p>
          ) : null}
          <div className="workspace-actions">
            <button
              type="button"
              className="primary-button"
              onClick={() => {
                setSelected(listing.path);
                onSelect(listing.path);
              }}
            >
              Use this workspace
            </button>
            {selected ? <p className="success-line">Selected · {selected}</p> : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}

type ThreadPanelProps = {
  providers: ProviderView[];
  providerReady: boolean;
};

function ThreadPanel({ providers, providerReady }: ThreadPanelProps) {
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadView[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composer, setComposer] = useState("");
  const [newProviderId, setNewProviderId] = useState<string | null>(null);
  const [newModel, setNewModel] = useState<string | null>(null);
  const [mobileView, setMobileView] = useState<
    "navigation" | "conversation" | "activity"
  >("conversation");

  const active = threads.find(
    (thread) => thread.snapshot.thread_id === activeId,
  ) ?? null;
  const configuredProviders = providers.filter((provider) => provider.configured);
  const defaultProvider =
    providers.find((provider) => provider.is_default) ?? configuredProviders[0] ?? null;
  const creationProvider =
    providers.find((provider) => provider.provider_id === newProviderId) ??
    defaultProvider;
  const creationModel = newModel ?? creationProvider?.selected_model ?? "";

  useEffect(() => {
    let mounted = true;
    void getThreads()
      .then((response) => {
        if (mounted) {
          setThreads(response);
          setActiveId((current) => current ?? response[0]?.snapshot.thread_id ?? null);
          setError(null);
        }
      })
      .catch((reason: unknown) => {
        if (mounted) {
          setError(reason instanceof Error ? reason.message : "Threads unavailable");
        }
      })
      .finally(() => {
        if (mounted) {
          setLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const replaceThread = useCallback((next: ThreadView) => {
    setThreads((current) =>
      current.some(
        (thread) => thread.snapshot.thread_id === next.snapshot.thread_id,
      )
        ? current.map((thread) =>
            thread.snapshot.thread_id === next.snapshot.thread_id ? next : thread,
          )
        : [...current, next],
    );
    setActiveId(next.snapshot.thread_id);
  }, []);

  const run = async (failureLabel: string, operation: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "Host request failed";
      setError(`${failureLabel} · ${message}`);
    } finally {
      setBusy(false);
    }
  };

  const create = () =>
    run("Thread creation failed", async () => {
      if (workspace === null) {
        throw new Error("Select a workspace before creating a Thread");
      }
      if (!providerReady) {
        throw new Error("Configure a default Provider and model first");
      }
      if (creationProvider === null || !creationModel.trim()) {
        throw new Error("Choose a configured Provider and model");
      }
      replaceThread(
        await createThread(workspace, {
          provider_config_id: creationProvider.provider_id,
          model: creationModel.trim(),
        }),
      );
    });

  const refresh = () =>
    run("Thread refresh failed", async () => {
      if (activeId !== null) {
        replaceThread(await getThread(activeId));
      }
    });

  const close = () =>
    run("Thread close failed", async () => {
      if (activeId !== null) {
        replaceThread(await closeThread(activeId));
      }
    });

  const submit = () =>
    run("Turn submission failed", async () => {
      if (active === null) {
        throw new Error("Create or select a Thread first");
      }
      if (!composer.trim()) {
        throw new Error("Enter a task for the Agent");
      }
      const submission = await startTurn(active.snapshot.thread_id, composer);
      replaceThread({ ...active, submission });
      setComposer("");
    });

  const stop = () =>
    run("Cancel failed", async () => {
      if (active === null) {
        throw new Error("No active Thread");
      }
      const submission = await cancelTurn(active.snapshot.thread_id);
      replaceThread({ ...active, submission });
    });

  const saveSettings = (providerId: string, model: string) =>
    run("Settings update failed", async () => {
      if (active === null) {
        throw new Error("No active Thread");
      }
      replaceThread(
        await updateThreadSettings(active.snapshot.thread_id, {
          ...active.snapshot.settings,
          provider_config_id: providerId,
          model,
        }),
      );
    });

  const providerName = active
    ? providers.find(
        (provider) =>
          provider.provider_id === active.snapshot.settings.provider_config_id,
      )?.display_name ?? active.snapshot.settings.provider_config_id
    : null;

  return (
    <section className="agent-console" aria-label="Coding Agent console">
      <div className="mobile-view-switcher" aria-label="Console views">
        <button
          type="button"
          aria-label="Show navigation"
          aria-pressed={mobileView === "navigation"}
          onClick={() => setMobileView("navigation")}
        >
          Workspace
        </button>
        <button
          type="button"
          aria-label="Show conversation"
          aria-pressed={mobileView === "conversation"}
          onClick={() => setMobileView("conversation")}
        >
          Conversation
        </button>
        <button
          type="button"
          aria-label="Show activity"
          aria-pressed={mobileView === "activity"}
          onClick={() => setMobileView("activity")}
        >
          Activity
        </button>
      </div>
      <nav
        className={`thread-sidebar mobile-view-${mobileView === "navigation" ? "active" : "inactive"}`}
        aria-label="Workspace and threads"
      >
        <WorkspacePicker onSelect={setWorkspace} />
        <div className="sidebar-divider" />
        <div className="new-thread-settings">
          <label htmlFor="new-thread-provider">Provider</label>
          <select
            id="new-thread-provider"
            value={creationProvider?.provider_id ?? ""}
            disabled={configuredProviders.length === 0}
            onChange={(event) => {
              const selected = providers.find(
                (provider) => provider.provider_id === event.target.value,
              );
              setNewProviderId(event.target.value);
              setNewModel(selected?.selected_model ?? "");
            }}
          >
            {configuredProviders.map((provider) => (
              <option key={provider.provider_id} value={provider.provider_id}>
                {provider.display_name}
              </option>
            ))}
          </select>
          <label htmlFor="new-thread-model">Model</label>
          <input
            id="new-thread-model"
            value={creationModel}
            disabled={creationProvider === null}
            onChange={(event) => setNewModel(event.target.value)}
          />
        </div>
        <div className="thread-sidebar-heading">
          <div>
            <p className="step-label">SESSIONS</p>
            <h2 id="threads-heading">Threads</h2>
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={
              busy ||
              workspace === null ||
              !providerReady ||
              creationProvider === null ||
              !creationModel.trim()
            }
            onClick={() => void create()}
          >
            New thread
          </button>
        </div>
        {loading ? <p className="status-line">Loading Threads…</p> : null}
        <div className="thread-list">
          {threads.map((thread) => (
            <button
              type="button"
              key={thread.snapshot.thread_id}
              className={
                thread.snapshot.thread_id === activeId ? "active" : undefined
              }
              onClick={() => setActiveId(thread.snapshot.thread_id)}
            >
              <span>{thread.snapshot.thread_id}</span>
              <small>{thread.snapshot.status}</small>
            </button>
          ))}
        </div>
      </nav>
      {error ? (
        <div className="console-error inline-error" role="alert">
          {error}
        </div>
      ) : null}
      {active ? (
        <ActiveThreadView
          key={`${active.snapshot.thread_id}:${active.snapshot.updated_at}:${active.submission?.accepted_at ?? "idle"}`}
          thread={active}
          providerName={providerName}
          busy={busy}
          composer={composer}
          mobileView={mobileView}
          onComposer={setComposer}
          onRefresh={() => void refresh()}
          onClose={() => void close()}
          onSubmit={() => void submit()}
          onStop={() => void stop()}
          onSaveSettings={(providerId, model) =>
            void saveSettings(providerId, model)
          }
          onThread={replaceThread}
          providers={configuredProviders}
        />
      ) : (
        <>
          <section
            className={`thread-detail mobile-view-${mobileView === "conversation" ? "active" : "inactive"}`}
            aria-label="Agent conversation"
          >
            <p className="thread-empty">
              {workspace
                ? providerReady
                  ? "Create a Thread for the selected workspace."
                  : "Configure a default Provider and model before creating a Thread."
                : "Select a Host workspace to enable Thread creation."}
            </p>
          </section>
          <aside
            className={`activity-panel mobile-view-${mobileView === "activity" ? "active" : "inactive"}`}
            aria-label="Activity"
          >
            <p className="step-label">ACTIVITY</p>
            <p className="thread-empty">No active Thread.</p>
          </aside>
        </>
      )}
      {/* Errors are kept outside switchable panes so narrow layouts never hide them. */}
      <div className="visually-hidden" aria-live="polite">
        {busy ? "Host request in progress" : ""}
      </div>
    </section>
  );
}

function ActiveThreadView({
  thread,
  providerName,
  busy,
  composer,
  mobileView,
  onComposer,
  onRefresh,
  onClose,
  onSubmit,
  onStop,
  onSaveSettings,
  onThread,
  providers,
}: {
  thread: ThreadView;
  providerName: string | null;
  busy: boolean;
  composer: string;
  mobileView: "navigation" | "conversation" | "activity";
  onComposer: (value: string) => void;
  onRefresh: () => void;
  onClose: () => void;
  onSubmit: () => void;
  onStop: () => void;
  onSaveSettings: (providerId: string, model: string) => void;
  onThread: (thread: ThreadView) => void;
  providers: ProviderView[];
}) {
  const [state, setState] = useState(() => initialThreadState(thread));
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "disconnected"
  >("connecting");
  const [streamError, setStreamError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsProvider, setSettingsProvider] = useState(
    thread.snapshot.settings.provider_config_id,
  );
  const [settingsModel, setSettingsModel] = useState(
    thread.snapshot.settings.model,
  );
  const [initialCursor] = useState(thread.event_cursor);
  const threadId = thread.snapshot.thread_id;
  const submissionActive = thread.submission !== null;

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      if (!submissionActive) {
        return;
      }
      let stopped = false;
      let timer: ReturnType<typeof setTimeout> | null = null;
      const poll = async () => {
        try {
          const next = await getThread(threadId);
          if (stopped) {
            return;
          }
          onThread(next);
          setState(initialThreadState(next));
          setStreamError(null);
          if (next.submission !== null) {
            timer = setTimeout(() => void poll(), 500);
          }
        } catch (reason: unknown) {
          if (stopped) {
            return;
          }
          setStreamError(
            reason instanceof Error
              ? `Thread refresh failed · ${reason.message}`
              : "Thread refresh failed",
          );
          timer = setTimeout(() => void poll(), 1000);
        }
      };
      void poll();
      return () => {
        stopped = true;
        if (timer !== null) {
          clearTimeout(timer);
        }
      };
    }
    const client = new ThreadEventClient(
      threadId,
      initialCursor,
      {
        onEvent: (event) => {
          setState((current) => applyAgentEvent(current, event));
          setStreamError(null);
          if (TERMINAL_EVENT_TYPES.has(event.type)) {
            void getThread(threadId).then(onThread).catch((reason: unknown) => {
              setStreamError(
                reason instanceof Error
                  ? `Snapshot recovery failed · ${reason.message}`
                  : "Snapshot recovery failed",
              );
            });
          }
        },
        onSnapshot: (next) => {
          onThread(next);
          setState(initialThreadState(next));
          setStreamError(null);
        },
        onConnection: setConnection,
        recover: () => getThread(threadId),
        onError: setStreamError,
      },
    );
    client.start();
    return () => client.stop();
  }, [initialCursor, onThread, submissionActive, threadId]);

  const turnActive = thread.submission !== null && state.terminal === null;
  const turnStatus = summaryText(state.terminal, "status") ?? "idle";
  const usage = isRecord(state.terminal?.usage) ? state.terminal.usage : null;

  return (
    <>
      <section
        className={`thread-detail mobile-view-${mobileView === "conversation" ? "active" : "inactive"}`}
        aria-label="Agent conversation"
      >
        <div className="thread-detail-heading">
              <div>
                <p className="step-label">ACTIVE THREAD</p>
                <h3>{thread.snapshot.thread_id}</h3>
              </div>
              <div className="thread-controls">
                <button
                  type="button"
                  aria-expanded={showSettings}
                  onClick={() => setShowSettings((current) => !current)}
                >
                  Thread settings
                </button>
                <button type="button" disabled={busy} onClick={onRefresh}>
                  Refresh active thread
                </button>
                <button type="button" disabled={busy} onClick={onClose}>
                  Close active thread
                </button>
              </div>
        </div>
            {showSettings ? (
              <div className="thread-settings-editor" aria-label="Thread settings">
                <label htmlFor="thread-settings-provider">Thread Provider</label>
                <select
                  id="thread-settings-provider"
                  value={settingsProvider}
                  disabled={busy || thread.snapshot.status === "closed"}
                  onChange={(event) => {
                    const next = providers.find(
                      (provider) => provider.provider_id === event.target.value,
                    );
                    setSettingsProvider(event.target.value);
                    setSettingsModel(next?.selected_model ?? "");
                  }}
                >
                  {providers.map((provider) => (
                    <option key={provider.provider_id} value={provider.provider_id}>
                      {provider.display_name}
                    </option>
                  ))}
                </select>
                <label htmlFor="thread-settings-model">Thread model</label>
                <input
                  id="thread-settings-model"
                  value={settingsModel}
                  disabled={busy || thread.snapshot.status === "closed"}
                  onChange={(event) => setSettingsModel(event.target.value)}
                />
                <button
                  type="button"
                  className="primary-button"
                  disabled={
                    busy ||
                    thread.snapshot.status === "closed" ||
                    !settingsModel.trim()
                  }
                  onClick={() => onSaveSettings(settingsProvider, settingsModel.trim())}
                >
                  Save thread settings
                </button>
                <p className="field-help">
                  Version {thread.snapshot.settings.version}; changes apply to the next model request.
                </p>
              </div>
            ) : null}
            <p className="thread-workspace">{thread.snapshot.workspace}</p>
            <p className="thread-model">
              {providerName} · {thread.snapshot.settings.model} · settings v
              {thread.snapshot.settings.version}
            </p>
            <p className="thread-status">Status · {thread.snapshot.status}</p>
            {state.cancel_requested ? (
              <p className="submission-status cancel-requested" aria-live="polite">
                Cancel requested…
              </p>
            ) : turnActive ? (
              <p className="submission-status" aria-live="polite">
                {thread.submission?.status === "starting"
                  ? "Starting…"
                  : thread.submission?.status === "running"
                    ? "Running…"
                    : "Cancelling…"}
              </p>
            ) : null}
            <ConversationFeed
              state={state}
              connection={connection}
              streamError={streamError}
            />
            <div className="composer">
              <label htmlFor="agent-composer">Ask Agent</label>
              <textarea
                id="agent-composer"
                rows={4}
                value={composer}
                disabled={thread.snapshot.status === "closed"}
                onChange={(event) => onComposer(event.target.value)}
                placeholder="Describe the coding task…"
              />
              <div className="composer-actions">
                <button
                  type="button"
                  className="primary-button"
                  disabled={
                    busy ||
                    turnActive ||
                    thread.snapshot.status === "closed" ||
                    !composer.trim()
                  }
                  onClick={onSubmit}
                >
                  Send
                </button>
                {turnActive ? (
                  <button
                    type="button"
                    className="danger-button"
                    disabled={busy || thread.submission?.status === "cancelling"}
                    onClick={onStop}
                  >
                    Stop
                  </button>
                ) : null}
              </div>
            </div>
      </section>
      <aside
        className={`activity-panel mobile-view-${mobileView === "activity" ? "active" : "inactive"}`}
        aria-label="Activity"
      >
        <div className="activity-heading">
          <div>
            <p className="step-label">ACTIVITY</p>
            <h4>Execution</h4>
          </div>
          <span className={`connection-state ${connection}`}>{connection}</span>
        </div>
        <dl className="activity-summary">
          <div><dt>Thread</dt><dd>{thread.snapshot.status}</dd></div>
          <div><dt>Turn</dt><dd>{state.cancel_requested ? "cancel requested" : turnStatus}</dd></div>
          <div><dt>Iterations</dt><dd>{summaryText(state.terminal, "iterations") ?? "—"}</dd></div>
          <div><dt>Tool calls</dt><dd>{summaryText(state.terminal, "tool_calls") ?? state.tools.length}</dd></div>
          <div><dt>Input tokens</dt><dd>{summaryText(usage, "input_tokens") ?? "—"}</dd></div>
          <div><dt>Output tokens</dt><dd>{summaryText(usage, "output_tokens") ?? "—"}</dd></div>
          <div><dt>Stop reason</dt><dd>{summaryText(state.terminal, "stop_reason") ?? "—"}</dd></div>
          <div><dt>Completed</dt><dd>{thread.snapshot.completed_turns}</dd></div>
        </dl>
        <dl className="activity-timestamps">
          <div><dt>Started</dt><dd>{summaryText(state.terminal, "started_at") ?? "—"}</dd></div>
          <div><dt>Ended</dt><dd>{summaryText(state.terminal, "ended_at") ?? "—"}</dd></div>
        </dl>
        <div className="activity-tool-list" aria-label="Tool activity summary">
          {state.tools.map((tool) => (
            <article className={`activity-tool ${tool.status}`} key={tool.id}>
              <span aria-hidden="true">
                {tool.status === "success" ? "✓" : tool.status === "error" ? "×" : "○"}
              </span>
              <div><strong>{tool.name}</strong><small>{tool.status}</small></div>
            </article>
          ))}
          {state.tools.length === 0 ? <p className="thread-empty">No tool calls yet.</p> : null}
        </div>
        <div className="changed-files">
          <p className="step-label">CHANGED FILES</p>
          {state.files.map((file) => (
            <details className="file-change" key={file.path}>
              <summary><span>{file.change_type}</span> {file.path}</summary>
              {file.diff ? <pre>{file.diff}</pre> : <p>No text diff available.</p>}
            </details>
          ))}
          {state.files.length === 0 ? (
            <p className="thread-empty">No reported file changes.</p>
          ) : null}
          {state.terminal?.diff_complete === false ? (
            <p className="diff-incomplete" role="status">
              Diff may be incomplete because a command could have changed files outside tracked file tools.
            </p>
          ) : null}
        </div>
        {state.terminal ? (
          <p className="terminal-state">Turn · {String(state.terminal.status ?? "finished")}</p>
        ) : null}
      </aside>
    </>
  );
}

function ConversationFeed({
  state,
  connection,
  streamError,
}: {
  state: ReturnType<typeof hydrateThread>;
  connection: "connecting" | "connected" | "disconnected";
  streamError: string | null;
}) {
  return (
    <section className="conversation" aria-label="Conversation timeline">
      <div className="conversation-heading">
        <h4>Conversation</h4>
        <span className={`connection-state ${connection}`}>{connection}</span>
      </div>
      {connection === "disconnected" ? (
        <p className="inline-error" role="status">
          Host event connection lost. Reconnecting without clearing this view…
        </p>
      ) : null}
      {streamError ? (
        <p className="inline-error" role="alert">
          {streamError}
        </p>
      ) : null}
      <div className="message-list">
        {state.messages.map((message) => (
          <article className={`message ${message.role}`} key={message.id}>
            <p className="step-label">{message.role}</p>
            <p>{message.text}</p>
          </article>
        ))}
        {state.messages.length === 0 ? (
          <p className="thread-empty">No messages yet.</p>
        ) : null}
      </div>
      <div className="tool-list" aria-label="Tool activity">
        {state.tools.map((tool) => (
          <article className={`tool-card ${tool.status}`} key={tool.id}>
            <div>
              <strong>{tool.name}</strong>
              <span>{tool.status}</span>
            </div>
            {tool.arguments !== null ? (
              <details>
                <summary>Arguments</summary>
                <pre>{JSON.stringify(tool.arguments, null, 2)}</pre>
              </details>
            ) : null}
            {tool.result ? (
              <details open={tool.status === "error"}>
                <summary>Result</summary>
                <pre>{tool.result}</pre>
              </details>
            ) : null}
            {tool.metadata ? (
              <details>
                <summary>Metadata</summary>
                <pre>{JSON.stringify(tool.metadata, null, 2)}</pre>
              </details>
            ) : null}
            {tool.error_code ? <p>{tool.error_code}</p> : null}
          </article>
        ))}
      </div>
      {state.files.length > 0 ? (
        <div className="conversation-files" aria-label="Conversation file changes">
          {state.files.map((file) => (
            <article className="conversation-file-card" key={file.path}>
              <div>
                <strong>{file.path}</strong>
                <span>{file.change_type}</span>
              </div>
              {file.diff ? (
                <details>
                  <summary>View diff</summary>
                  <pre>{file.diff}</pre>
                </details>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}
      {state.error ? (
        <div className="inline-error" role="alert">
          <strong>{state.error.code}</strong> · {state.error.message}
        </div>
      ) : null}
    </section>
  );
}

type ProviderEditorProps = {
  provider: ProviderView | null;
  onClose: () => void;
  onChange: (provider: ProviderView) => void;
};

function ProviderEditor({ provider, onClose, onChange }: ProviderEditorProps) {
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState(provider?.selected_model ?? "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);

  if (provider === null) {
    return null;
  }

  const run = async (operation: () => Promise<void>) => {
    setBusy(true);
    setEditorError(null);
    try {
      await operation();
    } catch (reason: unknown) {
      setEditorError(reason instanceof Error ? reason.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const saveAndDiscover = () =>
    run(async () => {
      if (!apiKey.trim()) {
        throw new Error("Enter an API key");
      }
      const saved = await saveProvider(
        provider.provider_id,
        apiKey,
        model || null,
      );
      onChange(saved);
      setApiKey("");
      setMessage(`Key saved · ${saved.credential_mask ?? "configured"}`);
      const discovered = await discoverModels(provider.provider_id);
      setModels(discovered.models);
      if (!model && discovered.models[0]) {
        setModel(discovered.models[0]);
      }
      if (discovered.models.length === 0) {
        setMessage("Key saved · Provider returned no models; enter one manually");
      }
    });

  const makeDefault = () =>
    run(async () => {
      if (!model.trim()) {
        throw new Error("Choose or enter a model");
      }
      const selected = await selectProviderDefault(provider.provider_id, model);
      onChange(selected);
      setMessage("Default provider");
    });

  const refreshModels = () =>
    run(async () => {
      const discovered = await discoverModels(provider.provider_id);
      setModels(discovered.models);
      if (!model && discovered.models[0]) {
        setModel(discovered.models[0]);
      }
      setMessage(
        discovered.models.length > 0
          ? `${discovered.models.length} models available`
          : "Provider returned no models; enter one manually",
      );
    });

  const clear = () =>
    run(async () => {
      const cleared = await clearProviderCredential(provider.provider_id);
      onChange(cleared);
      setModels([]);
      setMessage("Credential cleared");
    });

  return (
    <aside className="provider-editor" aria-labelledby="editor-heading">
      <div className="editor-heading-row">
        <div>
          <p className="step-label">PROVIDER SETTINGS</p>
          <h2 id="editor-heading">{provider.display_name}</h2>
        </div>
        <button type="button" className="quiet-button" onClick={onClose}>
          Close
        </button>
      </div>

      {editorError ? (
        <div className="inline-error" role="alert" tabIndex={-1}>
          {editorError}
        </div>
      ) : null}
      {message ? <p className="success-line">{message}</p> : null}

      <div className="field-group">
        <label htmlFor="provider-key">API key</label>
        <div className="input-with-action">
          <input
            id="provider-key"
            type={showKey ? "text" : "password"}
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <button
            type="button"
            className="quiet-button"
            aria-label={showKey ? "Hide API key" : "Show API key"}
            onClick={() => setShowKey((current) => !current)}
          >
            {showKey ? "Hide" : "Show"}
          </button>
        </div>
        <p className="field-help">Stored by the local Host, never returned by the API.</p>
      </div>

      <button
        type="button"
        className="primary-button"
        disabled={busy}
        onClick={() => void saveAndDiscover()}
      >
        {busy ? "Connecting…" : "Save and discover"}
      </button>

      <div className="field-group">
        <label htmlFor="provider-model">Provider model</label>
        {models.length > 0 ? (
          <select
            id="provider-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
          >
            {models.map((modelId) => (
              <option key={modelId} value={modelId}>
                {modelId}
              </option>
            ))}
          </select>
        ) : (
          <input
            id="provider-model"
            value={model}
            placeholder="Enter a Provider model ID"
            onChange={(event) => setModel(event.target.value)}
          />
        )}
        <p className="field-help">
          Discovery reports accessible IDs; Coding Agent compatibility is checked when used.
        </p>
      </div>

      <div className="editor-actions">
        {provider.configured ? (
          <button
            type="button"
            className="quiet-button"
            disabled={busy}
            onClick={() => void refreshModels()}
          >
            Refresh models
          </button>
        ) : null}
        <button
          type="button"
          className="primary-button"
          disabled={busy || !provider.configured}
          onClick={() => void makeDefault()}
        >
          Use as default
        </button>
        {provider.configured ? (
          <button
            type="button"
            className="danger-button"
            disabled={busy}
            onClick={() => void clear()}
          >
            Clear credential
          </button>
        ) : null}
      </div>
    </aside>
  );
}

function summaryText(
  record: Record<string, unknown> | null,
  key: string,
): string | null {
  const value = record?.[key];
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : null;
}

function initialThreadState(thread: ThreadView) {
  const state = hydrateThread(thread);
  if (thread.submission !== null) {
    state.terminal = null;
    state.error = null;
    state.files = [];
    state.cancel_requested = false;
  }
  return state;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

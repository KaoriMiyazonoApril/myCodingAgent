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
  type ProviderView,
  type ThreadView,
  type WorkspaceListing,
} from "./api";
import { ThreadEventClient } from "./eventClient";
import { applyAgentEvent, hydrateThread } from "./events";
import "./styles.css";

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
  const [mobileView, setMobileView] = useState<
    "navigation" | "conversation" | "activity"
  >("conversation");

  const active = threads.find(
    (thread) => thread.snapshot.thread_id === activeId,
  ) ?? null;

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

  const run = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Thread request failed");
    } finally {
      setBusy(false);
    }
  };

  const create = () =>
    run(async () => {
      if (workspace === null) {
        throw new Error("Select a workspace before creating a Thread");
      }
      if (!providerReady) {
        throw new Error("Configure a default Provider and model first");
      }
      replaceThread(await createThread(workspace));
    });

  const refresh = () =>
    run(async () => {
      if (activeId !== null) {
        replaceThread(await getThread(activeId));
      }
    });

  const close = () =>
    run(async () => {
      if (activeId !== null) {
        replaceThread(await closeThread(activeId));
      }
    });

  const submit = () =>
    run(async () => {
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
    run(async () => {
      if (active === null) {
        throw new Error("No active Thread");
      }
      const submission = await cancelTurn(active.snapshot.thread_id);
      replaceThread({ ...active, submission });
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
        <div className="thread-sidebar-heading">
          <div>
            <p className="step-label">SESSIONS</p>
            <h2 id="threads-heading">Threads</h2>
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={busy || workspace === null || !providerReady}
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
          key={active.snapshot.thread_id}
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
          onThread={replaceThread}
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
  onThread,
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
  onThread: (thread: ThreadView) => void;
}) {
  const [state, setState] = useState(() => hydrateThread(thread));
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "disconnected"
  >("connecting");
  const [streamError, setStreamError] = useState<string | null>(null);
  const [initialCursor] = useState(thread.event_cursor);
  const threadId = thread.snapshot.thread_id;

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      return;
    }
    const client = new ThreadEventClient(
      threadId,
      initialCursor,
      {
        onEvent: (event) => {
          setState((current) => applyAgentEvent(current, event));
          setStreamError(null);
        },
        onSnapshot: (next) => {
          onThread(next);
          setState(hydrateThread(next));
          setStreamError(null);
        },
        onConnection: setConnection,
        recover: () => getThread(threadId),
        onError: setStreamError,
      },
    );
    client.start();
    return () => client.stop();
  }, [initialCursor, onThread, threadId]);

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
                <button type="button" disabled={busy} onClick={onRefresh}>
                  Refresh active thread
                </button>
                <button type="button" disabled={busy} onClick={onClose}>
                  Close active thread
                </button>
              </div>
        </div>
            <p className="thread-workspace">{thread.snapshot.workspace}</p>
            <p className="thread-model">
              {providerName} · {thread.snapshot.settings.model} · settings v
              {thread.snapshot.settings.version}
            </p>
            <p className="thread-status">Status · {thread.snapshot.status}</p>
            {thread.submission ? (
              <p className="submission-status" aria-live="polite">
                {thread.submission.status === "starting"
                  ? "Starting…"
                  : thread.submission.status === "running"
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
                    thread.submission !== null ||
                    thread.snapshot.status === "closed" ||
                    !composer.trim()
                  }
                  onClick={onSubmit}
                >
                  Send
                </button>
                {thread.submission !== null ? (
                  <button
                    type="button"
                    className="danger-button"
                    disabled={busy || thread.submission.status === "cancelling"}
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
          <div><dt>Turn</dt><dd>{thread.submission?.status ?? "idle"}</dd></div>
          <div><dt>Tools</dt><dd>{state.tools.length}</dd></div>
          <div><dt>Completed</dt><dd>{thread.snapshot.completed_turns}</dd></div>
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
        <div className="changed-files-placeholder">
          <p className="step-label">CHANGED FILES</p>
          <p className="thread-empty">File changes will appear when reported by Runtime events.</p>
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
            {tool.error_code ? <p>{tool.error_code}</p> : null}
          </article>
        ))}
      </div>
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
        <label htmlFor="provider-model">Model</label>
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

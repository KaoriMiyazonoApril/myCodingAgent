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
import "./styles.css";

export function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<string | null>(null);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string | null>(null);

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

  return (
    <main className="setup-shell">
      <header className="product-bar">
        <span className="product-mark" aria-hidden="true">
          &gt;_
        </span>
        <div>
          <p className="eyebrow">LOCAL RUNTIME</p>
          <p className="product-name">Agent</p>
        </div>
        <span className="host-badge">127.0.0.1</span>
      </header>

      <section className="setup-panel" aria-labelledby="provider-heading">
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
      <WorkspacePicker onSelect={setSelectedWorkspace} />
      <ThreadPanel
        workspace={selectedWorkspace}
        providers={providers}
      />
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
    <section className="setup-panel workspace-panel" aria-labelledby="workspace-heading">
      <div className="setup-copy">
        <p className="step-label">STEP 02 · EXECUTION ROOT</p>
        <h2 id="workspace-heading">Choose a workspace</h2>
        <p className="lede">
          Browse directories exposed by this Linux or WSL Host. Runtime validation
          still applies when the Agent executes a task.
        </p>
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
  workspace: string | null;
  providers: ProviderView[];
};

function ThreadPanel({ workspace, providers }: ThreadPanelProps) {
  const [threads, setThreads] = useState<ThreadView[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composer, setComposer] = useState("");

  const active = threads.find(
    (thread) => thread.snapshot.thread_id === activeId,
  ) ?? null;
  const activeSubmission = active?.submission ?? null;

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

  useEffect(() => {
    if (activeId === null || activeSubmission === null) {
      return;
    }
    let mounted = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await getThread(activeId);
        if (!mounted) {
          return;
        }
        setThreads((current) =>
          current.map((thread) =>
            thread.snapshot.thread_id === activeId ? next : thread,
          ),
        );
        if (next.submission !== null) {
          timer = window.setTimeout(() => void poll(), 250);
        }
      } catch (reason: unknown) {
        if (mounted) {
          setError(reason instanceof Error ? reason.message : "Thread refresh failed");
        }
      }
    };
    timer = window.setTimeout(() => void poll(), 250);
    return () => {
      mounted = false;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [activeId, activeSubmission]);

  const replaceThread = (next: ThreadView) => {
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
  };

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
    <section className="setup-panel thread-panel" aria-labelledby="threads-heading">
      <div className="thread-sidebar">
        <div className="thread-sidebar-heading">
          <div>
            <p className="step-label">STEP 03 · THREADS</p>
            <h2 id="threads-heading">Threads</h2>
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={busy || workspace === null}
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
      </div>
      <div className="thread-detail">
        {error ? (
          <div className="inline-error" role="alert">
            {error}
          </div>
        ) : null}
        {active ? (
          <>
            <div className="thread-detail-heading">
              <div>
                <p className="step-label">ACTIVE THREAD</p>
                <h3>{active.snapshot.thread_id}</h3>
              </div>
              <div className="thread-controls">
                <button type="button" disabled={busy} onClick={() => void refresh()}>
                  Refresh active thread
                </button>
                <button type="button" disabled={busy} onClick={() => void close()}>
                  Close active thread
                </button>
              </div>
            </div>
            <p className="thread-workspace">{active.snapshot.workspace}</p>
            <p className="thread-model">
              {providerName} · {active.snapshot.settings.model} · settings v
              {active.snapshot.settings.version}
            </p>
            <p className="thread-status">Status · {active.snapshot.status}</p>
            {active.submission ? (
              <p className="submission-status" aria-live="polite">
                {active.submission.status === "starting"
                  ? "Starting…"
                  : active.submission.status === "running"
                    ? "Running…"
                    : "Cancelling…"}
              </p>
            ) : null}
            <div className="composer">
              <label htmlFor="agent-composer">Ask Agent</label>
              <textarea
                id="agent-composer"
                rows={4}
                value={composer}
                disabled={active.snapshot.status === "closed"}
                onChange={(event) => setComposer(event.target.value)}
                placeholder="Describe the coding task…"
              />
              <div className="composer-actions">
                <button
                  type="button"
                  className="primary-button"
                  disabled={
                    busy ||
                    active.submission !== null ||
                    active.snapshot.status === "closed" ||
                    !composer.trim()
                  }
                  onClick={() => void submit()}
                >
                  Send
                </button>
                {active.submission !== null ? (
                  <button
                    type="button"
                    className="danger-button"
                    disabled={busy || active.submission.status === "cancelling"}
                    onClick={() => void stop()}
                  >
                    Stop
                  </button>
                ) : null}
              </div>
            </div>
          </>
        ) : (
          <p className="thread-empty">
            {workspace
              ? "Create a Thread for the selected workspace."
              : "Select a Host workspace to enable Thread creation."}
          </p>
        )}
      </div>
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

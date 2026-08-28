import { useCallback, useEffect, useState } from "react";

import {
  clearProviderCredential,
  discoverModels,
  getProviders,
  getWorkspaces,
  saveProvider,
  selectProviderDefault,
  type ProviderView,
  type WorkspaceListing,
} from "./api";
import "./styles.css";

export function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<string | null>(null);

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
                current.map((item) =>
                  item.provider_id === next.provider_id ? next : item,
                ),
              );
            }}
          />
        ) : null}
      </section>
      <WorkspacePicker />
    </main>
  );
}

function WorkspacePicker() {
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
              onClick={() => setSelected(listing.path)}
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

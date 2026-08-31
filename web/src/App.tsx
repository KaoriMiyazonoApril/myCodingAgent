import { useCallback, useEffect, useRef, useState } from "react";

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
  HostError,
  saveProvider,
  selectProviderDefault,
  startTurn,
  selectWorkspace,
  resolveApproval,
  updateThreadSettings,
  type ProviderView,
  type ThreadView,
  type WorkspaceRecord,
  type WorkspaceListing,
} from "./api";
import { ThreadEventClient } from "./eventClient";
import {
  applyAgentEvent,
  eventRequiresSnapshotRefresh,
  hydrateThread,
  type ApprovalRequest,
} from "./events";
import "./styles.css";

export function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<string | null>(null);
  const [showProviderSettings, setShowProviderSettings] = useState(false);
  const [workspace, setWorkspace] = useState<WorkspaceRecord | null>(null);
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);

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
          setError(reason instanceof Error ? reason.message : "本地服务不可用");
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
  const configuredProviders = providers.filter((provider) => provider.configured);
  const defaultProvider =
    providers.find((provider) => provider.is_default && provider.configured) ??
    configuredProviders[0] ??
    null;

  const selectedProvider =
    providers.find(
      (provider) =>
        provider.provider_id === selectedProviderId && provider.configured,
    ) ?? defaultProvider;
  const effectiveProviderId = selectedProvider?.provider_id ?? null;
  const effectiveModel = selectedModel ?? selectedProvider?.selected_model ?? "";

  const settingsOpen = showProviderSettings || editing !== null;

  const updateProvider = (next: ProviderView) => {
    setProviders((current) =>
      current.map((item) => {
        if (item.provider_id === next.provider_id) {
          return next;
        }
        return next.is_default ? { ...item, is_default: false } : item;
      }),
    );
  };

  return (
    <main className="app-shell">
      <a className="skip-link" href="#agent-workspace">
        跳转到工作区
      </a>
      <header className="product-bar">
        <div className="brand-lockup">
          <span className="product-mark" aria-hidden="true">
            &gt;_
          </span>
          <div>
            <p className="eyebrow">本地</p>
            <p className="product-name">编码助手</p>
          </div>
        </div>
        <div className="topbar-context">
          <ProjectSelector
            workspace={workspace}
            onOpenProject={() => setWorkspaceDialogOpen(true)}
          />
          <ModelSelector
            providers={configuredProviders}
            providerId={effectiveProviderId}
            model={effectiveModel}
            onProviderChange={(providerId) => {
              const next = configuredProviders.find(
                (provider) => provider.provider_id === providerId,
              );
              setSelectedProviderId(providerId);
              setSelectedModel(next?.selected_model ?? "");
            }}
            onModelChange={(model) => setSelectedModel(model)}
          />
        </div>
        <div className="product-actions">
          <span
            className="local-status"
            title="本地服务 · 127.0.0.1:3080"
          >
            <span className="status-dot" aria-hidden="true" />
            本地
          </span>
          <button
            type="button"
            className="quiet-button settings-toggle"
            aria-expanded={settingsOpen}
            onClick={() => {
              setEditing(null);
              setShowProviderSettings((current) => !current);
            }}
          >
            设置
          </button>
        </div>
      </header>

      <div id="agent-workspace" className="app-content">
        <div
          className="workspace-view"
          hidden={settingsOpen}
          aria-hidden={settingsOpen}
        >
          <ThreadPanel
            providers={providers}
            providerReady={providerReady}
            workspace={workspace}
        onWorkspaceRecovered={setWorkspace}
            selectedProviderId={effectiveProviderId}
            selectedModel={effectiveModel}
            onOpenProject={() => setWorkspaceDialogOpen(true)}
            onOpenSettings={() => setShowProviderSettings(true)}
          />
        </div>
        {settingsOpen ? (
          <ProviderSettingsView
            providers={providers}
            loading={loading}
            error={error}
            editing={editing}
            onEdit={setEditing}
            onCloseEditor={() => setEditing(null)}
            onChange={updateProvider}
            onClose={() => {
              setEditing(null);
              setShowProviderSettings(false);
            }}
          />
        ) : null}
      </div>
      <WorkspaceDialog
        open={workspaceDialogOpen}
        onClose={() => setWorkspaceDialogOpen(false)}
        onSelect={(selectedWorkspace) => {
          setWorkspace(selectedWorkspace);
          setWorkspaceDialogOpen(false);
        }}
      />
    </main>
  );
}

function ProjectSelector({
  workspace,
  onOpenProject,
}: {
  workspace: WorkspaceRecord | null;
  onOpenProject: () => void;
}) {
  const [open, setOpen] = useState(false);
  const selectorRef = useRef<HTMLButtonElement>(null);
  const name = workspace ? workspace.display_name : "选择项目";

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  return (
    <div className="topbar-selector-wrap">
      <button
        type="button"
        className="topbar-selector project-selector"
        ref={selectorRef}
        aria-label={workspace ? `当前项目：${name}` : "选择项目"}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((current) => !current)}
        title={workspace?.path ?? "打开一个项目开始工作"}
      >
        <span className="topbar-selector-label">项目</span>
        <span className="topbar-selector-value">{name}</span>
        <span className="selector-chevron" aria-hidden="true">
          ▾
        </span>
      </button>
      {open ? (
        <div className="topbar-menu" role="menu" aria-label="项目菜单">
          <p className="menu-label">最近项目</p>
          {workspace ? (
            <button
              type="button"
              role="menuitem"
              className="menu-item selected"
              onClick={() => setOpen(false)}
            >
              <span>{name}</span>
              <small>{workspace.path}</small>
            </button>
          ) : (
            <p className="menu-empty">暂无最近项目</p>
          )}
          <button
            type="button"
            role="menuitem"
            className="menu-item menu-item-action"
            onClick={() => {
              setOpen(false);
              // The menu item is removed in the same React commit that opens
              // the dialog. Focus the durable trigger first so the dialog's
              // focus-return contract does not fall back to <body>.
              selectorRef.current?.focus();
              onOpenProject();
            }}
          >
            <span aria-hidden="true">＋</span>
            打开项目…
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ModelSelector({
  providers,
  providerId,
  model,
  onProviderChange,
  onModelChange,
}: {
  providers: ProviderView[];
  providerId: string | null;
  model: string;
  onProviderChange: (providerId: string) => void;
  onModelChange: (model: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const provider = providers.find((item) => item.provider_id === providerId) ?? providers[0];
  const value = provider
    ? `${provider.display_name} · ${model || provider.selected_model || "model not set"}`
    : "尚未配置模型";

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  return (
    <div className="topbar-selector-wrap model-selector-wrap">
      <button
        type="button"
        className="topbar-selector model-selector"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="topbar-selector-label">模型</span>
        <span className="topbar-selector-value">{value}</span>
        <span className="selector-chevron" aria-hidden="true">
          ▾
        </span>
      </button>
      {open ? (
        <div className="model-popover" role="dialog" aria-label="模型选择器">
          <div className="popover-heading">
            <div>
              <p className="step-label">新对话默认值</p>
              <h2>选择模型</h2>
            </div>
            <button
              type="button"
              className="icon-button"
              aria-label="关闭模型选择器"
              onClick={() => setOpen(false)}
            >
              ×
            </button>
          </div>
          {providers.length > 0 ? (
            <>
              <label htmlFor="topbar-provider">模型服务商</label>
              <select
                id="topbar-provider"
                value={provider?.provider_id ?? ""}
                onChange={(event) => onProviderChange(event.target.value)}
              >
                {providers.map((item) => (
                  <option key={item.provider_id} value={item.provider_id}>
                    {item.display_name}
                  </option>
                ))}
              </select>
              <label htmlFor="topbar-model">模型 ID</label>
              <input
                id="topbar-model"
                value={model}
                onChange={(event) => onModelChange(event.target.value)}
                placeholder="e.g. deepseek-chat"
              />
              <p className="field-help">用于接下来创建的新对话。</p>
            </>
          ) : (
            <p className="menu-empty">请先配置模型服务商，再选择模型。</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function WorkspaceDialog({
  open,
  onClose,
  onSelect,
}: {
  open: boolean;
  onClose: () => void;
  onSelect: (workspace: WorkspaceRecord) => void;
}) {
  const [listing, setListing] = useState<WorkspaceListing | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selecting, setSelecting] = useState(false);
  const requestGeneration = useRef(0);
  const dialogRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const load = useCallback(async (path?: string) => {
    const generation = ++requestGeneration.current;
    if (path === undefined) {
      setListing(null);
      setSelected(null);
      setSelecting(false);
    }
    setLoading(true);
    setError(null);
    try {
      const next = await getWorkspaces(path);
      if (!open || generation !== requestGeneration.current) {
        return;
      }
      setListing(next);
    } catch (reason: unknown) {
      if (!open || generation !== requestGeneration.current) {
        return;
      }
      setError(formatWorkspaceError(reason, "项目目录请求失败"));
    } finally {
      if (open && generation === requestGeneration.current) {
        setLoading(false);
      }
    }
  }, [open]);

  const selectCurrent = useCallback(async () => {
    if (listing === null || selecting) {
      return;
    }
    const generation = requestGeneration.current;
    setSelecting(true);
    setError(null);
    try {
      const workspace = await selectWorkspace(listing.path);
      if (open && generation === requestGeneration.current) {
        setSelected(workspace.path);
        onSelect(workspace);
      }
    } catch (reason: unknown) {
      if (open && generation === requestGeneration.current) {
        setError(formatWorkspaceError(reason, "项目选择失败"));
      }
    } finally {
      if (open && generation === requestGeneration.current) {
        setSelecting(false);
      }
    }
  }, [listing, onSelect, open, selecting]);

  useEffect(() => {
    if (open) {
      returnFocusRef.current = document.activeElement as HTMLElement | null;
      // Defer the request one microtask so opening the modal does not perform
      // a synchronous state transition from inside the effect itself.
      const scheduledGeneration = requestGeneration.current;
      queueMicrotask(() => {
        if (open && requestGeneration.current === scheduledGeneration) {
          void load();
        }
      });
      dialogRef.current?.focus();
      return;
    }
    requestGeneration.current += 1;
    returnFocusRef.current?.focus();
  }, [load, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose, open]);

  if (!open) {
    return null;
  }

  return (
    <div className="modal-scrim">
      <section
        className="workspace-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-heading"
        aria-busy={selecting || loading}
        tabIndex={-1}
        ref={dialogRef}
      >
        <div className="dialog-heading">
          <div>
            <p className="step-label">项目</p>
            <h2 id="workspace-heading">打开项目</h2>
            <p className="field-help">选择一个本地目录作为编码助手的项目上下文。</p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭项目对话框"
            onClick={onClose}
          >
            ×
          </button>
        </div>

        {error ? (
          <div className="error-banner" role="alert">
            <strong>项目不可用</strong>
            <span>{error}</span>
            <button type="button" className="quiet-button" onClick={() => void load()}>
              重新加载根目录
            </button>
          </div>
        ) : null}
        {loading && listing === null ? (
          <p className="status-line">正在加载 Host 项目目录…</p>
        ) : null}

        {listing ? (
          <div className="workspace-browser" aria-busy={loading}>
            <div className="workspace-roots" aria-label="项目根目录">
              {listing.roots.map((root) => (
                <button key={root} type="button" onClick={() => void load(root)}>
                  {projectName(root)}
                  <small title={root}>{root}</small>
                </button>
              ))}
            </div>
            <div className="path-bar">
              <div className="path-summary">
                <strong>{projectName(listing.path)}</strong>
                <code title={listing.path}>{listing.path}</code>
              </div>
              <button type="button" className="quiet-button" onClick={() => void load(listing.path)}>
                重新加载
              </button>
            </div>
            {listing.parent ? (
              <button
                type="button"
                className="directory-row"
                aria-label="打开上级目录"
                onClick={() => void load(listing.parent ?? undefined)}
              >
                <span aria-hidden="true">↰</span>
                <span>上级目录</span>
              </button>
            ) : null}
            <div className="directory-list">
              {listing.entries.map((entry) => (
                <button
                  type="button"
                  className="directory-row"
                  key={entry.path}
                  aria-label={`打开 ${entry.name}`}
                  onClick={() => void load(entry.path)}
                >
                  <span aria-hidden="true">▸</span>
                  <span>{entry.name}</span>
                  <small title={entry.path}>{entry.path}</small>
                </button>
              ))}
            </div>
            {listing.truncated ? (
              <p className="field-help">仅显示前 500 个目录。</p>
            ) : null}
            <div className="workspace-actions">
              <button
                type="button"
                className="primary-button"
                onClick={() => {
                  void selectCurrent();
                }}
                disabled={selecting || loading}
              >
                使用此项目
              </button>
              {selecting ? <p className="status-line">正在保存 Workspace…</p> : null}
              {selected ? <p className="success-line">已选择 · {selected}</p> : null}
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function projectName(path: string): string {
  const normalized = path.replace(/[\\/]$/, "");
  return normalized.split(/[\\/]/).at(-1) || normalized || "/";
}

function formatWorkspaceError(reason: unknown, fallback: string): string {
  if (reason instanceof HostError) {
    return `${reason.code} · ${reason.message}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function titleFromText(value: string): string {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > 52 ? `${text.slice(0, 52)}…` : text || "新对话";
}

function threadTitle(thread: ThreadView, localTitle?: string): string {
  if (localTitle) {
    return localTitle;
  }
  const firstUserMessage = thread.snapshot.messages.find(
    (message) => message.role === "user" && Array.isArray(message.content),
  );
  if (firstUserMessage && Array.isArray(firstUserMessage.content)) {
    const text = firstUserMessage.content
      .filter(isRecord)
      .filter((block) => block.type === "text" && typeof block.text === "string")
      .map((block) => String(block.text))
      .join("")
      .trim();
    if (text) {
      return titleFromText(text);
    }
  }
  return "新对话";
}

function threadStatusLabel(status: ThreadView["snapshot"]["status"]): string {
  return {
    idle: "",
    running: "运行中",
    waiting_approval: "等待确认",
    closed: "已关闭",
  }[status];
}

const STARTER_PROMPTS = [
  "解释这个项目",
  "查找一个可能的错误",
  "运行项目测试",
  "实现一个新功能",
];

function toolNameLabel(name: string): string {
  const labels: Record<string, string> = {
    read_file: "读取文件",
    write_file: "写入文件",
    edit_file: "编辑文件",
    list_files: "浏览文件",
    search_files: "搜索文件",
    run_command: "运行命令",
  };
  return labels[name] ?? "工具调用";
}

function toolStatusLabel(status: ReturnType<typeof hydrateThread>["tools"][number]["status"]): string {
  return {
    requested: "等待执行",
    running: "执行中",
    success: "已完成",
    error: "失败",
  }[status];
}

function fileChangeLabel(changeType: string): string {
  return {
    modified: "已修改",
    created: "已创建",
    deleted: "已删除",
  }[changeType] ?? changeType;
}

function runtimeStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: "空闲",
    starting: "正在启动",
    running: "运行中",
    cancelling: "正在停止",
    "cancel requested": "已请求停止",
    completed: "已完成",
    cancelled: "已取消",
    failed: "失败",
    rejected: "已拒绝",
    closed: "已关闭",
    limit_reached: "达到限制",
  };
  return labels[status] ?? status;
}

function toolTarget(argumentsValue: unknown): string | null {
  if (!isRecord(argumentsValue)) {
    return null;
  }
  for (const key of ["path", "command", "query", "pattern"]) {
    const value = argumentsValue[key];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return null;
}

function ProviderSettingsView({
  providers,
  loading,
  error,
  editing,
  onEdit,
  onCloseEditor,
  onChange,
  onClose,
}: {
  providers: ProviderView[];
  loading: boolean;
  error: string | null;
  editing: string | null;
  onEdit: (providerId: string) => void;
  onCloseEditor: () => void;
  onChange: (provider: ProviderView) => void;
  onClose: () => void;
}) {
  return (
    <section className="settings-view" aria-labelledby="models-heading">
      <div className="settings-heading">
        <div>
          <p className="eyebrow">设置 / 模型</p>
          <h1 id="models-heading">模型与服务商</h1>
          <p className="settings-lede">
            管理本地对话可用的模型服务商。凭据保存在本机，不会由接口返回浏览器。
          </p>
        </div>
        <button type="button" className="quiet-button" onClick={onClose}>
          返回工作区
        </button>
      </div>

      {loading ? <p className="status-line">正在加载模型配置…</p> : null}
      {error ? (
        <div className="error-banner" role="alert">
          <strong>设置不可用</strong>
          <span>{error}</span>
        </div>
      ) : null}

      <div className="provider-list" aria-busy={loading}>
        {providers.map((provider) => (
          <ProviderRow key={provider.provider_id} provider={provider} onEdit={onEdit} />
        ))}
        {!loading && providers.length === 0 ? (
          <p className="empty-inline">本地服务没有提供可用的模型服务商。</p>
        ) : null}
      </div>

      {editing ? (
        <div className="editor-surface">
          <ProviderEditor
            provider={providers.find((item) => item.provider_id === editing) ?? null}
            onClose={onCloseEditor}
            onChange={onChange}
          />
        </div>
      ) : (
        <p className="settings-note">
          连接模型服务商后，模型会出现在顶栏并可用于新对话。
        </p>
      )}
    </section>
  );
}

function ProviderRow({
  provider,
  onEdit,
}: {
  provider: ProviderView;
  onEdit: (providerId: string) => void;
}) {
  return (
    <article className="provider-row">
      <div className="provider-row-main">
        <div className="provider-row-title">
          <h2>{provider.display_name}</h2>
          {provider.is_default ? <span className="default-badge">默认</span> : null}
        </div>
        <p className={`provider-state ${provider.configured ? "ready" : "not-ready"}`}>
          <span className="status-dot" aria-hidden="true" />
          {provider.configured ? "已连接" : "未连接"}
        </p>
        <p className="provider-model">
          {provider.configured
            ? provider.selected_model ?? "尚未选择模型"
            : "添加凭据以发现模型"}
        </p>
      </div>
      <button
        type="button"
        className={provider.configured ? "quiet-button" : "primary-button"}
        aria-label={`${provider.configured ? "管理" : "连接"} ${provider.display_name}`}
        onClick={() => onEdit(provider.provider_id)}
      >
        {provider.configured ? "管理" : "连接"}
      </button>
    </article>
  );
}

type ThreadPanelProps = {
  providers: ProviderView[];
  providerReady: boolean;
  workspace: WorkspaceRecord | null;
  onWorkspaceRecovered: (workspace: WorkspaceRecord) => void;
  selectedProviderId: string | null;
  selectedModel: string;
  onOpenProject: () => void;
  onOpenSettings: () => void;
};

function ThreadPanel({
  providers,
  providerReady,
  workspace,
  onWorkspaceRecovered,
  selectedProviderId,
  selectedModel,
  onOpenProject,
  onOpenSettings,
}: ThreadPanelProps) {
  const [threads, setThreads] = useState<ThreadView[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composer, setComposer] = useState("");
  const [mobileView, setMobileView] = useState<
    "navigation" | "conversation" | "activity"
  >("conversation");
  const [activityCollapsed, setActivityCollapsed] = useState(true);
  const [localTitles, setLocalTitles] = useState<Record<string, string>>({});
  const [pendingUserMessages, setPendingUserMessages] = useState<Record<string, string>>({});

  const active = threads.find(
    (thread) => thread.snapshot.thread_id === activeId,
  ) ?? null;
  const configuredProviders = providers.filter((provider) => provider.configured);
  const defaultProvider =
    providers.find((provider) => provider.is_default) ?? configuredProviders[0] ?? null;
  const creationProvider =
    providers.find((provider) => provider.provider_id === selectedProviderId) ??
    defaultProvider;
  const creationModel = selectedModel || creationProvider?.selected_model || "";

  useEffect(() => {
    let mounted = true;
    void getThreads()
      .then((response) => {
        if (mounted) {
          setThreads(response);
          setActiveId((current) => current ?? response[0]?.snapshot.thread_id ?? null);
          if (response[0]?.workspace) {
            onWorkspaceRecovered(response[0].workspace);
          }
          setError(null);
        }
      })
      .catch((reason: unknown) => {
        if (mounted) {
          setError(reason instanceof Error ? reason.message : "无法加载对话记录");
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
  }, [onWorkspaceRecovered]);

  useEffect(() => {
    if (active?.workspace) {
      onWorkspaceRecovered(active.workspace);
    }
  }, [active, onWorkspaceRecovered]);

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
      const message = reason instanceof Error ? reason.message : "本地服务请求失败";
      setError(`${failureLabel} · ${message}`);
    } finally {
      setBusy(false);
    }
  };

  const create = () =>
    run("创建对话失败", async () => {
      if (workspace === null) {
        throw new Error("请先选择项目");
      }
      if (!providerReady) {
        throw new Error("请先配置默认模型服务商和模型");
      }
      if (creationProvider === null || !creationModel.trim()) {
        throw new Error("请选择已配置的模型服务商和模型");
      }
      const next = await createThread(workspace.workspace_id, {
        provider_config_id: creationProvider.provider_id,
        model: creationModel.trim(),
      });
      replaceThread(next);
      setActivityCollapsed(true);
    });

  const refresh = () =>
    run("刷新对话失败", async () => {
      if (activeId !== null) {
        replaceThread(await getThread(activeId));
      }
    });

  const close = () =>
    run("关闭对话失败", async () => {
      if (activeId !== null) {
        replaceThread(await closeThread(activeId));
      }
    });

  const submit = () =>
    run("发送任务失败", async () => {
      if (active === null) {
        throw new Error("请先创建或选择对话");
      }
      if (!composer.trim()) {
        throw new Error("请输入要交给编码助手的任务");
      }
      const message = composer.trim();
      const submission = await startTurn(active.snapshot.thread_id, message);
      const threadId = active.snapshot.thread_id;
      if (threadTitle(active) === "新对话") {
        setLocalTitles((current) => ({ ...current, [threadId]: titleFromText(message) }));
      }
      setPendingUserMessages((current) => ({ ...current, [threadId]: message }));
      replaceThread({ ...active, submission });
      setComposer("");
    });

  const stop = () =>
    run("停止任务失败", async () => {
      if (active === null) {
        throw new Error("当前没有活动对话");
      }
      const submission = await cancelTurn(active.snapshot.thread_id);
      replaceThread({ ...active, submission });
    });

  const saveSettings = (providerId: string, model: string) =>
    run("更新对话设置失败", async () => {
      if (active === null) {
        throw new Error("当前没有活动对话");
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
    <section
      className={`agent-console ${activityCollapsed ? "activity-collapsed" : "activity-expanded"}`}
      aria-label="编码助手工作区"
    >
      <div className="mobile-view-switcher" aria-label="工作区视图">
        <button
          type="button"
          aria-label="显示对话记录"
          aria-pressed={mobileView === "navigation"}
          onClick={() => setMobileView("navigation")}
        >
          记录
        </button>
        <button
          type="button"
          aria-label="显示对话"
          aria-pressed={mobileView === "conversation"}
          onClick={() => setMobileView("conversation")}
        >
          对话
        </button>
        <button
          type="button"
          aria-label="显示动态"
          aria-pressed={mobileView === "activity"}
          onClick={() => setMobileView("activity")}
        >
          动态
        </button>
      </div>
      <nav
        className={`thread-sidebar mobile-view-${mobileView === "navigation" ? "active" : "inactive"}`}
        aria-label="对话记录"
      >
        <div className="thread-sidebar-heading">
          <button
            type="button"
            className="primary-button new-conversation-button"
            disabled={
              busy ||
              workspace === null ||
              !providerReady ||
              creationProvider === null ||
              !creationModel.trim()
            }
            onClick={() => void create()}
          >
            <span aria-hidden="true">＋</span> 新对话
          </button>
        </div>
        <div className="conversation-history-heading">
          <h2 id="threads-heading">对话记录</h2>
          <span>{threads.length}</span>
        </div>
        {loading ? <p className="status-line">正在加载对话记录…</p> : null}
        <div className="thread-list">
          {threads.map((thread) => (
            <button
              type="button"
              key={thread.snapshot.thread_id}
              className={
                thread.snapshot.thread_id === activeId ? "active" : undefined
              }
              onClick={() => {
                setActiveId(thread.snapshot.thread_id);
                setActivityCollapsed(true);
              }}
              title={threadTitle(thread, localTitles[thread.snapshot.thread_id])}
            >
              <span>{threadTitle(thread, localTitles[thread.snapshot.thread_id])}</span>
              {threadStatusLabel(thread.snapshot.status) ? (
                <small>{threadStatusLabel(thread.snapshot.status)}</small>
              ) : null}
            </button>
          ))}
          {!loading && threads.length === 0 ? (
            <p className="thread-list-empty">还没有对话。创建一个对话开始工作。</p>
          ) : null}
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
          conversationTitle={threadTitle(active, localTitles[active.snapshot.thread_id])}
          pendingUserMessage={pendingUserMessages[active.snapshot.thread_id] ?? null}
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
          activityCollapsed={activityCollapsed}
          onToggleActivity={() => setActivityCollapsed((current) => !current)}
        />
      ) : (
        <>
          <section
            className={`thread-detail mobile-view-${mobileView === "conversation" ? "active" : "inactive"}`}
            aria-label="编码助手对话"
          >
            <div className="empty-state">
              <span className="empty-state-mark" aria-hidden="true">＋</span>
              <p className="step-label">编码助手已就绪</p>
              <h1>
                {!workspace ? "选择一个项目开始" : !providerReady ? "尚未配置模型" : "开始新对话"}
              </h1>
              <p>
                {!workspace
                  ? "打开本地项目，然后让编码助手检查或修改代码。"
                  : !providerReady
                    ? "请先在设置中连接模型服务商，再创建对话。"
                    : "为当前项目创建对话，将任务、工具执行和文件修改集中在一起。"}
              </p>
              <button
                type="button"
                className="primary-button"
                onClick={!workspace ? onOpenProject : !providerReady ? onOpenSettings : () => void create()}
                disabled={busy}
              >
                {!workspace ? "打开项目" : !providerReady ? "配置模型" : "新对话"}
              </button>
            </div>
          </section>
          <aside
            className={`activity-panel activity-panel-collapsed mobile-view-${mobileView === "activity" ? "active" : "inactive"}`}
            aria-label="动态与修改"
          >
            <button
              type="button"
              className="activity-rail-toggle"
              aria-label="展开动态与修改"
              aria-expanded="false"
              onClick={() => setActivityCollapsed(false)}
            >
              <span aria-hidden="true">‹</span>
              <span>动态</span>
            </button>
          </aside>
        </>
      )}
      {/* Errors are kept outside switchable panes so narrow layouts never hide them. */}
      <div className="visually-hidden" aria-live="polite">
        {busy ? "本地服务请求处理中" : ""}
      </div>
    </section>
  );
}

function ActiveThreadView({
  thread,
  conversationTitle,
  pendingUserMessage,
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
  activityCollapsed,
  onToggleActivity,
}: {
  thread: ThreadView;
  conversationTitle: string;
  pendingUserMessage: string | null;
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
  activityCollapsed: boolean;
  onToggleActivity: () => void;
}) {
  const [state, setState] = useState(() => initialThreadState(thread));
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "disconnected"
  >("connecting");
  const previousConnection = useRef(connection);
  const [showReconnected, setShowReconnected] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [showMenu, setShowMenu] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [contextTab, setContextTab] = useState<"activity" | "changes">(
    state.files.length > 0 ? "changes" : "activity",
  );
  const [settingsProvider, setSettingsProvider] = useState(
    thread.snapshot.settings.provider_config_id,
  );
  const [settingsModel, setSettingsModel] = useState(
    thread.snapshot.settings.model,
  );
  const [initialCursor] = useState(thread.event_cursor);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const mountedRef = useRef(true);
  const threadId = thread.snapshot.thread_id;
  const submissionActive = thread.submission !== null;

  useEffect(() => () => {
    mountedRef.current = false;
  }, []);

  useEffect(() => {
    const wasDisconnected = previousConnection.current === "disconnected";
    previousConnection.current = connection;
    if (!wasDisconnected || connection !== "connected") {
      return;
    }
    setShowReconnected(true);
    const timer = window.setTimeout(() => setShowReconnected(false), 2400);
    return () => window.clearTimeout(timer);
  }, [connection]);

  useEffect(() => {
    if (!showMenu) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowMenu(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [showMenu]);

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
              ? `刷新对话失败 · ${reason.message}`
              : "刷新对话失败",
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
          if (!mountedRef.current) {
            return;
          }
          setState((current) => applyAgentEvent(current, event));
          setStreamError(null);
          if (eventRequiresSnapshotRefresh(event.type)) {
            void getThread(threadId)
              .then((next) => {
                if (mountedRef.current) {
                  onThread(next);
                }
              })
              .catch((reason: unknown) => {
                if (mountedRef.current) {
                  setStreamError(
                    reason instanceof Error
                      ? `恢复对话快照失败 · ${reason.message}`
                      : "恢复对话快照失败",
                  );
                }
              });
          }
        },
        onSnapshot: (next) => {
          if (!mountedRef.current) {
            return;
          }
          onThread(next);
          setState(initialThreadState(next));
          setStreamError(null);
        },
        onConnection: setConnection,
        recover: () => getThread(threadId),
        onError: (message) => {
          if (mountedRef.current) {
            setStreamError(message);
          }
        },
      },
    );
    client.start();
    return () => client.stop();
  }, [initialCursor, onThread, submissionActive, threadId]);

  const turnActive = thread.submission !== null;
  const turnStatus = summaryText(state.terminal, "status") ?? "idle";
  const displayedThreadStatus = turnActive ? "running" : thread.snapshot.status;
  const displayedTurnStatus = state.cancel_requested
    ? "cancel requested"
    : turnActive
      ? thread.submission?.status ?? "running"
      : turnStatus;
  const currentTerminal = turnActive ? null : state.terminal;
  const usage = isRecord(currentTerminal?.usage) ? currentTerminal.usage : null;
  const headerStatus = state.cancel_requested
    ? "正在停止…"
    : turnActive
      ? thread.submission?.status === "starting"
        ? "正在启动编码助手…"
        : thread.submission?.status === "cancelling"
          ? "正在停止…"
          : "编码助手正在工作"
      : thread.snapshot.status === "closed"
        ? "已关闭"
        : showReconnected
          ? "已重新连接"
        : connection === "disconnected"
          ? "连接中断，正在重连…"
          : connection === "connecting"
            ? "正在连接…"
            : null;

  return (
    <>
      <section
        className={`thread-detail mobile-view-${mobileView === "conversation" ? "active" : "inactive"}`}
        aria-label="编码助手对话"
      >
        <div className="thread-scroll">
          <div className="thread-detail-heading">
            <div className="thread-heading-copy">
              <h3>{conversationTitle}</h3>
              {headerStatus ? (
                <p className={`conversation-status status-${displayedThreadStatus}`} aria-live="polite">
                  <span className="status-dot" aria-hidden="true" />
                  {headerStatus}
                </p>
              ) : null}
            </div>
            <div className="conversation-menu-wrap">
              <button
                type="button"
                className="icon-button conversation-menu-trigger"
                aria-label="对话选项"
                aria-expanded={showMenu}
                aria-haspopup="menu"
                onClick={() => setShowMenu((current) => !current)}
              >
                ⋯
              </button>
              {showMenu ? (
                <div className="conversation-menu" role="menu" aria-label="对话选项菜单">
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setShowSettings(true);
                      setShowDetails(false);
                      setShowMenu(false);
                    }}
                  >
                    对话设置
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    disabled={busy}
                    onClick={() => {
                      onRefresh();
                      setShowMenu(false);
                    }}
                  >
                    刷新状态
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setShowDetails(true);
                      setShowSettings(false);
                      setShowMenu(false);
                    }}
                  >
                    对话详情
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className="danger-menu-item"
                    disabled={busy || thread.snapshot.status === "closed"}
                    onClick={() => {
                      onClose();
                      setShowMenu(false);
                    }}
                  >
                    关闭对话
                  </button>
                </div>
              ) : null}
            </div>
          </div>
          {showSettings ? (
            <div className="thread-settings-editor" aria-label="对话设置">
              <div className="settings-editor-heading">
                <div>
                  <p className="step-label">对话设置</p>
                  <h4>当前对话使用的模型</h4>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="关闭对话设置"
                  onClick={() => setShowSettings(false)}
                >
                  ×
                </button>
              </div>
              <div className="settings-editor-fields">
                <label htmlFor="thread-settings-provider">模型服务商</label>
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
                <label htmlFor="thread-settings-model">对话模型</label>
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
                  保存对话设置
                </button>
              </div>
              <p className="field-help">
                更改将在下一次模型请求时生效。
              </p>
            </div>
          ) : null}
          {showDetails ? (
            <div className="conversation-details" aria-label="对话详情">
              <div className="settings-editor-heading">
                <div>
                  <p className="step-label">技术信息</p>
                  <h4>对话详情</h4>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="关闭对话详情"
                  onClick={() => setShowDetails(false)}
                >
                  ×
                </button>
              </div>
              <dl>
                <div><dt>内部 ID</dt><dd><code>{thread.snapshot.thread_id}</code></dd></div>
                <div><dt>项目目录</dt><dd><code>{thread.snapshot.workspace}</code></dd></div>
                <div><dt>模型服务商</dt><dd>{providerName}</dd></div>
                <div><dt>模型</dt><dd><code>{thread.snapshot.settings.model}</code></dd></div>
                <div><dt>设置版本</dt><dd>{thread.snapshot.settings.version}</dd></div>
                <div><dt>状态</dt><dd>{threadStatusLabel(thread.snapshot.status) || "空闲"}</dd></div>
              </dl>
            </div>
          ) : null}
          <ConversationFeed
            state={state}
            connection={connection}
            streamError={streamError}
            workspace={thread.snapshot.workspace}
            pendingUserMessage={pendingUserMessage}
            onStarter={onComposer}
          />
          {state.approval ? (
            <ApprovalCard
              approval={state.approval}
              busy={approvalBusy}
              onResolve={(approved) => {
                setApprovalBusy(true);
                void resolveApproval(threadId, state.approval?.approval_id ?? "", approved)
                  .catch((reason: unknown) => {
                    setStreamError(
                      reason instanceof Error ? reason.message : "确认请求失败",
                    );
                  })
                  .finally(() => setApprovalBusy(false));
              }}
            />
          ) : null}
        </div>
        <div className="composer">
          <div className="composer-heading">
            <label htmlFor="agent-composer">询问编码助手</label>
            <span>
              {thread.snapshot.status === "closed"
                ? "此对话已关闭"
                : turnActive
                  ? "编码助手正在工作，可随时停止"
                  : "回车发送 · Shift + 回车换行"}
            </span>
          </div>
          <div className="composer-row">
            <textarea
              id="agent-composer"
              rows={3}
              value={composer}
              disabled={thread.snapshot.status === "closed"}
              onChange={(event) => onComposer(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !busy &&
                  !turnActive &&
                  thread.snapshot.status !== "closed" &&
                  composer.trim()
                ) {
                  event.preventDefault();
                  onSubmit();
                }
              }}
              placeholder="让编码助手处理当前项目中的任务…"
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
                发送
              </button>
              {turnActive ? (
                <button
                  type="button"
                  className="danger-button"
                  disabled={busy || thread.submission?.status === "cancelling"}
                  onClick={onStop}
                >
                  停止
                </button>
              ) : null}
            </div>
          </div>
        </div>
      </section>
      <aside
        className={`activity-panel ${activityCollapsed ? "activity-panel-collapsed" : ""} mobile-view-${mobileView === "activity" ? "active" : "inactive"}`}
        aria-label="动态与修改"
      >
        {activityCollapsed ? (
          <button
            type="button"
            className="activity-rail-toggle"
            aria-label="展开动态与修改"
            aria-expanded="false"
            aria-controls="conversation-context-panel"
            onClick={() => {
              setContextTab(!turnActive && state.files.length > 0 ? "changes" : "activity");
              onToggleActivity();
            }}
          >
            <span aria-hidden="true">‹</span>
            <span>{turnActive ? "动态 ●" : state.files.length > 0 ? `修改 ${state.files.length}` : "动态"}</span>
          </button>
        ) : (
          <>
            <div className="context-panel-header">
              <div className="context-tabs" role="tablist" aria-label="上下文面板">
                <button
                  type="button"
                  role="tab"
                  aria-selected={contextTab === "activity"}
                  onClick={() => setContextTab("activity")}
                >
                  动态
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={contextTab === "changes"}
                  onClick={() => setContextTab("changes")}
                >
                  修改 {state.files.length}
                </button>
              </div>
              <button
                type="button"
                className="icon-button context-collapse-button"
                aria-label="收起动态与修改"
                aria-expanded="true"
                aria-controls="conversation-context-panel"
                onClick={onToggleActivity}
              >
                ›
              </button>
            </div>
            <div className="activity-content" id="conversation-context-panel">
              {contextTab === "activity" ? (
                <div role="tabpanel" aria-label="动态">
                  {connection === "disconnected" ? (
                    <p className="inline-error" role="status">连接已中断，正在重连…</p>
                  ) : null}
                  <dl className="activity-summary">
                    <div><dt>对话状态</dt><dd>{runtimeStatusLabel(displayedThreadStatus)}</dd></div>
                    <div><dt>任务状态</dt><dd>{runtimeStatusLabel(displayedTurnStatus)}</dd></div>
                    <div><dt>迭代次数</dt><dd>{summaryText(currentTerminal, "iterations") ?? "—"}</dd></div>
                    <div><dt>工具调用</dt><dd>{summaryText(currentTerminal, "tool_calls") ?? state.tools.length}</dd></div>
                    <div><dt>输入令牌</dt><dd>{summaryText(usage, "input_tokens") ?? "—"}</dd></div>
                    <div><dt>输出令牌</dt><dd>{summaryText(usage, "output_tokens") ?? "—"}</dd></div>
                    <div><dt>结束原因</dt><dd>{runtimeStatusLabel(summaryText(currentTerminal, "stop_reason") ?? "—")}</dd></div>
                    <div><dt>已完成任务</dt><dd>{thread.snapshot.completed_turns}</dd></div>
                  </dl>
                  <dl className="activity-timestamps">
                    <div><dt>开始时间</dt><dd>{summaryText(currentTerminal, "started_at") ?? "—"}</dd></div>
                    <div><dt>结束时间</dt><dd>{summaryText(currentTerminal, "ended_at") ?? "—"}</dd></div>
                  </dl>
                  <div className="activity-section" aria-labelledby="tool-activity-heading">
                    <div className="activity-section-heading">
                      <p id="tool-activity-heading">工具执行</p>
                      <span>{state.tools.length}</span>
                    </div>
                    <div className="activity-tool-list" aria-label="工具执行摘要">
                      {state.tools.map((tool) => (
                        <article className={`activity-tool ${tool.status}`} key={tool.id}>
                          <span aria-hidden="true">
                            {tool.status === "success" ? "✓" : tool.status === "error" ? "×" : "○"}
                          </span>
                          <div>
                            <strong>{toolNameLabel(tool.name)}</strong>
                            <small>{toolStatusLabel(tool.status)}</small>
                          </div>
                        </article>
                      ))}
                      {state.tools.length === 0 ? <p className="thread-empty">暂无工具调用。</p> : null}
                    </div>
                  </div>
                  {currentTerminal ? (
                    <p className="terminal-state">
                      任务 · {runtimeStatusLabel(String(currentTerminal.status ?? "completed"))}
                    </p>
                  ) : null}
                </div>
              ) : (
                <div role="tabpanel" aria-label="修改">
                  <div className="activity-section-heading changes-panel-heading">
                    <div>
                      <h4>文件修改</h4>
                      <p>查看编码助手报告的文件和差异。</p>
                    </div>
                    <span>{state.files.length}</span>
                  </div>
                  <div className="changed-files">
                    {state.files.map((file) => (
                      <details className="file-change" key={file.path}>
                        <summary><span>{fileChangeLabel(file.change_type)}</span> {file.path}</summary>
                        {file.diff ? <pre>{file.diff}</pre> : <p>没有可显示的文本差异。</p>}
                      </details>
                    ))}
                    {state.files.length === 0 ? (
                      <p className="thread-empty">当前对话还没有文件修改。</p>
                    ) : null}
                    {currentTerminal?.diff_complete === false ? (
                      <p className="diff-incomplete" role="status">
                        差异可能不完整：命令可能修改了文件工具追踪范围之外的内容。
                      </p>
                    ) : null}
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </aside>
    </>
  );
}

function ConversationFeed({
  state,
  connection,
  streamError,
  workspace,
  pendingUserMessage,
  onStarter,
}: {
  state: ReturnType<typeof hydrateThread>;
  connection: "connecting" | "connected" | "disconnected";
  streamError: string | null;
  workspace: string;
  pendingUserMessage: string | null;
  onStarter: (value: string) => void;
}) {
  const showPendingUserMessage =
    pendingUserMessage !== null &&
    !state.messages.some(
      (message) => message.role === "user" && message.text === pendingUserMessage,
    );
  return (
    <section className="conversation" aria-label="对话时间线">
      {connection === "disconnected" ? (
        <p className="inline-error" role="status">
          连接已中断，正在恢复当前对话…
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
            <p className="message-role">
              {message.role === "user" ? "你" : "编码助手"}
            </p>
            <p>{message.text}</p>
          </article>
        ))}
        {showPendingUserMessage ? (
          <article className="message user pending-user-message">
            <p className="message-role">你</p>
            <p>{pendingUserMessage}</p>
          </article>
        ) : null}
        {state.provisional !== null &&
        (state.provisional.text ||
          state.provisional.reasoning ||
          state.provisional.tool_calls.length > 0) ? (
          <article className="message assistant provisional-message" aria-live="polite">
            <p className="message-role">编码助手 · 正在生成</p>
            {state.provisional.text ? <p>{state.provisional.text}</p> : null}
            {state.provisional.reasoning ? (
              <p className="message-reasoning">{state.provisional.reasoning}</p>
            ) : null}
            {state.provisional.tool_calls.length > 0 ? (
              <ul className="provisional-tool-calls">
                {state.provisional.tool_calls.map((call) => (
                  <li key={call.index}>
                    <code>{call.name ?? "tool"}</code>
                    {call.arguments ? <code>{call.arguments}</code> : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </article>
        ) : null}
        {state.messages.length === 0 && !showPendingUserMessage ? (
          <div className="conversation-empty">
            <div className="conversation-empty-mark" aria-hidden="true">&gt;_</div>
            <h4>想让编码助手做什么？</h4>
            <p>
              编码助手会在 <strong>{projectName(workspace)}</strong> 中读取文件、运行工具并汇报修改。
            </p>
            <div className="starter-prompts" aria-label="推荐任务">
              {STARTER_PROMPTS.map((prompt) => (
                <button type="button" key={prompt} onClick={() => onStarter(prompt)}>
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      {state.tools.length > 0 ? (
        <div className="agent-work-cluster" aria-label="编码助手工作过程">
          <div className="artifact-heading">
            <h4>工作过程</h4>
            <span>{state.tools.length} 项</span>
          </div>
          <div className="tool-list">
            {state.tools.map((tool) => {
              const target = toolTarget(tool.arguments);
              return (
                <article className={`tool-card ${tool.status}`} key={tool.id}>
                  <div className="tool-card-heading">
                    <span className="tool-status-icon" aria-hidden="true">
                      {tool.status === "success" ? "✓" : tool.status === "error" ? "×" : "●"}
                    </span>
                    <div className="tool-card-copy">
                      <strong>{toolNameLabel(tool.name)}</strong>
                      {target ? <code title={target}>{target}</code> : null}
                    </div>
                    <span>{toolStatusLabel(tool.status)}</span>
                  </div>
                  <details open={tool.status === "error"}>
                    <summary>技术详情</summary>
                    <dl className="tool-technical-details">
                      <div><dt>原始工具</dt><dd><code>{tool.name}</code></dd></div>
                    </dl>
                    {tool.arguments !== null ? (
                      <div className="tool-detail-block">
                        <strong>参数</strong>
                        <pre>{JSON.stringify(tool.arguments, null, 2)}</pre>
                      </div>
                    ) : null}
                    {tool.result ? (
                      <div className="tool-detail-block">
                        <strong>结果</strong>
                        <pre>{tool.result}</pre>
                      </div>
                    ) : null}
                    {tool.metadata ? (
                      <div className="tool-detail-block">
                        <strong>元数据</strong>
                        <pre>{JSON.stringify(tool.metadata, null, 2)}</pre>
                      </div>
                    ) : null}
                    {tool.error_code ? <p className="tool-error-code">{tool.error_code}</p> : null}
                  </details>
                </article>
              );
            })}
          </div>
        </div>
      ) : null}
      {state.files.length > 0 ? (
        <div className="conversation-files" aria-label="对话文件修改">
          <div className="artifact-heading">
            <h4>文件修改</h4>
            <span>{state.files.length} 个文件</span>
          </div>
          {state.files.map((file) => (
            <article className="conversation-file-card" key={file.path}>
              <div>
                <strong>{file.path}</strong>
                <span>{fileChangeLabel(file.change_type)}</span>
              </div>
              {file.diff ? (
                <details>
                  <summary>查看差异</summary>
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

function ApprovalCard({
  approval,
  busy,
  onResolve,
}: {
  approval: ApprovalRequest;
  busy: boolean;
  onResolve: (approved: boolean) => void;
}) {
  const target = toolTarget(approval.tool_call);
  return (
    <aside className="approval-card" role="alert" aria-label="等待确认">
      <div>
        <strong>需要确认后继续</strong>
        <p>{approval.message}</p>
        <p className="approval-reason">
          <code>{approval.reason_code}</code>
          {target ? <code title={target}>{target}</code> : null}
        </p>
      </div>
      <div className="approval-actions">
        <button
          type="button"
          className="primary-button"
          disabled={busy}
          onClick={() => onResolve(true)}
        >
          批准
        </button>
        <button
          type="button"
          className="quiet-button"
          disabled={busy}
          onClick={() => onResolve(false)}
        >
          拒绝
        </button>
      </div>
    </aside>
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
      setEditorError(reason instanceof Error ? reason.message : "请求失败");
    } finally {
      setBusy(false);
    }
  };

  const saveAndDiscover = () =>
    run(async () => {
      if (!apiKey.trim()) {
        throw new Error("请输入访问密钥");
      }
      const saved = await saveProvider(
        provider.provider_id,
        apiKey,
        model || null,
      );
      onChange(saved);
      setApiKey("");
      setMessage(`凭据已保存 · ${saved.credential_mask ?? "已配置"}`);
      const discovered = await discoverModels(provider.provider_id);
      setModels(discovered.models);
      if (!model && discovered.models[0]) {
        setModel(discovered.models[0]);
      }
      if (discovered.models.length === 0) {
        setMessage("凭据已保存 · 模型服务商未返回模型，请手动输入");
      }
    });

  const makeDefault = () =>
    run(async () => {
      if (!model.trim()) {
        throw new Error("请选择或输入模型");
      }
      const selected = await selectProviderDefault(provider.provider_id, model);
      onChange(selected);
      setMessage("已设为默认模型服务商");
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
          ? `发现 ${discovered.models.length} 个可用模型`
          : "模型服务商未返回模型，请手动输入",
      );
    });

  const clear = () =>
    run(async () => {
      const cleared = await clearProviderCredential(provider.provider_id);
      onChange(cleared);
      setModels([]);
      setMessage("凭据已清除");
    });

  return (
    <aside className="provider-editor" aria-labelledby="editor-heading">
      <div className="editor-heading-row">
        <div>
          <p className="step-label">模型服务商设置</p>
          <h2 id="editor-heading">{provider.display_name}</h2>
        </div>
        <button type="button" className="quiet-button" onClick={onClose}>
          关闭
        </button>
      </div>

      {editorError ? (
        <div className="inline-error" role="alert" tabIndex={-1}>
          {editorError}
        </div>
      ) : null}
      {message ? <p className="success-line">{message}</p> : null}

      <div className="field-group">
        <label htmlFor="provider-key">访问密钥</label>
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
            aria-label={showKey ? "隐藏访问密钥" : "显示访问密钥"}
            onClick={() => setShowKey((current) => !current)}
          >
            {showKey ? "隐藏" : "显示"}
          </button>
        </div>
        <p className="field-help">凭据保存在本地服务中，不会由接口返回。</p>
      </div>

      <button
        type="button"
        className="primary-button"
        disabled={busy}
        onClick={() => void saveAndDiscover()}
      >
        {busy ? "正在连接…" : "保存并发现模型"}
      </button>

      <div className="field-group">
        <label htmlFor="provider-model">服务商模型</label>
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
            placeholder="输入服务商模型标识"
            onChange={(event) => setModel(event.target.value)}
          />
        )}
        <p className="field-help">
          模型发现仅报告可访问的 ID；编码助手会在实际使用时检查兼容性。
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
            刷新模型
          </button>
        ) : null}
        <button
          type="button"
          className="primary-button"
          disabled={busy || !provider.configured}
          onClick={() => void makeDefault()}
        >
          设为默认
        </button>
        {provider.configured ? (
          <button
            type="button"
            className="danger-button"
            disabled={busy}
            onClick={() => void clear()}
          >
            清除凭据
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

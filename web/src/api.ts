export type ProviderView = {
  provider_id: "deepseek" | "moonshot" | "glm";
  display_name: string;
  configured: boolean;
  credential_mask: string | null;
  selected_model: string | null;
  is_default: boolean;
};

export type ProvidersResponse = {
  schema_version: number;
  default_provider_id: string | null;
  providers: ProviderView[];
};

export type ErrorEnvelope = {
  error: {
    status?: number;
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
};

export class HostError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "HostError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

type ProviderMutationResponse = {
  schema_version: number;
  provider: ProviderView;
};

export type ModelDiscoveryResponse = {
  schema_version: number;
  provider_id: string;
  models: string[];
  cached: boolean;
};

export type WorkspaceEntry = {
  name: string;
  path: string;
  type: "directory";
};

export type WorkspaceListing = {
  schema_version: number;
  path: string;
  parent: string | null;
  roots: string[];
  entries: WorkspaceEntry[];
  truncated: boolean;
};

export type WorkspaceRecord = {
  workspace_id: string;
  path: string;
  canonical_path: string;
  display_name: string;
};

export type WorkspaceSelectionResponse = {
  schema_version: number;
  workspace: WorkspaceRecord;
};

export type ThreadSettings = {
  provider_config_id: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  thinking: unknown | null;
  limits: {
    max_iterations: number;
    max_tool_calls: number;
    max_execution_seconds: number;
  };
  version: number;
  approval_mode?: "untrusted" | "on_request" | "never";
};

export type TurnSubmission = {
  thread_id: string;
  status: "starting" | "running" | "cancelling";
  accepted_at: string;
};

export type ApprovalResolutionResponse = {
  schema_version: number;
  thread_id: string;
  approval_id: string;
  approved: boolean;
};

export type ThreadView = {
  schema_version: number;
  snapshot: {
    schema_version: number;
    thread_id: string;
    workspace: string;
    status: "idle" | "running" | "waiting_approval" | "closed";
    active_turn_id: string | null;
    completed_turns: number;
    settings: ThreadSettings;
    messages: Array<Record<string, unknown>>;
    created_at: string;
    updated_at: string;
    latest_turn: Record<string, unknown> | null;
    pending_approval?: PendingApproval | null;
  };
  event_cursor: string | null;
  submission: TurnSubmission | null;
  workspace?: WorkspaceRecord;
  host_error?: {
    status?: number;
    code: string;
    message: string;
    details?: Record<string, unknown>;
  } | null;
};

export type PendingApproval = {
  approval_id: string;
  tool_call: Record<string, unknown> | null;
  timeout_seconds?: number;
  decision?: string;
  reason_code: string;
  message: string;
};

type ThreadsResponse = {
  schema_version: number;
  threads: ThreadView[];
};

type ThreadResponse = {
  schema_version: number;
  thread: ThreadView;
};

type SubmissionResponse = {
  schema_version: number;
  thread_id: string;
  submission: TurnSubmission;
};

export async function getProviders(): Promise<ProvidersResponse> {
  return requestJson<ProvidersResponse>("/api/providers");
}

export async function getWorkspaces(path?: string): Promise<WorkspaceListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return requestJson<WorkspaceListing>(`/api/workspaces${query}`);
}

export async function selectWorkspace(path: string): Promise<WorkspaceRecord> {
  return (
    await requestJson<WorkspaceSelectionResponse>("/api/workspaces/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    })
  ).workspace;
}

export async function createThread(
  workspaceId: string,
  selection?: { provider_config_id: string; model: string },
): Promise<ThreadView> {
  return (
    await requestJson<ThreadResponse>("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: workspaceId, ...selection }),
    })
  ).thread;
}

/*
 * Keep the lower-level request helper private so all API errors retain the
 * Host envelope rather than being flattened into an untyped Error string.
 */
async function requestJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    throw await hostError(response);
  }
  return (await response.json()) as T;
}

async function hostError(response: Response): Promise<HostError> {
  try {
    const payload = (await response.json()) as ErrorEnvelope;
    const error = payload.error;
    if (
      error &&
      typeof error.code === "string" &&
      typeof error.message === "string"
    ) {
      return new HostError(
        typeof error.status === "number" ? error.status : response.status,
        error.code,
        error.message,
        error.details ?? {},
      );
    }
  } catch {
    // Fall through to a stable transport-level HostError below.
  }
  return new HostError(
    response.status,
    "HOST_ERROR",
    `Host request failed (${response.status})`,
  );
}

export async function getThreads(): Promise<ThreadView[]> {
  return (await requestJson<ThreadsResponse>("/api/threads")).threads;
}

export async function getThread(threadId: string): Promise<ThreadView> {
  return (await requestJson<ThreadResponse>(`/api/threads/${threadId}`)).thread;
}


export async function updateThreadSettings(
  threadId: string,
  settings: ThreadSettings,
): Promise<ThreadView> {
  return (
    await requestJson<ThreadResponse>(`/api/threads/${threadId}/settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: settings.version,
        provider_config_id: settings.provider_config_id,
        model: settings.model,
        temperature: settings.temperature,
        max_tokens: settings.max_tokens,
        thinking: settings.thinking,
        limits: settings.limits,
        approval_mode: settings.approval_mode,
      }),
    })
  ).thread;
}

export async function closeThread(threadId: string): Promise<ThreadView> {
  return (
    await requestJson<ThreadResponse>(`/api/threads/${threadId}/close`, {
      method: "POST",
    })
  ).thread;
}

export async function startTurn(
  threadId: string,
  message: string,
): Promise<TurnSubmission> {
  return (
    await requestJson<SubmissionResponse>(`/api/threads/${threadId}/turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    })
  ).submission;
}

export async function cancelTurn(threadId: string): Promise<TurnSubmission> {
  return (
    await requestJson<SubmissionResponse>(`/api/threads/${threadId}/cancel`, {
      method: "POST",
    })
  ).submission;
}

export async function resolveApproval(
  threadId: string,
  approvalId: string,
  approved: boolean,
): Promise<ApprovalResolutionResponse> {
  return requestJson<ApprovalResolutionResponse>(
    `/api/threads/${threadId}/approvals/${encodeURIComponent(approvalId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    },
  );
}

export async function saveProvider(
  providerId: string,
  apiKey: string,
  selectedModel: string | null,
): Promise<ProviderView> {
  const response = await requestJson<ProviderMutationResponse>(
    `/api/providers/${providerId}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        selected_model: selectedModel,
      }),
    },
  );
  return response.provider;
}

export async function discoverModels(
  providerId: string,
): Promise<ModelDiscoveryResponse> {
  return requestJson<ModelDiscoveryResponse>(
    `/api/providers/${providerId}/models/discover`,
    { method: "POST" },
  );
}

export async function selectProviderDefault(
  providerId: string,
  model: string,
): Promise<ProviderView> {
  const response = await requestJson<ProviderMutationResponse>(
    "/api/provider-default",
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_id: providerId, model }),
    },
  );
  return response.provider;
}

export async function clearProviderCredential(
  providerId: string,
): Promise<ProviderView> {
  const response = await requestJson<ProviderMutationResponse>(
    `/api/providers/${providerId}/credential`,
    { method: "DELETE" },
  );
  return response.provider;
}

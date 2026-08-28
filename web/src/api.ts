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
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
};

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

export async function getProviders(): Promise<ProvidersResponse> {
  return requestJson<ProvidersResponse>("/api/providers");
}

export async function getWorkspaces(path?: string): Promise<WorkspaceListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return requestJson<WorkspaceListing>(`/api/workspaces${query}`);
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

async function hostError(response: Response): Promise<Error> {
  try {
    const payload = (await response.json()) as ErrorEnvelope;
    return new Error(payload.error.message);
  } catch {
    return new Error(`Host request failed (${response.status})`);
  }
}

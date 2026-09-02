import type { ProviderView } from "./api";

export type NextThreadSelection = {
  providerId: string;
  model: string;
};

export function resolveNextThreadSelection(
  providers: ProviderView[],
  requested: NextThreadSelection | null,
): NextThreadSelection | null {
  const configured = providers.filter((provider) => provider.configured);
  const provider =
    configured.find((candidate) => candidate.provider_id === requested?.providerId) ??
    configured.find((candidate) => candidate.is_default) ??
    configured[0];
  if (provider === undefined) {
    return null;
  }
  return {
    providerId: provider.provider_id,
    model:
      requested?.providerId === provider.provider_id
        ? requested.model
        : provider.selected_model ??
          (provider.catalog?.status === "ready" ? provider.catalog.models[0] : undefined) ??
          "",
  };
}

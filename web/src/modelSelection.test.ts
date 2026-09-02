import { expect, test } from "vitest";

import type { ProviderView } from "./api";
import { resolveNextThreadSelection } from "./modelSelection";

const providers: ProviderView[] = [
  {
    provider_id: "deepseek",
    display_name: "DeepSeek",
    configured: true,
    credential_mask: "••••one",
    selected_model: "deepseek-v4-pro",
    is_default: true,
  },
  {
    provider_id: "glm",
    display_name: "GLM",
    configured: true,
    credential_mask: "••••two",
    selected_model: "glm-5.3",
    is_default: false,
  },
];

test("topbar selection owns only the next Thread provider/model pair", () => {
  expect(resolveNextThreadSelection(providers, null)).toEqual({
    providerId: "deepseek",
    model: "deepseek-v4-pro",
  });
  expect(
    resolveNextThreadSelection(providers, { providerId: "glm", model: "glm-5.2" }),
  ).toEqual({ providerId: "glm", model: "glm-5.2" });
});

test("a removed provider cannot leak its model into the fallback provider", () => {
  expect(
    resolveNextThreadSelection(
      providers.map((provider) =>
        provider.provider_id === "glm" ? { ...provider, configured: false } : provider,
      ),
      { providerId: "glm", model: "glm-5.2" },
    ),
  ).toEqual({ providerId: "deepseek", model: "deepseek-v4-pro" });
});

test("changing a Provider default does not rewrite an explicit next-Thread choice", () => {
  expect(
    resolveNextThreadSelection(
      providers.map((provider) => ({
        ...provider,
        is_default: provider.provider_id === "glm",
      })),
      { providerId: "deepseek", model: "deepseek-v4-flash" },
    ),
  ).toEqual({ providerId: "deepseek", model: "deepseek-v4-flash" });
});

test("only a ready remote catalog seeds an unselected Provider model", () => {
  const withoutSelection: ProviderView[] = [{
    ...providers[0]!,
    selected_model: null,
    catalog: { status: "loading", models: [], cached: false, error_code: null },
  }];
  expect(resolveNextThreadSelection(withoutSelection, null)?.model).toBe("");
  expect(resolveNextThreadSelection([{
    ...withoutSelection[0]!,
    catalog: {
      status: "ready",
      models: ["account-model"],
      cached: false,
      error_code: null,
    },
  }], null)?.model).toBe("account-model");
});

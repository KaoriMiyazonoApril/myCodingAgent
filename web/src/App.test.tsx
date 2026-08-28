import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        schema_version: 1,
        default_provider_id: null,
        providers: [
          {
            provider_id: "deepseek",
            display_name: "DeepSeek",
            configured: false,
            credential_mask: null,
            selected_model: null,
            is_default: false,
          },
          {
            provider_id: "moonshot",
            display_name: "Moonshot / Kimi",
            configured: false,
            credential_mask: null,
            selected_model: null,
            is_default: false,
          },
          {
            provider_id: "glm",
            display_name: "GLM",
            configured: false,
            credential_mask: null,
            selected_model: null,
            is_default: false,
          },
        ],
      }),
    }),
  );
});

test("starts in provider setup mode without hiding the host state", async () => {
  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "Connect a model provider" }),
  ).toBeInTheDocument();
  expect(screen.getByText("DeepSeek")).toBeInTheDocument();
  expect(screen.getByText("Moonshot / Kimi")).toBeInTheDocument();
  expect(screen.getByText("GLM")).toBeInTheDocument();
  expect(
    screen.getByText(/Provider setup required before creating a thread\./),
  ).toBeInTheDocument();
});

test("saves a key, discovers models, and selects a default", async () => {
  const requests: Array<{ method: string; path: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ method, path });
      if (path.endsWith("/models/discover")) {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            provider_id: "deepseek",
            models: ["deepseek-chat", "deepseek-reasoner"],
            cached: false,
          }),
        };
      }
      if (method === "PUT") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            provider: {
              provider_id: "deepseek",
              display_name: "DeepSeek",
              configured: true,
              credential_mask: "••••cret",
              selected_model: null,
              is_default: false,
            },
          }),
        };
      }
      if (method === "PATCH") {
        return {
          ok: true,
          json: async () => ({
            schema_version: 1,
            provider: {
              provider_id: "deepseek",
              display_name: "DeepSeek",
              configured: true,
              credential_mask: "••••cret",
              selected_model: "deepseek-reasoner",
              is_default: true,
            },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          schema_version: 1,
          default_provider_id: null,
          providers: [
            {
              provider_id: "deepseek",
              display_name: "DeepSeek",
              configured: false,
              credential_mask: null,
              selected_model: null,
              is_default: false,
            },
          ],
        }),
      };
    }),
  );
  render(<App />);

  fireEvent.click(
    await screen.findByRole("button", { name: "Configure DeepSeek" }),
  );
  const key = screen.getByLabelText("API key");
  fireEvent.change(key, { target: { value: "sk-api-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and discover" }));

  expect(
    await screen.findByRole("option", { name: "deepseek-reasoner" }),
  ).toBeInTheDocument();
  expect(key).toHaveValue("");
  expect(screen.getByText("Key saved · ••••cret")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Model"), {
    target: { value: "deepseek-reasoner" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Use as default" }));

  expect((await screen.findAllByText("Default provider")).length).toBeGreaterThan(0);
  expect(requests).toEqual(
    expect.arrayContaining([
      { method: "PUT", path: "/api/providers/deepseek" },
      {
        method: "POST",
        path: "/api/providers/deepseek/models/discover",
      },
      { method: "PATCH", path: "/api/provider-default" },
    ]),
  );
});

test("shows provider request failures inline", async () => {
  const fetchMock = vi.mocked(fetch);
  fetchMock.mockImplementation(async (input, init) => {
    if ((init?.method ?? "GET") === "PUT") {
      return {
        ok: false,
        status: 400,
        json: async () => ({
          error: {
            code: "PROVIDER_AUTHENTICATION_FAILED",
            message: "Provider rejected the configured credential",
            details: {},
          },
        }),
      } as Response;
    }
    return {
      ok: true,
      json: async () => ({
        schema_version: 1,
        default_provider_id: null,
        providers: [
          {
            provider_id: "deepseek",
            display_name: "DeepSeek",
            configured: false,
            credential_mask: null,
            selected_model: null,
            is_default: false,
          },
        ],
      }),
    } as Response;
  });

  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Configure DeepSeek" }),
  );
  fireEvent.change(screen.getByLabelText("API key"), {
    target: { value: "rejected-key" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save and discover" }));

  expect(
    await screen.findByRole("alert"),
  ).toHaveTextContent("Provider rejected the configured credential");
});

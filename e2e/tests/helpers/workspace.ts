import { expect, type APIRequestContext } from "@playwright/test";

export const E2E_RESET_TOKEN = "novel-harness-playwright-reset-v1";

export async function resetToEmptyWorkspace(request: APIRequestContext) {
  const reset = await request.post("/api/v1/_e2e/reset", {
    headers: { "X-Novel-E2E-Reset": E2E_RESET_TOKEN },
  });
  expect(reset.status()).toBe(204);

  const projects = await request.get("/api/v1/projects");
  expect(projects.status()).toBe(200);
  expect(await projects.json()).toEqual([]);
}

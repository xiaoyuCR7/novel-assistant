import { afterEach, expect, it, vi } from "vitest";

import { projectApi } from "../src/lib/api";

afterEach(() => vi.restoreAllMocks());

it("encodes every version path segment while preserving cursor query encoding", async () => {
  const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    return new Response(url.includes("/diff/") ? JSON.stringify({ diff: "ok" }) : "{}", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  const client = projectApi("project/ ?#");

  await client.versions("chapter/ ?#");
  await client.versionPage("chapter/ ?#", "cursor /?#&");
  await client.compareVersions("chapter/ ?#", "from/ ?#", "to/ ?#");
  await client.restoreVersion("chapter/ ?#", "version/ ?#", 7);

  const urls = fetcher.mock.calls.map(([input]) => String(input));
  const base = "/api/v1/projects/project%2F%20%3F%23/chapters/chapter%2F%20%3F%23/versions";
  expect(urls[0]).toBe(base);
  expect(urls[1]).toBe(`${base}/page?limit=50&before=cursor+%2F%3F%23%26`);
  expect(urls[2]).toBe(`${base}/from%2F%20%3F%23/diff/to%2F%20%3F%23`);
  expect(urls[3]).toBe(`${base}/version%2F%20%3F%23/restore`);
});

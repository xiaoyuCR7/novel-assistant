/** @vitest-environment node */

import { describe, expect, it } from "vitest";

const repoRoot = new URL("../../", import.meta.url);
const staticRoot = new URL("backend/static/", repoRoot);
const distRoot = new URL("frontend/dist/", repoRoot);

describe("production static bundle", () => {
  it("keeps the complete frontend dist and served static trees byte-identical", async () => {
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { readFileSync, readdirSync } = await import("node:fs");
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { fileURLToPath } = await import("node:url");
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { join, relative } = await import("node:path");
    const files = (rootUrl: URL) => {
      const root = fileURLToPath(rootUrl);
      const walk = (directory: string): string[] => readdirSync(directory, { withFileTypes: true })
        .flatMap((entry: { isDirectory: () => boolean; name: string }) => {
          const path = join(directory, entry.name);
          return entry.isDirectory() ? walk(path) : [relative(root, path).replaceAll("\\", "/")];
        });
      return walk(root).sort();
    };
    const distFiles = files(distRoot);
    const staticFiles = files(staticRoot);

    expect(staticFiles).toEqual(distFiles);
    for (const file of distFiles) {
      const served = readFileSync(new URL(file, staticRoot));
      const built = readFileSync(new URL(file, distRoot));
      expect(served.equals(built), `served file differs from dist: ${file}`).toBe(true);
    }
  });

  it("uses the official build-and-copy script in the full test gate", async () => {
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { readFileSync } = await import("node:fs");
    const testScript = readFileSync(new URL("scripts/test.ps1", repoRoot), "utf8");
    const safetyTest = testScript.indexOf('tests\\build-safety.test.ps1');
    const buildCall = testScript.indexOf('scripts\\build.ps1');
    const frontendTests = testScript.indexOf("npm test -- --run");

    expect(safetyTest).toBeGreaterThanOrEqual(0);
    expect(buildCall).toBeGreaterThanOrEqual(0);
    expect(frontendTests).toBeGreaterThanOrEqual(0);
    expect(safetyTest).toBeLessThan(buildCall);
    expect(buildCall).toBeLessThan(frontendTests);
    expect(testScript.match(/scripts[\\/]build\.ps1/g)).toHaveLength(1);
    expect(testScript).not.toMatch(/npm\s+run\s+build/i);
  });

  it("reserves dist cleanup for the safe wrapper and documents the root verification command", async () => {
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { readFileSync } = await import("node:fs");
    const viteConfig = readFileSync(new URL("frontend/vite.config.ts", repoRoot), "utf8");
    const readme = readFileSync(new URL("README.md", repoRoot), "utf8");

    expect(viteConfig).toMatch(/emptyOutDir:\s*false/);
    expect(readme).toContain("powershell -ExecutionPolicy Bypass -File .\\scripts\\test.ps1");
    expect(readme).not.toMatch(/^\s*npm run build\s*$/m);
    expect(readme).toMatch(/build\.ps1[^\n]*backend[\\/]static/);
  });

  it("serves the paged version client and truncation visibility from the referenced bundle", async () => {
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { existsSync, readFileSync } = await import("node:fs");
    const html = readFileSync(new URL("index.html", staticRoot), "utf8");
    const scriptSource = html.match(/<script\b[^>]*\bsrc=["']([^"']+\.js)["'][^>]*>/i)?.[1];

    expect(scriptSource, "backend/static/index.html must reference a JavaScript bundle").toBeDefined();
    const bundlePath = new URL(scriptSource!.replace(/^\/+/, ""), staticRoot);
    expect(existsSync(bundlePath), `referenced bundle must exist: ${bundlePath.pathname}`).toBe(true);

    const bundle = readFileSync(bundlePath, "utf8");
    expect(bundle).toContain("versions/page");
    expect(bundle).toContain("上下文已截断");
    expect(bundle).toContain("paged-metadata-v1");

    const appSource = readFileSync(new URL("frontend/src/app/App.tsx", repoRoot), "utf8");
    expect(appSource).not.toMatch(/\.versions\s*\(/);
  });

  it("does not run a late browser drift check after summary publication", async () => {
    // @ts-expect-error This Vitest file runs in Node; the browser build intentionally omits global Node types.
    const { readFileSync } = await import("node:fs");
    const appSource = readFileSync(new URL("frontend/src/app/App.tsx", repoRoot), "utf8");

    expect(appSource).not.toMatch(/client\.checkDrift\s*\(/);
    expect(appSource).not.toContain("studio:drift-observed:");
  });
});

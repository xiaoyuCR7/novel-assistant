import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { removeE2eDataDir } from "../global-teardown.mjs";
import { runCommandWithCleanup } from "../run-playwright.mjs";

test("removes only a prefixed direct child of the system temp directory", async () => {
  const suffix = `${process.pid}-${Date.now()}`;
  const target = path.join(os.tmpdir(), `novel-harness-e2e-${suffix}`);
  const sibling = await mkdtemp(path.join(os.tmpdir(), "novel-harness-e2e-sibling-"));
  const sentinel = path.join(sibling, "sentinel.txt");

  try {
    await mkdir(target, { recursive: true });
    await writeFile(path.join(target, "project.db"), "temporary test data");
    await writeFile(sentinel, "must survive");

    await removeE2eDataDir(target);

    await assert.rejects(readFile(path.join(target, "project.db")), { code: "ENOENT" });
    assert.equal(await readFile(sentinel, "utf8"), "must survive");
  } finally {
    await rm(target, { recursive: true, force: true });
    await rm(sibling, { recursive: true, force: true });
  }
});

test("refuses targets outside temp, with the wrong prefix, or below a nested directory", async () => {
  const outside = path.join(process.cwd(), "novel-harness-e2e-do-not-delete");
  const wrongPrefix = path.join(os.tmpdir(), `other-e2e-${process.pid}`);
  const nested = path.join(
    os.tmpdir(),
    `novel-harness-e2e-${process.pid}`,
    `novel-harness-e2e-${process.pid}-nested`,
  );

  await assert.rejects(removeE2eDataDir(outside), /system temporary directory/);
  await assert.rejects(removeE2eDataDir(wrongPrefix), /novel-harness-e2e-/);
  await assert.rejects(removeE2eDataDir(nested), /direct child/);
});

test("waits for the child process to exit before removing its E2E data root", async () => {
  const target = path.join(os.tmpdir(), `novel-harness-e2e-${process.pid}-${Date.now()}`);
  await mkdir(target);
  const childScript = [
    "const fs = require('node:fs');",
    "const path = require('node:path');",
    "setTimeout(() => fs.writeFileSync(path.join(process.argv[1], 'child-finished'), 'yes'), 50);",
  ].join("");

  try {
    const exitCode = await runCommandWithCleanup(
      process.execPath,
      ["-e", childScript, target],
      { dataDir: target, env: process.env, stdio: "ignore" },
    );

    assert.equal(exitCode, 0);
    await assert.rejects(readFile(path.join(target, "child-finished")), { code: "ENOENT" });
  } finally {
    await rm(target, { recursive: true, force: true });
  }
});

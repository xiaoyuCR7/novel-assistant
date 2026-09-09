import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { removeE2eDataDir } from "./global-teardown.mjs";

export async function runCommandWithCleanup(
  command,
  args,
  { dataDir, env, stdio = "inherit" },
) {
  let runError;
  let exitCode = 1;
  try {
    exitCode = await new Promise((resolve, reject) => {
      const child = spawn(command, args, { env, stdio, windowsHide: true });
      child.once("error", reject);
      child.once("exit", (code) => resolve(code ?? 1));
    });
  } catch (error) {
    runError = error;
  }

  let cleanupError;
  try {
    await removeE2eDataDir(dataDir);
  } catch (error) {
    cleanupError = error;
  }
  if (runError) throw runError;
  if (cleanupError) throw cleanupError;
  return exitCode;
}

async function main() {
  const dataDir = path.join(os.tmpdir(), `novel-harness-e2e-${process.pid}`);
  const playwrightCli = fileURLToPath(
    new URL("./node_modules/@playwright/test/cli.js", import.meta.url),
  );
  const exitCode = await runCommandWithCleanup(
    process.execPath,
    [playwrightCli, "test", ...process.argv.slice(2)],
    {
      dataDir,
      env: { ...process.env, NOVEL_E2E_DATA_DIR: dataDir },
    },
  );
  process.exitCode = exitCode;
}

if (
  process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

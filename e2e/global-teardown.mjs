import { lstat, realpath, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const E2E_DIRECTORY_PREFIX = "novel-harness-e2e-";

export async function removeE2eDataDir(candidate) {
  if (typeof candidate !== "string" || candidate.length === 0) {
    throw new Error("E2E cleanup requires an explicit data directory.");
  }

  const tempRoot = await realpath(os.tmpdir());
  const target = path.resolve(candidate);
  const normalizedTempRoot = tempRoot.toLowerCase();
  const normalizedTarget = target.toLowerCase();
  if (!normalizedTarget.startsWith(`${normalizedTempRoot}${path.sep}`)) {
    throw new Error(`Refusing E2E cleanup outside the system temporary directory: ${target}`);
  }
  if (path.dirname(target).toLowerCase() !== normalizedTempRoot) {
    throw new Error(`Refusing E2E cleanup unless the target is a direct child of the system temporary directory: ${target}`);
  }
  if (!path.basename(target).startsWith(E2E_DIRECTORY_PREFIX)) {
    throw new Error(`Refusing E2E cleanup without the ${E2E_DIRECTORY_PREFIX} prefix: ${target}`);
  }

  let targetInfo;
  try {
    targetInfo = await lstat(target);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw error;
  }
  if (!targetInfo.isDirectory() || targetInfo.isSymbolicLink()) {
    throw new Error(`Refusing E2E cleanup of a non-directory or linked target: ${target}`);
  }

  const resolvedTarget = await realpath(target);
  if (
    resolvedTarget.toLowerCase() !== target.toLowerCase()
    || path.dirname(resolvedTarget).toLowerCase() !== tempRoot.toLowerCase()
  ) {
    throw new Error(`Refusing E2E cleanup through a linked or redirected path: ${target}`);
  }

  await rm(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
}

export default async function globalTeardown() {
  await removeE2eDataDir(process.env.NOVEL_E2E_DATA_DIR);
}

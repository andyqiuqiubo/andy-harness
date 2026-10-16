import { createHash } from "node:crypto";
import { expect, it } from "vitest";
import { JavaScriptAnalysisWorker } from "../../../src/javascript/analysis/JavaScriptAnalysisWorker.js";
import { cleanupOwnedProcessGroup } from "../../../src/process/ProcessOwnership.js";
import { ProviderCleanupError } from "../../../src/domain/providerCleanupError.js";
import { projectAnalysisError } from "../../../src/domain/analysisErrorProjection.js";
import { spawnOwnedProviderProcess } from "../../../src/process/ProviderProcess.js";
import type { JavaScriptArtifactFile } from "../../../src/domain/javascript/javascriptArtifactFiles.js";

const source = (path: string, value: string): JavaScriptArtifactFile => ({
  path,
  sha256: createHash("sha256").update(value).digest("hex"),
  container_sha256: "1".repeat(64),
  bytes: Buffer.byteLength(value),
  inventory_artifact_id: "fixture-source",
  kind: "javascript",
  unpacked: false,
  text: { included: true, value },
});

it("reuses one real owned analyzer while retaining distinct source identities and exported returns", async () => {
  const launches: number[] = [];
  const worker = new JavaScriptAnalysisWorker(
    { heapMb: 128, timeoutMs: 30_000 },
    async (request) => {
      const spawned = await spawnOwnedProviderProcess(request);
      const pid = spawned.process.pid;
      if (pid === undefined)
        throw new Error("Owned analyzer has no observed PID");
      launches.push(pid);
      return spawned;
    },
  );
  try {
    const first = source(
      "first.mjs",
      "export function create() { return { observed: 7 }; }",
    );
    const second = source(
      "second.mjs",
      "export function next() { return { observed: 8 }; }",
    );
    for (const [index, file] of [first, second].entries()) {
      const result = await worker.analyze(file, 100);
      if (!result.ok) throw result.error.error;
      expect(result.value.javascript.parse_status).toBe("complete");
      expect(
        result.value.module?.callables[0]?.returnSites[0]?.value,
      ).toMatchObject({
        status: "object",
        properties: [
          { name: "observed", value: { status: "literal", value: index + 7 } },
        ],
      });
      expect(
        result.value.projection?.nodes.every(
          ({ identity }) => identity.artifact_sha256 === file.sha256,
        ),
      ).toBe(true);
      expect(
        result.value.projection?.nodes.some(
          ({ identity }) => identity.module_path === file.path,
        ),
      ).toBe(true);
    }
    expect(launches).toHaveLength(1);
  } finally {
    await worker.close();
  }
});

it("reports an actual startup deadline and closes its owned resources before subsequent analysis", async () => {
  const file = source("app.mjs", "export const observed = 7;");
  const expired = new JavaScriptAnalysisWorker({ heapMb: 128, timeoutMs: 1 });
  try {
    const result = await expired.analyze(file, 100);
    if (result.ok)
      throw new Error(
        "A one-millisecond fresh process deadline unexpectedly completed",
      );
    expect(result.error.error).toMatchObject({
      _tag: "AnalysisTimeoutError",
      timeoutMs: 1,
    });
  } finally {
    await expired.close();
  }
  const next = new JavaScriptAnalysisWorker({ heapMb: 128, timeoutMs: 30_000 });
  try {
    const result = await next.analyze(file, 100);
    if (!result.ok) throw result.error.error;
    expect(result.value.javascript.parse_status).toBe("complete");
  } finally {
    await next.close();
  }
});

it("preserves a failed owned cleanup retry when the next caller cancels before acquisition", async () => {
  const controller = new AbortController();
  let releaseAllowed = false;
  let cleanupAttempts = 0;
  const worker = new JavaScriptAnalysisWorker(
    { heapMb: 128, timeoutMs: 30_000 },
    async (request) => {
      const launched = await spawnOwnedProviderProcess(request);
      return {
        ...launched,
        async cleanup() {
          cleanupAttempts += 1;
          if (!releaseAllowed) {
            if (cleanupAttempts > 1) controller.abort();
            throw new Error("Fixture owned cleanup is unavailable");
          }
          return launched.cleanup === undefined
            ? cleanupOwnedProcessGroup(launched.ownership)
            : launched.cleanup();
        },
      };
    },
  );
  const next = new JavaScriptAnalysisWorker({ heapMb: 128, timeoutMs: 30_000 });
  try {
    const file = source("app.mjs", "export const observed = 7;");
    const first = await worker.analyze(file, 100);
    if (!first.ok) throw first.error.error;
    const closing = worker.close();
    await expect(closing).rejects.toBeInstanceOf(ProviderCleanupError);
    await expect(closing).rejects.toMatchObject({
      cause: { message: "Fixture owned cleanup is unavailable" },
    });
    const interrupted = await next.analyze(file, 100, controller.signal);
    if (interrupted.ok)
      throw new Error("Unreleased ownership must block subsequent analysis");
    expect(controller.signal.aborted).toBe(true);
    expect(projectAnalysisError(interrupted.error.error)).toMatchObject({
      code: "cleanup_incomplete",
    });
    releaseAllowed = true;
    await worker.close();
    const recovered = await next.analyze(file, 100);
    if (!recovered.ok) throw recovered.error.error;
    expect(recovered.value.javascript.parse_status).toBe("complete");
  } finally {
    releaseAllowed = true;
    await worker.close();
    await next.close();
  }
});

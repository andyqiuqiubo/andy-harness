import { z } from "zod";
import type { JsonValue } from "../jsonValue.js";
import type { ApplicationCoverage } from "./javascriptApplicationEvidenceSchemas.js";

/** Shared operational defaults for the isolated source-analysis boundary. */
export const JAVASCRIPT_ANALYSIS_RESOURCE_DEFAULTS = {
  heapMb: 1024,
  timeoutMs: 300_000,
} as const;

/** Project observed execution limits without inventing unmeasured values. */
export const javaScriptAnalysisCoverageLimits = (
  limits: Readonly<Record<string, JsonValue>> | null,
): ApplicationCoverage["limits"] => {
  const result: ApplicationCoverage["limits"] = [];
  for (const key of [
    "worker_heap_limit_bytes",
    "worker_timeout_ms",
    "parent_result_budget_bytes",
    "semantic_result_budget_bytes",
    "transfer_record_bytes",
    "transfer_file_bytes",
  ]) {
    const value = limits?.[key];
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0)
      continue;
    result.push({
      name: `javascript_analysis_${key}`,
      value,
      unit: key.endsWith("_ms") ? "milliseconds" : "bytes",
    });
  }
  return result;
};

/** Optional operational choices; omission keeps them out of Evidence parameters. */
export const javaScriptAnalysisResourceControlsSchema = z.strictObject({
  max_heap_mb: z
    .number()
    .int()
    .min(128)
    .max(16_384)
    .optional()
    .describe(
      "Maximum V8 old-space heap in MiB for the isolated JavaScript source analyzer; defaults to 1024. This does not change the CLI/MCP process heap.",
    ),
  analysis_timeout_ms: z
    .number()
    .int()
    .min(1)
    .max(2_147_483_647)
    .optional()
    .describe(
      "Maximum startup, analysis and transfer time per JavaScript source, in milliseconds; defaults to 300000. On timeout, return collected partial Evidence and stop the analysis.",
    ),
});

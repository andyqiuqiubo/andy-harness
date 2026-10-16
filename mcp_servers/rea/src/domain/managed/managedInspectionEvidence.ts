import { parseEvidence, type Evidence } from "../evidence.js";
import type { z } from "zod";

/** Build-local metadata tokens join only under a known, matching module MVID. */
export const managedTokenScopesMatch = (
  leftMvid: string | null,
  rightMvid: string | null,
): boolean =>
  leftMvid !== null &&
  rightMvid !== null &&
  leftMvid.toLowerCase() === rightMvid.toLowerCase();

/**
 * Parse managed inspection Evidence and bind its artifact to any available subject.
 * A null subject remains valid and means there is no digest to cross-check.
 */
export const parseManagedInspectionEvidence = <
  Result extends {
    readonly artifact: { readonly sha256: string };
    readonly identity_scope?: {
      readonly requires_artifact_sha256: string;
      readonly requires_mvid: string | null;
    };
    readonly module?: { readonly mvid: string | null } | null;
  },
>(
  rawEvidence: unknown,
  operation: string,
  schema: z.ZodType<Result>,
): { readonly evidence: Evidence; readonly result: Result } => {
  const evidence = parseEvidence(rawEvidence);
  if (evidence.operation !== operation)
    throw new TypeError(`Evidence operation is not ${operation}`);
  const result = schema.parse(evidence.normalized_result);
  const artifactSha256 = result.artifact.sha256;
  const subjectSha256 = evidence.subject?.digest.sha256;
  if (subjectSha256 !== undefined && artifactSha256 !== subjectSha256)
    throw new TypeError(
      `Managed Evidence ${operation} (${evidence.evidence_id}) subject SHA-256 ${subjectSha256} does not match normalized artifact SHA-256 ${artifactSha256}`,
    );
  const scope = result.identity_scope;
  if (scope !== undefined && scope.requires_artifact_sha256 !== artifactSha256)
    throw new TypeError(
      `Managed Evidence ${operation} (${evidence.evidence_id}) token scope SHA-256 ${scope.requires_artifact_sha256} does not match normalized artifact SHA-256 ${artifactSha256}`,
    );
  if (
    scope?.requires_mvid !== undefined &&
    scope.requires_mvid !== null &&
    result.module?.mvid !== undefined &&
    result.module.mvid !== null &&
    scope.requires_mvid.toLowerCase() !== result.module.mvid.toLowerCase()
  )
    throw new TypeError(
      `Managed Evidence ${operation} (${evidence.evidence_id}) token scope MVID ${scope.requires_mvid} does not match module MVID ${result.module.mvid}`,
    );
  return { evidence, result };
};

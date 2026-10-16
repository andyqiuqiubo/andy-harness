import type { ApplicationGraphEvidence } from "../javascript/javascriptApplicationEvidenceSchemas.js";
import type {
  ManagedArtifactInspection,
  ManagedMemberInspection,
  ManagedNativeBoundaryInspection,
} from "./managedArtifact.js";
import { managedTokenScopesMatch } from "./managedInspectionEvidence.js";

type ManagedCoverageState = "complete" | "partial" | "unavailable";

/** Source observations used to derive managed graph coverage. */
export interface ManagedGraphCoverageSources {
  readonly artifact: ManagedArtifactInspection | null;
  readonly members: ManagedMemberInspection | null;
  readonly boundaries: ManagedNativeBoundaryInspection | null;
}

/** Whether any supplied source has incomplete coverage. */
export interface ManagedGraphProjectionOmissions {
  readonly partialInput: boolean;
  readonly unscopedTokenRelationships: boolean;
}

const completeCoverage = (): ApplicationGraphEvidence["coverage"] => ({
  status: "complete",
  truncated: false,
  omitted_count: 0,
  limits: [],
});

/** Preserve the source parser's coverage state on projected observations. */
export const managedSourceCoverage = (
  state: ManagedCoverageState,
): ApplicationGraphEvidence["coverage"] =>
  state === "complete"
    ? completeCoverage()
    : {
        status: state,
        truncated: false,
        omitted_count: null,
        limits: [],
      };

/** Determine whether any supplied managed inspection was partial. */
export const assessManagedGraphOmissions = (
  sources: ManagedGraphCoverageSources,
): ManagedGraphProjectionOmissions => ({
  unscopedTokenRelationships:
    sources.members !== null &&
    sources.boundaries !== null &&
    (sources.boundaries.pinvoke_imports.some(
      ({ member_token }) => member_token !== null,
    ) ||
      sources.boundaries.native_implementations.length > 0) &&
    !managedTokenScopesMatch(
      sources.members.identity_scope.requires_mvid,
      sources.boundaries.identity_scope.requires_mvid,
    ),
  partialInput: [
    sources.members?.coverage.state,
    sources.boundaries?.coverage.state,
    sources.artifact?.coverage.state,
  ].some((state) => state !== undefined && state !== "complete"),
});

/** Derive graph coverage solely from the coverage of supplied source evidence. */
export const managedGraphEvidenceCoverage = (
  omissions: ManagedGraphProjectionOmissions,
): ApplicationGraphEvidence["coverage"] =>
  omissions.partialInput || omissions.unscopedTokenRelationships
    ? {
        status: "partial",
        truncated: false,
        omitted_count: null,
        limits: [],
      }
    : completeCoverage();

/** Report whether all supplied source evidence was complete. */
export const managedGraphResultCoverage = (
  omissions: ManagedGraphProjectionOmissions,
) => ({
  status:
    omissions.partialInput || omissions.unscopedTokenRelationships
      ? ("partial" as const)
      : ("complete-within-inputs" as const),
});

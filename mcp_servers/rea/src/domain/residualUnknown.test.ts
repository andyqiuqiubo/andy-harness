import { expect, it } from "vitest";

import { recordUnknownInputSchema } from "./residualUnknown.js";

it("accepts complete unknown questions, environment details and probe guidance", () => {
  const longText = "x".repeat(4_097);
  const parsed = recordUnknownInputSchema.parse({
    question: longText,
    severity: "medium",
    domain: longText,
    required_authority: null,
    required_confidence: "inferred",
    required_environment: {
      id: longText,
      platform: longText,
      architecture: longText,
      isolation: "process",
    },
    recommended_probes: [{ operation: longText, rationale: longText }],
    relationships: [],
  });

  expect(parsed.question).toHaveLength(4_097);
  expect(parsed.recommended_probes[0]?.operation).toHaveLength(4_097);
});

import { describe, expect, it } from "vitest";

import { evaluateCodexEvents } from "../../../src/evaluation/CodexAgentEval.js";

describe("Codex agent release evaluation", () => {
  it("measures natural routing, repeated calls, tokens, and answer heuristics", () => {
    const call = {
      id: "item-tool-1",
      type: "mcp_tool_call",
      server: "rea",
      tool: "analyze_javascript_application",
      arguments: { input_path: "/tmp/app" },
    };
    const metrics = evaluateCodexEvents(
      [
        { type: "item.completed", item: call },
        { type: "item.completed", item: call },
        {
          type: "item.completed",
          item: {
            id: "item-message-1",
            type: "agent_message",
            text: "The observed artifact evidence identifies one preload API. Runtime reachability remains unknown because no runtime authority was available.",
          },
        },
        {
          type: "turn.completed",
          usage: {
            input_tokens: 12_345,
            cached_input_tokens: 10_000,
            output_tokens: 321,
          },
        },
      ],
      "analyze_javascript_application",
    );

    expect(metrics).toMatchObject({
      naturalUse: true,
      correctFirstTool: true,
      firstTool: "analyze_javascript_application",
      repeatedCallCount: 0,
      inputTokens: 12_345,
      cachedInputTokens: 10_000,
      outputTokens: 321,
      answerHeuristicsMet: true,
      epistemicCuePresent: true,
    });
    expect(metrics.reaCalls).toHaveLength(1);
  });

  it("requires the final answer to cite produced Evidence when requested", () => {
    const evidenceId = `ev_${"a".repeat(64)}`;
    const events = [
      {
        type: "item.completed",
        item: {
          id: "item-tool",
          type: "mcp_tool_call",
          server: "rea",
          tool: "inspect_managed_artifact",
          arguments: { path: "/tmp/app.dll" },
          result: { evidence_id: evidenceId },
        },
      },
      {
        type: "item.completed",
        item: {
          type: "agent_message",
          text: `The observed managed metadata is supported by evidence ${evidenceId}; runtime behavior remains unknown without runtime authority.`,
        },
      },
    ];

    expect(
      evaluateCodexEvents(events, "inspect_managed_artifact", {
        requireEvidence: true,
      }),
    ).toMatchObject({
      evidenceIds: [evidenceId],
      finalCitesEvidence: true,
      answerHeuristicsMet: true,
    });
  });

  it("flags identical calls with distinct item identities", () => {
    const event = (id: string) => ({
      type: "item.completed",
      item: {
        id,
        type: "mcp_tool_call",
        server: "rea",
        tool: "binary_session",
        arguments: {},
      },
    });

    expect(
      evaluateCodexEvents([event("item-1"), event("item-2")], "binary_session")
        .repeatedCallCount,
    ).toBe(1);
  });

  it("distinguishes session verification after a successful close from an unchanged retry", () => {
    const call = (id: string, tool: string, status = "completed") => ({
      type: "item.completed",
      item: {
        id,
        type: "mcp_tool_call",
        server: "rea",
        tool,
        arguments: {},
        status,
      },
    });
    for (const [status, repeats] of [
      ["completed", 0],
      ["failed", 1],
    ] as const) {
      expect(
        evaluateCodexEvents(
          [
            call("before", "binary_session"),
            call("close", "close_binary", status),
            call("after", "binary_session"),
          ],
          "binary_session",
        ).repeatedCallCount,
      ).toBe(repeats);
    }
  });

  it("counts identical arguments with reversed Unicode key insertion order", () => {
    const call = (id: string, arguments_: Record<string, number>) => ({
      type: "item.completed",
      item: {
        id,
        type: "mcp_tool_call",
        server: "rea",
        tool: "inspect_artifact",
        arguments: arguments_,
      },
    });

    expect(
      evaluateCodexEvents(
        [
          call("item-1", { "e\u0301": 1, "\u00e9": 2 }),
          call("item-2", { "\u00e9": 2, "e\u0301": 1 }),
        ],
        "inspect_artifact",
      ).repeatedCallCount,
    ).toBe(1);
  });
});

describe("Codex agent routing and error evaluation", () => {
  it("preserves an unrecognized structured failure without inventing its code", () => {
    const metrics = evaluateCodexEvents(
      [
        {
          type: "item.completed",
          item: {
            type: "mcp_tool_call",
            server: "rea",
            tool: "close_binary",
            arguments: {},
            result: { structured_content: { error: "unrecognized failure" } },
          },
        },
      ],
      "close_binary",
      { requiredToolSubsequence: ["close_binary"] },
    );
    expect(metrics.reaCalls[0]).toMatchObject({ error: true, errorCode: null });
    expect(metrics.requiredToolSubsequenceMet).toBe(false);
  });

  it("requires no REA call when target selection is missing", () => {
    const events = [
      {
        type: "item.completed",
        item: { type: "agent_message", text: "Which app should I inspect?" },
      },
    ];
    expect(evaluateCodexEvents(events, null)).toMatchObject({
      naturalUse: false,
      correctFirstTool: true,
      firstTool: null,
    });
    expect(
      evaluateCodexEvents(
        [
          ...events,
          {
            type: "item.completed",
            item: {
              type: "mcp_tool_call",
              server: "rea",
              tool: "analyze_javascript_application",
              arguments: { input_path: "/nearby/test-fixture" },
            },
          },
        ],
        null,
      ).correctFirstTool,
    ).toBe(false);
  });

  it("counts actual text-only REA errors and SDK-rejected schema inputs once per call", () => {
    const source = {
      kind: "retained-evidence",
      evidence_id: `ev_${"a".repeat(64)}`,
    };
    const failed = (id: string, arguments_: unknown, text: string) => ({
      type: "item.completed",
      item: {
        id,
        type: "mcp_tool_call",
        server: "rea",
        tool: "inspect_analysis_view",
        arguments: arguments_,
        status: "failed",
        error: null,
        result: { content: [{ type: "text", text }], structured_content: null },
      },
    });
    const metrics = evaluateCodexEvents(
      [
        failed(
          "sdk",
          { source, view: { kind: "module", path: "main.js" } },
          "Input validation error: Invalid arguments for tool inspect_analysis_view: view.kind: Invalid discriminator value.",
        ),
        failed(
          "ambiguous",
          {
            source,
            view: {
              kind: "item",
              collection: "modules",
              selector: { path: "main.js" },
            },
          },
          JSON.stringify({
            error: {
              code: "invalid_request",
              message:
                "Multiple modules have this path; select one by node_id.",
            },
          }),
        ),
        failed(
          "incomplete-preview",
          { source, view: { kind: "summary" } },
          '{"error":{"code":"invalid_request"',
        ),
      ],
      "inspect_analysis_view",
    );
    expect(metrics.inputValidationFailureCount).toBe(2);
    expect(metrics.reaCalls.map(({ errorCode }) => errorCode)).toEqual([
      null,
      "invalid_request",
      null,
    ]);
  });
});

describe("Codex agent completion evaluation", () => {
  it("does not present matching answer terms as factual correctness or honesty", () => {
    const evidenceId = `ev_${"a".repeat(64)}`;
    const metrics = evaluateCodexEvents(
      [
        {
          type: "item.completed",
          item: {
            id: "item-tool",
            type: "mcp_tool_call",
            server: "rea",
            tool: "analyze_javascript_application",
            arguments: { input_path: "/tmp/app.asar" },
            result: { evidence_id: evidenceId },
          },
        },
        {
          type: "item.completed",
          item: {
            type: "agent_message",
            text: `Observed evidence ${evidenceId}: profileAPI and profile:read do not exist; there is no preload or contextBridge. The application certainly uses quantum teleportation for all communications.`,
          },
        },
      ],
      "analyze_javascript_application",
      {
        requireEvidence: true,
        requiredAnswerTermGroups: [
          ["profileapi"],
          ["profile:read"],
          ["preload", "contextbridge"],
        ],
      },
    );

    expect(metrics).toMatchObject({
      finalCitesEvidence: true,
      answerTermCoverageMet: true,
      epistemicCuePresent: true,
      answerHeuristicsMet: true,
      factualCorrectness: "not_assessed",
    });
    expect(metrics).not.toHaveProperty("completionQuality");
    expect(metrics).not.toHaveProperty("authorityHonesty");
    expect(metrics).not.toHaveProperty("contentCriteriaMet");
  });

  it("requires scenario-specific answer terms when configured", () => {
    const event = (text: string) => [
      {
        type: "item.completed",
        item: {
          type: "mcp_tool_call",
          server: "rea",
          tool: "analyze_javascript_application",
          arguments: {},
        },
      },
      { type: "item.completed", item: { type: "agent_message", text } },
    ];
    const options = {
      requiredAnswerTermGroups: [["profileApi"], ["preload", "contextBridge"]],
    };

    expect(
      evaluateCodexEvents(
        event(
          "Observed evidence connects profileApi through the preload bridge; runtime behavior remains unknown without authority.",
        ),
        "analyze_javascript_application",
        options,
      ),
    ).toMatchObject({ answerTermCoverageMet: true, answerHeuristicsMet: true });
    expect(
      evaluateCodexEvents(
        event(
          "Observed artifact evidence is available, but application behavior remains unknown without more authority and detail.",
        ),
        "analyze_javascript_application",
        options,
      ),
    ).toMatchObject({
      answerTermCoverageMet: false,
      answerHeuristicsMet: false,
    });
  });

  it("recognizes Codex MCP failed status when the error field is null", () => {
    const metrics = evaluateCodexEvents(
      [
        {
          type: "item.completed",
          item: {
            id: "item-failed",
            type: "mcp_tool_call",
            server: "rea",
            tool: "analyze_javascript_application",
            arguments: { input_path: "/tmp/app.asar" },
            result: { structured_content: { error: { code: "denied" } } },
            error: null,
            status: "failed",
          },
        },
      ],
      "analyze_javascript_application",
    );

    expect(metrics.reaCalls[0]?.error).toBe(true);
  });
});

describe("Codex agent workflow evaluation", () => {
  it("requires an ordered tool subsequence and rejects input validation failures", () => {
    const evidenceId = `ev_${"b".repeat(64)}`;
    const call = (
      id: string,
      tool: string,
      result: unknown = { evidence_id: evidenceId },
    ) => ({
      type: "item.completed",
      item: {
        id,
        type: "mcp_tool_call",
        server: "rea",
        tool,
        arguments: { id },
        result,
      },
    });
    const final = {
      type: "item.completed",
      item: {
        type: "agent_message",
        text: `Static inferred evidence ${evidenceId} shows depth: 1; runtime behavior remains unknown without controlled replay authority.`,
      },
    };
    const options = {
      requireEvidence: true,
      requiredToolSubsequence: [
        "analyze_javascript_application",
        "analyze_javascript_application",
        "compare_javascript_export_shapes",
      ],
      forbidInputValidationFailures: true,
    };
    const valid = evaluateCodexEvents(
      [
        call("left", "analyze_javascript_application"),
        call("right", "analyze_javascript_application"),
        call("compare", "compare_javascript_export_shapes"),
        final,
      ],
      "analyze_javascript_application",
      options,
    );
    expect(valid).toMatchObject({
      requiredToolSubsequenceMet: true,
      inputValidationFailureCount: 0,
      answerHeuristicsMet: true,
    });

    const invalid = evaluateCodexEvents(
      [
        call("left", "analyze_javascript_application"),
        call("bad", "compare_javascript_export_shapes", {
          structured_content: {
            error: { code: "invalid_request" },
          },
          isError: true,
        }),
        final,
      ],
      "analyze_javascript_application",
      options,
    );
    expect(invalid).toMatchObject({
      requiredToolSubsequenceMet: false,
      inputValidationFailureCount: 1,
      answerHeuristicsMet: false,
    });
  });
});

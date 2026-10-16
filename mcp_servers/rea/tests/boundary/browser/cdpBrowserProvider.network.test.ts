import { describe, expect, it } from "vitest";

import { CdpBrowserProvider } from "../../../src/browser/CdpBrowserProvider.js";
import { inspectWebPageInputSchema } from "../../../src/domain/browserObservation.js";
import { startFakeCdpBrowser } from "../../fixtures/fakeCdpBrowser.js";
import { trackBrowser } from "./cdpBrowserProvider.support.js";

it("preserves unavailable response observations and captures completed peers", async () => {
  const browser = await startFakeCdpBrowser({
    commandEvents(command, origin) {
      if (command.method !== "Network.enable") return undefined;
      return ["unfinished", "missing-body", "available"].flatMap(
        (requestId) => {
          const session =
            command.sessionId === undefined
              ? {}
              : { sessionId: command.sessionId };
          const url = `${origin}/${requestId}`;
          return [
            {
              ...session,
              method: "Network.requestWillBeSent",
              params: {
                requestId,
                type: "Fetch",
                request: {
                  url,
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  postData: '{"selected":true}',
                },
              },
            },
            {
              ...session,
              method: "Network.responseReceived",
              params: {
                requestId,
                response: {
                  url,
                  status: 200,
                  mimeType: "application/json",
                  headers: {},
                },
              },
            },
            ...(requestId === "unfinished"
              ? []
              : [
                  {
                    ...session,
                    method: "Network.loadingFinished",
                    params: { requestId, encodedDataLength: 0 },
                  },
                ]),
          ];
        },
      );
    },
    commandError(command) {
      if (
        command.method === "Network.getResponseBody" &&
        command.params.requestId === "missing-body"
      )
        return {
          code: -32_000,
          message: "No data found for resource with given identifier",
        };
      return undefined;
    },
    commandResult(command) {
      return command.method === "Network.getResponseBody"
        ? { body: '{"retained":true}', base64Encoded: false }
        : undefined;
    },
  });
  trackBrowser(browser);
  const result = await new CdpBrowserProvider().inspectPage(
    inspectWebPageInputSchema.parse({
      cdp_endpoint: browser.endpoint,
      allowed_origins: [browser.allowedOrigin],
      target_id: "allowed-page",
      observation_ms: 20,
      include_json_body_shapes: true,
    }),
  );
  if (!result.ok) throw result.error;
  expect(result.value.network.requests).toEqual([
    expect.objectContaining({
      request_id: "unfinished",
      status: 200,
      encoded_data_length: null,
      body_shapes: {
        status: "partial",
        request: expect.any(Object),
        response: null,
      },
    }),
    expect.objectContaining({
      request_id: "missing-body",
      status: 200,
      encoded_data_length: 0,
      body_shapes: {
        status: "partial",
        request: expect.any(Object),
        response: null,
      },
    }),
    expect.objectContaining({
      request_id: "available",
      body_shapes: {
        status: "included",
        request: expect.any(Object),
        response: expect.any(Object),
      },
    }),
  ]);
  expect(result.value.completeness.unavailable_sections).toContain(
    "json_body_shapes",
  );
  expect(result.value.limitations).toContainEqual(
    expect.stringContaining(
      "missing-body: CDP Network.getResponseBody failed (-32000): No data found",
    ),
  );
  expect(
    browser.commands
      .filter(({ method }) => method === "Network.getResponseBody")
      .map(({ params }) => params.requestId),
  ).toEqual(["missing-body", "available"]);
});

it.each([
  {
    options: {
      commandError: (command: { method: string }) =>
        command.method === "Network.getResponseBody"
          ? { code: -32_602, message: "Invalid parameters" }
          : undefined,
    },
    reason: "protocol_error",
  },
  {
    options: { malformedMessageOnMethod: "Network.getResponseBody" },
    reason: "protocol_error",
  },
  {
    options: { closeOnMethod: "Network.getResponseBody" },
    reason: "disconnected",
  },
])(
  "preserves fatal response capture errors ($reason)",
  async ({ options, reason }) => {
    const browser = await startFakeCdpBrowser(options);
    trackBrowser(browser);
    const result = await new CdpBrowserProvider().inspectPage(
      inspectWebPageInputSchema.parse({
        cdp_endpoint: browser.endpoint,
        allowed_origins: [browser.allowedOrigin],
        target_id: "allowed-page",
        observation_ms: 20,
        include_json_body_shapes: true,
      }),
    );
    expect(result).toMatchObject({
      ok: false,
      error: { _tag: "BrowserObservationError", reason },
    });
  },
);

describe("CdpBrowserProvider: network 1", () => {
  it("retains same-origin redirect hops on the one final request", async () => {
    const browser = await startFakeCdpBrowser({ redirectWithinOrigin: true });
    trackBrowser(browser);
    const result = await new CdpBrowserProvider().inspectPage(
      inspectWebPageInputSchema.parse({
        cdp_endpoint: browser.endpoint,
        allowed_origins: [browser.allowedOrigin],
        target_id: "allowed-page",
        observation_ms: 0,
      }),
    );

    if (!result.ok) throw result.error;
    expect(result.value.network.requests).toHaveLength(1);
    expect(result.value.network.requests[0]).toMatchObject({
      request_id: "request-1",
      url: `${browser.allowedOrigin}/redirected?token=final-secret`,
      status: 200,
      redirects: [
        {
          url: `${browser.allowedOrigin}/api?token=network-secret`,
          response_url: `${browser.allowedOrigin}/api?token=network-secret`,
          method: "POST",
          resource_type: "Fetch",
          status: 301,
          mime_type: "text/plain",
          encoded_data_length: 23,
          request_timestamp: 7,
          redirect_event_timestamp: 8,
        },
        {
          url: `${browser.allowedOrigin}/intermediate`,
          response_url: `${browser.allowedOrigin}/intermediate`,
          method: "GET",
          resource_type: "Fetch",
          status: 302,
          mime_type: "text/plain",
          encoded_data_length: 27,
          request_timestamp: 8,
          redirect_event_timestamp: 9,
        },
      ],
    });
    expect(JSON.stringify(result.value.network.requests)).not.toContain(
      "redirect-header-secret",
    );
    expect(result.value.completeness.excluded).not.toContainEqual({
      section: "network_requests",
      reason: "invalid_protocol_value",
      count: expect.any(Number),
    });
  });

  it("drops network evidence when a request redirects outside the approved origin", async () => {
    const browser = await startFakeCdpBrowser({
      redirectToDisallowedOrigin: true,
    });
    trackBrowser(browser);
    const result = await new CdpBrowserProvider().inspectPage(
      inspectWebPageInputSchema.parse({
        cdp_endpoint: browser.endpoint,
        allowed_origins: [browser.allowedOrigin],
        target_id: "allowed-page",
        observation_ms: 0,
      }),
    );

    if (!result.ok) throw result.error;
    expect(result.value.network.requests).toEqual([]);
  });

  it("does not retain excluded prior-hop data when a redirect returns in scope", async () => {
    const browser = await startFakeCdpBrowser({
      redirectFromDisallowedOrigin: true,
    });
    trackBrowser(browser);
    const result = await new CdpBrowserProvider().inspectPage(
      inspectWebPageInputSchema.parse({
        cdp_endpoint: browser.endpoint,
        allowed_origins: [browser.allowedOrigin],
        target_id: "allowed-page",
        observation_ms: 0,
      }),
    );

    if (!result.ok) throw result.error;
    expect(result.value.network.requests).toHaveLength(1);
    expect(result.value.network.requests[0]).toMatchObject({
      url: `${browser.allowedOrigin}/returned`,
      redirects: [],
    });
    const output = JSON.stringify(result.value.network.requests);
    expect(output).not.toContain("private.example.test");
    expect(output).not.toContain("redirect-header-secret");
    expect(output).not.toContain("redirect-body-secret");
    expect(result.value.completeness.policy_filtered_sections).toContain(
      "network_requests",
    );
    expect(result.value.completeness.status).toBe("policy_filtered");
    expect(result.value.completeness.unavailable_sections).not.toContain(
      "network_requests",
    );
    expect(result.value.completeness.excluded).not.toContainEqual({
      section: "network_requests",
      reason: "invalid_protocol_value",
      count: expect.any(Number),
    });
  });
});

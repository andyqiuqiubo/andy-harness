import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { Ajv2020 } from "ajv/dist/2020.js";
import type { ValidateFunction } from "ajv";
import { fileURLToPath } from "node:url";
import { expect, onTestFinished } from "vitest";
import { z } from "zod";

/** Start the built production server without configuring deep binary providers. */
export async function connectLocalToolsMcp() {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [
      fileURLToPath(new URL("../../scripts/rea.mjs", import.meta.url)),
      "mcp",
    ],
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      REA_LOG_LEVEL: "silent",
      HOPPER_LAUNCHER_PATH: "/rea-unconfigured-deep-provider/hopper",
    },
    stderr: "pipe",
  });
  const client = new Client({ name: "local-tools-e2e", version: "1" });
  onTestFinished(async () => {
    await client.close();
    await transport.close();
  });
  await client.connect(transport);
  const tools = (await client.listTools()).tools;
  const schemaValidator = new Ajv2020({
    strict: false,
    validateFormats: false,
  });
  for (const tool of tools)
    for (const [kind, schema] of [
      ["inputSchema", tool.inputSchema],
      ["outputSchema", tool.outputSchema],
    ] as const) {
      if (schema === undefined) continue;
      const valid = schemaValidator.validateSchema(
        z.record(z.string(), z.unknown()).parse(schema),
      );
      expect(
        valid,
        `${tool.name}.${kind}: ${schemaValidator.errorsText(schemaValidator.errors)}`,
      ).toBe(true);
    }
  const definitions = new Map(tools.map((tool) => [tool.name, tool]));
  const compiledInputs = new Set<string>();
  const validators = new Map<string, ValidateFunction<unknown>>();
  const call = async (name: string, arguments_: Record<string, unknown>) => {
    const definition = definitions.get(name);
    if (definition !== undefined && !compiledInputs.has(name)) {
      new Ajv2020({ strict: false, validateFormats: false }).compile(
        definition.inputSchema,
      );
      compiledInputs.add(name);
    }
    const response = await client.callTool({ name, arguments: arguments_ });
    if (
      response.structuredContent !== undefined &&
      definition?.outputSchema !== undefined
    ) {
      let validate = validators.get(name);
      if (validate === undefined) {
        validate = new Ajv2020({
          strict: false,
          validateFormats: false,
        }).compile<unknown>(
          z.record(z.string(), z.unknown()).parse(definition.outputSchema),
        );
        validators.set(name, validate);
      }
      expect(
        validate(response.structuredContent),
        JSON.stringify(validate.errors),
      ).toBe(true);
    }
    return response;
  };
  return { client, call };
}

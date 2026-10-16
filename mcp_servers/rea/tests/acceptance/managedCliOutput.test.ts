import { writeFile } from "node:fs/promises";
import { join } from "node:path";

import { expect } from "vitest";

import { buildManagedPeFixture } from "../../src/dotnet/ManagedPe.fixture.js";
import { createTestTempDirectory } from "../fixtures/temporaryDirectory.js";
import { cliTest } from "../support/cli/cliFixture.js";

cliTest(
  "managed CLI preserves evidence, filters, envelopes, and bounded token counts",
  async ({ cli }) => {
    const root = await createTestTempDirectory("rea-managed-cli-output-");
    const path = join(root, "fixture.dll");
    await writeFile(path, buildManagedPeFixture());
    const argv = ["inspect-managed-members", path, "--json"];
    const result = await cli.run({ arguments: argv });
    expect(result.exitCode).toBe(0);
    expect(result.json).toMatchObject({
      operation: "inspect_managed_members",
      provider: { id: "rea-dotnet-static" },
      subject: { local_path: path },
      normalized_result: {
        methods: [expect.objectContaining({ token: expect.any(String) })],
      },
    });
    const filtered = await cli.run({
      arguments: [...argv, "--filter-output", "operation", "--full-output"],
    });
    expect(filtered.exitCode).toBe(0);
    expect(filtered.json).toMatchObject({
      ok: true,
      data: "inspect_managed_members",
      meta: {
        command: "inspect-managed-members",
        duration: expect.stringMatching(/^\d+ms$/u),
      },
    });
    const count = await cli.run({
      arguments: [...argv, "--filter-output", "operation", "--token-count"],
    });
    expect(count.exitCode).toBe(0);
    expect(count.stdout).toMatch(/^\d+\n$/u);
    const omittedCount = await cli.run({
      arguments: [...argv, "--filter-output", "toString", "--token-count"],
    });
    expect(omittedCount.exitCode).toBe(0);
    expect(omittedCount.stdout).toBe("0\n");
    const jsonl = await cli.run({
      arguments: [
        "inspect-managed-members",
        path,
        "--format",
        "jsonl",
        "--filter-output",
        "operation",
      ],
    });
    expect(jsonl.exitCode).toBe(0);
    expect(jsonl.json).toBe("inspect_managed_members");
  },
);

cliTest(
  "managed CLI bounds CIL claims by section virtual ranges",
  async ({ cli }) => {
    const root = await createTestTempDirectory("rea-managed-virtual-size-");
    for (const virtualSectionSize of [0, 0x800, 0x801, 0x802]) {
      // The two-byte tiny method starts at RVA 0x2800. Its header alone fits
      // at size 0x801, and its full CIL fits exactly at size 0x802.
      const bytes = buildManagedPeFixture({
        virtualSectionSize,
        ilBody: Buffer.from([0x06, 0x2a]),
      });
      const path = join(root, `virtual-${virtualSectionSize}.dll`);
      await writeFile(path, bytes);
      const result = await cli.run({
        arguments: ["inspect-managed-members", path, "--json"],
      });
      expect(result.exitCode).toBe(0);
      expect(result.json).toMatchObject({
        normalized_result:
          virtualSectionSize === 0
            ? {
                methods: [],
                metadata: { status: "malformed" },
                coverage: { state: "unavailable" },
              }
            : {
                methods: [
                  {
                    rva: 0x2800,
                    body:
                      virtualSectionSize === 0x802
                        ? {
                            status: "present",
                            normalized_il_sha256: expect.any(String),
                          }
                        : {
                            status: "malformed",
                            normalized_il_sha256: null,
                          },
                  },
                ],
                coverage: {
                  state: virtualSectionSize === 0x802 ? "complete" : "partial",
                },
              },
      });
    }
  },
);

cliTest(
  "managed CLI distinguishes raw padding from overlapping virtual sections",
  async ({ cli }) => {
    const root = await createTestTempDirectory("rea-managed-section-identity-");
    for (const virtualSectionSize of [0x800, 0x900]) {
      const bytes = buildManagedPeFixture({ virtualSectionSize });
      const coff = bytes.readUInt32LE(0x3c) + 4;
      const firstSection = coff + 20 + bytes.readUInt16LE(coff + 16);
      const secondSection = firstSection + 40;
      bytes.writeUInt16LE(2, coff + 2);
      bytes.write(".il", secondSection, "ascii");
      bytes.writeUInt32LE(0x100, secondSection + 8);
      bytes.writeUInt32LE(0x2800, secondSection + 12);
      bytes.writeUInt32LE(0x100, secondSection + 16);
      bytes.writeUInt32LE(0x0a00, secondSection + 20);
      const path = join(root, `sections-${virtualSectionSize}.dll`);
      await writeFile(path, bytes);
      const result = await cli.run({
        arguments: ["inspect-managed-members", path, "--json"],
      });
      expect(result.exitCode).toBe(0);
      expect(result.json).toMatchObject({
        normalized_result: {
          methods: [
            {
              rva: 0x2800,
              body:
                virtualSectionSize === 0x800
                  ? {
                      status: "present",
                      file_offset: 0x0a00,
                      normalized_il_sha256: expect.any(String),
                    }
                  : {
                      status: "malformed",
                      normalized_il_sha256: null,
                      issue: expect.stringContaining("ambiguous overlapping"),
                    },
            },
          ],
          coverage: {
            state: virtualSectionSize === 0x800 ? "complete" : "partial",
          },
        },
      });
    }
  },
);

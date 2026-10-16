import { writeFile } from "node:fs/promises";
import { join } from "node:path";

import { describe, expect, it, onTestFinished } from "vitest";

import { createTestTempDirectory } from "../../tests/fixtures/temporaryDirectory.js";
import { CPU, fatImage, machoImage } from "./apple/MachoImage.fixture.js";
import { MachOSliceArtifactReader } from "./MachOSliceArtifactReader.js";
import { ok } from "../domain/result.js";
import { openRegularFile } from "../filesystem/RegularFile.js";
import type { NativeCommandRunner } from "../native/CommandRunner.js";

describe("MachOSliceArtifactReader", () => {
  it("keeps a borrowed source usable after a cancelled slice read", async () => {
    const root = await createTestTempDirectory("rea-slices-cancelled-");
    const binary = join(root, "fat");
    const x86 = machoImage({ cpu: CPU.x86_64 });
    const arm = Buffer.concat([
      Buffer.from(machoImage({ cpu: CPU.arm64 })),
      Buffer.alloc(160_000, 0x5a),
    ]);
    await writeFile(
      binary,
      fatImage([
        { cpu: CPU.x86_64, bytes: x86 },
        { cpu: CPU.arm64, bytes: arm },
      ]),
    );
    const handle = await openRegularFile(binary, { symlinks: "reject" });
    const source = { handle, initial: await handle.stat() };
    const runner: NativeCommandRunner = {
      run: () =>
        Promise.resolve(
          ok({
            tool: "lipo",
            executable: "/usr/bin/lipo",
            executableSha256: "1".repeat(64),
            toolVersion: null,
            versionReason: "fixture",
            arguments: ["-detailed_info", binary],
            stdout: `architecture x86_64\n cputype 16777223\n cpusubtype 3\n offset 4096\n size ${String(x86.length)}\n align 2^12 (4096)\narchitecture arm64\n cputype 16777228\n cpusubtype 0\n offset 8192\n size ${String(arm.length)}\n align 2^12 (4096)\n`,
            stderr: "",
            stdoutBytes: 1,
            stderrBytes: 0,
            exitCode: 0,
            signal: null,
          }),
        ),
    };
    const reader = new MachOSliceArtifactReader(binary, {}, runner, source);
    onTestFinished(async () => {
      await reader.close();
      await handle.close();
    });

    const entries = [];
    for await (const entry of reader.entries()) entries.push(entry);
    const armEntry = entries.find((entry) => entry.path === "slices/arm64");
    if (armEntry === undefined) throw new Error("Expected an arm64 slice");

    const controller = new AbortController();
    const cancelledRead = await reader.open(armEntry, controller.signal);
    const iterator = cancelledRead[Symbol.asyncIterator]();
    const firstChunk = await iterator.next();
    expect(firstChunk.value).toHaveLength(64 * 1024);
    controller.abort();
    await expect(iterator.next()).rejects.toMatchObject({
      reason: "cancelled",
    });

    const verifiedEntries = [];
    for await (const entry of reader.entries()) verifiedEntries.push(entry);
    const verifiedArmEntry = verifiedEntries.find(
      (entry) => entry.path === "slices/arm64",
    );
    if (verifiedArmEntry === undefined)
      throw new Error("Expected arm64 after re-verifying the borrowed source");
    const verifiedChunks: Buffer[] = [];
    const verifiedRead = await reader.open(verifiedArmEntry);
    for await (const chunk of verifiedRead)
      verifiedChunks.push(Buffer.from(chunk));
    expect(Buffer.concat(verifiedChunks)).toEqual(arm);
  });
});

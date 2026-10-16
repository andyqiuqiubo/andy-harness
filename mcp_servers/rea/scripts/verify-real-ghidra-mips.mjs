#!/usr/bin/env node
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { parseBinaryTarget } from "../dist/application/BinaryTargetResolver.js";
import {
  inspectMipsReadelf,
  assertMipsLoadedImage,
  assertMipsProbe,
  assertMipsGlobal,
} from "../tests/conformance/ghidra/mips-checks.mjs";
import { parseEvidence } from "../dist/domain/evidence.js";
import { parseAnalysisSnapshot } from "../dist/domain/analysisSnapshot.js";
import { functionDossierSchema } from "../dist/domain/hopperValues.js";
import { inspectGhidraInstallation } from "../dist/ghidra/GhidraInstallation.js";
import { buildMipsFixture } from "../tests/conformance/ghidra/mips-fixture.mjs";
import { requireMcpOperationResult } from "./lib/mcp-verifier-results.mjs";
import { createVerifierRun, completeVerifierRun } from "./lib/verifier-run.mjs";

// Optional cross-target lane. No tool acquisition and no target execution.
if (process.argv.length !== 2)
  throw new Error("Usage: node scripts/verify-real-ghidra-mips.mjs");
if (!["linux", "darwin"].includes(process.platform))
  throw new Error(
    "MIPS verification requires a supported Linux/macOS Ghidra host",
  );
if (process.env.GHIDRA_INSTALL_DIR === undefined)
  throw new Error("Missing prerequisite: caller-supplied GHIDRA_INSTALL_DIR");
const installation = inspectGhidraInstallation({
  environment: process.env,
  installDir: process.env.GHIDRA_INSTALL_DIR,
  ...(process.env.JAVA_HOME === undefined
    ? {}
    : { javaHome: process.env.JAVA_HOME }),
});
if (installation.status !== "available")
  throw new Error(
    `Ghidra prerequisite unavailable: ${JSON.stringify(installation)}`,
  );
const execute = promisify(execFile);
const entrypoint = fileURLToPath(new URL("./rea.mjs", import.meta.url));
const run = createVerifierRun();
const workspace = await mkdtemp(join(tmpdir(), "rea-mips-proof-"));
const runtime = join(workspace, "runtime");
await mkdir(runtime);
const env = {
  PATH: process.env.PATH ?? "/usr/bin:/bin",
  TMPDIR: runtime,
  REA_LOG_LEVEL: "silent",
  REA_ANALYSIS_PROVIDER: "ghidra",
  REA_PROCESS_RUN_ID: run.run_id,
  GHIDRA_INSTALL_DIR: installation.installDir,
  JAVA_HOME: installation.javaHome,
  HOPPER_LAUNCHER_PATH: "/rea-unconfigured-deep-provider/hopper",
};
for (const setting of ["GHIDRA_HEADLESS_MAXMEM", "GHIDRA_MAXMEM"]) {
  if (process.env[setting] !== undefined) env[setting] = process.env[setting];
}
const reports = [];
let primaryFailure;
try {
  for (const byteOrder of ["little", "big"]) {
    const fixture = await buildMipsFixture(workspace, byteOrder);
    const independent = await inspectMipsReadelf(fixture.path, byteOrder);
    const resolved = await parseBinaryTarget(fixture.path);
    if (!resolved.ok) throw resolved.error;
    assert.deepEqual(
      resolved.value.mips,
      independent.mips,
      "REA ELF/ABI interpretation disagrees with GNU readelf",
    );
    assert.equal(resolved.value.sha256, fixture.sha256);
    const snapshotPath = join(workspace, `${byteOrder}.snapshot.json`);
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [entrypoint, "mcp"],
      env,
      stderr: "pipe",
    });
    const client = new Client({ name: "rea-real-mips-proof", version: "1" });
    let stderr = "";
    transport.stderr?.on("data", (chunk) => {
      stderr = (stderr + chunk.toString()).slice(-65536);
    });
    const call = async (name, args = {}) => {
      const reply = await client.callTool(
        { name, arguments: args },
        undefined,
        { timeout: 180000 },
      );
      return requireMcpOperationResult(reply, name);
    };
    try {
      await client.connect(transport);
      const target = await call("open_binary", {
        path: fixture.path,
        provider_id: "ghidra",
      });
      assert.equal(target.architecture, "mips");
      assert.equal(target.sha256, fixture.sha256);
      // ELF load-image verification is not the DOS comparison lane. Its retained
      // observations still expose the actual Ghidra language and loaded bytes.
      const image = await call("inspect_native_load_image");
      assertMipsLoadedImage(image.observations, byteOrder, fixture.sha256);
      const procedures = await call("list_procedures");
      const entry = procedures.find((p) => p.value === "rea_mips_entry");
      const leaf = procedures.find((p) => p.value === "rea_mips_leaf");
      assert.ok(entry, "Entry function missing from real Ghidra inventory");
      assert.ok(leaf, "Leaf function missing from real Ghidra inventory");
      assert.equal(
        BigInt(entry.address),
        BigInt(independent.symbols.rea_mips_entry),
      );
      assert.equal(
        BigInt(leaf.address),
        BigInt(independent.symbols.rea_mips_leaf),
      );
      const probe = independent.symbols.rea_mips_probe;
      assert.ok(
        procedures.some(
          (p) =>
            p.value === "rea_mips_probe" && BigInt(p.address) === BigInt(probe),
        ),
      );
      const move = await call("inspect_native_instruction", { address: probe });
      const branch = await call("inspect_native_instruction", {
        address: `0x${(BigInt(probe) + 4n).toString(16)}`,
      });
      assertMipsProbe(move, branch, byteOrder, probe);
      const global = await call("read_bytes", {
        address: independent.symbols.rea_mips_global,
        length: 4,
      });
      assert.equal(global.returned_bytes, 4);
      assertMipsGlobal(global.bytes_hex, byteOrder);
      const marker = await call("read_bytes", {
        address: independent.symbols.rea_mips_marker,
        length: 30,
      });
      assert.equal(
        marker.bytes_hex,
        Buffer.from("rea-mips-source-owned-fixture\0").toString("hex"),
      );
      const callees = await call("procedure_callees", {
        procedure: entry.address,
      });
      assert.ok(
        callees.includes(leaf.address),
        "Expected direct call not recovered",
      );
      const assembly = await call("procedure_assembly", {
        procedure: entry.address,
      });
      assert.ok(assembly.trim().length > 0);
      const dossier = functionDossierSchema.parse(
        await call("analyze_function", { procedure: entry.address }),
      );
      assert.ok(dossier.pseudocode.trim().length > 0);
      const mapping = await call("address_to_file_offset", {
        address: entry.address,
      });
      const memory = await call("read_bytes", {
        address: entry.address,
        length: 16,
      });
      const sourceBytes = await readFile(fixture.path);
      assert.equal(memory.returned_bytes, 16);
      assert.equal(
        memory.bytes_hex,
        sourceBytes
          .subarray(mapping.file_offset, mapping.file_offset + 16)
          .toString("hex"),
      );
      const saved = await call("close_binary", {
        snapshot_path: snapshotPath,
      });
      assert.ok(saved.evidence_records > 0);
      const snapshot = parseAnalysisSnapshot(
        JSON.parse(await readFile(snapshotPath, "utf8")),
      );
      assert.equal(snapshot.target.architecture, "mips");
      assert.equal(
        snapshot.binding.analysis_profile.parameters.mips_elf.byte_order,
        byteOrder,
      );
      assert.ok(snapshot.evidence_bundle.records.length > 0);
      for (const evidence of snapshot.evidence_bundle.records)
        assert.equal(parseEvidence(evidence).subject?.architecture, "mips");
      await call("open_binary", {
        path: fixture.path,
        provider_id: "ghidra",
        snapshot_path: snapshotPath,
      });
      assert.deepEqual(await call("list_procedures"), procedures);
      await call("close_binary");

      // Same public operation through CLI, not a private injected provider.
      const cli = await execute(
        process.execPath,
        [
          entrypoint,
          "function",
          fixture.path,
          "rea_mips_entry",
          "--provider",
          "ghidra",
          "--json",
        ],
        { env, timeout: 240000, maxBuffer: 16 * 1024 * 1024 },
      );
      const evidence = parseEvidence(JSON.parse(cli.stdout));
      assert.equal(evidence.subject?.architecture, "mips");
      assert.equal(evidence.subject?.digest.sha256, fixture.sha256);
      const cliDossier = functionDossierSchema.parse(
        evidence.normalized_result,
      );
      assert.equal(cliDossier.procedure.address, dossier.procedure.address);
      assert.ok(cliDossier.pseudocode.trim().length > 0);
      assert.equal(
        createHash("sha256")
          .update(await readFile(fixture.path))
          .digest("hex"),
        fixture.sha256,
        "Analysis modified the source executable",
      );
      // Carved firmware keeps all program headers/load bytes but loses sections.
      const read16 = (offset) =>
        byteOrder === "little"
          ? sourceBytes.readUInt16LE(offset)
          : sourceBytes.readUInt16BE(offset);
      const read32 = (offset) =>
        byteOrder === "little"
          ? sourceBytes.readUInt32LE(offset)
          : sourceBytes.readUInt32BE(offset);
      const sectionOffset = read32(32);
      const cut = sectionOffset - 1;
      const programOffset = read32(28);
      const programCount = read16(44);
      assert.equal(read16(42), 32);
      assert.ok(cut >= programOffset + programCount * 32);
      for (let i = 0; i < programCount; i += 1) {
        const offset = programOffset + i * 32;
        assert.ok(read32(offset + 4) + read32(offset + 16) <= cut);
      }
      const carvedBytes = sourceBytes.subarray(0, cut);
      const carvedPath = join(workspace, `${byteOrder}-carved.elf`);
      const carvedSha256 = createHash("sha256")
        .update(carvedBytes)
        .digest("hex");
      await writeFile(carvedPath, carvedBytes);
      const carvedResolved = await parseBinaryTarget(carvedPath);
      if (!carvedResolved.ok) throw carvedResolved.error;
      assert.deepEqual(
        carvedResolved.value.mips.abiFlags,
        independent.mips.abiFlags,
      );
      assert.match(
        carvedResolved.value.mips.limitations.join(" "),
        /section header table/u,
      );
      const carvedTarget = await call("open_binary", {
        path: carvedPath,
        provider_id: "ghidra",
      });
      assert.equal(carvedTarget.sha256, carvedSha256);
      const carvedImage = await call("inspect_native_load_image");
      assertMipsLoadedImage(carvedImage.observations, byteOrder, carvedSha256);
      const carvedProcedures = await call("list_procedures");
      assert.ok(
        carvedProcedures.some(
          (p) =>
            BigInt(p.address) === BigInt(independent.symbols.rea_mips_entry),
        ),
      );
      const carvedDossier = functionDossierSchema.parse(
        await call("analyze_function", {
          procedure: independent.symbols.rea_mips_entry,
        }),
      );
      assert.ok(carvedDossier.pseudocode.trim().length > 0);
      const carvedSnapshotPath = join(
        workspace,
        `${byteOrder}-carved.snapshot.json`,
      );
      await call("close_binary", { snapshot_path: carvedSnapshotPath });
      const carvedSnapshot = parseAnalysisSnapshot(
        JSON.parse(await readFile(carvedSnapshotPath, "utf8")),
      );
      assert.equal(carvedSnapshot.target.sha256, carvedSha256);
      assert.ok(
        carvedSnapshot.evidence_bundle.records.some((record) =>
          parseEvidence(record).limitations.some((item) =>
            item.includes("section header table"),
          ),
        ),
      );
      const carvedCli = await execute(
        process.execPath,
        [
          entrypoint,
          "function",
          carvedPath,
          independent.symbols.rea_mips_entry,
          "--provider",
          "ghidra",
          "--json",
        ],
        { env, timeout: 240000, maxBuffer: 16 * 1024 * 1024 },
      );
      const carvedEvidence = parseEvidence(JSON.parse(carvedCli.stdout));
      assert.equal(carvedEvidence.subject?.digest.sha256, carvedSha256);
      assert.ok(
        carvedEvidence.limitations.some((item) =>
          item.includes("section header table"),
        ),
      );
      assert.equal(
        functionDossierSchema.parse(carvedEvidence.normalized_result).procedure
          .address,
        carvedDossier.procedure.address,
      );
      assert.deepEqual(await readFile(carvedPath), carvedBytes);
      reports.push({
        ...fixture,
        independent_reader: independent,
        effective_language: image.observations.language_id,
        effective_compiler_spec: image.observations.compiler_spec_id,
        checks: [
          "independent ELF/ABI and symbols",
          "loaded Ghidra identity",
          "fixed instruction bytes and immediate",
          "conditional branch destination",
          "global word and marker bytes",
          "CLI/MCP and snapshot identity",
        ],
        procedures: procedures.length,
        carved: {
          sha256: carvedSha256,
          source_bytes: carvedBytes.length,
          section_header_offset: sectionOffset,
          effective_language: carvedImage.observations.language_id,
          limitations: carvedResolved.value.mips.limitations,
          procedures: carvedProcedures.length,
        },
        status: "passed",
      });
    } catch (cause) {
      throw new Error(
        `MIPS ${byteOrder} verification failed; MCP stderr: ${stderr}`,
        { cause },
      );
    } finally {
      try {
        await client.close();
      } finally {
        await transport.close();
      }
    }
  }
} catch (cause) {
  primaryFailure = cause;
}
const completed = await completeVerifierRun(run);
if (
  completed.process_lineage.status !== "verified" ||
  completed.process_lineage.descendants.length !== 0
)
  throw new Error(
    `Cleanup unverified; retained diagnostic workspace: ${workspace}`,
    { cause: primaryFailure },
  );
if (primaryFailure !== undefined)
  throw new Error(
    `MIPS verification failed; retained workspace: ${workspace}`,
    {
      cause: primaryFailure,
    },
  );
await rm(workspace, { recursive: true, force: true });
console.log(JSON.stringify({ ...completed, fixtures: reports }, null, 2));

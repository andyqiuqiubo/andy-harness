import { execFile } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";

import { describe, expect, it } from "vitest";

import { createTestTempDirectory } from "../../fixtures/temporaryDirectory.js";

const execute = promisify(execFile);
const MARKER = "REA_TEST_RESOLVED ";
// Dependencies and composition modules used by one command family; startup
// must not resolve them just to register the complete command catalog.
const DEFERRED_IMPORTS = [
  {
    name: "playwright-core",
    path: "/node_modules/playwright-core/",
  },
  {
    name: "isomorphic-git",
    path: "/node_modules/isomorphic-git/",
  },
  {
    name: "binary-composition",
    path: "/dist/composition/binary.js",
  },
] as const;
// Report each resolved deferred package on stderr without changing resolution.
const RESOLUTION_HOOK = `data:text/javascript,${encodeURIComponent(`
import { registerHooks } from "node:module";
const imports = ${JSON.stringify(DEFERRED_IMPORTS)};
registerHooks({
  resolve(specifier, context, next) {
    const resolved = next(specifier, context);
    const name = imports.find((item) =>
      resolved.url.includes(item.path),
    );
    if (name !== undefined)
      process.stderr.write(${JSON.stringify(MARKER)} + name.name + "\\n");
    return resolved;
  },
});
`)}`;

const resolvedPackages = async (
  arguments_: readonly string[],
): Promise<ReadonlySet<string>> => {
  const { stderr } = await execute(
    process.execPath,
    [`--import=${RESOLUTION_HOOK}`, ...arguments_],
    { cwd: process.cwd(), maxBuffer: 16 * 1_024 * 1_024 },
  );
  return new Set(
    stderr
      .split("\n")
      .filter((line) => line.startsWith(MARKER))
      .map((line) => line.slice(MARKER.length)),
  );
};

const evaluate = (source: string): readonly string[] => [
  "--input-type=module",
  "--eval",
  source,
];

describe("CLI startup imports", () => {
  it("detects each deferred package when its command code loads", async () => {
    await expect(
      resolvedPackages(
        evaluate('await import("./dist/composition/browserScenario.js");'),
      ),
    ).resolves.toContain("playwright-core");
    const source = await createTestTempDirectory("rea-startup-imports-");
    await mkdir(join(source, ".git"));
    await expect(
      resolvedPackages(
        evaluate(
          `const { readReferenceSourceVcs } = await import("./dist/application/ReferenceSourceVcsAdapter.js"); const { ArtifactResourceScope } = await import("./dist/artifacts/ArtifactResourceScope.js"); const resources = new ArtifactResourceScope(); try { await readReferenceSourceVcs(${JSON.stringify(source)}, resources); } finally { await resources.close(); }`,
        ),
      ),
    ).resolves.toContain("isomorphic-git");
  });

  it("does not load command-specific code to register the command catalog", async () => {
    await expect(
      resolvedPackages(["scripts/rea.mjs", "--help"]),
    ).resolves.toEqual(new Set());
  });

  it("does not load native provider composition for JavaScript application analysis", async () => {
    const source = await createTestTempDirectory("rea-cli-js-startup-");
    await mkdir(join(source, "src"));
    await writeFile(
      join(source, "package.json"),
      JSON.stringify({
        name: "startup-fixture",
        version: "1.0.0",
        main: "src/index.js",
      }),
    );
    await writeFile(
      join(source, "src/index.js"),
      "export const answer = 42;\n",
    );

    await expect(
      resolvedPackages([
        "scripts/rea.mjs",
        "analyze-javascript-application",
        source,
        "--json",
      ]),
    ).resolves.toEqual(new Set());
  });

  it("loads native provider composition when a provider operation is called", async () => {
    await expect(
      resolvedPackages(
        evaluate(
          'const { createDirectAnalysis } = await import("./dist/composition/directAnalysis.js"); await createDirectAnalysis({}).runSessionStatus();',
        ),
      ),
    ).resolves.toContain("binary-composition");
  });
});

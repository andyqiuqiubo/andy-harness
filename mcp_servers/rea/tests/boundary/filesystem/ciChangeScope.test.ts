import { mkdir, readFile, readdir, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

import { expect, it } from "vitest";
import { parse } from "yaml";
import { z } from "zod";

import { createTestTempDirectory } from "../../fixtures/temporaryDirectory.js";
import {
  executeWorkflowFixture as execFileAsync,
  workflowGit as git,
} from "../../support/workflowGit.js";

async function commit(directory: string) {
  await git(directory, ["add", "."]);
  await git(directory, ["commit", "-m", "fix: fixture change"]);
  return git(directory, ["rev-parse", "HEAD"]);
}

async function change(
  directory: string,
  paths: readonly string[],
  content = "Changed fixture content\n",
) {
  for (const path of paths) {
    const file = join(directory, path);
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, content);
  }
  return commit(directory);
}

async function fixture() {
  const directory = await createTestTempDirectory("rea-ci-scope-");
  await git(directory, ["init", "--initial-branch=main"]);
  const base = await change(
    directory,
    ["src/provider.ts", "README.md"],
    "Initial fixture content\n",
  );
  return { directory, base };
}

async function classify(
  directory: string,
  base: string,
  head: string,
  event = "pull_request",
  full = false,
) {
  const output = join(directory, "scope-output");
  await writeFile(output, "");
  const result = await execFileAsync(
    process.execPath,
    [new URL("../../../scripts/ci/plan.mjs", import.meta.url).pathname],
    {
      cwd: directory,
      env: {
        ...process.env,
        BASE_SHA: base,
        HEAD_SHA: head,
        GITHUB_EVENT_NAME: event,
        CI_FULL: String(full),
        GITHUB_OUTPUT: output,
      },
    },
  );
  return z.record(z.string(), z.unknown()).parse(JSON.parse(result.stdout));
}

it("keeps every translated root README and authored guide on the documentation lane", async () => {
  const { directory, base } = await fixture();
  const readmes = (await readdir(new URL("../../../", import.meta.url))).filter(
    (name) => /^README(?:_.*)?\.md$/u.test(name),
  );
  expect(readmes).toContain("README.md");
  const head = await change(directory, [
    ...readmes,
    "docs/guide.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
  ]);
  expect(await classify(directory, base, head)).toMatchObject({
    code: false,
    package: false,
    docs: true,
  });
});

it("does not charge a documentation PR for unrelated implementation updates on main", async () => {
  const { directory, base } = await fixture();
  await git(directory, ["switch", "-c", "docs/change"]);
  const head = await change(directory, ["README_zh-TW.md"]);
  await git(directory, ["switch", "main"]);
  const advancedBase = await change(directory, ["src/anotherProvider.ts"]);
  expect(advancedBase).not.toBe(base);
  expect(await classify(directory, advancedBase, head)).toMatchObject({
    code: false,
    package: false,
  });
});

it.each([
  "src/README.md",
  "README.md.ts",
  "bridge/ghidra/provider.py",
  "tests/fixture.test.ts",
  ".github/workflows/ci.yml",
  "unknown-file",
])(
  "retains complete checks when documentation is mixed with %s",
  async (path) => {
    const { directory, base } = await fixture();
    const head = await change(directory, ["README.md", path]);
    expect(await classify(directory, base, head)).toMatchObject({ code: true });
  },
);

it("retains complete checks when implementation is renamed into documentation", async () => {
  const { directory, base } = await fixture();
  await mkdir(join(directory, "docs"));
  await rename(
    join(directory, "src/provider.ts"),
    join(directory, "docs/provider.md"),
  );
  const head = await commit(directory);
  expect(await classify(directory, base, head)).toMatchObject({
    code: true,
    package: true,
  });
});

it("retains the complete main-push baseline even for documentation", async () => {
  const { directory, base } = await fixture();
  const head = await change(directory, ["README_ja.md"]);
  expect(await classify(directory, base, head, "push")).toMatchObject({
    code: true,
    package: true,
  });
});

it("fails classification rather than treating an invalid Git comparison as a scope", async () => {
  const { directory, base } = await fixture();
  await expect(classify(directory, "missing-ref", base)).rejects.toMatchObject({
    code: 1,
  });
  expect(await readFile(join(directory, "scope-output"), "utf8")).toBe("");
});

it("retains native Inspector verification on every documented package host", async () => {
  const stepSchema = z.object({
    run: z.string().optional(),
    if: z.string().optional(),
  });
  const steps = z.array(stepSchema);
  const workflow = z
    .object({
      jobs: z.object({
        "package-e2e": z.object({
          steps,
        }),
        "windows-curated": z.object({ steps }),
      }),
    })
    .parse(
      parse(
        await readFile(
          new URL("../../../.github/workflows/ci.yml", import.meta.url),
          "utf8",
        ),
      ),
    );
  const packageLane = workflow.jobs["package-e2e"];
  const packageInspector = stepSchema.parse(
    packageLane.steps.find((step) => step.run === "npm run verify:inspector"),
  );
  expect(packageInspector.if).toBeUndefined();
  expect(
    (await classify((await fixture()).directory, "", "", "push")).matrix,
  ).toEqual({
    include: [
      { os: "ubuntu-latest", platform: "linux", architecture: "x64" },
      { os: "ubuntu-24.04-arm", platform: "linux", architecture: "arm64" },
      { os: "macos-15", platform: "darwin", architecture: "arm64" },
      { os: "macos-15-intel", platform: "darwin", architecture: "x64" },
    ],
  });
  const windowsInspector = stepSchema.parse(
    workflow.jobs["windows-curated"].steps.find(
      (step) => step.run === "npm run verify:inspector",
    ),
  );
  expect(windowsInspector.if).toBeUndefined();
});

it.each([
  [
    "src/evm/client.ts",
    { code: true, runtime: true, evm: true, browser: false, package: false },
  ],
  [
    "src/browser/execution/evaluate.ts",
    {
      code: true,
      runtime: true,
      browser: true,
      web_runtime: true,
      evm: false,
      package: false,
    },
  ],
  [
    "tests/module/example.test.ts",
    { code: true, runtime: false, browser: false, package: false },
  ],
  ["website/src/index.ts", { code: false, website: true, package: false }],
  [
    "src/process/ProviderProcess.ts",
    { code: true, package: true, evm: true, browser: true },
  ],
  [
    "package-lock.json",
    { code: true, package: true, evm: true, browser: true },
  ],
  [
    ".github/workflows/real-evm-interface.yml",
    { code: false, evm: true, browser: false, package: false },
  ],
  ["unknown-input", { code: true, package: true, evm: true, browser: true }],
])("selects the appropriate lanes for %s", async (path, expected) => {
  const { directory, base } = await fixture();
  const head = await change(directory, [path]);
  expect(await classify(directory, base, head)).toMatchObject(expected);
});

it("scopes package script edits while retaining broad dependency checks", async () => {
  const { directory } = await fixture();
  const before = {
    name: "fixture",
    scripts: { "verify:evm": "node scripts/verify-evm-interface.mjs" },
  };
  const base = await change(
    directory,
    ["package.json"],
    JSON.stringify(before),
  );
  const after = {
    ...before,
    scripts: {
      "verify:evm": "node scripts/verify-evm-interface.mjs --verbose",
    },
  };
  const head = await change(directory, ["package.json"], JSON.stringify(after));
  expect(await classify(directory, base, head)).toMatchObject({
    code: true,
    evm: true,
    browser: false,
    package: false,
  });
  const dependency = await change(
    directory,
    ["package.json"],
    JSON.stringify({ ...after, dependencies: { example: "1.0.0" } }),
  );
  expect(await classify(directory, head, dependency)).toMatchObject({
    code: true,
    browser: true,
    package: true,
  });
});

it.each([
  ["browser", "real-browser"],
  ["binary_layout", "real-binary-layout"],
  ["evm", "real-evm-interface"],
  ["javascript_recovery", "real-javascript-recovery"],
  ["recorded_crash", "real-recorded-crash"],
  ["web_network", "real-web-network-captures"],
  ["web_runtime", "real-web-runtime"],
  ["web_source_map", "real-web-source-map"],
  ["nativeaot", "nativeaot-fixtures"],
  ["website", "website-check"],
])(
  "routes %s through the selector without a duplicate PR trigger",
  async (scope, name) => {
    const workflow = z
      .object({
        on: z.object({ workflow_call: z.null() }).passthrough(),
      })
      .parse(
        parse(
          await readFile(
            new URL(`../../../.github/workflows/${name}.yml`, import.meta.url),
            "utf8",
          ),
        ),
      );
    expect(workflow.on).not.toHaveProperty("pull_request");
    const ci = z
      .object({
        jobs: z.record(
          z.string(),
          z.object({
            uses: z.string().optional(),
            if: z.string().optional(),
          }),
        ),
      })
      .parse(
        parse(
          await readFile(
            new URL("../../../.github/workflows/ci.yml", import.meta.url),
            "utf8",
          ),
        ),
      );
    expect(ci.jobs[scope]).toMatchObject({
      uses: `./.github/workflows/${name}.yml`,
      if: `needs.changes.outputs.${scope} == 'true'`,
    });
  },
);

it("rejects skipped selected work while allowing unselected lanes to skip", async () => {
  const command = new URL("../../../scripts/ci/required.mjs", import.meta.url)
    .pathname;
  const jobs = {
    changes: { result: "success", outputs: { evm: "true" } },
    static: { result: "success" },
    test: { result: "success" },
    evm: { result: "success" },
    browser: { result: "skipped" },
  };
  const run = (result: string) =>
    execFileAsync(process.execPath, [command], {
      env: {
        ...process.env,
        CI_JOBS: JSON.stringify({ ...jobs, evm: { result } }),
      },
    });
  await expect(run("success")).resolves.toMatchObject({ stderr: "" });
  for (const result of ["failure", "cancelled", "skipped"]) {
    await expect(run(result)).rejects.toMatchObject({ code: 1 });
  }
});

it.each([
  [
    ".github/workflows/real-termux.yml",
    { workflows: true, code: false, package: false, website: false },
  ],
  [
    "src/domain/javascript/example.test.ts",
    { code: true, runtime: false, javascript_recovery: false, package: false },
  ],
  [
    "src/process/capture/example.test.ts",
    { code: true, runtime: false, browser: false, package: false },
  ],
  [".nvmrc", { code: true, package: true, browser: true, evm: true }],
  [
    "tsconfig.build.json",
    { code: true, package: true, browser: true, evm: true },
  ],
  ["docs/.vitepress/config.ts", { code: false, docs: true, package: false }],
  [
    "src/contracts/evm/tools.ts",
    { code: true, docs: true, evm: true, package: false },
  ],
  ["scripts/unknown-website-helper.mjs", { code: true, package: true }],
  [
    ".github/workflows/release.yml",
    { workflows: true, code: true, package: false, website: false },
  ],
  [
    ".github/workflows/real-ghidra-windows.yml",
    { workflows: true, code: true, package: false, website: false },
  ],
  [
    "tests/conformance/c/fixture.c",
    { code: true, fixtures: true, readiness: true, package: false },
  ],
  [
    "tests/conformance/swift/fixture.swift",
    { code: true, fixtures: true, readiness: true, package: false },
  ],
  [
    "tests/conformance/readiness/javascript-cli/package.json",
    { code: true, fixtures: false, readiness: true, package: false },
  ],
  [
    "tests/conformance/ghidra/no-return.c",
    { code: true, fixtures: false, readiness: false, package: false },
  ],
])("preserves scope boundaries for %s", async (path, expected) => {
  const { directory, base } = await fixture();
  const head = await change(directory, [path]);
  expect(await classify(directory, base, head)).toMatchObject(expected);
});

it("unions provider scopes across mixed changes and deletions", async () => {
  const { directory } = await fixture();
  const base = await change(directory, ["src/evm/client.ts"]);
  await git(directory, ["rm", "src/evm/client.ts"]);
  const head = await change(directory, ["src/browser/execution/evaluate.ts"]);
  expect(await classify(directory, base, head)).toMatchObject({
    evm: true,
    browser: true,
    runtime: true,
    package: false,
  });
});

it("fails without emitting a plan when package metadata is malformed", async () => {
  const { directory } = await fixture();
  const base = await change(directory, ["package.json"], '{"scripts":{}}');
  const head = await change(directory, ["package.json"], "{broken");
  await expect(classify(directory, base, head)).rejects.toMatchObject({
    code: 1,
  });
  expect(await readFile(join(directory, "scope-output"), "utf8")).toBe("");
});

it("retains a broad fallback when a provider script adds shared shell work", async () => {
  const { directory } = await fixture();
  const before = {
    scripts: { "verify:evm": "node scripts/verify-evm-interface.mjs" },
  };
  const base = await change(
    directory,
    ["package.json"],
    JSON.stringify(before),
  );
  const after = {
    scripts: {
      "verify:evm": "node scripts/verify-evm-interface.mjs && npm run compile",
    },
  };
  const head = await change(directory, ["package.json"], JSON.stringify(after));
  expect(await classify(directory, base, head)).toMatchObject({
    evm: true,
    browser: true,
    package: true,
  });
});

it.each(["workflow_dispatch", "schedule"])(
  "retains full selection for %s",
  async (event) => {
    const { directory, base } = await fixture();
    expect(await classify(directory, base, base, event)).toMatchObject({
      code: true,
      package: true,
      evm: true,
      browser: true,
    });
  },
);

it("honors ci:full for successive PR revisions", async () => {
  const { directory, base } = await fixture();
  const first = await change(directory, ["README.md"]);
  const second = await change(directory, ["README_ja.md"]);
  for (const head of [first, second]) {
    expect(
      await classify(directory, base, head, "pull_request", true),
    ).toMatchObject({ code: true, package: true, evm: true, browser: true });
  }
});

it.each(["compile:termux", "build:termux", "build:termux:run"])(
  "does not expand %s edits into unrelated providers",
  async (script) => {
    const { directory } = await fixture();
    const base = await change(
      directory,
      ["package.json"],
      JSON.stringify({ scripts: { [script]: "npm run compile:termux" } }),
    );
    const head = await change(
      directory,
      ["package.json"],
      JSON.stringify({
        scripts: {
          [script]:
            "node scripts/run-exclusive.mjs artifacts npm run compile:termux",
        },
      }),
    );
    expect(await classify(directory, base, head)).toMatchObject({
      code: true,
      workflows: true,
      runtime: false,
      package: false,
      browser: false,
      evm: false,
    });
  },
);

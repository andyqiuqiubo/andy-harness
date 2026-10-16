import { resolve } from "node:path";

import {
  processTest,
  type ProcessResult,
  type TestProcesses,
} from "../process/processFixture.js";

/** Caller-controlled values for one compiled REA CLI invocation. */
export interface CliInvocation {
  readonly arguments: readonly string[];
  readonly cwd?: string;
  readonly environment?: NodeJS.ProcessEnv;
  readonly input?: string | Uint8Array;
  readonly timeoutMs?: number;
}

/** Normalized result from the compiled REA CLI. */
export interface CliResult extends ProcessResult {
  readonly json: unknown | undefined;
}

/** Compiled REA CLI driver owned by one test. */
export interface TestCli {
  run(invocation: CliInvocation): Promise<CliResult>;
}

const parseJson = (stdout: string): unknown | undefined => {
  const document = stdout.trim();
  if (document.length === 0) return undefined;
  try {
    return JSON.parse(document) as unknown;
  } catch {
    return undefined;
  }
};

/** Create a compiled CLI driver backed by the supplied process registry. */
export const createTestCli = (processes: TestProcesses): TestCli => ({
  run: async ({
    arguments: arguments_,
    cwd,
    environment,
    input,
    timeoutMs,
  }) => {
    const result = await processes.run(
      process.execPath,
      [resolve("scripts/rea.mjs"), ...arguments_],
      {
        cwd: cwd ?? process.cwd(),
        env: {
          PATH: process.env.PATH ?? "",
          ...environment,
        },
        ...(input === undefined ? {} : { input }),
        ...(timeoutMs === undefined ? {} : { timeoutMs }),
      },
    );
    return { ...result, json: parseJson(result.stdout) };
  },
});

/** Vitest API extended with an owned process registry and compiled CLI driver. */
export const cliTest = processTest.extend<{ cli: TestCli }>({
  cli: async ({ processes }, use) => {
    await use(createTestCli(processes));
  },
});

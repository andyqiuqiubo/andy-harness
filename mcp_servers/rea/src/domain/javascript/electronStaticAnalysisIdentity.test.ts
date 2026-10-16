import { describe, expect, it } from "vitest";

import { analyzeJavaScriptStaticSource } from "./javascriptStaticAnalysis.js";

describe("Electron API identity", () => {
  it("recovers APIs through an ES module default import of Electron", () => {
    const analysis = analyzeJavaScriptStaticSource(`
      import electron from "electron";
      const { ipcMain } = electron;
      new electron.BrowserWindow({ webPreferences: { preload: "./preload.js" } });
      electron.contextBridge.exposeInMainWorld("api", { ping: true });
      electron.ipcRenderer.invoke("default:ping");
      ipcMain.handle("destructured:ping", () => "pong");
      electron.default.ipcMain.handle("nested-default", () => "local");
    `);

    expect(analysis.electron.browser_windows).toMatchObject([
      { preload_path: "./preload.js" },
    ]);
    expect(analysis.electron.context_bridge_apis).toMatchObject([
      { api_key: "api", members: ["ping"] },
    ]);
    expect(
      analysis.electron.ipc.map(({ side, operation, channel }) => ({
        side,
        operation,
        channel,
      })),
    ).toEqual([
      { side: "renderer", operation: "invoke", channel: "default:ping" },
      { side: "main", operation: "handle", channel: "destructured:ping" },
    ]);
  });

  it("recovers APIs and sender checks written with optional chaining", () => {
    const analysis = analyzeJavaScriptStaticSource(`
      const { contextBridge, ipcMain, utilityProcess } = require("electron");
      contextBridge?.exposeInMainWorld("api", {});
      utilityProcess?.fork("./worker.js");
      ipcMain?.handle("optional:ping", (event) => {
        if (!event.senderFrame?.url.startsWith("file://")) return null;
        return event.sender?.getURL() === "file:///index.html";
      });
    `);

    expect(analysis.electron.context_bridge_apis).toMatchObject([
      { api_key: "api" },
    ]);
    expect(analysis.electron.utility_processes).toMatchObject([
      { module_path: "./worker.js" },
    ]);
    expect(analysis.electron.ipc).toMatchObject([
      { side: "main", operation: "handle", channel: "optional:ping" },
    ]);
    expect(
      analysis.electron.sender_validations.map(
        ({ subject, mechanism, expected }) => ({
          subject,
          mechanism,
          expected: expected.value,
        }),
      ),
    ).toEqual([
      {
        subject: "sender-url",
        mechanism: "call:startsWith",
        expected: "file://",
      },
      {
        subject: "sender-url",
        mechanism: "comparison:===",
        expected: "file:///index.html",
      },
    ]);
  });
});

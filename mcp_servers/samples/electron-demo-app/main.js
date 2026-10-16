// Electron 主进程：创建窗口、注册 IPC 处理器。
// 该文件是 REA analyze_javascript_application 的验证样本，请勿当作可运行产品。
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('node:path');
const { NoteStore } = require('./shared/store');

/** @type {NoteStore | null} */
let store = null;

function resolveStore() {
  if (store === null) {
    store = new NoteStore(path.join(app.getPath('userData'), 'notes.json'));
  }
  return store;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 960,
    height: 640,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: false,
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  return win;
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// IPC：渲染进程搜索入口 —— 主进程侧的唯一搜索实现。
ipcMain.handle('notes:search', async (_event, query, options) => {
  const results = resolveStore().search(query, options);
  return { total: results.length, items: results };
});

// IPC：导出单条笔记为 Markdown。
ipcMain.handle('notes:export', async (_event, noteId) => {
  const { canceled, filePath } = await dialog.showSaveDialog({
    defaultPath: `${noteId}.md`,
  });
  if (canceled || !filePath) return { ok: false, canceled: true };
  resolveStore().exportMarkdown(noteId, filePath);
  return { ok: true, filePath };
});

ipcMain.handle('notes:version', async () => ({ version: app.getVersion() }));

// 预加载脚本：通过 contextBridge 向渲染进程暴露受限 API。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('notesApi', {
  search: (query, options) => ipcRenderer.invoke('notes:search', query, options),
  export: (noteId) => ipcRenderer.invoke('notes:export', noteId),
  version: () => ipcRenderer.invoke('notes:version'),
});

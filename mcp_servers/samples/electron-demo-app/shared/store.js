// 笔记存储：JSON 文件持久化 + 分词搜索 + Markdown 导出。
// 仅在主进程使用。
const fs = require('node:fs');
const path = require('node:path');
const { normalizeQuery, tokenize } = require('./utils.js');

class NoteStore {
  constructor(file) {
    this.file = file;
    this.notes = this.#load();
  }

  #load() {
    try {
      return JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch {
      return [];
    }
  }

  search(query, options = {}) {
    const tokens = tokenize(normalizeQuery(query));
    const limit = options.limit ?? 50;
    if (tokens.length === 0) return [];
    return this.notes
      .map((note) => ({
        note,
        score: tokens.reduce(
          (sum, token) =>
            sum +
            (note.title.toLowerCase().includes(token) ? 2 : 0) +
            (note.body.toLowerCase().includes(token) ? 1 : 0),
          0,
        ),
      }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, limit)
      .map((entry) => entry.note);
  }

  exportMarkdown(noteId, target) {
    const note = this.notes.find((item) => item.id === noteId);
    if (!note) throw new Error(`note not found: ${noteId}`);
    const markdown = `# ${note.title}\n\n${note.body}\n`;
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, markdown, 'utf8');
    return target;
  }
}

module.exports = { NoteStore };

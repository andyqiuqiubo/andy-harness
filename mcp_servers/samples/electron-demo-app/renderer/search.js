// 搜索控制器：归一化查询词后转发给主进程（经 preload 的 notesApi）。
import { normalizeQuery, tokenize } from '../shared/utils.js';

export function createSearchController(api) {
  return {
    async run(query, options = {}) {
      const tokens = tokenize(normalizeQuery(query));
      if (tokens.length === 0) return { total: 0, items: [] };
      return api.search(tokens.join(' '), options);
    },
  };
}
